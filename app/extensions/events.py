from __future__ import annotations

import asyncio
import datetime
import functools
import json
import random
import re
from collections import defaultdict
from typing import Any, Final

import better_exceptions
import discord
from discord.ext import commands
from discord.ext.ipc import ClientPayload, Server
from discord.utils import format_dt

from app.core import Cog, Command, Context
from app.core.flags import FlagMeta
from app.core.helpers import ActiveTransactionLock, CURRENCY_COGS, GenericError
from app.data.events import EVENT_RARITY_WEIGHTS, Event, Events
from app.data.items import Items, VOTE_REWARDS
from app.database import NotificationData
from app.util.ansi import AnsiColor, AnsiStringBuilder
from app.util.common import cutoff, humanize_duration, pluralize, walk_collection
from app.util.converters import BadItemArgument, IncompatibleItemType
from app.util.views import StaticCommandButton
from config import Colors, errors_channel, guilds_channel, support_server, votes_channel


class EventsCog(Cog, name='Events'):
    __hidden__ = True

    def __setup__(self) -> None:
        self._channel_event_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._global_stats: dict[str, int] | None = None
        self._global_stats_expiry: datetime.datetime | None = None

    @discord.utils.cached_property
    def _cooldowns_remind_command(self) -> Any:
        return self.bot.get_command('cooldowns remind')

    @staticmethod
    async def _report_error(ctx: Context, error: Exception) -> None:
        generate = lambda f: "".join(f.format_exception(type(error), error, error.__traceback__))

        exception = (
            generate(better_exceptions.ExceptionFormatter(colored=True))
            .replace('\x1b[m', '\x1b[0m')  # hack for platform-specific color codes
        )
        if len(exception) > 1800:
            entry = await ctx.bot.cdn.paste(
                generate(better_exceptions.ExceptionFormatter(colored=False, pipe_char='|', cap_char='\\')),
                directory='coined_error_tracebacks',
            )
            exception = f'*Exception traceback was uploaded to {entry.paste_url}*'
        else:
            exception = f'```ansi\n{exception}\n```'

        report = (
            f'Uncaught error in command **{ctx.command.qualified_name}** ({format_dt(ctx.now, "R")}):\n{exception}'
        )
        embed = discord.Embed(
            color=Colors.error,
            description=f'User ID: {ctx.author.id} (account created {format_dt(ctx.author.created_at, "R")})',
            timestamp=ctx.now,
        )
        embed.set_author(name=ctx.author, icon_url=ctx.author.display_avatar)
        if itx := ctx.interaction:
            app_command_display = (
                ctx.bot.tree.get_app_command(itx.command.qualified_name).mention
                if itx.command else 'N/A (invoked via component)'
            )
            embed.add_field(
                name='Invoked via interaction',
                value=f'Interaction ID: {itx.id}\nApplication Command: {app_command_display}',
                inline=False,
            )
        else:
            embed.add_field(
                name=f'Invoked via prefix: `{discord.utils.escape_markdown(ctx.clean_prefix)}`',
                value=cutoff(ctx.message.content, 512),
                inline=False,
            )

        jump = f'Message ID: {ctx.message.id} ([Jump!]({ctx.message.jump_url}))'
        if ctx.guild:
            embed.add_field(
                name='Context',
                value=(
                    f'Guild: {ctx.guild.name} ({ctx.guild.id})\n'
                    f'Channel: {ctx.channel.mention} ({ctx.channel.id})\n{jump}'
                ),
                inline=False,
            )
        else:
            embed.add_field(name='Context', value=f'DM Channel ID: {ctx.channel.id}\n{jump}', inline=False)

        if ctx.args:
            args = '\n'.join(f'- {i}: `{arg!r}`' for i, arg in enumerate(ctx.args))
            embed.add_field(name='Command Args', value=cutoff(args, 512), inline=False)

        if ctx.kwargs:
            kwargs = '\n'.join(f'- `{k}`: `{v!r}`' for k, v in ctx.kwargs.items())
            embed.add_field(name='Command Kwargs', value=cutoff(kwargs, 256), inline=False)

        channel = ctx.bot.get_partial_messageable(errors_channel)
        heading = (
            f'\N{WARNING SIGN}\ufe0f **Error!** ({error})\n'
            'This was likely a bug on our end and usually it\'s not your fault.'
        )
        try:
            await channel.send(report, embed=embed)
        except BaseException as exc:
            await ctx.reply(
                f'{heading}\n\n**Note that an error occured while trying to report this exception!** ({exc})\n'
                f'Since no one could be notified of this error, please join our **support server** ({support_server}) '
                'and report this error **with extra context**.',
            )
        else:
            await ctx.reply(
                f'{heading}\n\n**This error has been automatically reported to the developers.**\n'
                f'*If this error persists, please join our **support server** ({support_server}) and report this error '
                '**with extra context**!*',
            )

    ERROR_PRIORITY = (IncompatibleItemType, BadItemArgument)

    @Cog.listener()
    async def on_command_error(self, ctx: Context, error: Exception) -> Any:
        # sourcery no-metrics
        error = getattr(error, 'original', error)

        if isinstance(error, commands.BadUnionArgument):
            for err in error.errors:
                if isinstance(err, self.ERROR_PRIORITY):
                    error = err
                    break
            else:
                error = error.errors[0]

        respond = functools.partial(ctx.send, reference=ctx.message, delete_after=30, ephemeral=True)

        if isinstance(error, (commands.MissingPermissions, commands.BotMissingPermissions)):
            return await respond(error)

        blacklist = (
            commands.CommandNotFound,
            commands.CheckFailure,
        )
        if isinstance(error, blacklist):
            return

        if isinstance(error, commands.BadArgument):
            view = None
            if isinstance(error, ActiveTransactionLock) and error.lock.jump_url is not None:
                view = discord.ui.View().add_item(
                    discord.ui.Button(label='Jump to Transaction', url=error.lock.jump_url),
                )

            ctx.command.reset_cooldown(ctx)
            if isinstance(error, GenericError):
                error.kwargs.setdefault('view', view)
                return await respond(**error.kwargs)

            return await respond(error, view=view)

        if isinstance(error, commands.MaxConcurrencyReached):
            # noinspection PyUnresolvedReferences
            return await respond(
                pluralize(f'Calm down there! This command can only be used {error.number} time(s) at once per {error.per.name}.'),
            )

        if isinstance(error, discord.NotFound) and error.code == 10062:
            return

        if isinstance(error, commands.CommandOnCooldown):
            command = ctx.command

            embed = discord.Embed(color=Colors.error, timestamp=ctx.now)
            embed.set_author(name='Command on cooldown!', icon_url=ctx.author.display_avatar)
            embed.description = getattr(command.callback, '__cooldown_message__', 'Please wait before using this command again.')

            default = pluralize(f'{error.cooldown.rate} time(s) per {humanize_duration(error.cooldown.per)}')

            embed.add_field(name='Try again after', value=humanize_duration(error.retry_after))
            embed.add_field(name='Default cooldown', value=default)

            view = None
            if error.retry_after > 30:
                view = discord.ui.View(timeout=60).add_item(
                    StaticCommandButton(
                        command=self._cooldowns_remind_command,
                        command_kwargs={'command': command},
                        label='Remind me when I can use this command again',
                        emoji='\u23f0',
                        style=discord.ButtonStyle.primary,
                    )
                )

            return await respond(embed=embed, view=view)

        if isinstance(error, (commands.ConversionError, commands.MissingRequiredArgument, commands.BadLiteralArgument)):
            ctx.command.reset_cooldown(ctx)
            param = ctx.current_parameter
        elif isinstance(error, commands.MissingRequiredArgument):
            param = error.param
        else:
            try:
                await self._report_error(ctx, error)
            finally:
                raise error

        builder = AnsiStringBuilder()
        builder.append('Attempted to parse command signature:').newline(2)
        builder.append('    ' + ctx.clean_prefix, color=AnsiColor.white, bold=True)

        if ctx.invoked_parents and ctx.invoked_subcommand:
            invoked_with = ' '.join((*ctx.invoked_parents, ctx.invoked_with))
        elif ctx.invoked_parents:
            invoked_with = ' '.join(ctx.invoked_parents)
        else:
            invoked_with = ctx.invoked_with

        builder.append(invoked_with + ' ', color=AnsiColor.green, bold=True)

        command = ctx.command
        signature = Command.ansi_signature_of(command)
        builder.extend(signature)
        signature = signature.raw

        if match := re.search(
            fr"[<\[](--)?{re.escape(param.name)}((=.*)?| [<\[]\w+(\.{{3}})?[>\]])(\.{{3}})?[>\]](\.{{3}})?",
            signature,
        ):
            lower, upper = match.span()
        elif isinstance(param.annotation, FlagMeta):
            param_store = command.params
            old = command.params.copy()

            flag_key, _ = next(filter(lambda p: p[1].annotation is command.custom_flags, param_store.items()))

            del param_store[flag_key]
            lower = len(command.raw_signature) + 1

            command.params = old
            del param_store

            upper = len(command.signature) - 1
        else:
            lower, upper = 0, len(command.signature) - 1

        builder.newline()

        offset = len(ctx.clean_prefix) + len(invoked_with)  # noqa
        content = f'{" " * (lower + offset + 5)}{"^" * (upper - lower)} Error occured here'
        builder.append(content, color=AnsiColor.gray, bold=True).newline(2)
        builder.append(str(error), color=AnsiColor.red, bold=True)

        if invoked_with != ctx.command.qualified_name:
            builder.newline(2)
            builder.append('Hint: ', color=AnsiColor.white, bold=True)

            builder.append('command alias ')
            builder.append(repr(invoked_with), color=AnsiColor.cyan, bold=True)
            builder.append(' points to ')
            builder.append(ctx.command.qualified_name, color=AnsiColor.green, bold=True)
            builder.append(', is this correct?')

        ansi = builder.ensure_codeblock().dynamic(ctx)
        await ctx.send(f'Could not parse your command input properly:\n{ansi}', reference=ctx.message, ephemeral=True)

    @Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Log minimal information about a guild to a private channel when the bot joins it, for security purposes only.

        What is logged:
        - Guild ID, name, and description
        - Guild owner ID and name
        - Member count

        This is outlined in the bot's privacy policy.
        """
        channel = self.bot.get_partial_messageable(guilds_channel)
        embed = discord.Embed(
            title=guild.name,
            description=guild.description,
            color=Colors.success,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=guild.icon)
        embed.set_author(name='Chat, we got a new guild', icon_url=self.bot.user.avatar)
        embed.set_footer(text=f'Now in {len(self.bot.guilds)} guilds')
        embed.add_field(
            name='Guild',
            value=f'ID: {guild.id}\nCreated {format_dt(guild.created_at, "R")} ({format_dt(guild.created_at)})',
            inline=False,
        )
        embed.add_field(
            name='Owner',
            value=(
                f'{guild.owner} ({guild.owner_id})\n'
                f'Account created {format_dt(guild.owner.created_at, "R")} ({format_dt(guild.owner.created_at)})'
            ),
            inline=False
        )
        embed.add_field(
            name='Member Count',
            value=f'Total: {guild.member_count}\nHumans: {sum(not m.bot for m in guild.members)}',
        )
        await channel.send(embed=embed)

    @Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        channel = self.bot.get_partial_messageable(guilds_channel)
        embed = discord.Embed(
            title=guild.name,
            color=Colors.error,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=guild.icon)
        embed.set_author(name='Chat, we were removed from a guild', icon_url=self.bot.user.avatar)
        embed.set_footer(text=f'Now in {len(self.bot.guilds)} guilds')
        embed.add_field(
            name='Guild',
            value=f'ID: {guild.id}\nCreated {format_dt(guild.created_at, "R")} ({format_dt(guild.created_at)})',
            inline=False,
        )
        await channel.send(embed=embed)

    @Server.route()
    async def dbl_vote(self, data: ClientPayload) -> None:
        """Handle a vote from top.gg"""
        record = await self.bot.db.get_user_record(data.user_id)
        inventory = await record.inventory_manager.wait()
        item = Items.epic_crate if data.is_weekend else Items.voting_crate

        async with self.bot.db.acquire() as conn:
            kwargs = (
                # reset monthly votes if the last vote was in a different month
                {} if record.last_dbl_vote is None or record.last_dbl_vote.month == discord.utils.utcnow().month
                else dict(votes_this_month=0)
            )
            await record.update(
                last_dbl_vote=datetime.datetime.fromisoformat(data.voted_at),
                connection=conn,
                **kwargs,
            )
            count = 2 if data.is_weekend else 1
            await record.add(votes_this_month=count, total_votes=count, connection=conn)

            if reward := (
                VOTE_REWARDS.get(milestone := record.votes_this_month)  # -> milestone
                or VOTE_REWARDS.get(milestone := record.votes_this_month - count + 1)  # for weekends: -> milestone + 1
            ):
                await reward.apply(record, connection=conn)

            await inventory.add_item(item, connection=conn)

            kwargs = reward.to_notification_data_kwargs() if reward else {}
            notification = NotificationData.Vote(item=item.key, milestone=reward and milestone, **kwargs)
            await record.notifications_manager.add_notification(notification, connection=conn)

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label='Vote for Coined', url=f'https://top.gg/bot/{self.bot.user.id}/vote'))
        weekend = (
            '\n\U0001f525 **Weekend Bonus:** Received an epic crate instead of a voting crate'
            if data.is_weekend else ''
        )
        reward = (
            f'\n\u2728 **Milestone Rewards** for hitting **{milestone} votes** this month:\n{reward}' if reward else ''
        )

        user = self.bot.get_user(data.user_id) or 'Unknown User'
        channel = self.bot.get_partial_messageable(votes_channel)
        await channel.send(
            f'{user} ({data.user_id}) just voted for the bot! '
            f'They received {item.get_sentence_chunk()} for their vote. Thank you!{weekend}{reward}',
            view=view,
        )

    @Server.route()
    async def global_stats(self, _) -> dict[str, int]:
        """Returns global statistics regarding Coined"""
        if self._global_stats_expiry and self._global_stats_expiry > discord.utils.utcnow():
            return self._global_stats

        await self.bot.db.wait()
        self._global_stats = {
            'users': len(self.bot.users),
            'guilds': len(self.bot.guilds),
            'coins': sum(record.wallet + record.bank for record in self.bot.db.user_records.values()),
        }
        self._global_stats_expiry = discord.utils.utcnow() + datetime.timedelta(minutes=10)
        return self._global_stats

    @Server.route()
    async def user_data(self, data: ClientPayload) -> dict[str, Any]:
        """Returns user-specific statistics"""
        def serializer(value):
            if isinstance(value, datetime.datetime):
                return value.timestamp()
            raise TypeError(f'Type {type(value)} is not serializable')

        def transform_pet_record(entry: Any) -> Any:
            entry = vars(entry).copy()
            del entry['manager']
            entry['pet'] = entry['pet'].key
            return entry

        user = self.bot.get_user(data.user_id)
        if not user:
            return {}

        record = await self.bot.db.get_user_record(data.user_id)
        await record.inventory_manager.wait()
        await record.skill_manager.wait()
        await record.pet_manager.wait()

        base = record.data.copy()
        base['user'] = user._to_minimal_user_json()
        base['inventory'] = {
            item.key: quantity for item, quantity in record.inventory_manager.cached.items() if quantity
        }
        base['skills'] = [skill._asdict() for skill in record.skill_manager.cached.values()]
        base['pets'] = [transform_pet_record(pet) for pet in record.pet_manager.cached.values()]
        return json.loads(json.dumps(base, default=serializer))  # inefficient hack

    @Cog.listener()
    async def on_command_completion(self, ctx: Context) -> Any:
        if not ctx.cog or ctx.cog.qualified_name not in CURRENCY_COGS or random.random() > 0.04:
            return

        lock = self._channel_event_locks[ctx.channel.id]
        if lock.locked():
            return

        old = ctx._message
        async with lock:
            rarity = random.choices(list(EVENT_RARITY_WEIGHTS), weights=list(EVENT_RARITY_WEIGHTS.values()))[0]
            if choices := [e for e in walk_collection(Events, Event) if e.rarity is rarity]:
                await random.choice(choices)(ctx)
        ctx._message = old


setup = EventsCog.simple_setup
