from __future__ import annotations

import datetime
from collections import OrderedDict
from functools import wraps
from typing import Any, Awaitable, Callable, NamedTuple, Literal, TYPE_CHECKING, Union, Self

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import CommandError, HybridCommandError
from discord.utils import MISSING, async_all, maybe_coroutine as maybe_coro, maybe_coroutine

from app.core.flags import ConsumeUntilFlag, FlagMeta, Flags
from app.features.guide import GuideView
from app.util.ansi import AnsiColor, AnsiStringBuilder
from app.util.structures import TemporaryAttribute
from app.util.types import TypedContext
from app.util.views import AnyUser, ConfirmationButton, ConfirmationView

if TYPE_CHECKING:
    from typing import ClassVar

    from app.core.bot import Bot
    from app.database import Database, UserRecord
    from app.util.pagination import Paginator
    from app.util.types import AsyncCallable, TypedInteraction


class Context(TypedContext):
    bot: Bot
    command: Command | GroupCommand

    def __init__(self, **attrs) -> None:
        self._message: discord.Message | None = None
        super().__init__(**attrs)

    @property
    def db(self) -> Database:
        return self.bot.db

    @property
    def now(self) -> datetime.datetime:
        return self.message.created_at

    @property
    def clean_prefix(self) -> str:
        if self.prefix is None:
            return ''

        user = self.bot.user
        return self.prefix.replace(f'<@{user.id}>', f'@{user.name}').replace(f'<@!{user.id}>', f'@{user.name}')

    @property
    def is_interaction(self) -> bool:
        return self.interaction is not None

    @staticmethod
    def utcnow() -> datetime.datetime:
        return discord.utils.utcnow()

    async def thumbs(self, message: discord.Message = None) -> None:
        message = message or self.message
        try:
            await message.add_reaction('\U0001f44d')
        except discord.HTTPException:
            pass

    async def confirm(
        self,
        content: str = None,
        *,
        delete_after: bool = False,
        view: ConfirmationView = None,
        interaction: TypedInteraction = None,
        paginator: Paginator = None,
        user: AnyUser = None,
        timeout: float = 60.,
        true: str = 'Yes',
        false: str = 'No',
        replace_interaction: bool | str = False,
        **kwargs,
    ) -> bool:
        user = user or self.author
        view = view or ConfirmationView(user=user, true=true, false=false, timeout=timeout)
        edit = kwargs.pop('edit', True)

        if paginator is not None:
            view = paginator._underlying_view
            view._other_components.append(ConfirmationButton(toggle=True, label=true, row=4))
            view._other_components.append(ConfirmationButton(toggle=False, label=false, row=4))
            view._update_view()

            message = await paginator.start(interaction=interaction, **kwargs)
        elif interaction is not None:
            await interaction.response.send_message(content, view=view, **kwargs)
            message = await interaction.original_response()
        else:
            message = await self.send(content, view=view, **kwargs)

        await view.wait()
        if message is not None:
            if delete_after:
                await message.delete(delay=0)
            elif edit:
                await message.edit(view=view)
        else:
            await interaction.edit_original_response(view=view)

        if replace_interaction:
            attr = replace_interaction if isinstance(replace_interaction, str) else 'interaction'
            setattr(self, attr, view.interaction)

        return getattr(view, '__confirm_value__', False)

    async def maybe_edit(self, message: discord.Message = MISSING, content: Any = None, **kwargs: Any) -> discord.Message | None:
        if message is MISSING:
            message = self._message
        try:
            await message.edit(content=content, **kwargs)
        except (AttributeError, discord.NotFound):
            if not message or message.channel == self.channel:
                return await self.send(content, **kwargs)

            return await message.channel.send(content, **kwargs)

    @staticmethod
    async def maybe_delete(message: discord.Message, *args: Any, **kwargs: Any) -> None:
        try:
            await message.delete(*args, **kwargs)
        except (AttributeError, discord.NotFound, discord.Forbidden):
            pass

    async def send(self, content: Any = None, **kwargs) -> discord.Message:
        if kwargs.get('embed') and kwargs.get('embeds') is not None:
            kwargs['embeds'].append(kwargs['embed'])
            del kwargs['embed']

        if kwargs.get('file') and kwargs.get('files') is not None:
            kwargs['files'].append(kwargs['file'])
            del kwargs['file']

        if kwargs.pop('edit', False) and self._message:
            kwargs.pop('files', None)
            kwargs.pop('reference', None)

            await self.maybe_edit(self._message, content, **kwargs)
            return self._message

        try:
            result = await super().send(content, **kwargs)
        except discord.NotFound:
            if kwargs.pop('reference', False):
                result = await super().send(content, **kwargs)
            else:
                raise

        self._message = result
        return result

    async def send_guide(self, page: str = 'index') -> GuideView:
        view = GuideView(self, page=page)
        await self.reply(embed=view.render(), view=view)
        return view

    async def fetch_author_record(self) -> UserRecord:
        """Fetches the author's record from the database."""
        return await self.db.get_user_record(self.author.id)

    async def add_random_exp(self, minimum: int, maximum: int, **kwargs) -> int:
        """Adds random EXP to the author."""
        record = await self.fetch_author_record()
        return await record.add_random_exp(minimum, maximum, ctx=self, **kwargs)


class Cog(commands.Cog):
    __hidden__: bool = False
    emoji: ClassVar[str]

    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        bot.loop.create_task(maybe_coro(self.__setup__))  # type: ignore

    def __setup__(self) -> Awaitable[None] | None:
        pass

    @classmethod
    async def simple_setup(cls, bot: Bot) -> None:
        await bot.add_cog(cls(bot))


class PermissionSpec(NamedTuple):
    """A pair of sets of permissions which will be checked for before a command is run."""
    user: set[str]
    bot: set[str]

    @classmethod
    def new(cls) -> Self:
        """Creates a new permission spec.

        Users default to requiring no permissions.
        Bots default to requiring Read Message History, View Channel, Send Messages, Embed Links, and External Emojis permissions.
        """
        required = {'read_message_history', 'view_channel', 'send_messages', 'embed_links', 'use_external_emojis'}
        return cls(user=set(), bot=required)

    @staticmethod
    def permission_as_str(permission: str) -> str:
        """Takes the attribute name of a permission and turns it into a capitalized, readable one."""
        # weird and hacky solution but it works.
        return permission.replace('_', ' ').title().replace('Tts ', 'TTS ').replace('Guild', 'Server')

    @staticmethod
    def _is_owner(bot: Bot, user: discord.User) -> bool:
        if bot.owner_id:
            return user.id == bot.owner_id

        elif bot.owner_ids:
            return user.id in bot.owner_ids

        return False

    # We don't use @has_permissions/@bot_has_permissions as I may want to implement custom permission checks later on
    def check(self, ctx: Context) -> bool:
        """Checks if the given context meets the required permissions."""
        if ctx.bot.bypass_checks and self._is_owner(ctx.bot, ctx.author):
            return True

        if not ctx.guild or ctx.interaction:
            return True

        other = ctx.channel.permissions_for(ctx.author)
        missing = [perm for perm, value in other if perm in self.user and not value]

        if missing and not other.administrator:
            raise commands.MissingPermissions(missing)

        other = ctx.channel.permissions_for(ctx.me)
        missing = [perm for perm, value in other if perm in self.bot and not value]

        if missing and not other.administrator:
            raise commands.BotMissingPermissions(missing)

        return True


class ParamInfo(NamedTuple):
    """Parameter information."""
    name: str
    required: bool
    default: Any
    greedy: bool
    choices: list[str | int | bool] | None
    show_default: bool
    flag: bool
    store_true: bool

    def is_flag(self) -> bool:
        return self.flag


@discord.utils.copy_doc(commands.Command)
class Command(commands.Command):
    def __init__(self, func: AsyncCallable[..., Any], **kwargs: Any) -> None:
        self._permissions: PermissionSpec = PermissionSpec.new()
        if user_permissions := kwargs.pop('user_permissions', None):
            self._permissions.user.update(user_permissions)

        if bot_permissions := kwargs.pop('bot_permissions', None):
            self._permissions.bot.update(bot_permissions)

        self.custom_flags: FlagMeta[Any] | None = None
        self.expand_subcommands: bool = kwargs.pop('expand_subcommands', False)
        self.app_command_name: str | None = kwargs.pop('app_command_name', None)

        super().__init__(func, **kwargs)
        self.add_check(self._permissions.check)

    def _ensure_assignment_on_copy(self, other: Command) -> Command:
        super()._ensure_assignment_on_copy(other)

        other._permissions = self._permissions
        other.custom_flags = self.custom_flags
        other.expand_subcommands = self.expand_subcommands
        other.app_command_name = self.app_command_name

        return other

    def transform_flag_parameters(self) -> None:
        first_consume_rest = None

        for name, param in self.params.items():
            if param.kind is not param.KEYWORD_ONLY:
                continue

            try:
                is_flags = issubclass(param.annotation, Flags)
            except TypeError:
                is_flags = False

            if is_flags:
                self.custom_flags = param.annotation
                try:
                    default = self.custom_flags.default
                except ValueError:
                    pass
                else:
                    self.params[name] = param.replace(default=default)

                if not first_consume_rest:
                    break

                target = self.params[first_consume_rest]
                default = MISSING if target.default is param.empty else target.default
                annotation = None if target.annotation is param.empty else target.annotation

                self.params[first_consume_rest] = target.replace(
                    annotation=ConsumeUntilFlag(annotation, default),
                    kind=param.POSITIONAL_OR_KEYWORD,
                )
                break

            elif not first_consume_rest:
                first_consume_rest = name

        if first_consume_rest and self.custom_flags:  # A kw-only has been transformed into a pos-or-kw, reverse this here
            @wraps(original := self.callback)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                idx = 2 if self.cog else 1

                for i, (arg, (k, v)) in enumerate(zip(args[idx:], self.params.items())):
                    if k == first_consume_rest:
                        args = args[:i + idx]
                        kwargs[k] = arg
                        break

                return await original(*args, **kwargs)

            self._callback = wrapper  # leave the params alone

    @classmethod
    def ansi_signature_of(cls, command: commands.Command, /) -> AnsiStringBuilder:
        if isinstance(command, cls):
            return command.ansi_signature  # type: ignore

        with TemporaryAttribute(command, attr='custom_flags', value=None):
            return cls.ansi_signature.fget(command)

    @property
    def permission_spec(self) -> PermissionSpec:
        """Return the permission specification for this command.

        Useful for the help command.
        """
        return self._permissions

    @staticmethod
    def _disect_param(param: commands.Parameter) -> tuple:
        greedy = isinstance(param.annotation, commands.Greedy)
        optional = False  # postpone evaluation of if it's an optional argument

        # for typing.Literal[...], typing.Optional[typing.Literal[...]], and Greedy[typing.Literal[...]], the
        # parameter signature is a literal list of it's values
        annotation = param.annotation.converter if greedy else param.annotation
        origin = getattr(annotation, '__origin__', None)
        if not greedy and origin is Union:
            none_cls = type(None)
            union_args = annotation.__args__
            optional = union_args[-1] is none_cls

            if len(union_args) == 2 and optional:
                annotation = union_args[0]
                origin = getattr(annotation, '__origin__', None)

        return annotation, greedy, optional, origin

    @property
    def param_info(self) -> OrderedDict[str, ParamInfo]:
        """Returns a dict mapping parameter names to their rich info."""
        result = OrderedDict()
        params = self.clean_params
        if not params:
            return result

        for name, param in params.items():
            annotation, greedy, optional, origin = Command._disect_param(param)
            default = param.default

            if isinstance(annotation, FlagMeta) and self.custom_flags:
                for flag in self.custom_flags.walk_flags():
                    optional = not flag.required
                    name = '--' + flag.name
                    default = param.empty

                    if not flag.store_true and flag.default or flag.default is False:
                        default = flag.default
                        optional = True

                    result[name] = ParamInfo(
                        name=name,
                        required=not optional,
                        show_default=bool(flag.default) or flag.default is False,
                        default=default,
                        choices=None,
                        greedy=greedy,
                        flag=True,
                        store_true=flag.store_true,
                    )

                continue

            choices = annotation.__args__ if origin is Literal else None

            if default is not param.empty:
                show_default = bool(default) if isinstance(default, str) else default is not None
                optional = True
            else:
                show_default = False

            if param.kind is param.VAR_POSITIONAL:
                optional = not self.require_var_positional
            elif param.default is param.empty:
                optional = optional or greedy

            result[name] = ParamInfo(
                name=name,
                required=not optional,
                show_default=show_default,
                default=default,
                choices=choices,
                greedy=greedy,
                flag=False,
                store_true=False,
            )

        return result

    @property
    def ansi_signature(self) -> AnsiStringBuilder:  # sourcery no-metrics
        """Returns an ANSI builder for the signature of this command."""
        if self.usage is not None:
            return AnsiStringBuilder(self.usage)

        params = self.clean_params
        result = AnsiStringBuilder()
        if not params:
            return result

        for name, param in params.items():
            annotation, greedy, optional, origin = Command._disect_param(param)

            if isinstance(annotation, FlagMeta) and self.custom_flags:
                for flag in self.custom_flags.walk_flags():
                    start, end = '<>' if flag.required else '[]'
                    base = '--' + flag.name

                    result.append(start, bold=True, color=AnsiColor.gray)
                    result.append(base, color=AnsiColor.yellow if flag.required else AnsiColor.blue)

                    if not flag.store_true:
                        result.append(' <', color=AnsiColor.gray, bold=True)
                        result.append(flag.dest, color=AnsiColor.magenta)

                        if flag.default or flag.default is False:
                            result.append('=', color=AnsiColor.gray)
                            result.append(str(flag.default), color=AnsiColor.cyan)

                        result.append('>', color=AnsiColor.gray, bold=True)

                    result.append(end + ' ', color=AnsiColor.gray, bold=True)

                continue

            if origin is Literal:
                name = '|'.join(f'"{v}"' if isinstance(v, str) else str(v) for v in annotation.__args__)

            if param.default is not param.empty:
                # We don't want None or '' to trigger the [name=value] case, and instead it should
                # do [name] since [name=None] or [name=] are not exactly useful for the user.
                should_print = param.default if isinstance(param.default, str) else param.default is not None
                result.append('[', color=AnsiColor.gray, bold=True)
                result.append(name, color=AnsiColor.blue)

                if should_print:
                    result.append('=', color=AnsiColor.gray, bold=True)
                    result.append(str(param.default), color=AnsiColor.cyan)
                    extra = '...' if greedy else ''
                else:
                    extra = ''

                result.append(']' + extra + ' ', color=AnsiColor.gray, bold=True)
                continue

            elif param.kind == param.VAR_POSITIONAL:
                if self.require_var_positional:
                    start = '<'
                    end = '...>'
                else:
                    start = '['
                    end = '...]'

            elif greedy:
                start = '['
                end = ']...'

            elif optional:
                start, end = '[]'
            else:
                start, end = '<>'

            result.append(start, color=AnsiColor.gray, bold=True)
            result.append(name, color=AnsiColor.blue if start == '[' else AnsiColor.yellow)
            result.append(end + ' ', color=AnsiColor.gray, bold=True)

        return result

    @property
    def signature(self) -> str:
        """Adds POSIX-like flag support to the signature"""
        return self.ansi_signature.raw

    @property
    def raw_signature(self) -> str:
        return super().signature


class _AppCommandOverride(discord.app_commands.Command):
    def __init__(self, source: HybridCommand | HybridGroupCommand, *args: Any, **kwargs: Any):
        self.wrapped = source
        self.binding = source.cog
        super().__init__(*args, **kwargs)

    def _copy_with(self, **kwargs) -> Self:
        copy: Self = super()._copy_with(**kwargs)  # type: ignore
        copy.wrapped = self.wrapped
        return copy

    def copy(self) -> Self:
        bindings = {
            self.binding: self.binding,
        }
        return self._copy_with(parent=self.parent, binding=self.binding, bindings=bindings)

    async def _check_can_run(self, interaction: TypedInteraction) -> bool:
        bot: Bot = interaction.client  # type: ignore
        ctx: Context = interaction._baton

        if not await bot.can_run(ctx, call_once=True):
            return False

        if not await bot.can_run(ctx):
            return False

        if self.parent is not None and self.parent is not self.binding:
            # For commands with a parent which isn't the binding, i.e.
            # <binding>
            #     <parent>
            #         <command>
            # The parent check needs to be called first
            if not await maybe_coroutine(self.parent.interaction_check, interaction):  # type: ignore
                return False

        if self.binding is not None:
            try:
                # Type checker does not like runtime attribute retrieval
                check = self.binding.interaction_check  # type: ignore
            except AttributeError:
                pass
            else:
                ret = await maybe_coroutine(check, interaction)
                if not ret:
                    return False

            local_check = Cog._get_overridden_method(self.binding.cog_check)
            if local_check is not None:
                ret = await maybe_coroutine(local_check, ctx)
                if not ret:
                    return False

        if self.checks and not await async_all(f(interaction) for f in self.checks):
            return False

        if self.wrapped.checks and not await async_all(f(ctx) for f in self.wrapped.checks):
            return False

        return True

    async def _invoke_with_namespace(self, interaction: discord.Interaction, namespace: app_commands.Namespace) -> Any:
        # Wrap the interaction into a Context
        bot: Bot = interaction.client  # type: ignore

        # Unfortunately, `get_context` has to be called for this to work.
        # If someone doesn't inherit this to replace it with their custom class
        # then this doesn't work.
        interaction._baton = ctx = await bot.get_context(interaction)
        command = self.wrapped
        command.cog = self.binding
        bot.dispatch('command', ctx)
        value = None
        callback_completed = False
        try:
            await command.prepare(ctx)
            # This lies and just always passes a Context instead of an Interaction.
            value = await self._do_call(ctx, ctx.kwargs)  # type: ignore
            callback_completed = True
        except app_commands.CommandSignatureMismatch:
            raise
        except (app_commands.TransformerError, app_commands.CommandInvokeError) as e:
            if isinstance(e.__cause__, CommandError):
                exc = e.__cause__
            else:
                exc = HybridCommandError(e)
                exc.__cause__ = e
            await command.dispatch_error(ctx, exc.with_traceback(e.__traceback__))
        except app_commands.AppCommandError as e:
            exc = HybridCommandError(e)
            exc.__cause__ = e
            await command.dispatch_error(ctx, exc.with_traceback(e.__traceback__))
        except CommandError as e:
            await command.dispatch_error(ctx, e)
        finally:
            if command._max_concurrency is not None:
                await command._max_concurrency.release(ctx.message)

            if callback_completed:
                await command.call_after_hooks(ctx)

        if not ctx.command_failed:
            bot.dispatch('command_completion', ctx)

        interaction.command_failed = ctx.command_failed
        return value


class HybridContext(Context):
    full_invoke: AsyncCallable[..., Any]


@discord.utils.copy_doc(commands.HybridCommand)
class HybridCommand(Command, commands.HybridCommand):
    def define_app_command(self, **kwargs: Any) -> Callable[[AsyncCallable[..., Any]], None]:
        def decorator(func: AsyncCallable[..., Any]) -> None:
            parent = kwargs.pop('parent', False)
            allowed_installs = getattr(
                func,
                '__discord_app_commands_installation_types__',
                discord.app_commands.AppInstallationType(user=True, guild=True),
            )
            allowed_contexts = getattr(
                func,
                '__discord_app_commands_contexts__',
                discord.app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
            )
            command = _AppCommandOverride(
                self,
                name=kwargs.pop('name', self.name),
                description=self.short_doc,
                parent=parent or (
                    self.parent.app_command if isinstance(self.parent, HybridGroupCommand) else None
                ),
                callback=func,  # type: ignore
                allowed_installs=allowed_installs,
                allowed_contexts=allowed_contexts,
                **kwargs,
            )
            if parent:
                parent.add_command(command)  # type: ignore
                self.app_command_name = command.qualified_name
            else:
                self.app_command = command

        return decorator


@discord.utils.copy_doc(commands.Group)
class GroupCommand(commands.Group, Command):
    @discord.utils.copy_doc(commands.Group.command)
    def command(self, *args: Any, **kwargs: Any) -> Callable[[AsyncCallable[..., Any]], Command | HybridCommand]:
        def decorator(func: AsyncCallable[..., Any]) -> Command:
            from app.core.helpers import command

            kwargs.setdefault('parent', self)
            result = command(*args, **kwargs)(func)
            self.add_command(result)
            return result

        return decorator

    @discord.utils.copy_doc(commands.Group.group)
    def group(self, *args: Any, **kwargs: Any) -> Callable[
        [AsyncCallable[..., Any]], GroupCommand | HybridGroupCommand
    ]:
        def decorator(func: AsyncCallable[..., Any]) -> GroupCommand:
            from app.core.helpers import group

            kwargs.setdefault('parent', self)
            result = group(*args, **kwargs)(func)
            self.add_command(result)
            return result

        return decorator


@discord.utils.copy_doc(commands.HybridGroup)
class HybridGroupCommand(GroupCommand, commands.HybridGroup):
    def define_app_command(self, **kwargs: Any) -> Callable[[AsyncCallable[..., Any]], None]:
        def decorator(func: AsyncCallable[..., Any]) -> None:
            guild_ids = kwargs.pop('guild_ids', None) or getattr(
                self.callback, '__discord_app_commands_default_guilds__', None
            )
            guild_only = getattr(self.callback, '__discord_app_commands_guild_only__', False)
            default_permissions = getattr(self.callback, '__discord_app_commands_default_permissions__', None)
            nsfw = getattr(self.callback, '__discord_app_commands_is_nsfw__', False)
            allowed_installs = getattr(
                self.callback,
                '__discord_app_commands_installation_types__',
                discord.app_commands.AppInstallationType(user=True, guild=True),
            )
            allowed_contexts = getattr(
                self.callback,
                '__discord_app_commands_contexts__',
                discord.app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True),
            )
            self.app_command = app_commands.Group(
                name=self._locale_name or self.name,
                description=self._locale_description or self.description or self.short_doc or '…',
                guild_ids=guild_ids,
                guild_only=guild_only,
                default_permissions=default_permissions,
                nsfw=nsfw,
                allowed_installs=allowed_installs,
                allowed_contexts=allowed_contexts,
            )

            parent = kwargs.pop('parent', None)
            if self.parent is not None:
                if isinstance(self.parent, commands.HybridGroup):
                    parent = self.parent.app_command

            # This prevents the group from re-adding the command at __init__
            self.app_command.parent = parent
            self.app_command.module = self.module

            if self.fallback is not None:
                command = _AppCommandOverride(
                    self,
                    name=self.fallback,
                    description=self.app_command.description,
                    parent=self.app_command,
                    callback=func,  # type: ignore
                    **kwargs,
                )
                self.app_command.add_command(command)
                self.app_command_name = command.qualified_name

        return decorator
