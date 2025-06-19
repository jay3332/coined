from __future__ import annotations

import inspect
import random
from datetime import timedelta
from functools import wraps
from typing import Any, Callable, Final, Iterable, Literal, NamedTuple, TYPE_CHECKING, overload

import discord
from discord.app_commands import Command as AppCommand
from discord.ext import commands

from app.core.models import Command, GroupCommand, HybridCommand, HybridGroupCommand
from app.util.common import format_line, sentinel, weighted_choice
from app.util.pagination import Paginator
from app.util.structures import LockWithReason
from config import Emojis

if TYPE_CHECKING:
    from app.core.models import Context, Cog
    from app.database import GuildPremiumType, UserPremiumType
    from app.util.types import TypedInteraction

__all__ = (
    'REPLY',
    'EDIT',
    'BAD_ARGUMENT',
    'MISSING',
    'easy_command_callback',
    'command',
    'group',
    'simple_cooldown',
    'database_cooldown',
)

EDIT  = sentinel('EDIT', repr='EDIT')
REPLY = sentinel('REPLY', repr='REPLY')
BAD_ARGUMENT = sentinel('BAD_ARGUMENT', repr='BAD_ARGUMENT')
ERROR = sentinel('ERROR', repr='ERROR')
EPHEMERAL = sentinel('ERROR', repr='EPHEMERAL')
NO_EXTRA = sentinel('NO_EXTRA', repr='NO_EXTRA')

MISSING = sentinel('MISSING', bool=False, repr='MISSING')

CURRENCY_COGS: Final[frozenset[str]] = frozenset({
    'Casino',
    'Combat',
    'Farming',
    'Jobs',
    'Pets',
    'Profit',
    'Skill',
    'Stats',
    'Transactions',
})


async def _into_interaction_response(interaction: TypedInteraction, kwargs: dict[str, Any]) -> None:
    kwargs.pop('reference', None)

    if kwargs.get('embed') and kwargs.get('embeds') is not None:
        kwargs['embeds'].append(kwargs['embed'])
        del kwargs['embed']

    if kwargs.pop('edit', False):
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(**kwargs)
            else:
                await interaction.response.edit_message(**kwargs)
        except discord.NotFound:
            pass
        else:
            return

    if 'attachments' in kwargs:
        kwargs['files'] = kwargs.pop('attachments')
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


class GenericError(commands.BadArgument):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(kwargs.get('content', 'Unknown error'))
        self.kwargs = kwargs


with open('assets/tips.txt', 'r') as f:
    TIPS = f.readlines()


async def process_message(ctx: Context, payload: Any) -> discord.Message | None:
    # sourcery no-metrics
    if payload is None:
        return

    kwargs = {}
    # noinspection PyTypeChecker
    kwargs.setdefault('embeds', [])
    # noinspection PyTypeChecker
    kwargs.setdefault('files', [])

    if not isinstance(payload, (set, tuple, list)):
        payload = [payload]

    paginator = None
    extra = True
    edit = False
    error = False

    for part in payload:
        if part is REPLY:
            kwargs['reference'] = ctx.message

        elif part is EDIT:
            kwargs['edit'] = edit = True

        elif part is BAD_ARGUMENT:
            raise commands.BadArgument(kwargs['content'])

        elif part is EPHEMERAL:
            kwargs['ephemeral'] = True

        elif part is ERROR:
            error = True

        elif part is NO_EXTRA:
            extra = False

        elif isinstance(part, discord.Embed):
            kwargs['embeds'].append(part)

        elif isinstance(part, discord.File):
            kwargs['files'].append(part)

        elif isinstance(part, (discord.ui.View, discord.ui.LayoutView)):
            kwargs['view'] = part

        elif isinstance(part, Paginator):
            paginator = part

        elif isinstance(part, dict):
            kwargs.update(part)

        elif part is None:
            continue

        else:
            kwargs['content'] = str(part)

    if not (
        not ctx.interaction and ctx.guild and ctx.channel.permissions_for(ctx.guild.me).external_emojis
        or ctx.interaction and ctx.interaction.app_permissions.external_emojis
    ):
        if content := kwargs.get('content'):
            kwargs['content'] = content.replace(Emojis.coin, '\U0001fa99')

        for embed in kwargs.get('embeds', []):
            if embed.description:
                embed.description = embed.description.replace(Emojis.coin, '\U0001fa99')

            for field in embed.fields:
                field.name = field.name.replace(Emojis.coin, '\U0001fa99')
                field.value = field.value.replace(Emojis.coin, '\U0001fa99')

    if error:
        raise GenericError(**kwargs)

    if extra and ctx.cog.qualified_name in CURRENCY_COGS and not kwargs.get('content') and not edit:
        record = await ctx.db.get_user_record(ctx.author.id)

        if notifs := record.unread_notifications:
            notifications_mention = ctx.bot.tree.get_app_command('notifications list').mention
            kwargs['content'] = (
                f"\U0001f514 You have {notifs:,} unread notification{'s' if notifs != 1 else ''}. "
                f"Run {notifications_mention} to view them."
            )

        if not record.hide_tips and random.random() < 0.2:
            tip = random.choice(TIPS)
            kwargs.setdefault('content', '')
            kwargs['content'] += f'\n\U0001f4a1 **Tip:** {format_line(ctx, tip)}'

        if not record.hide_partnerships and random.random() < 0.1 and ctx.bot.partnership_weights:
            partner = weighted_choice(ctx.bot.partnership_weights)
            kwargs.setdefault('content', '')
            kwargs['content'] += (
                f'\n\U0001f91d The growth of Coined is made possible by partnerships! '
                f'Here is one of our partners: https://discord.gg/{partner}'
            )

    if kwargs.get('content') and isinstance(kwargs.get('view'), discord.ui.LayoutView):
        kwargs['view'].add_item(discord.ui.TextDisplay(kwargs['content']))
        del kwargs['content']

    if 'files' in kwargs and kwargs.get('edit'):
        kwargs['attachments'] = kwargs.pop('files')

    interaction = getattr(ctx, 'interaction', None)

    if paginator:
        return await paginator.start(interaction=interaction, **kwargs)

    if interaction:
        return await _into_interaction_response(interaction, kwargs)

    return await ctx.send(**kwargs)


# We are using non-generic "callable" as return types would be
# a bit too complicated
def easy_command_callback(func: callable) -> callable:
    @wraps(func)
    async def wrapper(cog: Cog, ctx: Context, /, *args, **kwargs) -> None:
        coro = func(cog, ctx, *args, **kwargs)

        if inspect.isasyncgen(coro):
            async for payload in coro:
                await process_message(ctx, payload)
        else:
            await process_message(ctx, await coro)

    return wrapper


def get_transaction_lock(ctx: Context, *, update_jump_url: bool = False) -> LockWithReason:
    lock = ctx.bot.transaction_locks.setdefault(ctx.author.id, LockWithReason())
    if update_jump_url:
        lock.jump_url = ctx.message.jump_url
    return lock


class ActiveTransactionLock(commands.BadArgument):
    def __init__(self, lock: LockWithReason) -> None:
        super().__init__(lock.reason or 'Please finish your pending transaction(s) first.')
        self.lock = lock


async def check_transaction_lock(ctx: Context) -> bool:
    lock = get_transaction_lock(ctx)

    if lock.locked():
        raise ActiveTransactionLock(lock)

    return True


def lock_transactions(func: callable) -> callable:
    if inspect.isasyncgenfunction(func):
        @wraps(func)
        async def wrapper(cog: Cog, ctx: Context, /, *args, **kwargs) -> Any:
            async with get_transaction_lock(ctx, update_jump_url=True):
                async for item in func(cog, ctx, *args, **kwargs):
                    yield item

    else:
        @wraps(func)
        async def wrapper(cog: Cog, ctx: Context, /, *args, **kwargs) -> Any:
            async with get_transaction_lock(ctx, update_jump_url=True):
                return await func(cog, ctx, *args, **kwargs)

    return commands.check(check_transaction_lock)(wrapper)


def _installation_wrapper(deco):
    @wraps(deco)
    def wrapper(func):
        func.__discord_app_commands_installation_types__ = getattr(
            func,
            '__discord_app_commands_installation_types__',
            discord.app_commands.AppInstallationType(guild=True, user=True),
        )
        func.__discord_app_commands_contexts__ = getattr(
            func,
            '__discord_app_commands_contexts__',
            discord.app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
        )
        return deco(func)

    return wrapper


# noinspection PyShadowingBuiltins
def _resolve_command_kwargs(
    cls: type,
    *,
    name: str = MISSING,
    alias: str = MISSING,
    aliases: Iterable[str] = MISSING,
    usage: str = MISSING,
    brief: str = MISSING,
    help: str = MISSING
) -> dict[str, Any]:
    kwargs = {'cls': cls}

    if name is not MISSING:
        kwargs['name'] = name

    if alias is not MISSING and aliases is not MISSING:
        raise TypeError('cannot have alias and aliases kwarg filled')

    if alias is not MISSING:
        kwargs['aliases'] = (alias,)

    if aliases is not MISSING:
        kwargs['aliases'] = tuple(aliases)

    if usage is not MISSING:
        kwargs['usage'] = usage

    if brief is not MISSING:
        kwargs['brief'] = brief

    if help is not MISSING:
        kwargs['help'] = help

    return kwargs


# noinspection PyShadowingBuiltins
@overload
def command(
    name: str = MISSING,
    *,
    alias: str = MISSING,
    aliases: Iterable[str] = MISSING,
    usage: str = MISSING,
    brief: str = MISSING,
    help: str = MISSING,
    easy_callback: bool = True,
    hybrid: Literal[True] | AppCommand = False,
    **other_kwargs: Any,
) -> Callable[..., HybridCommand]:
    ...


# noinspection PyShadowingBuiltins
@overload
def command(
    name: str = MISSING,
    *,
    alias: str = MISSING,
    aliases: Iterable[str] = MISSING,
    usage: str = MISSING,
    brief: str = MISSING,
    help: str = MISSING,
    easy_callback: bool = True,
    hybrid: Literal[False] = False,
    **other_kwargs: Any,
) -> Callable[..., Command]:
    ...


# noinspection PyShadowingBuiltins
def command(
    name: str = MISSING,
    *,
    alias: str = MISSING,
    aliases: Iterable[str] = MISSING,
    usage: str = MISSING,
    brief: str = MISSING,
    help: str = MISSING,
    easy_callback: bool = True,
    hybrid: bool | AppCommand = False,
    **other_kwargs: Any,
) -> Callable[..., Command]:
    kwargs = _resolve_command_kwargs(
        HybridCommand if hybrid else Command,
        name=name, alias=alias, aliases=aliases, brief=brief, help=help, usage=usage,
    )
    result = commands.command(**kwargs, **other_kwargs)
    if isinstance(hybrid, AppCommand):
        result.app_command = hybrid

    result = _installation_wrapper(result)

    if easy_callback:
        return lambda func: result(easy_command_callback(func))

    return result


# noinspection PyShadowingBuiltins
@overload
def group(
    name: str = MISSING,
    *,
    alias: str = MISSING,
    aliases: Iterable[str] = MISSING,
    usage: str = MISSING,
    brief: str = MISSING,
    help: str = MISSING,
    easy_callback: bool = True,
    hybrid: Literal[True] = False,
    **other_kwargs: Any,
) -> Callable[..., HybridGroupCommand]:
    ...


# noinspection PyShadowingBuiltins
@overload
def group(
    name: str = MISSING,
    *,
    alias: str = MISSING,
    aliases: Iterable[str] = MISSING,
    usage: str = MISSING,
    brief: str = MISSING,
    help: str = MISSING,
    easy_callback: bool = True,
    hybrid: Literal[False] = False,
    **other_kwargs: Any,
) -> Callable[..., GroupCommand]:
    ...


# noinspection PyShadowingBuiltins
def group(
    name: str = MISSING,
    *,
    alias: str = MISSING,
    aliases: Iterable[str] = MISSING,
    usage: str = MISSING,
    brief: str = MISSING,
    help: str = MISSING,
    easy_callback: bool = True,
    hybrid: bool = False,
    iwc: bool = True,
    **other_kwargs: Any,
) -> Callable[..., GroupCommand]:
    kwargs = _resolve_command_kwargs(
        HybridGroupCommand if hybrid else GroupCommand,
        name=name, alias=alias, aliases=aliases, brief=brief, help=help, usage=usage,
    )
    kwargs['invoke_without_command'] = iwc
    result = _installation_wrapper(commands.group(**kwargs, **other_kwargs))

    if easy_callback:
        return lambda func: result(easy_command_callback(func))

    return result


def simple_cooldown(rate: int, per: float, bucket: commands.BucketType = commands.BucketType.user) -> Callable[[callable], callable]:
    return commands.cooldown(rate, per, bucket)


class _DummyCooldown(NamedTuple):
    rate: int
    per: float


def user_premium_dynamic_cooldown(
    rate: int,
    per: float,
    *,
    silver: tuple[int, float] = MISSING,
    gold: tuple[int, float] = MISSING,
    bucket: commands.BucketType = commands.BucketType.user,
) -> Callable[[callable], callable]:
    from app.database import UserPremiumType

    if silver is MISSING:
        silver = (rate, per)
    if gold is MISSING:
        gold = silver

    def cooldown(ctx: Context) -> commands.Cooldown:
        record = ctx.db.get_user_record(ctx.author.id, fetch=False)
        if record.premium_type >= UserPremiumType.gold:
            return commands.Cooldown(*gold)
        elif record.premium_type >= UserPremiumType.silver:
            return commands.Cooldown(*silver)
        else:
            return commands.Cooldown(rate, per)

    deco = commands.dynamic_cooldown(cooldown, type=bucket)

    @wraps(deco)
    def wrapper(func: callable) -> callable:
        func.__user_premium_dynamic_cooldown__ = (
            _DummyCooldown(rate, per), _DummyCooldown(*silver), _DummyCooldown(*gold), bucket
        )
        return deco(func)

    return wrapper


class UserPremiumOnlyCommand(commands.CommandError):
    def __init__(self, min_premium_type: UserPremiumType) -> None:
        super().__init__(
            f'You need to subscribe to {min_premium_type.emoji} **{min_premium_type.name}** to use this command.'
        )
        self.min_premium_type = min_premium_type


class GuildPremiumOnlyCommand(commands.CommandError):
    def __init__(self, min_premium_type: GuildPremiumType) -> None:
        super().__init__(
            f'This command can only be used in servers with {min_premium_type.emoji} **{min_premium_type.name}**.'
        )
        self.min_premium_type = min_premium_type


def user_premium(min_premium_type: UserPremiumType = MISSING) -> Callable[[callable], callable]:
    from app.database import UserPremiumType
    if min_premium_type is MISSING:
        min_premium_type = UserPremiumType.silver

    async def predicate(ctx: Context) -> bool:
        record = await ctx.fetch_author_record()
        if record.premium_type >= min_premium_type:
            return True

        raise UserPremiumOnlyCommand(min_premium_type)

    deco = commands.check(predicate)

    @wraps(deco)
    def wrapper(func: callable) -> callable:
        func.__user_premium_required__ = min_premium_type
        return deco(func)

    return wrapper


def guild_premium(min_premium_type: GuildPremiumType = MISSING) -> Callable[[callable], callable]:
    from app.database import GuildPremiumType
    if min_premium_type is MISSING:
        min_premium_type = GuildPremiumType.premium

    async def predicate(ctx: Context) -> bool:
        if ctx.guild is None or ctx.interaction and ctx.interaction.is_user_integration():
            raise commands.NoPrivateMessage()

        record = await ctx.fetch_guild_record()
        if record.premium_type >= min_premium_type:
            return True

        raise GuildPremiumOnlyCommand(min_premium_type)

    deco = commands.check(predicate)

    @wraps(deco)
    def wrapper(func: callable) -> callable:
        func.__guild_premium_required__ = min_premium_type
        return deco(func)

    return wrapper


def user_max_concurrency(count: int, *, wait: bool = False) -> Callable[[callable], callable]:
    return commands.max_concurrency(count, commands.BucketType.user, wait=wait)


def cooldown_message(message: str) -> Callable[[callable | commands.Command], callable]:
    def decorator(func: callable | commands.Command) -> callable:
        target = func
        if isinstance(func, commands.Command):
            target = func.callback

        target.__cooldown_message__ = message
        return func

    return decorator


def database_cooldown(per: float, /) -> Callable[[callable], callable]:
    async def predicate(ctx: Context) -> bool:
        data = await ctx.db.get_user_record(ctx.author.id)
        manager = data.cooldown_manager

        await manager.wait()
        cooldown = manager.get_cooldown(ctx.command)

        if cooldown is False:
            expires = discord.utils.utcnow() + timedelta(seconds=per)
            await manager.set_cooldown(ctx.command, expires=expires)

            return True

        raise commands.CommandOnCooldown(commands.Cooldown(1, per), cooldown, commands.BucketType.user)

    deco = commands.check(predicate)

    @wraps(deco)
    def wrapper(func: callable) -> callable:
        func.__database_cooldown__ = per
        return deco(func)

    return wrapper
