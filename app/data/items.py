from __future__ import annotations

import asyncio
import datetime
import random
import warnings
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from functools import partial
from textwrap import dedent
from typing import (
    Any,
    Awaitable,
    Callable,
    Collection,
    Final,
    Generator,
    Generic,
    NamedTuple,
    TYPE_CHECKING,
    TypeAlias,
    TypeVar,
)

from discord.ext.commands import BadArgument
from discord.utils import format_dt

from app.data.pets import Pet, Pets, generate_pet_weights
from app.util.common import get_by_key, humanize_duration, humanize_list, ordinal, pluralize
from app.util.structures import DottedDict
from config import Emojis

if TYPE_CHECKING:
    import asyncpg

    from app.core import Context
    from app.database import UserRecord
    from app.data.enemies import Enemy

    UsageCallback: TypeAlias = 'Callable[[Items, Context, Item], Awaitable[Any]] | Callable[[Items, Context, Item, int], Awaitable[Any]]'
    RemovalCallback: TypeAlias = 'Callable[[Items, Context, Item], Awaitable[Any]]'

T = TypeVar('T')


class ItemType(Enum):
    """Stores the type of this item."""
    tool          = 0
    power_up      = 1
    fish          = 2
    wood          = 3
    crate         = 4
    collectible   = 5
    worm          = 6
    ore           = 7
    crop          = 8
    harvest       = 9
    net           = 10
    dirt          = 11
    miscellaneous = 12


class ItemRarity(Enum):
    common       = 0
    uncommon     = 1
    rare         = 2
    epic         = 3
    legendary    = 4
    mythic       = 5
    unobtainable = 6

    def __lt__(self, other: Any):
        if not isinstance(other, self.__class__):
            return NotImplemented
        return self.value < other.value

    def __gt__(self, other: Any):
        if not isinstance(other, self.__class__):
            return NotImplemented
        return self.value > other.value

    def __le__(self, other: Any):
        if not isinstance(other, self.__class__):
            return NotImplemented
        return self.value <= other.value

    def __ge__(self, other: Any):
        if not isinstance(other, self.__class__):
            return NotImplemented
        return self.value >= other.value

    @property
    def emoji(self) -> str:
        return getattr(Emojis.Rarity, self.name)


class CrateMetadata(NamedTuple):
    minimum: int
    maximum: int
    items: dict[Item, tuple[float, int, int]]


class CropMetadata(NamedTuple):
    time: int
    count: tuple[int, int]
    item: Item


class HarvestMetadata(NamedTuple):
    get_source_crop: Callable[[], Item[CropMetadata]]


class NetMetadata(NamedTuple):
    weights: dict[Pet, float]
    priority: int


class FishingPoleMetadata(NamedTuple):
    weights: dict[Item, float]
    iterations: int


class ToolMetadata(NamedTuple):
    strength: int


def fish_weights(
    local_ns: dict[str, Any], *, exclude: Collection[Item] = (), **rarity_weights: float,
) -> dict[Item, float]:
    items = DottedDict(local_ns)
    # base weights
    weights = {
        None: 1,
        items.fish: 0.4,
        items.anchovy: 0.3,
        items.sardine: 0.3,
        items.catfish: 0.25,
        items.clownfish: 0.25,
        items.angel_fish: 0.2,
        items.goldfish: 0.2,
        items.blowfish: 0.15,
        items.crab: 0.1,
        items.turtle: 0.09,
        items.lobster: 0.08,
        items.squid: 0.06,
        items.octopus: 0.04,
        items.seahorse: 0.03,
        items.axolotl: 0.02,
        items.jellyfish: 0.015,
        items.dolphin: 0.01,
        items.swordfish: 0.008,
        items.siamese_fighting_fish: 0.007,
        items.shark: 0.006,
        items.rainbow_trout: 0.004,
        items.whale: 0.003,
        items.vibe_fish: 0.001,
    }

    # remove excluded items
    for item in exclude:
        try:
            del weights[item]
        except KeyError:
            warnings.warn(f'Excluded item not found in default fish weights: {item}')

    for rarity, weight in rarity_weights.items():
        if rarity == 'none':
            weights[None] *= weight
            continue

        rarity = ItemRarity[rarity]
        # update all items with this rarity
        for item in weights:
            if item and item.rarity >= rarity:
                weights[item] *= weight

    return weights


class OverrideQuantity(NamedTuple):
    quantity: int


class EnemyRef(NamedTuple):
    ref: str
    damage: tuple[int, int]

    @property
    def resolved(self) -> Enemy:
        from app.data.enemies import Enemies

        return get_by_key(Enemies, self.ref)


@dataclass
class Item(Generic[T]):
    """Stores data about an item."""
    type: ItemType
    key: str
    name: str
    emoji: str
    description: str
    brief: str = None
    price: int = None
    sell: int = None
    buyable: bool = False
    sellable: bool = True
    giftable: bool = True
    dispose: bool = False  # Dispose on use?
    singular: str = None
    plural: str = None
    rarity: ItemRarity = ItemRarity.common
    metadata: T = None
    energy: int | None = None

    hp: int = 0  # hit points for digging
    volume: int = 1  # volume in storage units

    durability: int | None = None
    repair_rate: int | None = None  # cost per damage to repair
    repair_time: datetime.timedelta | None = None  # time per damage to repair

    usage_callback: UsageCallback | None = None
    removal_callback: RemovalCallback | None = None

    def __post_init__(self) -> None:
        assert (self.durability is None) is (self.repair_rate is None) is (self.repair_time is None), (
            'durability, repair_rate, and repair_time must be specified together'
        )

        if not self.brief:
            self.brief = self.description.split('\n')[0]

        if not self.singular:
            self.singular = 'an' if self.name.lower().startswith(tuple('aeiou')) else 'a'

        if self.sell and not self.price:
            self.price = self.sell

        elif self.price and not self.sell:
            self.sell = round(self.price / 2.7)

        if not self.plural:
            self.plural = self.name + 's'

    def __hash__(self) -> int:
        return hash(self.key)

    def __str__(self) -> str:
        return self.key

    def __repr__(self) -> str:
        return f'<Item key={self.key} name={self.name!r}>'

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, self.__class__) and self.key == other.key

    @property
    def display_name(self) -> str:
        return self.get_display_name()

    @property
    def usable(self) -> bool:
        return self.usage_callback is not None

    @property
    def removable(self) -> bool:
        return self.removal_callback is not None

    def quantify(self, quantity: int) -> str:
        if quantity == 1:
            return f'{self.singular} {self.name}'
        return f'{quantity:,} {self.plural}'

    def get_sentence_chunk(self, quantity: int = 1, *, bold: bool = True) -> str:
        fmt = '{} **{}**' if bold else '{} {}'
        name = self.name if quantity == 1 else self.plural
        middle = fmt.format(self.emoji, name).strip()

        quantifier = format(quantity, ',') if quantity != 1 else self.singular
        return f'{quantifier} {middle}'

    def get_display_name(self, *, bold: bool = False, plural: bool = False) -> str:
        fmt = '{} **{}**' if bold else '{} {}'
        return fmt.format(self.emoji, self.plural if plural else self.name).strip()

    def to_use(self, func: UsageCallback) -> UsageCallback:
        self.usage_callback = func
        return func

    def to_remove(self, func: RemovalCallback) -> RemovalCallback:
        self.removal_callback = func
        return func

    async def use(self, ctx: Context, quantity: int) -> int:
        assert self.usable

        try:
            coro = self.usage_callback(ITEMS_INST, ctx, self, quantity)
        except TypeError:
            coro = self.usage_callback(ITEMS_INST, ctx, self)
            quantity = 1

        try:
            result = await coro
        except ItemUsageError as exc:
            await ctx.send(exc, reference=ctx.message)
            return 0
        else:
            if isinstance(result, OverrideQuantity):
                return result.quantity

        return quantity

    async def remove(self, ctx: Context) -> None:
        assert self.removable

        await self.removal_callback(ITEMS_INST, ctx, self)


class ItemUsageError(Exception):
    """When raised, disposed items will not be disposed."""


if TYPE_CHECKING:
    Fish = Wood = Crate = Worm = Ore = Harvest = Net = Item
else:
    Fish = partial(Item, type=ItemType.fish)
    Wood = partial(Item, type=ItemType.wood)
    Crate: Callable[..., Item[CrateMetadata]] = partial(Item, type=ItemType.crate, dispose=True, sellable=False)
    Worm = partial(Item, type=ItemType.worm)
    Ore = partial(Item, type=ItemType.ore)
    Harvest = partial(Item, type=ItemType.harvest)
    Net = partial(Item, type=ItemType.net)


def Crop(*, metadata: CropMetadata, **kwargs) -> Item[CropMetadata]:
    return Item(
        type=ItemType.crop,
        metadata=metadata,
        description=f'A crop that produces {metadata.item.emoji} {metadata.item.plural}.',
        buyable=True,
        sellable=False,
        **kwargs,
    )


class Items:
    """Stores all items"""

    lifesaver = Item(
        type=ItemType.tool,
        key='lifesaver',
        name='Lifesaver',
        emoji='<:lifesaver:1379661143059988572>',
        description='These quite literally save your life.',
        price=4200,
        buyable=True,
    )

    pistol = Item(
        type=ItemType.tool,
        key='pistol',
        name='Pistol',
        emoji='<:pistol:1379661168557424841>',
        brief='A quite deadly weapon that can be used to shoot and kill others.',
        description=(
            'A quite deadly weapon that can be used to shoot and kill others. We do not condone violence of any sort '
            '(especially with deadly weapons) in real life, but in this virtual economy system it is perfectly fine.\n\n'
            'Shoot others with the `shoot` command and steal their full wallet in the process. Owning a pistol also '
            'boosts profits from the `crime` command by **50%**.\n\n'
            'You can be protected against being shot by using a **lifesaver**. There is also a large chance that you can '
            'be caught by the police, pay a large fine, and even get yourself killed.'
        ),
        price=10_000,
        buyable=True,
    )

    alcohol = Item(
        type=ItemType.tool,
        key='alcohol',
        name='Alcohol',
        emoji='<:alcohol:1379661014269952050>',
        brief='Intoxicate yourself with alcohol for two hours!',
        description=(
            'Intoxicate yourself with alcohol! Drinking alcohol will make you drunk for two hours.\n\nWhile drunk, you will:\n'
            '- have a +25% coin multiplier,\n'
            '- have a +25% gambling multiplier,\n'
            '- have a +15% chance to successfully rob others,\n'
            '- have a +15% chance to successfully shoot others, **but:**\n'
            '- not be able to work,\n'
            '- are 20% more susceptible to being robbed, and\n'
            '- are 20% more susceptible to being shot.\n\n'
            'Additionally, when drinking alcohol, there is:\n'
            '- a small chance you will be caught by the police and pay a fine,\n'
            '- a small chance you will kill yourself of alcohol poisoning, and\n'
            '- a 6-hour cooldown from when you last drank alcohol for when you can drink again.'
        ),
        price=8_000,
        buyable=True,
    )

    ALCOHOL_USAGE_COOLDOWN = datetime.timedelta(hours=6)
    ALCOHOL_FINE_MESSAGES = (
        'You drink your alcohol in public alcohol-free zone and you are caught by the police. They force you to pay a fine of {}.',
        'You get a bit too woozy and break a few laws, you end up accumulating {} in fines.',
    )
    ALCOHOL_DEATH_MESSAGES = (
        'You drink a bit too much alcohol and die due to alcohol poisoning. Good going!',
    )

    @alcohol.to_use
    async def use_alcohol(self, ctx: Context, item: Item) -> None:
        record = await ctx.db.get_user_record(ctx.author.id)
        # enforce 6 hour cooldown
        if record.last_alcohol_usage and ctx.now - record.last_alcohol_usage <= self.ALCOHOL_USAGE_COOLDOWN:
            retry_at = record.last_alcohol_usage + self.ALCOHOL_USAGE_COOLDOWN
            raise ItemUsageError(
                "Calm down you drunkard, you're drinking too fast! "
                f"You can drink alcohol again {format_dt(retry_at, 'R')}."
            )

        message = await ctx.reply(f'{item.emoji} Drinking the alcohol...')
        await asyncio.sleep(2)

        # pay a fine
        if random.random() < 0.1:
            fine = max(500, int(record.wallet * random.uniform(0.4, 1.0)))
            msg = random.choice(self.ALCOHOL_FINE_MESSAGES).format(f'{Emojis.coin} **{fine}**')

            if record.wallet < 500:
                msg += ' Since you\'re poor, they kill you instead and take your wallet.'
                async with ctx.db.acquire() as conn:
                    await record.make_dead(
                        reason='The police shot you to death since you couldn\'t afford fines from robbery.',
                        connection=conn,
                    )
                    await record.update(wallet=0)

                await ctx.maybe_edit(message, f'\U0001f480 {msg}')
                return

            await record.add(wallet=-fine)
            await ctx.maybe_edit(message, f'\U0001f6a8 {msg}')
            return

        # make dead
        if random.random() < 0.01:
            await record.make_dead(reason='You died of alcohol poisoning.')
            await ctx.maybe_edit(message, f'\U0001f480 {random.choice(self.ALCOHOL_DEATH_MESSAGES)}')
            return

        await record.update(last_alcohol_usage=ctx.now)
        await ctx.maybe_edit(message, dedent(f'''
            You drink the {item.emoji} **Alcohol** and for the next two hours you are granted with:
            - a **+25%** coin multiplier,
            - a **+25%** gambling multiplier,
            - a **+15%** chance to successfully rob others, and
            - a **+15%** chance to successfully shoot others.

            However, for these two hours, you will also be:
            - unable to work,
            - **20%** more susceptible to being robbed, and
            - **20%** more susceptible to being shot.
        '''))

    @alcohol.to_remove
    async def remove_alcohol(self, ctx: Context, item: Item) -> None:
        record = await ctx.db.get_user_record(ctx.author.id)
        if record.alcohol_expiry is None:
            await ctx.reply('You are not drunk (i.e. you don\'t have alcohol active).')
            return
        await record.update(last_alcohol_usage=None)
        await ctx.reply(f'{item.emoji} Removed the effects of alcohol; you are no longer drunk.')

    meth = Item(
        type=ItemType.tool,
        rarity=ItemRarity.unobtainable,
        key='meth',
        name='Meth',
        plural='Meth',
        emoji='<:meth:1262851173250240604>',
        description='kill',
        price=100000000,
        buyable=False,
        dispose=True,
    )

    @meth.to_use
    async def use_meth(self, ctx: Context, item: Item) -> None:
        record = await ctx.db.get_user_record(ctx.author.id)
        await record.make_dead(reason='meth')
        await ctx.reply(f'{item.emoji} You died')

    padlock = Item(
        type=ItemType.tool,
        key='padlock',
        name='Padlock',
        emoji='<:padlock:1379661160936374303>',
        description='Add a layer of protection to your wallet! When used, others will pay a fine when they try to rob you.',
        price=5000,
        buyable=True,
        dispose=True,
    )

    @padlock.to_use
    async def use_padlock(self, ctx: Context, item: Item) -> None:
        record = await ctx.db.get_user_record(ctx.author.id)
        if record.padlock_active:
            raise ItemUsageError('You already have a padlock active!')

        await record.update(padlock_active=True)

        await ctx.reply(f'{item.emoji} Successfully activated your padlock.')

    @padlock.to_remove
    async def remove_padlock(self, ctx: Context, item: Item) -> None:
        record = await ctx.db.get_user_record(ctx.author.id)
        if not record.padlock_active:
            raise BadArgument('You do not have a padlock active!')

        await record.update(padlock_active=False)
        await ctx.reply(f'{item.emoji} Successfully deactivated your padlock.')

    ban_hammer = Item(
        type=ItemType.miscellaneous,
        key='ban_hammer',
        name='Ban Hammer',
        emoji='<:ban_hammer:1379661030883590155>',
        description='A ban hammer, obtained from the Discord Mod job.',
        sell=1000,
        sellable=True,
        rarity=ItemRarity.rare,
    )

    camera = Item(
        type=ItemType.tool,
        key='camera',
        name='Camera',
        emoji='<:camera:1379661040337293408>',
        description='A camera, obtained from various jobs. Can be used to post videos online for profit.',
        sell=5000,
        sellable=True,
        rarity=ItemRarity.rare,
    )

    banknote = Item(
        type=ItemType.tool,
        key='banknote',
        name='Banknote',
        emoji='<:banknote:1379661032661844069>',
        description='You can sell these for coins, or use these in order to expand your bank space. Gives between 1,500 to 3,500 bank space.',
        sell=10000,
        rarity=ItemRarity.uncommon,
        dispose=True,
    )

    @banknote.to_use
    async def use_banknote(self, ctx: Context, item: Item, quantity: int) -> None:
        message = await ctx.reply(pluralize(f'{item.emoji} Using {quantity} banknote(s)...'))

        await asyncio.sleep(random.uniform(2, 4))
        record = await ctx.db.get_user_record(ctx.author.id)

        profit = sum(random.randint(1500, 3500) for _ in range(quantity))  # simulate random distribution
        additional = int(profit * record.prestige * 0.2)
        await record.add(max_bank=profit + additional)

        extra = ''
        if additional:
            extra = (
                f'\n{Emojis.Expansion.standalone} {Emojis.coin} +**{additional:,}** bank space because you are '
                f'{Emojis.get_prestige_emoji(record.prestige)} **Prestige {record.prestige}**.'
            )

        await message.edit(content=pluralize(
            f'{item.emoji} Your {quantity} banknote(s) expanded your bank space by {Emojis.coin} **{profit:,}**.{extra}'
        ))

    cheese = Item(
        type=ItemType.tool,
        key='cheese',
        name='Cheese',
        plural='Cheese',
        emoji='<:cheese:1379661048126373970>',
        description=(
            'A lucsious slice of cheese. Eating (using) these will increase your permanent EXP multiplier. '
            'There is a super small chance (2% per slice of cheese) you could die from lactose intolerance, though.'
        ),
        price=7500,
        buyable=True,
        dispose=True,
        energy=75,
    )

    CHEESE_DEATH_MESSAGE_END = 'only to find out that you are lactose intolerant, and now you\'re dead.'
    CHEESE_DEATH_MESSAGE = f'eat the cheese {CHEESE_DEATH_MESSAGE_END}'

    @staticmethod
    def _format_in_slices(item: Item, quantity: int) -> str:
        if quantity == 1:
            return f'a slice of {item.name}'

        return f'{quantity:,} slices of {item.name}'

    @cheese.to_use
    async def use_cheese(self, ctx: Context, item: Item, quantity: int) -> OverrideQuantity | None:
        if quantity > 500:
            raise ItemUsageError('You can only eat up to 500 slices of cheese at a time.')

        record = await ctx.db.get_user_record(ctx.author.id)

        readable = self._format_in_slices(item, quantity)
        original = await ctx.reply(f'{item.emoji} Eating {readable}...')
        await asyncio.sleep(random.uniform(2, 4))

        # Simulate chances
        simulator = (i for i in range(quantity) if random.random() < 0.02)
        died_on = next(simulator, None)
        if died_on is not None:
            await record.make_dead(reason='You died due to lactose intolerance from eating cheese.')
            
            if quantity <= 1:
                await original.edit(content=f'{item.emoji} You {self.CHEESE_DEATH_MESSAGE}')
                return

            if died_on == 0:
                await original.edit(content=(
                    f'{item.emoji} You eat the first slice of cheese {self.CHEESE_DEATH_MESSAGE_END}\n'
                    'The rest of your cheese was left untouched.'
                ))
                return OverrideQuantity(1)

        used_quantity = died_on + 1 if died_on is not None else quantity
        working_quantity = died_on if died_on is not None else quantity
        # simulate random distrobution
        if gain := sum(random.uniform(0.001, 0.01) for _ in range(working_quantity)):
            await record.add(exp_multiplier=gain)

        readable = self._format_in_slices(item, working_quantity)
        content = dedent(f'''
            {item.emoji} You ate {readable} and gained a **{gain:.02%}** EXP multiplier.
            You now have a **{record.base_exp_multiplier:.02%}** base EXP multiplier.
        ''')

        if died_on is not None:
            content += (
                f'\n\N{WARNING SIGN}\ufe0f On eating your **{ordinal(used_quantity)}** slice of cheese, you {self.CHEESE_DEATH_MESSAGE}'
            )

        pets = await record.pet_manager.wait()
        if mouse := pets.get_active_pet(Pets.mouse):
            multiplier = 0.05 + mouse.level * 0.005
            gain += (extra := gain * multiplier)
            content += f'\n{Pets.mouse.emoji} Your mouse gave you extra **{extra:.03%}** EXP multiplier!'

        await original.edit(content=content)
        return OverrideQuantity(used_quantity)

    cigarette = Item(
        type=ItemType.tool,
        key='cigarette',
        name='Cigarette',
        emoji='<:cigarette:1379661050734973113>',
        brief='A standard cigarette. Smoke these to temporarily get huge multipliers.',
        description=(
            'A standard cigarette. Smoking (using) these will give you a temporary +200% global EXP multiplier and +25% '
            'global coin multiplier for a short duration (5-30 minutes). However, there is a 5% chance you could die from '
            'various causes (e.g. lung cancer, lighting yourself on fire, etc.). You may only smoke one cigarette at a time. '
            'You cannot directly buy cigarettes; you must craft this item.'
        ),
        rarity=ItemRarity.rare,
        sell=15000,
        dispose=True,
    )

    CIGARETTE_DEATH_REASONS = (
        'Smoking isn\'t good for your lungs, silly. You died from lung cancer.',
        'You try lighting the cigarette, but accidentally light your clothes on fire instead \N{FIRE}\N{FIRE}, '
        'and you burn to death. What a horrible way to go.',
    )

    @cigarette.to_use
    async def use_cigarette(self, ctx: Context, item: Item) -> None:
        record = await ctx.db.get_user_record(ctx.author.id)
        if record.cigarette_expiry and record.cigarette_expiry > ctx.now:
            raise ItemUsageError('You are already smoking a cigarette.')

        original = await ctx.reply(f'{item.emoji} You light up a cigarette and start smoking it... {Emojis.loading}')
        await asyncio.sleep(random.uniform(2, 4))

        if random.random() < 0.05:
            await ctx.maybe_edit(original, reason := random.choice(self.CIGARETTE_DEATH_REASONS))
            await record.make_dead(reason=reason)
            return

        duration = random.randint(300, 1800)
        expiry = ctx.now + datetime.timedelta(seconds=duration)
        await record.update(cigarette_expiry=expiry)
        await ctx.maybe_edit(
            original,
            f'{item.emoji} You are now smoking a cigarette. You will have a temporary **+200% XP multiplier** '
            f'and a **+25% coin multiplier** for the next **{humanize_duration(duration)}** (until {format_dt(expiry, "t")}).',
        )

    @cigarette.to_remove
    async def remove_cigarette(self, ctx: Context, item: Item) -> None:
        record = await ctx.db.get_user_record(ctx.author.id)
        if not record.cigarette_expiry or record.cigarette_expiry < ctx.now:
            await ctx.reply('You are not smoking a cigarette.')

        await record.update(cigarette_expiry=None)
        await ctx.reply(f'{item.emoji} You put out your cigarette.')

    spinning_coin = Item(
        type=ItemType.collectible,
        key='spinning_coin',
        name='Spinning Coin',
        emoji='<a:spinning_coin:939937188836147240>',
        description='A coin but it spins automatically, cool isn\'t it?',
        price=500_000,
        rarity=ItemRarity.epic,
        buyable=True,
        sellable=False,
    )

    key = Item(
        type=ItemType.collectible,
        key='key',
        name='Key',
        emoji='\U0001f511',
        description='A key that has a small chance (25%) to open a padlock (when robbing). This can\'t be directrly bought; only received from commands.',
        rarity=ItemRarity.rare,
        sell=5_000,
    )

    fish_bait = Item(
        type=ItemType.tool,
        key='fish_bait',
        name='Fish Bait',
        emoji='\U0001fab1',
        description='When you fish while owning this, your chances of catching rarer fish will increase. Disposed every time you fish, no matter success or fail.',
        price=100,
        buyable=True,
    )

    stick = Item(
        type=ItemType.miscellaneous,
        key='stick',
        name='Stick',
        emoji='<:stick:1379661200857759764>',
        description='A stick. It\'s not very useful on it\'s own, but it can be used to craft other items. Although gainable from commands, you can manually craft these.',
        sell=100,
    )

    axe = Item(
        type=ItemType.tool,
        key='axe',
        name='Axe',
        emoji='<:axe:1379661025451704391>',
        description='Chop down trees using the `.chop` command to gain wood. You can sell wood, or save them for crafting!',
        price=10000,
        buyable=True,
    )

    @axe.to_use
    async def use_axe(self, ctx: Context, _) -> None:
        await ctx.invoke(ctx.bot.get_command('chop'))  # type: ignore

    dirt = Item(
        type=ItemType.dirt,
        key='dirt',
        name='Dirt',
        emoji='<:dirt:1379661084776071288>',
        description='A chunk of dirt that was dug up from the ground.',
        sell=10,
        hp=1,
    )

    clay = Item(
        type=ItemType.dirt,
        key='clay',
        name='Clay',
        emoji='<:clay:1375921854589702257>',
        description='A dense chunk of clay, dug up from the ground.',
        sell=20,
        hp=3,
    )

    gravel = Item(
        type=ItemType.dirt,
        key='gravel',
        name='Gravel',
        emoji='<:gravel:1375928383984373800>',
        description='Little chunks of rock dug up from the ground.',
        rarity=ItemRarity.uncommon,
        sell=30,
        hp=5,
    )

    limestone = Item(
        type=ItemType.dirt,
        key='limestone',
        name='Limestone',
        emoji='<:limestone:1376340511597527090>',
        description='Light-colored rock made from calcium carbonate',
        rarity=ItemRarity.uncommon,
        sell=40,
        hp=8,
    )

    granite = Item(
        type=ItemType.dirt,
        key='granite',
        name='Granite',
        emoji='<:granite:1376340486817845248>',
        description='A hard, light-colored igneous rock consisting almost entirely of quartz and feldspar.',
        sell=50,
        rarity=ItemRarity.rare,
        hp=12,
    )

    magma = Item(
        type=ItemType.dirt,
        key='magma',
        name='Magma',
        emoji='<:magma:1376344509570355200>',
        description='Found at the deepest layer of the backyard biome.',
        rarity=ItemRarity.epic,
        sell=100,
        hp=20,
    )

    worm = Worm(
        key='worm',
        name='Worm',
        emoji='<:worm:1379661232021180617>',
        description='The common worm. You can sell these or craft Fish Bait from these.',
        sell=100,
        energy=3,
        hp=3,
    )

    gummy_worm = Worm(
        key='gummy_worm',
        name='Gummy Worm',
        emoji='<:gummy_worm:1379661125817470996>',
        description='A gummy worm - at least it\'s better than a normal worm.',
        sell=250,
        energy=6,
        hp=5,
        volume=2,
    )

    earthworm = Worm(
        key='earthworm',
        name='Earthworm',
        emoji='<:earthworm:1379661094674497707>',
        description='Quite literally an "earth" worm.',
        sell=500,
        energy=12,
        hp=10,
        volume=2,
    )

    hook_worm = Worm(
        key='hook_worm',
        name='Hook Worm',
        emoji='<:hook_worm:1379661129051144314>',
        description='hookworm',
        sell=1000,
        rarity=ItemRarity.uncommon,
        energy=24,
        hp=15,
        volume=2,
    )

    poly_worm = Worm(
        key='poly_worm',
        name='Poly Worm',
        emoji='<:poly_worm:1379661170826285076>',
        description='A very colorful worm',
        sell=1500,
        rarity=ItemRarity.rare,
        energy=36,
        hp=20,
        volume=2,
    )

    ancient_relic = Item(
        type=ItemType.collectible,
        key='ancient_relic',
        name='Ancient Relic',
        emoji='<:ancient_relic:1379661018153881742>',
        description='An ancient relic originally from an unknown cave. It\'s probably somewhere in the ground, I don\'t know.',
        sell=25000,
        rarity=ItemRarity.mythic,
        hp=30,
        volume=3,
    )

    # Desert Biome

    sand = Item(
        type=ItemType.dirt,
        key='sand',
        name='Sand',
        emoji='<:sand:1379633068670714007>',
        description='A chunk of sand that was dug up from the desert.',
        sell=20,
        hp=10,
    )

    sand_clay = Item(
        type=ItemType.dirt,
        key='sand_clay',
        name='Sand Clay',
        emoji='<:sand_clay:1379633089654948010>',
        description='If there was clay in the dirt, why cant there be clay in the sand?',
        sell=40,
        hp=20,
    )

    sandstone = Item(
        type=ItemType.dirt,
        key='sandstone',
        name='Sandstone',
        emoji='<:sandstone:1379633106419712071>',
        description='A hard rock made from compressed sand.',
        rarity=ItemRarity.uncommon,
        sell=60,
        hp=30,
    )

    fossil_rock = Item(
        type=ItemType.dirt,
        key='fossil_rock',
        name='Fossil Rock',
        emoji='<:fossil_rock:1379633583865598012>',
        description='These rocks contain remains of ancient creatures.',
        rarity=ItemRarity.rare,
        sell=150,
        hp=40,
    )

    quartzite = Item(
        type=ItemType.dirt,
        key='quartzite',
        name='Quartzite',
        emoji='<:quartzite:1379634317608620152>',
        description='These chunks of quartz are rather hard to dig through...',
        rarity=ItemRarity.epic,
        sell=300,
        hp=50,
    )

    sunstone = Item(
        type=ItemType.dirt,
        key='sunstone',
        name='Sunstone',
        emoji='<:sunstone:1379634338945306734>',
        description=(
            'A super rare stone that has the bleaming glow of the sun. This is the deepest layer of the desert biome.'
        ),
        rarity=ItemRarity.legendary,
        sell=500,
        hp=75,
    )

    dust_mite = Item(
        type=ItemType.miscellaneous,
        key='dust_mte',
        name='Dust Mite',
        emoji='<:dust_mite:1384399956772786256>',
        description='A tiny, microscopic creature that lives in dust. Who wants these anyways?',
        sell=200,
        rarity=ItemRarity.common,
        energy=2,
        hp=7,
    )

    cactus_worm = Item(
        type=ItemType.worm,
        key='cactus_worm',
        name='Cactus Worm',
        emoji='<:cactus_worm:1384396033034948658>',
        description='Prickly worms that sort of look like cacti.',
        sell=400,
        rarity=ItemRarity.common,
        energy=6,
        hp=20,
    )

    cricket = Item(
        type=ItemType.miscellaneous,
        key='cricket',
        name='Cricket',
        emoji='<:cricket:1384398130275029066>',
        description='These are the insects that chirp at night. They are commonly found in the desert biome.',
        sell=600,
        rarity=ItemRarity.uncommon,
        energy=12,
        hp=30,
    )

    beetle = Item(
        type=ItemType.miscellaneous,
        key='beetle',
        name='Beetle',
        emoji='<:beetle:1384398654093135973>',
        description='Not just your average beetle. These beetles pack lots of energy for their size.',
        sell=800,
        rarity=ItemRarity.uncommon,
        energy=18,
        hp=36,
    )

    fossil = Item(
        type=ItemType.collectible,
        key='fossil',
        name='Fossil',
        emoji='<:fossil:1384398142979702794>',
        description='A fossil of probably some ancient creature. Usually found in fossil rock.',
        sell=5000,
        rarity=ItemRarity.epic,
        hp=54,
    )

    shovel: Item[ToolMetadata] = Item(
        type=ItemType.tool,
        key='shovel',
        name='Shovel',
        emoji='<:shovel:1376356818258759751>',
        description='Dig up dirt faster when digging (`.dig`). You can sell these items for profit.',
        price=10000,
        buyable=True,
        metadata=ToolMetadata(strength=2),
    )

    durable_shovel: Item[ToolMetadata] = Item(
        type=ItemType.tool,
        key='durable_shovel',
        name='Durable Shovel',
        emoji='<:durable_shovel:1376356847052914758>',
        description=(
            'A shovel reinforced with durable materials. Slightly more powerful than a standard shovel. '
            'This item cannot be directly bought; instead, it must be crafted.'
        ),
        sell=30000,
        rarity=ItemRarity.rare,
        metadata=ToolMetadata(strength=3),
    )

    golden_shovel: Item[ToolMetadata] = Item(
        type=ItemType.tool,
        key='golden_shovel',
        name='Golden Shovel',
        emoji='<:golden_shovel:1376356874244587540>',
        description=(
            'A shiny, all-powerful golden shovel. This item can only be crafted.'
        ),
        sell=100000,
        rarity=ItemRarity.epic,
        metadata=ToolMetadata(strength=5),
    )

    diamond_shovel: Item[ToolMetadata] = Item(
        type=ItemType.tool,
        key='diamond_shovel',
        name='Diamond Shovel',
        emoji='<:diamond_shovel:1376356892774760508>',
        description=(
            'Made with the finest diamond on Earth, this shovel is exceptionally durable and strong. '
            'Perfect for breaking through tough terrain with ease. This item can only be crafted.'
        ),
        sell=250000,
        rarity=ItemRarity.legendary,
        metadata=ToolMetadata(strength=7),
    )

    plasma_shovel: Item[ToolMetadata] = Item(
        type=ItemType.tool,
        key='plasma_shovel',
        name='Plasma Shovel',
        emoji='<:plasma_shovel:1376356902425989160>',
        description=(
            'A shovel energized with plasma, ionizing the ground as it digs. Who knows where these come from?'
        ),
        sell=750000,
        rarity=ItemRarity.mythic,
        metadata=ToolMetadata(strength=10),
    )

    @shovel.to_use
    @durable_shovel.to_use
    async def use_shovel(self, ctx: Context, _) -> None:
        await ctx.invoke(ctx.bot.get_command('dig'))  # type: ignore

    __shovels__: tuple[Item[ToolMetadata], ...] = (
        plasma_shovel,
        diamond_shovel,
        golden_shovel,
        durable_shovel,
        shovel,
    )

    fish = Fish(
        key='fish',
        name='Fish',
        plural='Fish',
        emoji='<:fish:1379661106079076352>',
        description='A normal fish. Commonly found in the ocean.',
        sell=75,
        energy=3,
    )

    anchovy = Fish(
        key='anchovy',
        name='Anchovy',
        plural='Anchovies',
        emoji='<:anchovy:1379661016392011796>',
        description='A small, common, yet nutrient-rich fish.',
        sell=100,
        energy=4,
    )

    sardine = Fish(
        key='sardine',
        name='Sardine',
        emoji='<:sardine:1379661184348848129>',
        description='A nutritious fish. They are small and easy to catch.',
        sell=100,
        energy=4,
    )

    catfish = Fish(
        key='catfish',
        name='Catfish',
        plural='Catfish',
        emoji='<:catfish:1379661046196863107>',
        description='Catfish are bottom-dwelling fish with whisker-like barbels.',
        sell=125,
        energy=4,
    )

    clownfish = Fish(
        key='clownfish',
        name='Clownfish',
        plural='Clownfish',
        emoji='<:clownfish:1379661054312714360>',
        description='These fish better stop clowning around!! \U0001f606 \U0001f606',
        sell=150,
        energy=5,
    )

    angel_fish = Fish(
        key='angel_fish',
        name='Angel Fish',
        plural='Angel Fish',
        emoji='<:angel_fish:1379661023333585007>',
        description='Angelfish are tropical freshwater fish that come in a variety of colors.',
        sell=200,
        energy=6,
    )

    goldfish = Fish(
        key='goldfish',
        name='Goldfish',
        plural='Goldfish',
        emoji='<:goldfish:1379661122508165171>',
        description='Goldfish are a type of carp that are commonly kept as pets.',
        sell=250,
        energy=7,
    )

    blowfish = Fish(
        key='blowfish',
        name='Blowfish',
        plural='Blowfish',
        emoji='<:blowfish:1379661038269501512>',
        description='These are also known as pufferfish. These are caught in it\'s inflated form.',
        sell=350,
        energy=9,
    )

    crab = Fish(
        key='crab',
        name='Crab',
        emoji='<:crab:1379661068418158743>',
        description='Crabs are crustaceans that are found in the ocean. Also the mascot of the Rust programming language.',
        rarity=ItemRarity.uncommon,
        sell=450,
        energy=12,
    )

    turtle = Fish(
        key='turtle',
        name='Turtle',
        emoji='<:turtle:1379661211536326766>',
        description='A sea turtle. They have a hard shell that protects them from predators.',
        rarity=ItemRarity.uncommon,
        sell=500,
        energy=13,
    )

    lobster = Fish(
        key='lobster',
        name='Lobster',
        emoji='<:lobster:1379661148139421791>',
        description='Lobsters are large crustaceans that are found in the ocean.',
        rarity=ItemRarity.uncommon,
        sell=575,
        energy=15,
    )

    squid = Fish(
        key='squid',
        name='Squid',
        emoji='<:squid:1379661198638714890>',
        description='Squidward Tentacles',
        rarity=ItemRarity.uncommon,
        sell=650,
        energy=17,
    )

    octopus = Fish(
        key='octopus',
        name='Octopus',
        plural='Octopuses',
        emoji='<:octopus:1379661157694046260>',
        description='Octopuses have 3 hearts and 9 brains. And yes, that is the correct plural form of octopus.',
        rarity=ItemRarity.uncommon,
        sell=800,
        energy=20,
    )

    seahorse = Fish(
        key='seahorse',
        name='Seahorse',
        emoji='<:seahorse:1379661187838509218>',
        description='Seahorses are known for their unique appearance. They have a horse-like head and swim vertically.',
        rarity=ItemRarity.rare,
        sell=1200,
        energy=23,
    )

    legacy_axolotl = Fish(
        key='legacy_axolotl',
        name='Legacy Axolotl',
        emoji='<:axolotl:1379661027284881450>',
        description='The old version of the axolotl when it was rarer. This is now unobtainable.',
        rarity=ItemRarity.unobtainable,
        sell=6000,
        energy=70,
    )

    axolotl = Fish(
        key='axolotl',
        name='Axolotl',
        emoji='<:axolotl:1379661027284881450>',
        description='The cool salamander',
        rarity=ItemRarity.rare,
        sell=1400,
        energy=26,
    )

    jellyfish = Fish(
        key='jellyfish',
        name='Jellyfish',
        plural='Jellyfish',
        emoji='<:jellyfish:1379661133451104348>',
        description='No eyes, no heart, no brain. Yet they still manage to defeat you',
        rarity=ItemRarity.rare,
        sell=1500,
        energy=28,
    )

    dolphin = Fish(
        key='dolphin',
        name='Dolphin',
        emoji='<:dolphin:1379661086671765647>',
        description='Dolphins are large aquatic mammals that are found in the ocean.',
        rarity=ItemRarity.rare,
        sell=1700,
        energy=30,
        metadata=EnemyRef('dolphin', damage=(2, 4)),
    )

    swordfish = Fish(
        key='swordfish',
        name='Swordfish',
        plural='Swordfish',
        emoji='<:swordfish:1379661204049367041>',
        description='Swordfish are large predatory fish with a long, flat bill shaped like a sword.',
        rarity=ItemRarity.rare,
        sell=1800,
        energy=35,
    )

    siamese_fighting_fish = Fish(
        key='siamese_fighting_fish',
        name='Siamese Fighting Fish',
        plural='Siamese Fighting Fish',
        emoji='<:siamese_fighting_fish:1379661192707969034>',
        description='Also known as betta fish, these are among the most popular freshwater aquarium fish.',
        rarity=ItemRarity.rare,
        sell=1900,
        energy=40,
    )

    shark = Fish(
        key='shark',
        name='Shark',
        emoji='<:shark:1379661190506221659>',
        description='Sharks are large predatory fish that are found in the ocean.',
        rarity=ItemRarity.rare,
        sell=2000,
        energy=45,
        metadata=EnemyRef('shark', damage=(4, 6)),
    )

    rainbow_trout = Fish(
        key='rainbow_trout',
        name='Rainbow Trout',
        plural='Rainbow Trout',
        emoji='<:rainbow_trout:1379661176065228993>',
        description='Colorful freshwater fish known for their virabnt hues.',
        rarity=ItemRarity.epic,
        sell=2300,
        energy=50,
    )

    whale = Fish(
        key='whale',
        name='Whale',
        emoji='<:whale:1379661224379416676>',
        description='Whales are huge mammals that swim deep in the ocean. How do you even manage to catch these?',
        rarity=ItemRarity.epic,
        sell=2500,
        energy=60,
        metadata=EnemyRef('whale', damage=(5, 7)),
    )

    vibe_fish = Fish(
        key='vibe_fish',
        name='Vibe Fish',
        plural='Vibe Fish',
        emoji='<a:vibe_fish:935293751604183060>',
        description='\uff56\uff49\uff42\uff45',  # "vibe" in full-width text
        rarity=ItemRarity.legendary,
        sell=7500,
        metadata=EnemyRef('vibe_fish', damage=(7, 9)),
    )

    eel = Fish(
        key='eel',
        name='Eel',
        emoji='<:eel:1379661098394849310>',
        description='A long fish that is commonly found in the ocean. These are not obtainable from fishing.',
        rarity=ItemRarity.mythic,
        sell=35000,
        energy=200,
    )

    fishing_pole = Item(
        type=ItemType.tool,
        key='fishing_pole',
        name='Fishing Pole',
        emoji='<:fishing_pole:1379661107886555198>',
        description=(
            'Owning this will grant you access to more fish and better luck in the `fish` command - '
            'fish for fish and sell them for profit!'
        ),
        price=12000,
        buyable=True,
        metadata=FishingPoleMetadata(weights=fish_weights(locals()), iterations=5),
        durability=5,
        repair_rate=1000,
        repair_time=datetime.timedelta(minutes=2),
    )

    durable_fishing_pole = Item(
        type=ItemType.tool,
        key='durable_fishing_pole',
        name='Durable Fishing Pole',
        emoji='<:durable_fishing_pole:1379661090215956490>',
        rarity=ItemRarity.rare,
        description='A fishing pole that is more durable than the regular fishing pole.',
        price=30000,
        metadata=FishingPoleMetadata(weights=fish_weights(locals(), none=0.9, rare=1.1), iterations=6),
        durability=15,
        repair_rate=1750,
        repair_time=datetime.timedelta(minutes=5),
    )

    golden_fishing_pole = Item(
        type=ItemType.tool,
        key='golden_fishing_pole',
        name='Golden Fishing Pole',
        emoji='<:golden_fishing_pole:1379661118653337750>',
        rarity=ItemRarity.legendary,
        description='It\'s golden...',
        price=100000,
        metadata=FishingPoleMetadata(weights=fish_weights(locals(), none=0.85, rare=1.15, epic=1.3), iterations=7),
        durability=30,
        repair_rate=3000,
        repair_time=datetime.timedelta(minutes=10),
    )

    diamond_fishing_pole = Item(
        type=ItemType.tool,
        key='diamond_fishing_pole',
        name='Diamond Fishing Pole',
        emoji='<:diamond_fishing_pole:1379661079252308058>',
        rarity=ItemRarity.mythic,
        description='A fishing pole made out of pure diamond.',
        price=1000000,
        metadata=FishingPoleMetadata(
            weights=fish_weights(locals(), none=0.8, rare=1.2, epic=1.4, legendary=1.6),
            iterations=8,
        ),
        durability=50,
        repair_rate=15000,
        repair_time=datetime.timedelta(minutes=30),
    )

    __fishing_poles__: tuple[Item[FishingPoleMetadata], ...] = (
        diamond_fishing_pole,
        golden_fishing_pole,
        durable_fishing_pole,
        fishing_pole,
    )

    wood = Wood(
        key='wood',
        name='Wood',
        plural='Wood',
        emoji='<:wood:1379661229693337651>',
        description='The most abundant type of wood.',
        sell=30,
    )

    redwood = Wood(
        key='redwood',
        name='Redwood',
        plural='Redwood',
        emoji='<:redwood:1379661182667063437>',
        description='Only found from Redwood trees whose lifespan is one of the longest.',
        sell=100,
    )

    blackwood = Wood(
        key='blackwood',
        name='Blackwood',
        plural='Blackwood',
        emoji='<:blackwood:1379661034867916820>',
        description='A rare type of wood',
        rarity=ItemRarity.uncommon,
        sell=1000,
    )

    iron = Ore(
        key='iron',
        name='Iron',
        plural='Iron',
        emoji='<:iron:1379661131315937300>',
        description='A common metal mined from the ground.',
        sell=60,
        hp=2,
    )

    copper = Ore(
        key='copper',
        name='Copper',
        plural='Copper',
        emoji='<:copper:1379661056250740898>',
        description='A soft metal with high thermal and electrial conductivity.',
        sell=200,
        hp=4,
    )

    silver = Ore(
        key='silver',
        name='Silver',
        plural='Silver',
        emoji='<:silver:1379661195879120907>',
        description='A shiny, lustrous metal with the highest thermal and electrical conductivity of any metal.',
        rarity=ItemRarity.uncommon,
        sell=400,
        hp=6,
    )

    gold = Ore(
        key='gold',
        name='Gold',
        plural='Gold',
        emoji='<:gold:1379661116476493918>',
        description='A bright, dense, and popular metal.',
        rarity=ItemRarity.rare,
        sell=900,
        hp=8,
    )

    obsidian = Ore(
        key='obsidian',
        name='Obsidian',
        plural='Obsidian',
        emoji='<:obsidian:1379661155320205312>',
        description='A volcanic, glassy mineral formed from the rapid cooling of felsic lava.',
        rarity=ItemRarity.rare,
        sell=1250,
        hp=10,
        volume=2,
    )

    emerald = Ore(
        key='emerald',
        name='Emerald',
        plural='Emerald',
        emoji='<:emerald:1379661100370628662>',
        description='A valuable green gemstone.',
        rarity=ItemRarity.epic,
        sell=2000,
        hp=12,
        volume=2,
    )

    ruby = Ore(
        key='ruby',
        name='Ruby',
        plural='Ruby',
        emoji='<:ruby:1384399664987635752>',
        description='They say these are just red diamonds...',
        rarity=ItemRarity.epic,
        sell=3000,
        hp=15,
        volume=2,
    )

    diamond = Ore(
        key='diamond',
        name='Diamond',
        plural='Diamond',
        emoji='<:diamond:1379661076890648598>',
        description='A super-hard mineral known for being extremely expensive.',
        rarity=ItemRarity.legendary,
        sell=5000,
        hp=20,
        volume=3,
    )

    pickaxe: Item[ToolMetadata] = Item(
        type=ItemType.tool,
        key='pickaxe',
        name='Pickaxe',
        emoji='<:pickaxe:1379661165449183442>',
        description='Allows you to mine ores while digging (`.dig`). You can sell these ores for profit, and use some in crafting.',
        price=10000,
        buyable=True,
        metadata=ToolMetadata(strength=1),
    )

    durable_pickaxe = Item(
        type=ItemType.tool,
        key='durable_pickaxe',
        name='Durable Pickaxe',
        emoji='<:durable_pickaxe:1379661092715761697>',
        description='A durable, re-enforced pickaxe. Able to find rare ores more commonly than a normal pickaxe. This item must be crafted.',
        rarity=ItemRarity.rare,
        sell=30000,
        metadata=ToolMetadata(strength=3),
    )

    diamond_pickaxe = Item(
        type=ItemType.tool,
        key='diamond_pickaxe',
        name='Diamond Pickaxe',
        emoji='<:diamond_pickaxe:1379661081273700402>',
        description='A pickaxe made of pure diamond. This pickaxe is better than both the normal and durable pickaxes. This item must be crafted.',
        rarity=ItemRarity.legendary,
        sell=200000,
        metadata=ToolMetadata(strength=5),
    )

    @pickaxe.to_use
    @durable_pickaxe.to_use
    @diamond_pickaxe.to_use
    async def use_pickaxe(self, ctx: Context, _) -> None:
        await ctx.invoke(ctx.bot.get_command('mine'))  # type: ignore

    __pickaxes__: tuple[Item[ToolMetadata]] = (
        diamond_pickaxe,
        durable_pickaxe,
        pickaxe,
    )

    railgun = Item(
        type=ItemType.power_up,
        key='railgun',
        name='Railgun',
        emoji='<:railgun:1376071981719359518>',
        description=(
            'Obliterate the ground when digging! Every hour, you can use this to instantly deal 30 HP to the dirt '
            'below you while digging, cascading to deeper layers until all 30 HP is used.'
        ),
        rarity=ItemRarity.rare,
        price=50000,
        buyable=True,
    )

    dynamite = Item(
        type=ItemType.power_up,
        key='dynamite',
        name='Dynamite',
        emoji='<:dynamite:1377482796255154246>',
        description='Deal a great amount of HP to surrounding dirt when digging! Consumed upon use.',
        rarity=ItemRarity.uncommon,
        sell=5000,
        buyable=False,
    )

    common_crate = Crate(
        key='common_crate',
        name='Common Crate',
        emoji='<:crate:1379661070792261682>',
        description='The most common type of crate.',
        price=200,
        metadata=CrateMetadata(
            minimum=200,
            maximum=600,
            items={
                dynamite: (0.03, 1, 1),
                banknote: (0.05, 1, 1),
                padlock: (0.5, 1, 1),
            },
        ),
    )

    uncommon_crate = Crate(
        key='uncommon_crate',
        name='Uncommon Crate',
        emoji='<:uncommon_crate:1379661213612380170>',
        description='A slightly more common type of crate.',
        price=500,
        metadata=CrateMetadata(
            minimum=500,
            maximum=1500,
            items={
                dynamite: (0.1, 1, 1),
                banknote: (0.15, 1, 1),
                cheese: (0.5, 1, 2),
                lifesaver: (0.5, 1, 1),
                padlock: (0.75, 1, 2),
            },
        ),
        rarity=ItemRarity.uncommon,
    )

    rare_crate = Crate(
        key='rare_crate',
        name='Rare Crate',
        emoji='<:rare_crate:1379661180410269706>',
        description='A pretty rare crate.',
        price=2000,
        metadata=CrateMetadata(
            minimum=1500,
            maximum=3500,
            items={
                dynamite: (0.1, 1, 1),
                fishing_pole: (0.1, 1, 1),
                alcohol: (0.1, 1, 1),
                banknote: (0.15, 1, 2),
                cheese: (0.4, 1, 2),
                lifesaver: (0.5, 1, 2),
                padlock: (0.75, 1, 2),
            },
        ),
        rarity=ItemRarity.rare,
    )

    voting_crate = Crate(
        key='voting_crate',
        name='Voting Crate',
        emoji='<:voting_crate:1379661219811688600>',
        description='A crate that can be obtained by [voting for the bot](https://top.gg/bot/753017377922482248).',
        price=4000,
        metadata=CrateMetadata(
            minimum=3000,
            maximum=6000,
            items={
                dynamite: (0.1, 1, 1),
                fishing_pole: (0.1, 1, 1),
                alcohol: (0.1, 1, 1),
                banknote: (0.15, 1, 2),
                cheese: (0.4, 1, 2),
                lifesaver: (0.5, 1, 2),
                padlock: (0.75, 1, 2),
            },
        ),
        rarity=ItemRarity.uncommon,
    )

    epic_crate = Crate(
        key='epic_crate',
        name='Epic Crate',
        emoji='<:epic_crate:1379661103000195212>',
        description='A pretty epic crate.',
        price=6000,
        metadata=CrateMetadata(
            minimum=5000,
            maximum=12500,
            items={
                fishing_pole: (0.1, 1, 1),
                pickaxe: (0.1, 1, 1),
                shovel: (0.1, 1, 1),
                alcohol: (0.1, 1, 1),
                dynamite: (0.2, 1, 2),
                banknote: (0.2, 1, 3),
                fish_bait: (0.3, 5, 15),
                cheese: (0.4, 1, 3),
                lifesaver: (0.5, 1, 3),
                padlock: (0.75, 2, 3),
            },
        ),
        rarity=ItemRarity.epic,
    )

    legendary_crate = Crate(
        key='legendary_crate',
        name='Legendary Crate',
        emoji='<:legendary_crate:1379661136470741114>',
        description='A pretty legendary crate.',
        price=25000,
        metadata=CrateMetadata(
            minimum=20000,
            maximum=50000,
            items={
                uncommon_crate: (0.01, 1, 1),
                common_crate: (0.01, 1, 1),
                fishing_pole: (0.1, 1, 1),
                pickaxe: (0.1, 1, 1),
                shovel: (0.1, 1, 1),
                axe: (0.1, 1, 1),
                alcohol: (0.1, 1, 1),
                banknote: (0.2, 1, 5),
                dynamite: (0.25, 1, 3),
                fish_bait: (0.3, 20, 50),
                cheese: (0.4, 2, 5),
                lifesaver: (0.5, 2, 4),
                padlock: (0.75, 2, 5),
            },
        ),
        rarity=ItemRarity.legendary,
    )

    mythic_crate = Crate(
        key='mythic_crate',
        name='Mythic Crate',
        emoji='<:mythic_crate:1379661152333725807>',
        description='A pretty mythic crate.',
        price=60000,
        metadata=CrateMetadata(
            minimum=50000,
            maximum=150000,
            items={
                epic_crate: (0.002, 1, 1),
                rare_crate: (0.005, 1, 1),
                uncommon_crate: (0.01, 1, 1),
                common_crate: (0.01, 1, 2),
                fishing_pole: (0.1, 1, 2),
                pickaxe: (0.1, 1, 2),
                shovel: (0.1, 1, 2),
                axe: (0.1, 1, 2),
                alcohol: (0.1, 1, 2),
                banknote: (0.2, 2, 7),
                dynamite: (0.3, 2, 8),
                fish_bait: (0.3, 50, 100),
                cheese: (0.4, 2, 7),
                lifesaver: (0.5, 2, 6),
                padlock: (0.75, 3, 8),
            },
        ),
        rarity=ItemRarity.mythic,
    )

    @common_crate.to_use
    @uncommon_crate.to_use
    @rare_crate.to_use
    @voting_crate.to_use
    @epic_crate.to_use
    @legendary_crate.to_use
    @mythic_crate.to_use
    async def use_crate(self, ctx: Context, crate: Item[CrateMetadata], quantity: int) -> None:
        if quantity == 1:
            formatted = f'{crate.singular} {crate.name}'
        else:
            formatted = f'{quantity:,} {crate.plural}'

        original = await ctx.send(f'{crate.emoji} Opening {formatted}...', reference=ctx.message)

        metadata = crate.metadata
        profit = random.randint(metadata.minimum * quantity, metadata.maximum * quantity)

        async with ctx.db.acquire() as conn:
            record = await ctx.db.get_user_record(ctx.author.id)
            await record.add(wallet=profit, connection=conn)

            items = defaultdict(int)

            for _ in range(quantity):
                for item, (chance, lower, upper) in metadata.items.items():
                    if random.random() >= chance:
                        continue

                    amount = random.randint(lower, upper)

                    items[item] += amount
                    await record.inventory_manager.add_item(item, amount, connection=conn)
                    break

        await asyncio.sleep(random.uniform(1.5, 3.5))

        readable = f'{Emojis.coin} {profit:,}\n' + '\n'.join(
            f'{item.emoji} {item.name} x{quantity:,}' for item, quantity in items.items()
        )
        await original.edit(content=f'You opened {formatted} and received:\n{readable}')

    net = Net(
        key='net',
        name='Net',
        emoji='<:net:1376754285806882816>',
        description='A net used to catch better pets using the `.hunt` command.',
        price=10000,
        buyable=True,
        metadata=NetMetadata(
            weights=generate_pet_weights(
                none=12,
                common=67,
                uncommon=12,
                rare=6,
                epic=2,
                legendary=0.8,
                mythic=0.2,
            ),
            priority=0,
        ),
    )

    golden_net = Net(
        key='golden_net',
        name='Golden Net',
        emoji='<:golden_net:1376754300440678481>',
        description=(
            'A net made of pure gold. It has a higher chance of catching rarer pets. '
            'This item must be crafted.'
        ),
        rarity=ItemRarity.rare,
        price=30000,
        metadata=NetMetadata(
            weights=generate_pet_weights(
                none=10,
                common=62,
                uncommon=17,
                rare=7,
                epic=2.5,
                legendary=1.1,
                mythic=0.4,
            ),
            priority=1,
        ),
    )

    diamond_net = Net(
        key='diamond_net',
        name='Diamond Net',
        emoji='<:diamond_net:1384385760903303289>',
        description=(
            'Crystal clear net made out of diamonds! This net increases the chance of catching rarer pets. '
            'This item must be crafted.'
        ),
        rarity=ItemRarity.legendary,
        price=250000,
        metadata=NetMetadata(
            weights=generate_pet_weights(
                none=8,
                common=60,
                uncommon=15,
                rare=10,
                epic=4,
                legendary=2,
                mythic=1,
            ),
            priority=2,
        ),
    )

    cup = Item(
        type=ItemType.tool,
        key='cup',
        name='Cup',
        emoji='<:cup:1379661072830693518>',
        description='A cup that can hold liquid. Relatively cheap.',
        price=50,
        buyable=True
    )

    watering_can = Item(
        type=ItemType.tool,
        key='watering_can',
        name='Watering Can',
        emoji='<:watering_can:1379661222080675891>',
        description='Use these to water your plants [crops], boosting their EXP.',
        price=1000,
        buyable=True,
    )

    glass_of_water = Item(
        type=ItemType.tool,
        key='glass_of_water',
        name='Glass of Water',
        plural='Glasses of Water',
        emoji='<:glass_of_water:1379661114262159506>',
        description='Usually used for crafting, but can also be a refresher.',
        sell=1000,
    )

    tomato = Harvest(
        key='tomato',
        name='Tomato',
        plural='Tomatoes',
        emoji='<:tomato:1379661208508039188>',
        description='A regular tomato, grown from the tomato crop.',
        sell=50,
        energy=3,
    )

    tomato_crop = Crop(
        key='tomato_crop',
        name='Tomato Crop',
        emoji='<:tomato:1379661208508039188>',
        price=1200,
        metadata=CropMetadata(
            time=600,
            count=(1, 3),
            item=tomato,
        ),
    )

    wheat = Harvest(
        key='wheat',
        name='Wheat',
        plural='Wheat',
        emoji='<:wheat:1379661227675877426>',
        description='An ear of wheat, grown from the wheat crop.',
        sell=40,
        energy=3,
    )

    wheat_crop = Crop(
        key='wheat_crop',
        name='Wheat Crop',
        emoji='<:wheat:1379661227675877426>',
        price=1250,
        metadata=CropMetadata(
            time=600,
            count=(1, 2),
            item=wheat,
        ),
    )

    carrot = Harvest(
        key='carrot',
        name='Carrot',
        emoji='<:carrot:1379661042673647746>',
        description='A carrot, grown from the carrot crop.',
        sell=75,
        energy=3,
    )

    carrot_crop = Crop(
        key='carrot_crop',
        name='Carrot Crop',
        emoji='<:carrot:1379661042673647746>',
        price=2000,
        metadata=CropMetadata(
            time=800,
            count=(1, 2),
            item=carrot,
        ),
    )

    corn = Harvest(
        key='corn',
        name='Corn',
        plural='Corn',
        emoji='<:corn:1379661060575072340>',
        description='An ear of corn, grown from the corn crop.',
        sell=75,
        energy=3,
    )

    corn_crop = Crop(
        key='corn_crop',
        name='Corn Crop',
        emoji='<:corn:1379661060575072340>',
        price=2200,
        metadata=CropMetadata(
            time=800,
            count=(1, 1),
            item=corn,
        ),
    )

    lettuce = Harvest(
        key='lettuce',
        name='Lettuce',
        plural='Lettuce',
        emoji='<:lettuce:1379661140430160083>',
        description='A head of lettuce, grown from the lettuce crop.',
        sell=80,
        energy=3,
    )

    lettuce_crop = Crop(
        key='lettuce_crop',
        name='Lettuce Crop',
        emoji='<:lettuce:1379661140430160083>',
        price=2400,
        metadata=CropMetadata(
            time=1200,
            count=(1, 2),
            item=lettuce,
        ),
    )

    potato = Harvest(
        key='potato',
        name='Potato',
        plural='Potatoes',
        emoji='<:potato:1379661174177796216>',
        description='A potato, grown from the potato crop.',
        sell=110,
        energy=3,
    )

    potato_crop = Crop(
        key='potato_crop',
        name='Potato Crop',
        emoji='<:potato:1379661174177796216>',
        price=2800,
        metadata=CropMetadata(
            time=1500,
            count=(1, 2),
            item=potato,
        ),
    )

    tobacco = Harvest(
        key='tobacco',
        name='Tobacco',
        plural='Tobacco',
        emoji='<:tobacco:1379661206205366364>',
        description='A piece of tobacco, grown from the tobacco crop.',
        sell=125,
    )

    tobacco_crop = Crop(
        key='tobacco_crop',
        name='Tobacco Crop',
        emoji='<:tobacco:1379661206205366364>',
        price=3600,
        metadata=CropMetadata(
            time=1500,
            count=(1, 2),
            item=tobacco,
        ),
    )

    cotton_ball = Harvest(
        key='cotton_ball',
        name='Cotton Ball',
        emoji='<:cottonball:1379661064706461808>',
        description='A ball of cotton, grown from the cotton crop.',
        sell=150,
        metadata=HarvestMetadata(lambda: Items.cotton_crop),
    )

    cotton_crop = Crop(
        key='cotton_crop',
        name='Cotton Crop',
        emoji='<:cotton:1379661062906843177>',
        price=4500,
        metadata=CropMetadata(
            time=1800,
            count=(1, 2),
            item=cotton_ball,
        ),
    )

    flour = Item(
        type=ItemType.miscellaneous,
        key='flour',
        name='Flour',
        plural='Flour',
        emoji='<:flour:1379661110239559741>',
        description='A bag of flour, used to make [craft] bakery products.',
        sell=100,
    )

    bread = Item(
        type=ItemType.miscellaneous,
        key='loaf_of_bread',
        name='Loaf of Bread',
        plural='Loaves of Bread',
        emoji='<:loaf_of_bread:1379661146373488700>',
        description='A normal loaf of wheat bread.',
        sell=500,
        rarity=ItemRarity.uncommon,
        energy=15,
    )

    jar_of_honey = Item(
        type=ItemType.miscellaneous,
        key='jar_of_honey',
        name='Jar of Honey',
        plural='Jars of Honey',
        emoji='\U0001f36f',
        description='A jar of honey. Obtainable from bees.',
        sell=700,
        rarity=ItemRarity.uncommon,
        energy=20,
    )

    milk = Item(
        type=ItemType.miscellaneous,
        key='milk',
        name='Milk',
        plural='Milk',
        emoji='\U0001f95b',
        description='A glass of milk. Obtainable from cows.',
        sell=800,
        rarity=ItemRarity.uncommon,
        energy=22,
    )

    berries = Item(
        type=ItemType.miscellaneous,
        key='bunch_of_berries',
        name='Bunch of Berries',
        plural='Bunches of Berries',
        emoji='<:berries:1375294760276856932>',
        description='A handful of berries. Obtainable from foxes. Good for feeding pets.',
        sell=1200,
        rarity=ItemRarity.uncommon,
        energy=36,
    )

    sheet_of_paper = Item(
        type=ItemType.miscellaneous,
        key='sheet_of_paper',
        name='Sheet of Paper',
        plural='Sheets of Paper',
        emoji='<:paper:1379661163234590783>',
        description='A sheet of paper.',
        sell=10000,
        rarity=ItemRarity.rare,
    )

    voting_trophy = Item(
        type=ItemType.collectible,
        key='voting_trophy',
        name='Voting Trophy',
        emoji='<:voting_trophy:1379661215688687667>',
        description='Obtained by accumulating 50 [votes](http://top.gg/bot/753017377922482248) within a calendar month. (/vote)',
        price=200000,
        rarity=ItemRarity.legendary,
    )

    nineteen_dollar_fortnite_card = Item(
        type=ItemType.collectible,
        key='nineteen_dollar_fortnite_card',
        name='19 Dollar Fortnite Card',
        emoji='<a:19dollar:1133500138959163442>',
        description=(
            'Okay, 19 dollar Fortnite card, who wants it? And yes, I\'m giving it away. Remember; share, share share. '
            'And trolls, don\'t get blocked!'
        ),
        sell=50000,
        rarity=ItemRarity.mythic,
    )

    coinhead = Item(
        type=ItemType.collectible,
        key='coinhead',
        name='Coinhead',
        emoji='<a:coinhead:1138846404870152303>',
        description='this goofy coinhead',
        sell=5_000_000,
        rarity=ItemRarity.mythic,
    )

    @classmethod
    def all(cls) -> Generator[Item, Any, Any]:
        """Lazily iterates through all items."""
        for attr in dir(cls):
            if isinstance(item := getattr(cls, attr), Item):
                yield item


ITEMS_INST = Items()


class Reward(NamedTuple):
    """Reward for completing a milestone"""
    coins: int = 0
    items: dict[Item, int] = {}

    @classmethod
    def from_raw(cls, coins: int, items_by_key: dict[str, int]) -> Reward:
        return cls(
            coins=coins,
            items={get_by_key(Items, key): quantity for key, quantity in items_by_key.items()},
        )

    @property
    def chunks(self) -> list[str]:
        base = []
        if self.coins > 0:
            base.append(f'{Emojis.coin} **{self.coins:,}**')
        for item, quantity in self.items.items():
            base.append(item.get_sentence_chunk(quantity))
        return base

    @property
    def short(self) -> str:
        return humanize_list(self.chunks) or 'N/A'

    @property
    def principal_item(self) -> Item | None:
        if self.items:
            return max(self.items, key=lambda item: item.price)

    @property
    def principal_emoji(self) -> str:
        if self.items:
            return self.principal_item.emoji
        if self.coins > 0:
            return Emojis.coin
        return Emojis.space

    def __str__(self) -> str:
        return '\n'.join(f'- {chunk}' for chunk in self.chunks)

    def __add__(self, other: Reward) -> Reward:
        return Reward(
            coins=self.coins + other.coins,
            items={k: self.items.get(k, 0) + other.items.get(k, 0) for k in {*self.items, *other.items}},
        )

    def __bool__(self) -> bool:
        return self.coins > 0 or bool(self.items)

    @property
    def items_by_key(self) -> dict[str, int]:
        return {item.key: quantity for item, quantity in self.items.items()}

    async def apply(self, record: UserRecord, *, connection: asyncpg.Connection | None = None) -> None:
        """Applies the reward to the user."""
        if self.coins:
            await record.add(wallet=self.coins, connection=connection)
        if self.items:
            await record.inventory_manager.add_bulk(**self.items_by_key, connection=connection)

    def to_notification_data_kwargs(self) -> dict[str, Any]:
        return {
            'rcoins': self.coins,
            'ritems': self.items_by_key,
        }


VOTE_REWARDS: Final[dict[int, Reward]] = {
    5: Reward(coins=5000),
    10: Reward(items={Items.cigarette: 1, Items.banknote: 1, Items.dynamite: 1}),
    15: Reward(coins=10000, items={Items.alcohol: 1}),
    20: Reward(items={Items.key: 1, Items.cheese: 1, Items.banknote: 1}),
    25: Reward(coins=15000, items={Items.fish_bait: 100, Items.dynamite: 5, Items.banknote: 1}),
    30: Reward(items={Items.durable_pickaxe: 1, Items.dynamite: 5}),
    35: Reward(coins=20000, items={Items.banknote: 2}),
    40: Reward(items={Items.durable_shovel: 1, Items.dynamite: 5}),
    45: Reward(coins=25000, items={Items.banknote: 2}),
    50: Reward(items={Items.voting_trophy: 1}),
    55: Reward(coins=30000, items={Items.banknote: 3}),
    60: Reward(items={Items.spinning_coin: 1}),
    65: Reward(coins=35000, items={Items.banknote: 3}),
    70: Reward(items={Items.legendary_crate: 1}),
    75: Reward(coins=50000, items={Items.banknote: 5}),
    80: Reward(items={Items.mythic_crate: 1}),
}

LEVEL_REWARDS: Final[dict[int, Reward]] = {
    1: Reward(items={Items.fishing_pole: 1, Items.shovel: 1, Items.pickaxe: 1, Items.dynamite: 10}),
    2: Reward(items={Items.banknote: 2}),
    3: Reward(items={Items.lifesaver: 5}),
    4: Reward(items={Items.dynamite: 10}),
    5: Reward(coins=10000, items={Items.uncommon_crate: 1, Items.padlock: 3, Items.key: 1}),
    7: Reward(items={Items.shovel: 1, Items.cheese: 1}),
    10: Reward(coins=10000, items={Items.epic_crate: 1, Items.banknote: 3, Items.cigarette: 1}),
    15: Reward(items={Items.axe: 1, Items.net: 1, Items.banknote: 2}),
    20: Reward(items={Items.legendary_crate: 1}),
    25: Reward(coins=20000, items={Items.alcohol: 2, Items.sheet_of_paper: 2}),
    30: Reward(coins=50000, items={Items.banknote: 3, Items.lifesaver: 5}),
    35: Reward(items={Items.cheese: 5, Items.cigarette: 5}),
    40: Reward(coins=50000, items={Items.legendary_crate: 1, Items.banknote: 5}),
    45: Reward(items={
        Items.durable_fishing_pole: 1, Items.durable_pickaxe: 1,
        Items.durable_shovel: 1, Items.dynamite: 10,
    }),
    50: Reward(items={Items.spinning_coin: 1}),
    55: Reward(coins=100000, items={Items.camera: 1, Items.alcohol: 1}),
    60: Reward(coins=100000, items={Items.epic_crate: 1, Items.key: 2}),
}
