from __future__ import annotations

import json
import math
import os
import re
import sys
from asyncio import subprocess
from collections import defaultdict, deque
from datetime import datetime, time
from io import BytesIO
from typing import Any, ClassVar, Final, TYPE_CHECKING

import discord
import discord.gateway as gateway
import jishaku
from aiohttp import ClientSession
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.ipc import Server

from app.core.cdn import CDNClient
from app.core.help import HelpCommand
from app.core.i18n import Translator
from app.core.models import Command, Context, GroupCommand
from app.core.timers import TimerManager
from app.database import Database
from app.util.structures import LockWithReason
from config import (
    DatabaseConfig, allowed_mentions, backups_channel, beta, default_prefix,
    description, disable_app_commands, ipc_secret, name, owner, token, version,
)

if TYPE_CHECKING:
    from typing import TypeAlias

    from discord.abc import Snowflake

    AppCommandStore: TypeAlias = dict[str, app_commands.AppCommand]

jishaku.Flags.HIDE = True
jishaku.Flags.NO_UNDERSCORE = True
jishaku.Flags.NO_DM_TRACEBACK = True

ANSI_REGEX: re.Pattern[str] = re.compile(r"\x1b\[\d{2};[01]m")
EVERY_TWO_HOURS: list[time] = [time(hour) for hour in range(0, 24, 2)]


# TODO remove this patch
def _accent_colour_setter(self: discord.ui.Container, value: int | discord.Colour | None) -> None:
    self._colour = value


discord.ui.Container.accent_colour = property(lambda self: self._colour, _accent_colour_setter)


class TrackingKeepAliveHandler(gateway.KeepAliveHandler):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.latencies = deque[float]()  # track past 5 latencies

    def ack(self) -> None:
        super().ack()
        if math.isinf(self.latency):  # if the latency is infinite, don't add it to the list
            return

        self.latencies.append(self.latency)
        if len(self.latencies) > 5:
            self.latencies.popleft()

    @property
    def average_latency(self) -> float:
        """Returns the average latency of the past 5 heartbeats.

        This is not a simple mean, rather the worst and best latencies out of past 5 are removed,
        then the mean is taken from the remaining 3.
        """
        # No latencies? Return inf to indicate that we don't have any data
        if not self.latencies:
            return float('inf')

        # If there are three latencies or fewer, removing the worst and best latencies do more harm to the accuracy
        # than good, so just return the mean of all the latencies
        if len(self.latencies) <= 3:
            return sum(self.latencies) / len(self.latencies)

        # Remove the worst and best latencies. Since the worst case of sorted is O(n^2) and we're removing 2
        # predictable elements, we can do this in O(n) time instead:
        worst = float('-inf')
        best = float('inf')
        wpos = bpos = 0
        for i, latency in enumerate(self.latencies):
            if latency > worst:
                worst = latency
                wpos = i
            if latency < best:
                best = latency
                bpos = i

        # Remove the worst and best latencies
        values = [latency for i, latency in enumerate(self.latencies) if i not in (wpos, bpos)]
        return sum(values) / len(values)


# we do a lil bit of monkey patching
gateway.KeepAliveHandler = TrackingKeepAliveHandler


# taken from <https://gist.github.com/Soheab/fed903c25b1aae1f11a8ca8c33243131#file-command_tree-py>
# noinspection PyShadowingNames
class Tree(app_commands.CommandTree):
    def __init__(self, bot: Bot, **kwargs):
        super().__init__(bot, **kwargs)
        self._global_app_commands: AppCommandStore = {}
        # guild_id: AppCommandStore
        self._guild_app_commands: dict[int, AppCommandStore] = {}

    def find_app_command_by_names(
        self,
        *qualified_name: str,
        guild: Snowflake | int | None = None,
    ) -> app_commands.AppCommand | None:
        commands = self._global_app_commands
        if guild:
            guild_id = guild.id if not isinstance(guild, int) else guild
            guild_commands = self._guild_app_commands.get(guild_id, {})
            if not guild_commands and self.fallback_to_global:
                commands = self._global_app_commands
            else:
                commands = guild_commands

        for cmd_name, cmd in commands.items():
            if any(name in qualified_name for name in cmd_name.split()):
                return cmd

        return None

    def get_app_command(
        self,
        value: str | int,
        guild: Snowflake | int | None = None,
    ) -> app_commands.AppCommand | None:
        def search_dict(d: AppCommandStore) -> app_commands.AppCommand | None:
            for cmd_name, cmd in d.items():
                if value == cmd_name or (str(value).isdigit() and int(value) == cmd.id):
                    return cmd
            return None

        if guild:
            guild_id = guild.id if not isinstance(guild, int) else guild
            guild_commands = self._guild_app_commands.get(guild_id, {})
            if not self.fallback_to_global:
                return search_dict(guild_commands)
            else:
                return search_dict(guild_commands) or search_dict(self._global_app_commands)
        else:
            return search_dict(self._global_app_commands)

    @staticmethod
    def _unpack_app_commands(commands: list[app_commands.AppCommand]) -> AppCommandStore:
        ret: AppCommandStore = {}

        def unpack_options(
            options: list[app_commands.AppCommand | app_commands.AppCommandGroup | app_commands.Argument],
        ):
            for option in options:
                if isinstance(option, app_commands.AppCommandGroup):
                    ret[option.qualified_name] = option  # type: ignore
                    unpack_options(option.options)  # type: ignore

        for command in commands:
            ret[command.name] = command
            unpack_options(command.options)  # type: ignore

        return ret

    async def _update_cache(
        self,
        commands: list[app_commands.AppCommand],
        guild: Snowflake | int | None = None,
    ) -> None:
        # because we support both int and Snowflake
        # we need to convert it to a Snowflake like object if it's an int
        _guild: Snowflake | None = None
        if guild is not None:
            if isinstance(guild, int):
                _guild = discord.Object(guild)
            else:
                _guild = guild

        if _guild:
            self._guild_app_commands[_guild.id] = self._unpack_app_commands(commands)
        else:
            self._global_app_commands = self._unpack_app_commands(commands)

    async def fetch_command(self, command_id: int, /, *, guild: Snowflake | None = None) -> app_commands.AppCommand:
        res = await super().fetch_command(command_id, guild=guild)
        await self._update_cache([res], guild=guild)
        return res

    async def fetch_commands(self, *, guild: Snowflake | None = None) -> list[app_commands.AppCommand]:
        res = await super().fetch_commands(guild=guild)
        await self._update_cache(res, guild=guild)
        return res

    def clear_app_commands_cache(self, *, guild: Snowflake | None) -> None:
        if guild:
            self._guild_app_commands.pop(guild.id, None)
        else:
            self._global_app_commands = {}

    def clear_commands(
        self,
        *,
        guild: Snowflake | None,
        type: discord.AppCommandType | None = None,
        clear_app_commands_cache: bool = True,
    ) -> None:
        super().clear_commands(guild=guild)
        if clear_app_commands_cache:
            self.clear_app_commands_cache(guild=guild)

    async def sync(self, *, guild: Snowflake | None = None) -> list[app_commands.AppCommand]:
        res = await super().sync(guild=guild)
        await self._update_cache(res, guild=guild)
        return res

    async def _call(self, interaction):
        if not disable_app_commands:
            await super()._call(interaction)


class Bot(commands.Bot):
    """Dank Ripoff... Remastered."""

    alerts: defaultdict[int, list[dict[str, Any]]]
    bypass_checks: bool
    cdn: CDNClient
    db: Database
    ipc: Server
    partnership_weights: dict[str, int]
    session: ClientSession
    startup_timestamp: datetime
    timers: TimerManager
    tree: Tree
    transaction_locks: dict[int, LockWithReason]

    INTENTS: Final[ClassVar[discord.Intents]] = discord.Intents(
        messages=True,
        message_content=True,
        members=True,
        guilds=True,
    )

    def __init__(self) -> None:
        key = 'owner_id' if isinstance(owner, int) else 'owner_ids'

        super().__init__(
            command_prefix=self.__class__.resolve_command_prefix,  # type: ignore
            help_command=HelpCommand(),
            update_application_commands_at_startup=True,
            description=description,
            case_insensitive=True,
            allowed_mentions=allowed_mentions,
            intents=self.INTENTS,
            status=discord.Status.dnd,
            activity=discord.Game(default_prefix + 'help'),
            max_messages=10,
            tree_cls=Tree,
            **{key: owner},
        )

        self._BotBase__cogs = commands.core._CaseInsensitiveDict()

    @property
    def average_latency(self) -> float:
        """Returns the average latency of the past 5 heartbeats.

        This is not a simple mean, rather the worst and best latencies out of past 5 are removed,
        then the mean is taken from the remaining 3.
        """
        if not self.ws:
            return float('nan')

        if keep_alive := self.ws._keep_alive:  # type: ignore
            keep_alive: TrackingKeepAliveHandler
            return keep_alive.average_latency

        return float('nan')

    def add_command(self, command: Command, /) -> None:
        if isinstance(command, Command):
            command.transform_flag_parameters()

        if isinstance(command, GroupCommand):
            for child in command.walk_commands():
                if isinstance(child, Command):
                    child.transform_flag_parameters()  # type: ignore

        super().add_command(command)

    async def resolve_command_prefix(self, message: discord.Message) -> list[str]:
        """Resolves a command prefix from a message."""
        if beta or not message.guild:
            return commands.when_mentioned_or(default_prefix)(self, message)

        record = await self.db.get_guild_record(message.guild.id)
        prefixes = sorted(record.prefixes, key=len, reverse=True)

        return commands.when_mentioned_or(*prefixes)(self, message)

    async def _dispatch_first_ready(self) -> None:
        """Waits for the inbound READY gateway event, then dispatches the `first_ready` event."""
        await self.wait_until_ready()
        self.dispatch('first_ready')

    async def _load_extensions(self) -> None:
        """Loads all command extensions, including Jishaku."""
        await self.load_extension('jishaku')

        for file in os.listdir('./app/extensions'):
            if not file.startswith('_') and file.endswith('.py'):
                await self.load_extension(f'app.extensions.{file[:-3]}')

    async def setup_hook(self) -> None:
        """Prepares the bot for startup."""
        self.db = Database(self, loop=self.loop)
        self.timers = TimerManager(self)
        self.transaction_locks = {}
        self.partnership_weights = {}
        self.session = ClientSession()
        self.cdn = CDNClient(self)
        self.bypass_checks = False
        self.ipc = Server(self, secret_key=ipc_secret)
        self.alerts = defaultdict(list)

        self.loop.create_task(self._dispatch_first_ready())
        await self.ipc.start()
        await self._load_extensions()
        await self.tree.set_translator(Translator(self))
        await self.tree.fetch_commands()  # populate cache
        self.backup.start()

    async def get_context(
        self,
        origin: discord.Message | discord.Interaction,
        *,
        cls: type[Context] = Context,
    ) -> Context:
        return await super().get_context(origin, cls=cls)

    async def process_commands(self, message: discord.Message, /) -> None:
        if message.author.bot:
            return

        ctx = await self.get_context(message)
        await self.invoke(ctx)

    async def on_first_ready(self) -> None:
        self.startup_timestamp = discord.utils.utcnow()
        self._update_partner_weights()

        text = f'Ready as {self.user} ({self.user.id})'
        center = f' {name} v{version} '

        print(format(center, f'=^{len(text)}'))
        print(text)

    async def _get_display_prefix(self, message: discord.Message) -> str:
        prefix = await self.get_prefix(message)
        if prefix is None:
            prefix = default_prefix

        if isinstance(prefix, list):
            prefix = discord.utils.find(lambda p: p not in {f'<@{self.user.id}> ', f'<@!{self.user.id}> '}, prefix)
            if prefix is None:
                return f'@{self.user.display_name} '

        return prefix

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        if message.content in {f'<@{self.user.id}>', f'<@!{self.user.id}>'}:
            prefix = await self._get_display_prefix(message)
            help_guide = self.tree.get_app_command('help guide').mention
            help_commands = self.tree.get_app_command('help commands').mention
            await message.reply(
                f"Hey, I'm {self.user.name}. My command prefix here is **`{prefix}`**\n"
                f"Additionally, most of my commands are available as slash commands.\n"
                f"- For a guide on how to use this bot, run {help_guide}.\n"
                f"- To browse all commands available to you, run {help_commands}.\n"
            )

        if discord.PartialEmoji.from_str(message.content).id == 1140424004407144538:
            message.content = f'{self.user.mention} harvest'  # Easter egg

        await self.process_commands(message)

    def _update_partner_weights(self, *, raw: list[dict[str, Any]] | None = None) -> None:
        self.partnership_weights.clear()
        if raw is None:
            try:
                with open('assets/partners.json') as f:
                    raw = json.load(f)
            except FileNotFoundError:
                print('assets/partners.json not found, skipping partner weight update')
                return
            except json.JSONDecodeError:
                print('assets/partners.json is malformed, skipping partner weight update')
                return

        for partner in raw:
            guild = self.get_guild(partner['id'])
            if guild is None:
                continue

            humans = sum(not member.bot for member in guild.members)
            self.partnership_weights[partner['invite']] = humans + partner.get('adjustment', 0)

    def add_alert(self, user_id: int, alert: dict[str, Any]) -> None:
        self.alerts[user_id].append(alert)

    @tasks.loop(time=EVERY_TWO_HOURS)
    async def update_partner_weights(self) -> None:
        await self.wait_until_ready()
        self._update_partner_weights()

    # Run backup every 2 hours
    @tasks.loop(time=EVERY_TWO_HOURS)
    async def backup(self) -> None:
        await self.wait_until_ready()
        channel = self.get_partial_messageable(backups_channel)
        command = [
            'pg_dump',
            '-d', DatabaseConfig.name,
            *(('-U', DatabaseConfig.user) if DatabaseConfig.user else ()),
            *(('-h', DatabaseConfig.host) if DatabaseConfig.host else ()),
            *(('-p', DatabaseConfig.port) if DatabaseConfig.port else ()),
            '-w'
        ]

        proc = await subprocess.create_subprocess_shell(
            ' '.join(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **({'env': {'PGPASSWORD': DatabaseConfig.password}} if DatabaseConfig.password else {}),
        )
        stdout, stderr = await proc.communicate()
        if stdout:
            await channel.send(
                f'Backup {discord.utils.format_dt(discord.utils.utcnow())} on {sys.platform}',
                file=discord.File(BytesIO(stdout), filename='backup.sql'),
            )
        if stderr:
            await channel.send(
                f'ERROR backing up {discord.utils.format_dt(discord.utils.utcnow())} on {sys.platform} <@{owner}>',
                file=discord.File(BytesIO(stderr), filename='error.txt'),
            )

    async def close(self) -> None:
        await self.session.close()
        await self.ipc.stop()
        await super().close()

    def run(self, token_override: str | None = None, **kwargs) -> None:
        return super().run(token_override or token, **kwargs)
