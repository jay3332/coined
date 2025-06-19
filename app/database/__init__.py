from __future__ import annotations

import asyncio
import datetime
import json
import random
import secrets
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import IntEnum
from string import ascii_letters
from typing import Any, Awaitable, Callable, Generator, Iterable, Literal, NamedTuple, Protocol, overload, TYPE_CHECKING

import asyncpg
import discord.utils
from discord.utils import cached_property, format_dt

from app.data.abilities import Ability, Abilities
from app.data.backpacks import Backpack, Backpacks
from app.data.items import CropMetadata, Item, Items, Reward, LEVEL_REWARDS
from app.data.jobs import Job, Jobs
from app.data.pets import Pet, Pets
from app.data.quests import (
    QUEST_PASS_CURVE,
    QUEST_PASS_REWARDS,
    Quest,
    QuestCategory,
    QuestTemplate,
    QuestTemplates,
    QuestSlot,
)
from app.data.skills import Skill, Skills
from app.database.migrations import Migrator
from app.util.common import (
    CubicCurve,
    ExponentialCurve,
    expansion_list,
    get_by_key,
    image_url_from_emoji,
    next_utc_midnight,
    next_weekday_utc_midnight, pick,
    progress_bar,
)
from app.util.views import StaticCommandButton
from config import Colors, DatabaseConfig, Emojis, multiplier_guilds

if TYPE_CHECKING:
    from typing import Self

    from app.core import Bot, Command, Context

__all__ = (
    'Database',
    'Migrator',
)


class _Database:
    _internal_pool: asyncpg.Pool

    def __init__(self, *, loop: asyncio.AbstractEventLoop = None) -> None:
        self.loop: asyncio.AbstractEventLoop = loop or asyncio.get_event_loop()
        self.__connect_task = self.loop.create_task(self._connect())

    async def wait(self) -> None:
        await self.__connect_task

    async def _connect(self) -> None:
        self._internal_pool = await asyncpg.create_pool(
            host=DatabaseConfig.host,
            port=DatabaseConfig.port,
            user=DatabaseConfig.user,
            database=DatabaseConfig.name,
            password=DatabaseConfig.password,
        )

        async with self.acquire() as conn:
            migrator = Migrator(conn)
            await migrator.run_migrations()

    @overload
    def acquire(self, *, timeout: float = None) -> Awaitable[asyncpg.Connection]:
        ...

    def acquire(self, *, timeout: float = None) -> asyncpg.pool.PoolAcquireContext:
        return self._internal_pool.acquire(timeout=timeout)

    def release(self, conn: asyncpg.Connection, *, timeout: float = None) -> Awaitable[None]:
        return self._internal_pool.release(conn, timeout=timeout)

    def execute(self, query: str, *args: Any, timeout: float = None) -> Awaitable[str]:
        return self._internal_pool.execute(query, *args, timeout=timeout)

    def executemany(self, query: str, args: Iterable[Any], *, timeout: float = None) -> Awaitable[str]:
        return self._internal_pool.executemany(query, args, timeout=timeout)

    def fetch(self, query: str, *args: Any, timeout: float = None) -> Awaitable[list[asyncpg.Record]]:
        return self._internal_pool.fetch(query, *args, timeout=timeout)

    def fetchrow(self, query: str, *args: Any, timeout: float = None) -> Awaitable[asyncpg.Record]:
        return self._internal_pool.fetchrow(query, *args, timeout=timeout)

    def fetchval(self, query: str, *args: Any, column: str | int = 0, timeout: float = None) -> Awaitable[Any]:
        return self._internal_pool.fetchval(query, *args, column=column, timeout=timeout)


class Database(_Database):
    """Manages transactions to and from the database.

    Additionally, this is where you will find the cache which stores records to be used later.
    """

    def __init__(self, bot: Bot, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        super().__init__(loop=loop)
        self.user_records: dict[int, UserRecord] = {}
        self.guild_records: dict[int, GuildRecord] = {}
        self.bot: Bot = bot

        # FIXME: for now, fetch all users and cache them
        #  since there aren't that many records to fetch.
        #  if we ever have to scale, we can remove the following line.
        bot.loop.create_task(self.register_all_records())

    async def register_all_records(self) -> None:
        await self.__connect_task

        query = 'SELECT * FROM users'
        for data in await self.fetch(query):
            user_id = data['user_id']
            self.user_records[user_id] = record = UserRecord(user_id, db=self)
            record.data.update(data)

    @overload
    def get_user_record(self, user_id: int, *, fetch: Literal[True] = True) -> Awaitable[UserRecord]:
        ...

    @overload
    def get_user_record(self, user_id: int, *, fetch: Literal[False] = True) -> UserRecord:
        ...

    def get_user_record(self, user_id: int, *, fetch: bool = True):
        try:
            record = self.user_records[user_id]
        except KeyError:
            record = self.user_records[user_id] = UserRecord(user_id, db=self)

        if not fetch:
            return record

        return record.fetch_if_necessary()

    @overload
    def get_guild_record(self, guild_id: int, *, fetch: Literal[True] | None = None) -> Awaitable[GuildRecord]:
        ...

    @overload
    def get_guild_record(self, guild_id: int, *, fetch: Literal[False] = None) -> GuildRecord | None:
        ...

    def get_guild_record(self, guild_id: int, *, fetch: bool | None = None) -> GuildRecord | Awaitable[GuildRecord]:
        """Fetches a guild record."""
        try:
            record = self.guild_records[guild_id]
        except KeyError:
            record = self.guild_records[guild_id] = GuildRecord(guild_id, db=self)

        if fetch:
            return record.fetch()
        elif fetch is None:
            return record.fetch_if_necessary()
        return record


class InventoryMapping(dict[Item, int]):
    def get(self, k: Item | str, d: Any = None) -> int:
        try:
            return self[k]
        except KeyError:
            return d

    def quantity_of(self, item: Item | str) -> int:
        try:
            return self[item]
        except KeyError:
            return 0

    def __getitem__(self, item: Item | str) -> int:
        if isinstance(item, str):
            item = get_by_key(Items, item)

        if item is None:
            raise RuntimeError(f'Item {item!r} does not exist')

        return super().__getitem__(item)

    def __setitem__(self, item: Item | str, value: int) -> None:
        if isinstance(item, str):
            item = get_by_key(Items, item)

        if item is None:
            return

        return super().__setitem__(item, value)

    def __contains__(self, item: Item | str) -> bool:
        if isinstance(item, str):
            item = get_by_key(Items, item)

        if item is None:
            return False

        return super().__contains__(item)


class InventoryManager:
    def __init__(self, record: UserRecord) -> None:
        self.cached: InventoryMapping = InventoryMapping()
        self.damage: InventoryMapping = InventoryMapping()  # stored separately to avoid breaking chances

        self._record: UserRecord = record
        self._task: asyncio.Task = record.db.loop.create_task(self.fetch_items())

    async def wait(self) -> InventoryManager:
        await self._task
        return self

    async def fetch_items(self) -> None:
        query = 'SELECT * FROM items WHERE user_id = $1'
        records = await self._record.db.fetch(query, self._record.user_id)

        for record in records:
            self.cached[item := record['item']] = record['count']

            damage = record['damage']
            if damage is not None:
                self.damage[item] = damage

    async def add_item(self, item: Item | str, amount: int = 1, *, connection: asyncpg.Connection | None = None) -> None:
        await self.wait()

        query = """
                INSERT INTO items (user_id, item, count) VALUES ($1, $2, $3)
                ON CONFLICT (user_id, item) DO UPDATE SET count = items.count + $3 
                RETURNING items.count
                """

        row = await (connection or self._record.db).fetchrow(query, self._record.user_id, str(item), amount)
        self.cached[item] = row['count']

    async def _base_update(
        self,
        from_query: str,
        *,
        connection: asyncpg.Connection | None = None,
        transform: Callable[[int, int], int],
        **items: int,
    ) -> None:
        await self.wait()

        await (connection or self._record.db).executemany(
            from_query,
            [(self._record.user_id, k, v) for k, v in items.items()],
        )
        # update is not atomic, so we have to do this
        for k, v in items.items():
            self.cached[k] = transform(self.cached.get(k, 0), v)

    async def update(self, *, connection: asyncpg.Connection | None = None, **items: int) -> None:
        query = """
                INSERT INTO items (user_id, item, count) VALUES ($1, $2, $3)
                ON CONFLICT (user_id, item) DO UPDATE SET count = $3
                """
        await self._base_update(query, connection=connection, transform=lambda _, v: v, **items)

    async def add_bulk(self, *, connection: asyncpg.Connection | None = None, **items: int) -> None:
        query = """
                INSERT INTO items (user_id, item, count) VALUES ($1, $2, $3)
                ON CONFLICT (user_id, item) DO UPDATE SET count = items.count + $3
                """
        await self._base_update(query, connection=connection, transform=lambda p, v: p + v, **items)

    async def deal_damage(self, item: Item | str, damage: int, *, connection: asyncpg.Connection | None = None) -> tuple[int, bool]:
        """Deals damage to the item, removing it and resetting damage if it breaks."""
        await self.wait()
        if isinstance(item, str):
            item = get_by_key(Items, item)

        assert item.durability is not None, f'Item {item!r} has no durability rating'
        damage = self.damage.get(item, item.durability) - damage
        quantity = self.cached.quantity_of(item)

        if broken := damage <= 0:
            damage = item.durability
            quantity -= 1

        query = "UPDATE items SET damage = $3, count = $4 WHERE user_id = $1 AND item = $2"
        await (connection or self._record.db).execute(query, self._record.user_id, str(item), damage, quantity)
        self.cached[item] = quantity
        self.damage[item] = damage
        return damage, broken

    async def reset_damage(self, item: Item | str, *, to: int | None = None, connection: asyncpg.Connection | None = None) -> None:
        await self.wait()
        if isinstance(item, str):
            item = get_by_key(Items, item)

        assert item.durability is not None, f'Item {item!r} has no durability rating'
        damage = to if to is not None else item.durability

        query = "UPDATE items SET damage = $3 WHERE user_id = $1 AND item = $2"
        await (connection or self._record.db).execute(query, self._record.user_id, str(item), damage)
        self.damage[item] = damage

    async def wipe(self, *, connection: asyncpg.Connection | None = None) -> None:
        await self.wait()  # this is so the cache isn't prone to data races

        query = 'DELETE FROM items WHERE user_id = $1'
        await (connection or self._record.db).execute(query, self._record.user_id)
        self.cached.clear()


class RobFailReason(IntEnum):
    code_failure = 1
    spotted_by_police = 2
    padlock_active = 3
    bee_sting = 4


class NotificationData:
    class LevelUp(NamedTuple):
        level: int
        rcoins: int = 0
        ritems: dict[str, int] = {}

        type = 0
        title = 'You leveled up!'
        color = Colors.success
        emoji = '\u23eb'

        def describe(self, _: Bot) -> str:
            extra = ''
            if self.rcoins or self.ritems:
                reward = Reward.from_raw(self.rcoins, self.ritems)
                extra = (
                    f' You received some rewards.\n\n'
                    f'**Milestone reached!** You received the following for leveling up:\n{reward}'
                )

            return f'Congratulations on leveling up to **Level {self.level}**!{extra}'

    class RobInProgress(NamedTuple):
        user_id: int
        guild_name: str

        type = 1
        title = 'You are currently being robbed!'
        color = Colors.warning
        emoji = '\U0001f92b'

        def describe(self, _: Bot) -> str:
            return f'<@{self.user_id}> is trying to rob you in **{self.guild_name}**!'

    class RobSuccess(NamedTuple):
        user_id: int
        guild_name: str
        amount: int
        percent: float

        type = 2
        title = 'Someone stole coins from you!'
        color = Colors.error
        emoji = '\U0001f4b0'

        def describe(self, _: Bot) -> str:
            return (
                f'<@{self.user_id}> stole {Emojis.coin} **{self.amount:,}** coins ({self.percent:.1%}) '
                f'from your wallet in **{self.guild_name}**!'
            )

    class RobFailure(NamedTuple):
        user_id: int
        guild_name: str
        reason: RobFailReason
        received: int = 0

        type = 3
        title = 'Someone tried to rob you, but failed!'
        color = Colors.error
        emoji = '\U0001f913'

        def describe(self, _: Bot) -> str:
            match RobFailReason(self.reason):
                case RobFailReason.code_failure:
                    return f'<@{self.user_id}> tried to rob you in **{self.guild_name}**, but failed to enter in the correct code!'
                case RobFailReason.spotted_by_police:
                    end = (
                        '!' if not self.received
                        else f', who forced them to pay you {Emojis.coin} **{self.received:,}** in fines.'
                    )
                    return (
                        f'<@{self.user_id}> tried to rob you in **{self.guild_name}**, but was spotted by the police{end}'
                    )
                case RobFailReason.padlock_active:
                    return (
                        f'<@{self.user_id}> tried to rob you in **{self.guild_name}**, but you had a padlock active! '
                        f'Your padlock is now deactivated.'
                    )
                case RobFailReason.bee_sting:
                    return (
                        f'<@{self.user_id}> tried to rob you in **{self.guild_name}**, but was stung by your '
                        f'**{Pets.bee.display}** while doing so.'
                    )

    class PadlockOpened(NamedTuple):
        user_id: int
        guild_name: str
        device: str

        type = 4
        title = 'Someone opened your padlock!'
        color = Colors.error
        emoji = Items.padlock.emoji

        def describe(self, _: Bot) -> str:
            return f'<@{self.user_id}> opened your padlock using a **{self.device}** in **{self.guild_name}**!'

    class ReceivedCoins(NamedTuple):
        user_id: int
        coins: int

        type = 5
        title = 'You got coins!'
        color = Colors.success
        emoji = Emojis.coin

        def describe(self, _: Bot) -> str:
            return f'<@{self.user_id}> gave you {Emojis.coin} **{self.coins:,}**.'

    class ReceivedItems(NamedTuple):
        user_id: int
        item: str
        quantity: int

        type = 6
        title = 'You got items!'
        color = Colors.success
        emoji = '\U0001f381'

        def describe(self, _: Bot) -> str:
            item: Item = get_by_key(Items, self.item)
            return f'<@{self.user_id}> gave you {item.get_sentence_chunk(self.quantity)}.'

    class Vote(NamedTuple):
        item: str
        milestone: int | None = None
        rcoins: int = 0
        ritems: dict[str, int] = {}

        type = 7
        title = 'Thank you for voting!'
        color = Colors.success
        emoji = '\N{THUMBS UP SIGN}'

        def describe(self, bot: Bot) -> str:
            item: Item = get_by_key(Items, self.item)
            vote_command_mention = bot.tree.get_app_command('vote').mention

            extra = ''
            if self.rcoins or self.ritems:
                reward = Reward.from_raw(self.rcoins, self.ritems)
                extra = f'\n\n**Voting Milestone reached!** Upon reaching **{self.milestone} votes** you received:\n{reward}'

            return (
                f'Thank you for voting! You received {item.get_sentence_chunk()} for your vote.\n'
                f'To see upcoming voting milestones and their rewards, run {vote_command_mention}.{extra}'
            )

    class Death(NamedTuple):
        reason: str | None
        coins_lost: int
        item_lost: str | None = None
        quantity_lost: int = 0

        type = 8
        title = 'You died!'
        color = Colors.error
        emoji = '\N{SKULL}'

        def describe(self, _: Bot) -> str:
            extra = (
                f' and {get_by_key(Items, self.item_lost).get_sentence_chunk(self.quantity_lost)}'
                if self.item_lost is not None else ''
            )
            return f'{self.reason or "You died!"} You lost {Emojis.coin} **{self.coins_lost:,}**{extra}.'

    class NearDeath(NamedTuple):
        reason: str | None
        remaining: int

        type = 9
        title = 'You almost died!'
        color = Colors.warning
        emoji = '\u26a0\ufe0f'

        def describe(self, _: Bot) -> str:
            s = '' if self.remaining == 1 else 's'
            remaining = (
                f'You have {self.remaining:,} lifesaver{s} remaining.' if self.remaining > 0
                else (
                    '\n\n**You have no more lifesavers remaining!** You will lose coins and items the next time you die '
                    'unless you replenish your lifesavers.'
                )
            )
            return (
                f'{self.reason or "You almost died!"} You had {Items.lifesaver.get_sentence_chunk()} in your inventory, '
                f'which saved your life and is now consumed. {remaining}'
            )

    class RepairFinished(NamedTuple):
        item: str

        type = 10
        title = 'Your item has been repaired!'
        color = Colors.success
        emoji = '\N{HAMMER}'

        def describe(self, _: Bot) -> str:
            return (
                f'Your **{get_by_key(Items, self.item).display_name}** has been repaired! It has been returned to your inventory.'
            )

    class BotUpdate(NamedTuple):
        changelog_path: str
        image_path: str | None = None

        type = 11
        title = 'Coined got an update!'
        color = Colors.success
        emoji = Emojis.coined

        def describe(self, bot: Bot) -> str:
            if not hasattr(bot, '_changelogs'):
                setattr(bot, '_changelogs', {})

            if self.changelog_path not in bot._changelogs:  # type: ignore
                with open(f'assets/changelogs/{self.changelog_path}', 'r') as fp:
                    bot._changelogs[self.changelog_path] = fp.read()  # type: ignore

            return bot._changelogs[self.changelog_path]  # type: ignore

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> _NotificationData:
        type_ = record['type']
        for klass in cls.__dict__.values():
            if isinstance(klass, type) and getattr(klass, 'type', None) == type_:
                return klass(**json.loads(record['data']))

        raise ValueError(f'Unknown notification type {type_}')


class _NotificationData(Protocol):
    type: int
    title: str
    color: int
    emoji: str

    def describe(self, _: Bot) -> str:
        ...

    def _asdict(self) -> dict[str, Any]:
        ...


class Notification(NamedTuple):
    created_at: datetime.datetime
    data: _NotificationData

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> Notification:
        return cls(created_at=record['created_at'], data=NotificationData.from_record(record))


class NotificationsManager:
    def __init__(self, record: UserRecord) -> None:
        self.cached: list[Notification] | None = None

        self._record: UserRecord = record
        self._task: asyncio.Task = record.db.loop.create_task(self.fetch_notifications())

    async def wait(self) -> NotificationsManager:
        await self._task
        return self

    async def fetch_notifications(self) -> None:
        query = 'SELECT * FROM notifications WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1000'
        records = await self._record.db.fetch(query, self._record.user_id)

        self.cached = [Notification.from_record(record) for record in records]

    CHANGELOG_IMAGES_BASE_URL = 'https://github.com/jay3332/coined/blob/main/assets/changelogs'

    def get_notification_embed(self, notification: Notification) -> discord.Embed:
        base = discord.Embed(
            color=notification.data.color, description=notification.data.describe(self._record.db.bot),
            timestamp=notification.created_at,
        ).set_author(
            name=notification.data.title, icon_url=image_url_from_emoji(notification.data.emoji),
        )
        if isinstance(notification.data, NotificationData.BotUpdate) and notification.data.image_path:
            url = f'{self.CHANGELOG_IMAGES_BASE_URL}/{notification.data.image_path}?raw=true'
            base.set_image(url=url)
        return base

    async def _dispatch_dm_notification(self, notification: Notification) -> bool:
        bot = self._record.db.bot
        await bot.wait_until_ready()

        try:
            dm_channel = await bot.create_dm(discord.Object(self._record.user_id))
            await dm_channel.send(
                f'\N{BELL} **{notification.data.title}**',
                embed=self.get_notification_embed(notification),
            )
        except discord.DiscordException:
            return False
        else:
            return True

    async def add_notification(self, data: _NotificationData, *, connection: asyncpg.Connection | None = None) -> None:
        await self.wait()

        query = """
                INSERT INTO notifications (user_id, created_at, type, data)
                VALUES ($1, CURRENT_TIMESTAMP, $2, $3::JSONB)
                RETURNING *;
                """

        args = query, self._record.user_id, getattr(data, 'type'), json.dumps(data._asdict())
        try:
            row = await (connection or self._record.db).fetchrow(*args)
        except asyncpg.InterfaceError:
            row = await self._record.db.fetchrow(*args)

        self.cached.insert(0, notif := Notification.from_record(row))

        result = False
        if self._record.dm_notifications:
            result = self._record.db.loop.create_task(self._dispatch_dm_notification(notif))

        if not result:
            await self._record.add(unread_notifications=1, connection=connection)


class SkillInfo(NamedTuple):
    skill: str
    points: int
    cooldown_until: datetime.datetime | None

    def into_skill(self) -> Skill:
        return get_by_key(Skills, self.skill)

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> SkillInfo:
        return cls(skill=record['skill'], points=record['points'], cooldown_until=record['on_cooldown_until'])


class SkillManager:
    def __init__(self, record: UserRecord) -> None:
        self.cached: dict[str, SkillInfo] = {}

        self._record: UserRecord = record
        self._task: asyncio.Task = record.db.loop.create_task(self.fetch_skills())

    async def wait(self) -> SkillManager:
        await self._task
        return self

    async def fetch_skills(self) -> None:
        query = 'SELECT * FROM skills WHERE user_id = $1'
        records = await self._record.db.fetch(query, self._record.user_id)

        self.cached = {record['skill']: SkillInfo.from_record(record) for record in records}

    def get_skill(self, skill: Skill | str) -> SkillInfo | None:
        if not self.has_skill(skill := str(skill)):
            return None

        return self.cached[skill]

    def points_in(self, skill: Skill | str) -> int:
        if skill := self.get_skill(skill):
            return skill.points

        return 0

    def has_skill(self, skill: Skill | str) -> bool:
        return getattr(skill, 'key', skill) in self.cached

    async def add_skill(self, skill: Skill | str, *, connection: asyncpg.Connection | None = None) -> None:
        await self.wait()

        if isinstance(skill, Skill):
            skill = skill.key

        query = """
                INSERT INTO skills (user_id, skill) VALUES ($1, $2)
                ON CONFLICT (user_id, skill) DO UPDATE SET user_id = $1
                RETURNING *;
                """

        row = await (connection or self._record.db).fetchrow(query, self._record.user_id, skill)
        self.cached[skill] = SkillInfo.from_record(row)

    async def add_skill_points(self, skill: Skill | str, points: int, *, connection: asyncpg.Connection | None = None) -> None:
        await self.wait()

        if isinstance(skill, Skill):
            skill = skill.key

        query = """
                INSERT INTO skills (user_id, skill, points) VALUES ($1, $2, $3)
                ON CONFLICT (user_id, skill) DO UPDATE SET points = skills.points + $3
                RETURNING *;
                """

        row = await (connection or self._record.db).fetchrow(query, self._record.user_id, skill, points)
        self.cached[skill] = SkillInfo.from_record(row)

    async def add_skill_cooldown(
        self, skill: Skill | str, cooldown: datetime.timedelta, *, connection: asyncpg.Connection | None = None,
    ) -> None:
        await self.wait()

        if isinstance(skill, Skill):
            skill = skill.key

        query = """
                INSERT INTO skills (user_id, skill, on_cooldown_until) VALUES ($1, $2, CURRENT_TIMESTAMP + $3)
                ON CONFLICT (user_id, skill) DO UPDATE SET on_cooldown_until = CURRENT_TIMESTAMP + $3
                RETURNING *;
                """

        row = await (connection or self._record.db).fetchrow(query, self._record.user_id, skill, cooldown)
        self.cached[skill] = SkillInfo.from_record(row)


class CooldownInfo(NamedTuple):
    command: str
    expires: datetime.datetime
    previous_expiry: datetime.datetime | None

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> CooldownInfo:
        return cls(command=record['command'], expires=record['expires'], previous_expiry=record['previous_expiry'])


class CooldownManager:
    def __init__(self, record: UserRecord) -> None:
        self.cached: dict[str, CooldownInfo] = {}

        self._record: UserRecord = record
        self._task: asyncio.Task = record.db.loop.create_task(self.fetch_cooldowns())

    async def wait(self) -> CooldownManager:
        await self._task
        return self

    def get_cooldown(self, command: Command) -> Literal[False] | float:
        key = command.qualified_name
        if key not in self.cached:
            return False

        difference = (self.cached[key].expires - discord.utils.utcnow()).total_seconds()
        if difference > 0:
            return difference

        return False

    async def fetch_cooldowns(self) -> None:
        query = 'SELECT * FROM cooldowns WHERE user_id = $1 AND CURRENT_TIMESTAMP < expires'
        records = await self._record.db.fetch(query, self._record.user_id)

        self.cached = {
            record['command']: CooldownInfo.from_record(record) for record in records
        }

    async def set_cooldown(self, command: Command, expires: datetime.datetime) -> None:
        await self.wait()

        query = """
                INSERT INTO cooldowns (user_id, command, expires, previous_expiry) VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, command) DO UPDATE SET expires = $3, previous_expiry = $4
                RETURNING *
                """

        key = command.qualified_name
        previous = self.cached[key].expires if key in self.cached else None

        new = await self._record.db.fetchrow(query, self._record.user_id, key, expires, previous)

        self.cached[key] = CooldownInfo.from_record(new)


class CropInfo(NamedTuple):
    x: int
    y: int
    crop: Item[CropMetadata] | None
    exp: int
    last_harvest: datetime.datetime | None
    created_at: datetime.datetime

    @staticmethod
    def get_letters(x: int) -> str:
        letters = ascii_letters[26:52]

        return (' ' + letters)[x // 26].strip() + letters[x % 26]

    @staticmethod
    def into_coordinates(x: int, y: int) -> str:
        return CropInfo.get_letters(x) + str(y + 1)

    @cached_property
    def coordinates(self) -> str:
        return self.into_coordinates(self.x, self.y)

    @property
    def level_data(self) -> tuple[int, int, int]:
        return CropManager.LEVELING_CURVE.compute_level(self.exp)

    @property
    def level(self) -> int:
        return self.level_data[0]

    @property
    def xp(self) -> int:
        return self.level_data[1]

    @property
    def max_xp(self) -> int:
        return self.level_data[2]

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> CropInfo:
        return cls(
            x=record['x'],
            y=record['y'],
            crop=get_by_key(Items, record['crop']),
            exp=record['exp'],
            last_harvest=record['last_harvest'],
            created_at=record['created_at'],
        )


class CropManager:
    LEVELING_CURVE = ExponentialCurve(50, 1.15, precision=100)

    def __init__(self, record: UserRecord) -> None:
        self.cached: dict[tuple[int, int], CropInfo] = {}

        self._record: UserRecord = record
        self._task: asyncio.Task = record.db.loop.create_task(self.fetch_crops())

    async def wait(self) -> CropManager:
        await self._task
        return self

    async def fetch_crops(self) -> None:
        query = 'SELECT * FROM crops WHERE user_id = $1'

        async with self._record.db.acquire() as conn:
            records = await conn.fetch(query, self._record.user_id)
            self.cached = {
                (record['x'], record['y']): CropInfo.from_record(record) for record in records
            }

            default = [
                (self._record.user_id, x, y) for x in range(4) for y in range(4)
                if (x, y) not in self.cached
            ]
            if not default:
                return

            await conn.executemany('INSERT INTO crops (user_id, x, y) VALUES ($1, $2, $3)', default)

            records = await conn.fetch(query, self._record.user_id)
            self.cached = {
                (record['x'], record['y']): CropInfo.from_record(record) for record in records
            }

    def get_crop_info(self, x: int, y: int) -> CropInfo:
        return self.cached.get((x, y))

    def get_harvest_time(self, crop: Item[CropMetadata]) -> float:
        speed_up = 0
        pets = self._record.pet_manager
        if bee := pets.get_active_pet(Pets.bee):
            speed_up += 0.01 + bee.level * 0.004

        return crop.metadata.time * (1 - speed_up)

    async def harvest(self, coordinates: list[tuple[int, int]]) -> tuple[dict[tuple[int, int], tuple[Item, int]], dict[Item, int]]:
        level_ups = {}
        harvested = defaultdict(int)

        await self.wait()

        async with self._record.db.acquire() as conn:
            valid = 0
            for x, y in coordinates:
                info = self.get_crop_info(x, y)
                if info is None or info.crop is None or (
                    info.last_harvest + datetime.timedelta(seconds=self.get_harvest_time(info.crop)) > discord.utils.utcnow()
                ):
                    continue

                old_level = info.level
                query = """
                        UPDATE crops SET last_harvest = CURRENT_TIMESTAMP, exp = exp + $4
                        WHERE user_id = $1 AND x = $2 AND y = $3
                        RETURNING *;
                        """
                new = await conn.fetchrow(query, self._record.user_id, x, y, random.randint(5, 10))
                self.cached[x, y] = new = CropInfo.from_record(new)

                if new.level > old_level:
                    level_ups[x, y] = info.crop, new.level

                harvested[info.crop.metadata.item] += random.randint(*info.crop.metadata.count)
                valid += 1

            for item, quantity in harvested.items():
                await self._record.inventory_manager.add_item(item, quantity, connection=conn)

            quests = await self._record.quest_manager.wait()
            if quest := quests.get_active_quest(QuestTemplates.harvest_crops):
                await quest.add_progress(valid, connection=conn)

        return level_ups, harvested

    async def add_crop_exp(self, x: int, y: int, exp: int) -> bool:
        await self.wait()

        query = """
                UPDATE crops SET exp = exp + $4
                WHERE user_id = $1 AND x = $2 AND y = $3
                RETURNING *;
                """

        old = self.cached[x, y].level

        new = await self._record.db.fetchrow(query, self._record.user_id, x, y, exp)
        self.cached[x, y] = new = CropInfo.from_record(new)

        return new.level > old

    async def update_last_harvest(self, x: int, y: int) -> None:
        await self.wait()

        query = """
                UPDATE crops SET last_harvest = CURRENT_TIMESTAMP
                WHERE user_id = $1 AND x = $2 AND y = $3
                RETURNING *;
                """

        new = await self._record.db.fetchrow(query, self._record.user_id, x, y)
        self.cached[x, y] = CropInfo.from_record(new)

    async def plant_crop(self, coordinates: list[tuple[int, int]], crop: Item | str) -> None:
        if isinstance(crop, Item):
            crop = crop.key

        await self.wait()

        query = """
                UPDATE crops SET crop = $2, last_harvest = CURRENT_TIMESTAMP, exp = 0
                WHERE user_id = $1 AND x = $3 AND y = $4
                RETURNING *;
                """
        for x, y in coordinates:
            new = await self._record.db.fetchrow(query, self._record.user_id, crop, x, y)
            self.cached[x, y] = CropInfo.from_record(new)

        quests = await self._record.quest_manager.wait()
        if quest := quests.get_active_quest(QuestTemplates.plant_crops):
            await quest.add_progress(len(coordinates))

    async def add_land(self, x: int, y: int) -> None:
        await self.wait()

        query = """
                INSERT INTO crops (user_id, x, y) VALUES ($1, $2, $3)
                ON CONFLICT (user_id, x, y) DO UPDATE SET user_id = $1
                RETURNING *;
                """

        new = await self._record.db.fetchrow(query, self._record.user_id, x, y)
        self.cached[x, y] = CropInfo.from_record(new)

    async def remove_land(self, x: int, y: int) -> None:
        await self.wait()

        query = """
                DELETE FROM crops
                WHERE user_id = $1 AND x = $2 AND y = $3;
                """

        await self._record.db.execute(query, self._record.user_id, x, y)
        self.cached.pop((x, y), None)

    async def wipe_keeping_land(self, connection: asyncpg.Connection | None = None) -> None:
        await self.wait()

        query = """
                UPDATE crops SET crop = NULL, exp = 0, last_harvest = NULL
                WHERE user_id = $1;
                """

        await (connection or self._record.db).execute(query, self._record.user_id)
        for k in self.cached:
            self.cached[k] = self.cached[k]._replace(crop=None, exp=0, last_harvest=None)


@dataclass
class PetRecord:
    manager: PetManager
    pet: Pet
    total_exp: int
    duplicates: int
    evolution: int
    last_recorded_energy: int
    last_feed: datetime.datetime
    max_energy: int
    equipped: bool

    @property
    def level_data(self) -> tuple[int, int, int]:
        return self.pet.leveling_curve.compute_level(self.total_exp)

    @property
    def level(self) -> int:
        return self.level_data[0]

    @property
    def exp(self) -> int:
        return self.level_data[1]

    @property
    def exp_requirement(self) -> int:
        return self.level_data[2]

    @property
    def is_max_level(self) -> int:
        return self.level >= self.pet.max_level

    @property
    def energy(self) -> int:
        if self.last_recorded_energy <= 0:
            return 0
        elapsed = discord.utils.utcnow() - self.last_feed
        return max(0, round(self.last_recorded_energy - elapsed.total_seconds() / 60 * self.pet.energy_per_minute))

    @property
    def exhausts_at(self) -> datetime.datetime:
        return self.last_feed + datetime.timedelta(minutes=self.last_recorded_energy / self.pet.energy_per_minute)

    @staticmethod
    def _transform_record(record: asyncpg.Record) -> dict[str, Any]:
        return pick(
            record,
            'duplicates', 'evolution', 'last_recorded_energy', 'last_feed', 'max_energy', 'equipped',
            exp='total_exp',
        )

    @classmethod
    def from_record(cls, manager: PetManager, record: asyncpg.Record) -> Self:
        return cls(manager=manager, pet=get_by_key(Pets, record['pet']), **cls._transform_record(record))

    @property
    def user_id(self) -> int:
        return self.manager._record.user_id

    @property
    def db(self) -> Database:
        return self.manager._record.db

    async def update_with(self, query: str, *, connection: asyncpg.Connection | None = None, **kwargs: Any) -> None:
        record = await (self.db or connection).fetchrow(query, self.user_id, self.pet.key, *kwargs.values())
        self.__dict__.update(**self._transform_record(record))

    async def update(self, *, connection: asyncpg.Connection | None = None, **kwargs: Any) -> None:
        query = 'UPDATE pets SET {0} WHERE user_id = $1 AND pet = $2 RETURNING *'.format(
            ', '.join(f'{k} = ${i}' for i, k in enumerate(kwargs, start=3))
        )
        await self.update_with(query, connection=connection, **kwargs)

    async def add(self, *, connection: asyncpg.Connection | None = None, **kwargs: Any) -> None:
        query = 'UPDATE pets SET {0} WHERE user_id = $1 AND pet = $2 RETURNING *'.format(
            ', '.join(f'{k} = {k} + ${i}' for i, k in enumerate(kwargs, start=3))
        )
        await self.update_with(query, connection=connection, **kwargs)

    async def set_energy(self, energy: int, *, connection: asyncpg.Connection | None = None) -> None:
        query = """
                UPDATE pets SET last_recorded_energy = $3, last_feed = CURRENT_TIMESTAMP
                WHERE user_id = $1 AND pet = $2
                RETURNING last_feed;
                """

        self.last_feed = await (connection or self.db).fetchval(query, self.user_id, self.pet.key, energy)
        self.last_recorded_energy = energy

    async def add_energy(self, energy: int, *, connection: asyncpg.Connection | None = None) -> None:
        energy = max(0, min(self.max_energy, self.energy + energy))
        await self.set_energy(energy, connection=connection)

    async def evolve(self) -> None:
        async with self.db.acquire() as conn:
            await self.add(evolution=1, connection=conn)
            await self.update(exp=0, connection=conn)
            await self.set_energy(0, connection=conn)


class PetManager:
    def __init__(self, record: UserRecord) -> None:
        self._record = record
        self.cached: dict[Pet, PetRecord] = {}
        self._task = asyncio.create_task(self.fetch())

    async def wait(self) -> Self:
        await self._task
        return self

    async def fetch(self) -> None:
        await self._record.db.wait()
        query = 'SELECT * FROM pets WHERE user_id = $1;'
        records = await self._record.db.fetch(query, self._record.user_id)
        records = (PetRecord.from_record(manager=self, record=r) for r in records)
        self.cached = {r.pet: r for r in records}

    def get_active_pet(self, pet: Pet) -> PetRecord | None:
        if record := self.cached.get(pet):
            return record if record.equipped and record.energy > 0 else None

    @property
    def equipped_count(self) -> int:
        return sum(r.equipped for r in self.cached.values())

    async def add_pet(self, pet: Pet, *, connection: asyncpg.Connection | None = None) -> None:
        query = 'INSERT INTO pets (user_id, pet, max_energy) VALUES ($1, $2, $3) RETURNING *'
        record = await (connection or self._record.db).fetchrow(
            query, self._record.user_id, pet.key, pet.max_energy,
        )
        self.cached[pet] = PetRecord.from_record(manager=self, record=record)


@dataclass
class AbilityRecord:
    manager: AbilityManager
    ability: Ability
    total_exp: int
    equipped: bool

    @property
    def level_data(self) -> tuple[int, int, int]:
        return self.ability.curve.compute_level(self.total_exp)

    @property
    def level(self) -> int:
        return self.level_data[0]

    @property
    def exp(self) -> int:
        return self.level_data[1]

    @property
    def exp_requirement(self) -> int:
        return self.level_data[2]

    @property
    def user_id(self) -> int:
        return self.manager._record.user_id

    @property
    def db(self) -> Database:
        return self.manager._record.db

    @staticmethod
    def _transform_record(record: asyncpg.Record) -> dict[str, Any]:
        return pick(record, 'equipped', exp='total_exp')

    @classmethod
    def from_record(cls, manager: AbilityManager, record: asyncpg.Record) -> Self:
        return cls(manager=manager, ability=get_by_key(Abilities, record['ability']), **cls._transform_record(record))

    async def update_with(self, query: str, *, connection: asyncpg.Connection | None = None, **kwargs: Any) -> None:
        record = await (self.db or connection).fetchrow(query, self.user_id, self.ability.key, *kwargs.values())
        self.__dict__.update(**self._transform_record(record))

    async def update(self, *, connection: asyncpg.Connection | None = None, **kwargs: Any) -> None:
        query = 'UPDATE abilities SET {0} WHERE user_id = $1 AND ability = $2 RETURNING *'.format(
            ', '.join(f'{k} = ${i}' for i, k in enumerate(kwargs, start=3))
        )
        await self.update_with(query, connection=connection, **kwargs)

    async def add(self, *, connection: asyncpg.Connection | None = None, **kwargs: Any) -> None:
        query = 'UPDATE abilities SET {0} WHERE user_id = $1 AND ability = $2 RETURNING *'.format(
            ', '.join(f'{k} = {k} + ${i}' for i, k in enumerate(kwargs, start=3))
        )
        await self.update_with(query, connection=connection, **kwargs)


class AbilityManager:
    def __init__(self, record: UserRecord) -> None:
        self._record = record
        self.cached: dict[Ability, AbilityRecord] = {}
        self.__fetch_task = asyncio.create_task(self.fetch())

    async def wait(self) -> Self:
        await self.__fetch_task
        return self

    async def fetch(self) -> None:
        await self._record.db.wait()

        async with self._record.db.acquire() as conn:
            query = """
                INSERT INTO abilities (user_id, ability, equipped)
                VALUES 
                    ($1, 'punch', true),
                    ($1, 'kick', true),
                    ($1, 'block', true) -- default abilities
                ON CONFLICT (user_id, ability) DO NOTHING
            """
            await conn.execute(query, self._record.user_id)

            query = 'SELECT * FROM abilities WHERE user_id = $1'
            records = await conn.fetch(query, self._record.user_id)

        records = (AbilityRecord.from_record(manager=self, record=r) for r in records)
        self.cached = {r.ability: r for r in records}

    @property
    def equipped_count(self) -> int:
        return sum(r.equipped for r in self.cached.values())

    async def add_ability(self, ability: Ability, *, connection: asyncpg.Connection | None = None) -> None:
        query = 'INSERT INTO abilities (user_id, ability) VALUES ($1, $2) RETURNING *'
        record = await (connection or self._record.db).fetchrow(query, self._record.user_id, ability.key)
        self.cached[ability] = AbilityRecord.from_record(manager=self, record=record)


class UserHistoryEntry(NamedTuple):
    wallet: int
    total: int

    @property
    def bank(self) -> int:
        return self.total - self.wallet

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> Self:
        return cls(record['wallet'], record['total'])


class StackType(IntEnum):
    additive = 0
    multiplicative = 1


class Multiplier(NamedTuple):
    multiplier: float
    title: str
    description: str | None = None
    expires_at: datetime.datetime | None = None
    is_global: bool = True
    stack_type: StackType = StackType.additive

    @property
    def display(self) -> str:
        inner = f'+**{self.multiplier:.1%}**' if self.stack_type is StackType.additive else f'x**{self.multiplier:.1}**'
        base = f'- {self.title}: {inner} {"(global)" if self.is_global else ""}'

        if description := self.description:
            base += f'\n  - *{description}*'

        if expires_at := self.expires_at:
            base += f'\n  - Expires {format_dt(expires_at, "R")}'

        return base


def aggregate_multipliers(multipliers: Iterable[Multiplier]) -> float:
    multiplier = 1.0

    for m in multipliers:
        if m.stack_type is StackType.additive:
            multiplier += m.multiplier
        else:
            multiplier *= m.multiplier

    return multiplier


class BaseRecord(ABC):
    data: dict[str, Any]

    @abstractmethod
    async def fetch(self) -> Self:
        raise NotImplementedError

    async def fetch_if_necessary(self) -> Self:
        """Fetches the record if it is not already cached."""
        if not self.data:
            await self.fetch()

        return self

    @abstractmethod
    async def _update(
        self,
        key: Callable[[tuple[int, str]], str],
        values: dict[str, Any],
        *,
        connection: asyncpg.Connection | None = None,
    ) -> Self:
        raise NotImplementedError

    def update(self, *, connection: asyncpg.Connection | None = None, **values: Any) -> Awaitable[Self]:
        return self._update(lambda o: f'"{o[1]}" = ${o[0]}', values, connection=connection)

    def add(self, *, connection: asyncpg.Connection | None = None, **values: Any) -> Awaitable[Self]:
        return self._update(lambda o: f'"{o[1]}" = "{o[1]}" + ${o[0]}', values, connection=connection)

    def append(self, *, connection: asyncpg.Connection | None = None, **values: Any) -> Awaitable[Self]:
        return self._update(lambda o: f'"{o[1]}" = ARRAY_APPEND("{o[1]}", ${o[0]})', values, connection=connection)


class JobProvider(NamedTuple):
    record: UserRecord

    @property
    def key(self) -> str:
        return self.record.data['job']

    @property
    def job(self) -> Job:
        return get_by_key(Jobs, self.key)

    @property
    def salary(self) -> int:
        return self.record.data['job_salary']

    @property
    def cooldown_expires_at(self) -> datetime.datetime:
        return self.record.data['job_cooldown_expires_at']

    @property
    def hours(self) -> int:
        return self.record.data['job_hours']

    @property
    def fails(self) -> int:
        return self.record.data['job_fails']

    def __repr__(self) -> str:
        return f'<JobProvider job={self.job!r} salary={self.salary} hours={self.hours}>'


@dataclass
class QuestRecord:
    manager: QuestManager
    quest: Quest
    id: int
    progress: int
    completed_at: datetime.datetime | None = None
    expires_at: datetime.datetime | None = None

    @property
    def is_completed(self) -> bool:
        return self.completed_at is not None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return discord.utils.utcnow() > self.expires_at

    @property
    def is_active(self) -> bool:
        """Returns whether the quest is currently active."""
        return not self.is_completed and not self.is_expired

    async def add_progress(
        self,
        progress: int,
        *,
        connection: asyncpg.Connection | None = None,
    ) -> None:
        if not progress:
            return
        await self.set_progress(
            min(self.progress + progress, self.quest.max_progress),
            connection=connection,
        )

    async def set_progress(
        self,
        progress: int,
        *,
        connection: asyncpg.Connection | None = None,
    ) -> None:
        record = self.manager.record
        if progress >= self.quest.max_progress:
            self.completed_at = discord.utils.utcnow()

            reward = self.quest.tickets
            old_tier, old_n, old_d = record.quest_pass_tier_data
            await record.add(tickets=reward, connection=connection)

            new_tier, new_n, new_d = record.quest_pass_tier_data
            pbar = progress_bar(new_n / new_d)

            expansion = [
                f'{Emojis.ticket} **+{reward} Tickets** (you now have {record.tickets:,})',
                f'{Emojis.quest_pass} **Tier {new_tier}** {pbar} {Emojis.ticket} {new_n:,}/{new_d:,}',
            ]
            embed = None
            if new_tier > old_tier:
                rewards = sum(QUEST_PASS_REWARDS[old_tier:new_tier], start=Reward())
                await rewards.apply(record, connection=connection)

                embed = discord.Embed(
                    description=str(rewards), color=Colors.success
                ).set_author(
                    name=f'Tier {new_tier} Quest Pass Rewards',
                )
                expansion.append(f'\u23eb **TIER UP!** You have advanced to **Quest Pass Tier {new_tier}**.')

            content = (
                f'### Quest Complete!\n**{self.quest.title}**\n{expansion_list(expansion)}'
            )
            kwargs = dict(content=content, embed=embed, view=discord.ui.View().add_item(
                StaticCommandButton(
                    command=record.db.bot.get_command('quests'),
                    label='View Quests',
                    style=discord.ButtonStyle.primary,
                )
            ))
            record.db.bot.add_alert(record.user_id, kwargs)

        query = """
                UPDATE quests SET progress = $3, completed_at = $4
                WHERE id = $1 AND user_id = $2
                RETURNING *;
                """
        record = await (connection or self.manager.record.db).fetchrow(
            query, self.id, record.user_id,
            progress, self.completed_at,
        )
        self._update(record)

    def _update(self, record: asyncpg.Record) -> None:
        self.progress = record['progress']
        self.completed_at = record['completed_at']
        self.expires_at = record['expires_at']

    @classmethod
    def from_record(cls, manager: QuestManager, record: asyncpg.Record) -> Self:
        return cls(
            manager=manager,
            quest=Quest(
                record=manager.record,
                template=get_by_key(QuestTemplates, record['template_key']),
                slot=QuestSlot(record['type']),
                arg=record['arg'],
                extra=record.get('extra', None),
            ),
            id=record['id'],
            progress=record['progress'],
            completed_at=record['completed_at'],
            expires_at=record['expires_at'],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'key': self.quest.template.key,
            'slot': self.quest.slot.name,
            'arg': self.quest.arg,
            'extra': self.quest.extra,
            'progress': self.progress,
            'completed_at': self.completed_at,
            'expires_at': self.expires_at,
        }

    async def delete(self, *, connection: asyncpg.Connection | None = None) -> None:
        """Deletes the quest from the database."""
        query = 'DELETE FROM quests WHERE id = $1 AND user_id = $2'
        await (connection or self.manager.record.db).execute(query, self.id, self.manager.record.user_id)
        self.manager.cached.remove(self)


class CompletedQuest(NamedTuple):
    slot: QuestSlot
    refreshes_at: datetime.datetime


class ExpiredQuest(NamedTuple):
    slot: QuestSlot
    expired_at: datetime.datetime
    refreshes_at: datetime.datetime


class QuestSlots(NamedTuple):
    vote: QuestRecord | CompletedQuest
    recurring_easy: QuestRecord
    recurring_mid: QuestRecord
    recurring_hard: QuestRecord
    daily_1: QuestRecord | CompletedQuest | ExpiredQuest
    daily_2: QuestRecord | CompletedQuest | ExpiredQuest


class QuestManager:
    def __init__(self, record: UserRecord) -> None:
        self.record = record
        self.cached: deque[QuestRecord] = deque()
        self._task = record.db.bot.loop.create_task(self.fetch())

    @property
    def all_active_quests(self) -> list[QuestRecord]:
        """Returns all active quests."""
        return [record for record in self.cached if record.is_active]

    def get_active_quest(self, template: QuestTemplate) -> QuestRecord | None:
        for record in self.cached:
            if record.quest.template is template and record.is_active:
                return record
        return None

    def get_active_quest_for_slot(self, slot: QuestSlot, /) -> QuestRecord | None:
        """Returns the first active quest of the specified type."""
        for record in self.cached:
            if record.quest.slot is slot and record.is_active:
                return record
        return None

    def get_most_recent_quest_for_slot(self, slot: QuestSlot, /) -> QuestRecord | None:
        """Returns the most recent quest of the specified type."""
        if active := self.get_active_quest_for_slot(slot):
            return active
        for record in self.cached:
            if record.quest.slot is slot:
                return record
        return None

    async def get_or_create_active_quest_for_slot(self, slot: QuestSlot, /) -> QuestRecord:
        """Returns an active quest of the specified type, creating it if necessary."""
        record = self.get_active_quest_for_slot(slot)
        if record is None:
            record = await self.generate_quest(slot)
        return record

    async def _register_quest(self, quest: Quest, *, expires_at: datetime.datetime | None = None) -> QuestRecord:
        """Registers a new quest in the database."""
        query = """
                INSERT INTO quests (user_id, template_key, type, arg, extra, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *;
                """
        record = await self.record.db.fetchrow(
            query,
            self.record.user_id,
            quest.template.key,
            quest.slot.value,
            quest.arg,
            quest.extra,
            expires_at,
        )
        record = QuestRecord.from_record(self, record)
        self.cached.appendleft(record)
        return record

    def get_quest_for_slot(self, slot: QuestSlot, /) -> QuestRecord | CompletedQuest | ExpiredQuest:
        """Returns the quest for the specified slot, or a datetime specifying when this quest should regenerate."""
        if record := self.get_active_quest_for_slot(slot):
            return record
        if slot is QuestSlot.vote:
            return CompletedQuest(slot, self.record.last_dbl_vote + datetime.timedelta(hours=12))
        if slot in (QuestSlot.daily_1, QuestSlot.daily_2):
            if record := self.get_most_recent_quest_for_slot(slot):
                if record.is_expired:
                    return ExpiredQuest(slot, record.expires_at, next_utc_midnight())
                if record.is_completed:
                    return CompletedQuest(slot, next_utc_midnight())
        raise NotImplementedError

    async def refresh_daily_quest(self, slot: QuestSlot) -> QuestRecord | CompletedQuest | ExpiredQuest:
        """Similar to ``get_quest_for_slot`` but refreshes the quest if past expiry."""
        record = self.get_quest_for_slot(slot)
        if isinstance(record, QuestRecord) and record.is_expired:
            return await self.generate_quest(slot)

        if isinstance(record, CompletedQuest) and discord.utils.utcnow() > record.refreshes_at:
            return await self.generate_quest(slot)
        if isinstance(record, ExpiredQuest) and discord.utils.utcnow() > record.expired_at:
            return await self.generate_quest(slot)

        return record

    async def refresh_slots(self) -> QuestSlots:
        if not self.all_active_quests:
            await self.load_quests()

        vote = self.get_quest_for_slot(QuestSlot.vote)
        if (
            isinstance(vote, CompletedQuest)
            and discord.utils.utcnow() > self.record.last_dbl_vote + datetime.timedelta(hours=12)
        ):
            vote = await self.generate_quest(QuestSlot.vote)

        return QuestSlots(
            vote=vote,
            recurring_easy=await self.get_or_create_active_quest_for_slot(QuestSlot.recurring_easy),
            recurring_mid=await self.get_or_create_active_quest_for_slot(QuestSlot.recurring_mid),
            recurring_hard=await self.get_or_create_active_quest_for_slot(QuestSlot.recurring_hard),
            daily_1=await self.refresh_daily_quest(QuestSlot.daily_1),
            daily_2=await self.refresh_daily_quest(QuestSlot.daily_2),
        )

    async def generate_quest(self, slot: QuestSlot) -> QuestRecord:
        """Creates and registers a new quest of the specified type."""
        # deduce a good template
        previous = self.get_most_recent_quest_for_slot(slot)
        if previous:
            base = previous.completed_at or previous.expires_at
        else:
            base = discord.utils.utcnow()

        if slot is QuestSlot.vote:
            template = QuestTemplates.vote
        else:
            exclude = set(q.quest.template.category for q in self.cached)
            if len(exclude) == len(QuestCategory):
                exclude = set(q.quest.template.category for q in self.all_active_quests)
            category = slot.get_random_category(exclude=exclude)
            available_templates = list(QuestTemplates.walk_templates(category))
            template = random.choice(available_templates)

        quest = await template.generate(slot, self.record)

        expiry = base
        now = discord.utils.utcnow()
        while expiry <= now:
            match slot:
                case QuestSlot.daily_1 | QuestSlot.daily_2:
                    expiry = next_utc_midnight(expiry)
                case QuestSlot.recurring_easy:
                    expiry += datetime.timedelta(hours=12)
                case QuestSlot.recurring_mid:
                    expiry += datetime.timedelta(days=2)
                case QuestSlot.recurring_hard:
                    expiry += datetime.timedelta(days=7)
                case _:
                    expiry = None
                    break

        record = await self._register_quest(quest, expires_at=expiry)
        await template.setup(quest)
        return record

    async def load_quests(self) -> None:
        # If no recurring quests are found, create default ones
        for type in QuestSlot:
            await self.get_or_create_active_quest_for_slot(type)

    async def wait(self) -> Self:
        await self._task
        return self

    async def fetch(self) -> None:
        await self.record.db.wait()
        query = 'SELECT * FROM quests WHERE user_id = $1 ORDER BY id DESC'
        records = await self.record.db.fetch(query, self.record.user_id)
        self.cached = deque(QuestRecord.from_record(self, record) for record in records)
        await self.refresh_slots()


class UserPremiumType(IntEnum):
    none = 0
    silver = 1
    gold = 2

    @property
    def is_premium(self) -> bool:
        return self is not self.none

    @property
    def emoji(self) -> str:
        match self:
            case UserPremiumType.silver:
                return Emojis.Subscriptions.coined_silver
            case UserPremiumType.gold:
                return Emojis.Subscriptions.coined_gold
            case _:
                return ''

    def __bool__(self) -> bool:
        return self.is_premium


class GuildPremiumType(IntEnum):
    none = 0
    premium = 1

    @property
    def is_premium(self) -> bool:
        return self is not self.none

    @property
    def emoji(self) -> str:
        if not self:
            return ''
        return Emojis.Subscriptions.coined_premium

    def __bool__(self) -> bool:
        return self.is_premium


class UserRecord(BaseRecord):
    """Stores data about a user."""

    ALCOHOL_ACTIVE_DURATION = datetime.timedelta(hours=2)
    LEVELING_CURVE = CubicCurve.default()

    def __init__(self, user_id: int, *, db: Database) -> None:
        self.db: Database = db
        self.user_id: int = user_id
        self.data: dict[str, Any] = {}

        self.history: list[tuple[datetime.datetime, UserHistoryEntry]] = []  # Experimental
        self.__history_fetched: bool = False

        self.__inventory_manager: InventoryManager | None = None
        self.__notifications_manager: NotificationsManager | None = None
        self.__cooldown_manager: CooldownManager | None = None
        self.__skill_manager: SkillManager | None = None
        self.__crop_manager: CropManager | None = None
        self.__pet_manager: PetManager | None = None
        self.__quest_manager: QuestManager | None = None

    def __repr__(self) -> str:
        return f'<UserRecord user_id={self.user_id}>'

    async def update_history(self, connection: asyncpg.Connection) -> None:
        if self.history:
            _, previous = self.history[-1]
            # Prevent a useless duplicate entry
            if previous.wallet == self.wallet and previous.total == self.total_coins:
                return

        query = 'INSERT INTO user_coins_graph_data (user_id, wallet, total) VALUES ($1, $2, $3) RETURNING *;'
        record = await connection.fetchrow(query, self.user_id, self.wallet, self.total_coins)
        self.history.append((record['timestamp'], UserHistoryEntry.from_record(record)))

    async def fetch(self) -> UserRecord:
        await self.db.wait()
        query = """
                INSERT INTO users (user_id) VALUES ($1) 
                ON CONFLICT (user_id) DO UPDATE SET user_id = $1 -- useless upsert
                RETURNING *;
                """

        async with self.db.acquire() as conn:
            self.data.update(await conn.fetchrow(query, self.user_id))  # TODO: Welcome user if new
            await self.fetch_history(connection=conn)

        await self.pet_manager.wait()  # required for multipliers
        return self

    async def fetch_history(self, connection: asyncpg.Connection) -> None:
        self.__history_fetched = True
        self.history = [
            (record['timestamp'], UserHistoryEntry.from_record(record))
            for record in await connection.fetch(
                'SELECT * FROM user_coins_graph_data WHERE user_id = $1 ORDER BY timestamp',
                self.user_id,
            )
        ]
        if not self.history:
            await self.update_history(connection=connection)

    async def fetch_if_necessary(self) -> UserRecord:
        if not len(self.data):
            await self.fetch()

        if not self.__history_fetched:
            async with self.db.acquire() as conn:
                await self.fetch_history(connection=conn)

        return self

    async def _update(
        self,
        key: Callable[[tuple[int, str]], str],
        values: dict[str, Any],
        *,
        connection: asyncpg.Connection | None = None,
    ) -> UserRecord:
        query = "/**/ UPDATE users SET {} WHERE user_id = $1 RETURNING *;"  # prevent language injection with /**/
        actual_conn = await self.db.acquire() if connection is None else connection

        # noinspection PyTypeChecker
        try:
            self.data.update(
                await actual_conn.fetchrow(
                    query.format(', '.join(map(key, enumerate(values.keys(), start=2)))),
                    self.user_id,
                    *values.values(),
                ),
            )
            await self.update_history(connection=actual_conn)
        finally:
            if connection is None:
                await self.db.release(actual_conn)
        return self

    async def add_coins(
        self,
        coins: int,
        /,
        *,
        ctx: Context | None = None,
        multiplier: float | None = None,
        is_profit: bool = True,
        connection: asyncpg.Connection | None = None,
    ) -> int:
        """Adds coins including applying multipliers. Returns the amount of coins added."""
        multiplier = multiplier or self.coin_multiplier_in_ctx(ctx)
        coins = round(coins * multiplier) if coins > 0 else coins
        await self.add(wallet=coins, connection=connection)

        quests = await self.quest_manager.wait()
        if is_profit:
            if entry := quests.get_active_quest(QuestTemplates.earn_coins):
                await entry.add_progress(coins, connection=connection)

        return coins

    async def add_exp(
        self, exp: int, /, *, ctx: Context | None = None, connection: asyncpg.Connection | None = None,
    ) -> bool:
        """Return whether the user has leveled up."""
        old = self.level
        multiplier = self.exp_multiplier_in_ctx(ctx)
        exp = round(exp * multiplier)
        await self.add(exp=exp, connection=connection)

        if self.level > old:
            rewards = Reward()
            for milestone, reward in LEVEL_REWARDS.items():
                if self.last_level_reward < milestone <= self.level:
                    rewards += reward

            if rewards:
                await rewards.apply(self, connection=connection)
                await self.update(last_level_reward=self.level, connection=connection)

            await self.notifications_manager.add_notification(
                NotificationData.LevelUp(level=self.level, **rewards.to_notification_data_kwargs()),
                connection=connection,
            )
            return True

        return False

    async def add_bank_space(
        self, space: int, *, connection: asyncpg.Connection | None = None,
    ) -> int:
        """Adds bank space to the user, considering multipliers."""
        added = round(space * self.bank_space_growth_multiplier)
        await self.add(max_bank=added, connection=connection)
        return added

    async def add_random_bank_space(
        self,
        minimum: int,
        maximum: int,
        *,
        chance: float = 1,
        connection: asyncpg.Connection | None = None,
    ) -> int:
        if random.random() > chance:
            return 0

        return await self.add_bank_space(
            space=random.randint(minimum, maximum),
            connection=connection,
        )

    async def add_random_exp(
        self, minimum: int, maximum: int, *, chance: float = 1,
        ctx: Context | None = None, connection: asyncpg.Connection | None = None,
    ) -> int:
        if random.random() > chance:
            return 0

        amount = random.randint(minimum, maximum)
        await self.add_exp(amount, ctx=ctx, connection=connection)
        return amount

    async def make_dead(self, *, reason: str | None = None, connection: asyncpg.Connection | None = None) -> None:
        inventory = await self.inventory_manager.wait()
        quantity = inventory.cached.quantity_of('lifesaver')
        if quantity > 0:
            await inventory.add_item('lifesaver', -1, connection=connection)

            await self.notifications_manager.add_notification(
                NotificationData.NearDeath(reason=reason, remaining=quantity - 1),
                connection=connection,
            )
            return

        old = self.wallet
        await self.update(wallet=0, connection=connection)

        available = [(key, value) for key, value in inventory.cached.items() if value]
        if not len(available):
            item, quantity = None, 0
        else:
            item, quantity = random.choice(available)
            await inventory.add_item(item, -quantity, connection=connection)

        await self.notifications_manager.add_notification(
            NotificationData.Death(reason=reason, coins_lost=old, item_lost=item and item.key, quantity_lost=quantity),
            connection=connection,
        )

    async def regenerate_token(self) -> str:
        token = secrets.token_urlsafe(32)
        await self.update(token=token)
        return token

    async def get_or_generate_token(self) -> str:
        if not self.data.get('token'):
            return await self.regenerate_token()
        return self.data['token']

    @property
    def sanitized_data(self) -> dict[str, Any]:
        """Returns a sanitized copy of the data"""
        blacklisted = {'token', 'email'}
        return {k: v for k, v in self.data.items() if k not in blacklisted}

    @property
    def wallet(self) -> int:
        return self.data['wallet']

    @property
    def bank(self) -> int:
        return self.data['bank']

    @property
    def max_bank(self) -> int:
        return self.data['max_bank']

    @property
    def bank_ratio(self) -> float:
        return self.max_bank and self.bank / self.max_bank

    @property
    def total_coins(self) -> int:
        return self.wallet + self.bank

    @property
    def total_exp(self) -> int:
        return self.data['exp']

    @property
    def level_data(self) -> tuple[int, int, int]:
        return self.LEVELING_CURVE.compute_level(self.total_exp)

    @property
    def level(self) -> int:
        return self.level_data[0]

    @property
    def exp(self) -> int:
        return self.level_data[1]

    @property
    def exp_requirement(self) -> int:
        return self.level_data[2]

    @property
    def orbs(self) -> int:
        return self.data['orbs']

    @property
    def tickets(self) -> int:
        return self.data['tickets']

    @property
    def quest_pass_tier_data(self) -> tuple[int, int, int]:
        return QUEST_PASS_CURVE.compute_level(self.tickets)

    @property
    def base_exp_multiplier(self) -> float:
        return self.data['exp_multiplier']

    @property
    def _cigarette_active(self) -> bool:
        return self.cigarette_expiry and self.cigarette_expiry > discord.utils.utcnow()

    def walk_exp_multipliers(self, ctx: Context | None = None) -> Generator[Multiplier, Any, Any]:
        yield Multiplier(
            self.base_exp_multiplier,
            'Base Multiplier',
            description='accumulated from using items like cheese',
        )
        yield Multiplier(self.prestige * 0.25, f'{Emojis.get_prestige_emoji(self.prestige)} Prestige {self.prestige}')

        if self._cigarette_active:
            yield Multiplier(2, f'{Items.cigarette.emoji} Cigarette', expires_at=self.cigarette_expiry)

        if quantity := self.inventory_manager.cached.quantity_of(trophy := Items.voting_trophy):
            yield Multiplier(0.15 * quantity, f'{trophy.get_sentence_chunk(quantity, bold=False)} in inventory')

        pets = self.pet_manager
        if cat := pets.get_active_pet(Pets.cat):
            level = cat.level  # Somewhat expensive to calculate, store it first
            yield Multiplier(0.008 + level * 0.004, f'{Pets.cat.display} (Level {level})')

        if bunny := pets.get_active_pet(Pets.bunny):
            level = bunny.level
            yield Multiplier(0.01 + level * 0.005, f'{Pets.bunny.display} (Level {level})')

        if duck := pets.get_active_pet(Pets.duck):
            level = duck.level
            yield Multiplier(0.01 + level * 0.003, f'{Pets.duck.display} (Level {level})')

        if cow := pets.get_active_pet(Pets.cow):
            level = cow.level
            yield Multiplier(0.02 + level * 0.006, f'{Pets.cow.display} (Level {level})')

        if tortoise := pets.get_active_pet(Pets.tortoise):
            level = tortoise.level
            yield Multiplier(0.02 + level * 0.005, f'{Pets.tortoise.display} (Level {level})')

        if armadillo := pets.get_active_pet(Pets.armadillo):
            level = armadillo.level
            yield Multiplier(0.02 + level * 0.006, f'{Pets.armadillo.display} (Level {level})')

        subs = Emojis.Subscriptions
        if ctx is not None and ctx.guild is not None:
            if ctx.interaction and not ctx.interaction.is_guild_integration():
                return

            if sum(not m.bot for m in ctx.guild.members) > 50:
                yield Multiplier(0.25, 'Large Server', is_global=False)

            if ctx.guild.id in multiplier_guilds:
                yield Multiplier(0.5, ctx.guild.name, is_global=False)

            if self.db.get_guild_record(ctx.guild.id, fetch=False).premium_type is GuildPremiumType.premium:
                yield Multiplier(
                    2, f'{subs.coined_premium} Premium Server',
                    stack_type=StackType.multiplicative, is_global=False,
                )

        if self.premium_type is UserPremiumType.gold:
            yield Multiplier(3, f'{subs.coined_gold} Coined Gold', stack_type=StackType.multiplicative)
        elif self.premium_type is UserPremiumType.silver:
            yield Multiplier(2, f'{subs.coined_silver} Coined Silver', stack_type=StackType.multiplicative)


    @property
    def global_exp_multiplier(self) -> float:
        return self.exp_multiplier_in_ctx(None)

    def exp_multiplier_in_ctx(self, ctx: Context | None = None) -> float:
        return aggregate_multipliers(self.walk_exp_multipliers(ctx))

    def walk_coin_multipliers(self, ctx: Context | None = None) -> Generator[Multiplier, Any, Any]:
        yield Multiplier(self.prestige * 0.25, f'{Emojis.get_prestige_emoji(self.prestige)} Prestige {self.prestige}')

        if self.alcohol_expiry is not None:
            yield Multiplier(0.25, f'{Items.alcohol.emoji} Alcohol', expires_at=self.alcohol_expiry)
        if self._cigarette_active:
            yield Multiplier(0.25, f'{Items.cigarette.emoji} Cigarette', expires_at=self.cigarette_expiry)

        pets = self.pet_manager
        if bird := pets.get_active_pet(Pets.bird):
            level = bird.level
            yield Multiplier(0.01 + level * 0.004, f'{Pets.bird.display} (Level {level})')

        if panda := pets.get_active_pet(Pets.panda):
            level = panda.level
            yield Multiplier(0.02 + level * 0.01, f'{Pets.panda.display} (Level {level})')

        if fox := pets.get_active_pet(Pets.fox):
            level = fox.level
            yield Multiplier(0.05 + level * 0.01, f'{Pets.fox.display} (Level {level})')

        if weasel := pets.get_active_pet(Pets.weasel):
            level = weasel.level
            yield Multiplier(0.01 + level * 0.005, f'{Pets.weasel.display} (Level {level})')

        if jaguar := pets.get_active_pet(Pets.jaguar):
            level = jaguar.level
            yield Multiplier(0.05 + level * 0.01, f'{Pets.jaguar.display} (Level {level})')

        if tiger := pets.get_active_pet(Pets.tiger):
            level = tiger.level
            yield Multiplier(0.08 + level * 0.015, f'{Pets.tiger.display} (Level {level})')

        subs = Emojis.Subscriptions
        if ctx is not None and ctx.guild is not None:
            if ctx.interaction and not ctx.interaction.is_guild_integration():
                return

            if self.db.get_guild_record(ctx.guild.id, fetch=False).premium_type is GuildPremiumType.premium:
                yield Multiplier(
                    1.25, f'{subs.coined_premium} Premium Server',
                    stack_type=StackType.multiplicative, is_global=False,
                )

        if self.premium_type is UserPremiumType.gold:
            yield Multiplier(2, f'{subs.coined_gold} Coined Gold', stack_type=StackType.multiplicative)
        elif self.premium_type is UserPremiumType.silver:
            yield Multiplier(1.5, f'{subs.coined_silver} Coined Silver', stack_type=StackType.multiplicative)

    @property
    def global_coin_multiplier(self) -> float:
        return self.coin_multiplier_in_ctx(None)

    def coin_multiplier_in_ctx(self, ctx: Context | None = None) -> float:
        return aggregate_multipliers(self.walk_coin_multipliers(ctx))

    def walk_bank_space_growth_multipliers(self) -> Generator[Multiplier, Any, Any]:
        yield Multiplier(self.prestige * 0.5, f'{Emojis.get_prestige_emoji(self.prestige)} Prestige {self.prestige}')

        if fox := self.pet_manager.get_active_pet(Pets.fox):
            level = fox.level
            yield Multiplier(0.02 + level * 0.01, f'{Pets.fox.display} (Level {level})')

    @property
    def bank_space_growth_multiplier(self) -> float:
        return aggregate_multipliers(self.walk_bank_space_growth_multipliers())

    @property
    def prestige(self) -> int:
        return self.data['prestige']

    @property
    def padlock_active(self) -> bool:
        return self.data['padlock_active']

    @property
    def last_alcohol_usage(self) -> datetime.datetime | None:
        return self.data.get('last_alcohol_usage')

    @property
    def alcohol_expiry(self) -> datetime.datetime | None:
        if self.last_alcohol_usage is None:
            return None

        elapsed = discord.utils.utcnow() - self.last_alcohol_usage
        if elapsed > self.ALCOHOL_ACTIVE_DURATION:
            return None
        return self.last_alcohol_usage + self.ALCOHOL_ACTIVE_DURATION

    @property
    def unread_notifications(self) -> int:
        return self.data['unread_notifications']

    @property
    def daily_streak(self) -> int:
        return self.data['daily_streak']

    @property
    def weekly_streak(self) -> int:
        return self.data['weekly_streak']

    @property
    def dm_notifications(self) -> bool:
        return self.data['dm_notifications']

    @property
    def anonymous_mode(self) -> bool:
        return self.data['anonymous_mode']

    @property
    def hide_partnerships(self) -> bool:
        return self.data['hide_partnerships']

    @property
    def hide_tips(self) -> bool:
        return self.data['hide_tips']

    @property
    def max_equipped_pets(self) -> int:
        return self.data['max_equipped_pets']

    @property
    def pet_operations(self) -> int:
        return self.data['pet_operations']

    @property
    def pet_operations_cooldown_start(self) -> datetime.datetime:
        return self.data['pet_operations_cooldown_start']

    @property
    def last_dbl_vote(self) -> datetime.datetime:
        return self.data['last_dbl_vote']

    @property
    def total_votes(self) -> int:
        return self.data['total_votes']

    @property
    def votes_this_month(self) -> int:
        return self.data['votes_this_month']

    @property
    def deepest_dig(self) -> int:
        return self.data['deepest_dig']

    @property
    def job(self) -> JobProvider | None:
        if self.data.get('job') is None:
            return None
        return JobProvider(self)

    @property
    def job_switch_cooldown_expiry(self) -> datetime.datetime:
        return self.data['job_switch_cooldown_expires_at']

    @property
    def work_experience(self) -> int:
        return self.data['work_experience']

    @property
    def iq(self) -> int:
        return self.data['iq']

    @property
    def battle_hp(self) -> int:
        return self.data['battle_hp']

    @property
    def battle_stamina(self) -> int:
        return self.data['battle_stamina']

    @property
    def cigarette_expiry(self) -> datetime.datetime:
        return self.data['cigarette_expiry']

    @property
    def last_level_reward(self) -> int:
        return self.data['last_level_reward']

    @property
    def equipped_backpack(self) -> Backpack:
        return get_by_key(Backpacks, self.data['backpack'])

    @property
    def unlocked_backpacks(self) -> list[Backpack]:
        out = [get_by_key(Backpacks, b) for b in self.data['unlocked_backpacks']]
        if Backpacks.standard_backpack not in out:
            out.append(Backpacks.standard_backpack)
        return out

    @property
    def railgun_expiry(self) -> datetime.datetime | None:
        return self.data.get('railgun_cooldown_expires_at')

    @property
    def wheel_resets_at(self) -> datetime.datetime | None:
        return self.data['wheel_resets_at']

    @property
    def wheel_spins_this_cycle(self) -> int:
        return self.data['wheel_spins_this_cycle']

    @property
    def redeemed_vote_wheel_spin(self) -> bool:
        return self.data['redeemed_vote_wheel_spin']

    @property
    def quest_rerolls_remaining(self) -> int:
        return self.data['quest_rerolls_remaining']

    @property
    def last_quest_reroll_update(self) -> datetime.datetime | None:
        return self.data['last_quest_reroll_update']

    async def update_quest_rerolls_remaining(self) -> int:
        DEFAULT_MAX_REROLLS: int = 5

        now = discord.utils.utcnow()
        if self.last_quest_reroll_update is None or (
            now >= next_weekday_utc_midnight(self.last_quest_reroll_update, weekday=0)
        ):
            await self.update(last_quest_reroll_update=now, quest_rerolls_remaining=DEFAULT_MAX_REROLLS)
            return self.quest_rerolls_remaining

        return self.quest_rerolls_remaining

    @property
    def premium_type(self) -> UserPremiumType:
        return UserPremiumType.none

    @property
    def email(self) -> str | None:
        return self.data.get('email')

    @property
    def token(self) -> str | None:
        return self.data.get('token')

    @property
    def inventory_manager(self) -> InventoryManager:
        if not self.__inventory_manager:
            self.__inventory_manager = InventoryManager(self)

        return self.__inventory_manager

    @property
    def notifications_manager(self) -> NotificationsManager:
        if not self.__notifications_manager:
            self.__notifications_manager = NotificationsManager(self)

        return self.__notifications_manager

    @property
    def cooldown_manager(self) -> CooldownManager:
        if not self.__cooldown_manager:
            self.__cooldown_manager = CooldownManager(self)

        return self.__cooldown_manager

    @property
    def skill_manager(self) -> SkillManager:
        if not self.__skill_manager:
            self.__skill_manager = SkillManager(self)

        return self.__skill_manager

    @property
    def crop_manager(self) -> CropManager:
        if not self.__crop_manager:
            self.__crop_manager = CropManager(self)

        return self.__crop_manager

    @property
    def pet_manager(self) -> PetManager:
        if not self.__pet_manager:
            self.__pet_manager = PetManager(self)

        return self.__pet_manager

    @property
    def quest_manager(self) -> QuestManager:
        """Returns the quest manager for the user."""
        if not self.__quest_manager:
            self.__quest_manager = QuestManager(self)

        return self.__quest_manager


class GuildRecord(BaseRecord):
    """Represents a guild record in the database."""

    def __init__(self, guild_id: int, *, db: Database) -> None:
        self.guild_id: int = guild_id
        self.data: dict[str, Any] = {}
        self.db: Database = db

    async def fetch(self) -> GuildRecord:
        """Fetches the guild record from the database."""
        query = """
                INSERT INTO
                    guilds (guild_id)
                VALUES ($1)
                ON CONFLICT (guild_id)
                DO UPDATE
                    SET guild_id = $1
                RETURNING
                    *
                """

        self.data.update(await self.db.fetchrow(query, self.guild_id))
        return self

    async def _update(
        self,
        key: Callable[[tuple[int, str]], str],
        values: dict[str, Any],
        *,
        connection: asyncpg.Connection | None = None,
    ) -> GuildRecord:
        query = '/**/ UPDATE guilds SET {} WHERE guild_id = $1 RETURNING *'
        # noinspection PyTypeChecker
        self.data.update(
            await (connection or self.db).fetchrow(
                query.format(', '.join(map(key, enumerate(values.keys(), start=2)))),
                self.guild_id,
                *values.values(),
            ),
        )
        return self

    @property
    def prefixes(self) -> list[str]:
        """Returns the guild's prefixes."""
        return self.data['prefixes']

    @property
    def premium_type(self) -> GuildPremiumType:
        """Returns the guild's premium type."""
        return GuildPremiumType.none
