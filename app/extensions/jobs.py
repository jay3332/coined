from __future__ import annotations

import asyncio
import datetime
import random
from copy import deepcopy
from typing import Annotated

import discord
from discord import app_commands
from discord.ext.commands import BadArgument
from discord.utils import format_dt

from app.core import ERROR, Cog, Context, REPLY, group, user_max_concurrency
from app.core.helpers import EPHEMERAL
from app.data.items import Items
from app.data.jobs import Job, Jobs, MinigameFailure
from app.data.pets import Pets
from app.data.quests import QuestTemplates
from app.extensions.profit import Profit
from app.util.common import (
    expansion_list,
    get_by_key, humanize_duration,
    image_url_from_emoji,
    pluralize,
    query_collection,
    query_collection_many,
    walk_collection,
)
from app.util.pagination import ActiveItem, Formatter, Paginator
from app.util.types import CommandResponse, TypedInteraction
from app.util.views import FollowUpButton, StaticCommandButton, invoke_command
from config import Colors, Emojis


def query_job(query: str) -> Job:
    if job := query_collection(Jobs, Job, query):
        return job
    raise BadArgument(f'Job {query!r} not found.')


class JobTransformer(app_commands.Transformer):
    @classmethod
    async def convert(cls, _, argument: str) -> Job:
        return query_job(argument)

    async def transform(self, _, argument: str) -> Job:
        return query_job(argument)

    async def autocomplete(self, _, current: str) -> list[app_commands.Choice]:
        return [
            app_commands.Choice(name=job.name, value=job.key)
            for job in query_collection_many(Jobs, Job, current)
        ]


class ActiveJobSelect(discord.ui.Select, ActiveItem):
    def __init__(self, ctx: Context) -> None:
        super().__init__(placeholder='Apply for a job...', row=0)
        self.ctx = ctx

    async def active_update(self, paginator: Paginator, jobs: list[Job]) -> None:
        self.options = [
            discord.SelectOption(label=job.name, value=job.key, emoji=job.emoji)
            for job in jobs
        ]

    async def callback(self, interaction: TypedInteraction) -> None:
        await invoke_command(
            self.ctx.bot.get_command('work apply'),  # type: ignore
            interaction, args=(), kwargs={'job': get_by_key(Jobs, self.values[0])},
        )


class JobListFormatter(Formatter):
    def __init__(self, embed: discord.Embed, jobs: list[Job], *, per_page: int = 5) -> None:
        self.embed: discord.Embed = embed
        super().__init__(jobs, per_page=per_page)

    async def format_page(self, paginator: Paginator, jobs: list[Job]) -> discord.Embed:
        embed = discord.Embed.from_dict(deepcopy(self.embed.to_dict()))
        for job in jobs:
            expanded = [f'\N{ALARM CLOCK} Cooldown: **{humanize_duration(job.cooldown.total_seconds())}**']
            if job.work_experience_required:
                expanded.append(f'\N{CRYSTAL BALL} Experience Required: {job.work_experience_required:,} total shifts')
            if job.intelligence_required:
                expanded.append(f'\N{BRAIN} Intelligence Required: **{job.intelligence_required:,} IQ**')

            record = await paginator.ctx.db.get_user_record(paginator.ctx.author.id)
            lock = (
                '\N{LOCK}'
                if record.work_experience < job.work_experience_required or record.iq < job.intelligence_required
                else '\N{OPEN LOCK}'
            )
            embed.add_field(
                name=f'**{job.display}** \u2014 {Emojis.coin} **{job.base_salary:,}** per shift {lock}',
                value=expansion_list(expanded),
                inline=False,
            )

        return embed


class JobsCog(Cog, name='Jobs'):
    """Commands related to jobs and job management."""

    emoji = '\U0001f4bc'

    def _prepare_find_job_error(self, ctx: Context) -> tuple[str, discord.ui.View]:
        view = discord.ui.View()
        view.add_item(StaticCommandButton(
            command=self.job_list, label='Find me a job', style=discord.ButtonStyle.primary, emoji='\U0001f50e',  # type: ignore
        ))
        mention = ctx.bot.tree.get_app_command('work list').mention
        return mention, view

    @group(
        aliases={'wo', 'job', 'jobs', 'j', 'occupation', 'jo'},
        hybrid=True, fallback='start', expand_subcommands=True,
    )
    @user_max_concurrency(1)
    async def work(self, ctx: Context) -> CommandResponse:
        """Work your job and earn some money."""
        if ctx.invoked_with == 'job':
            return await ctx.invoke(self.job_view)  # type: ignore
        elif ctx.invoked_with == 'jobs':
            return await ctx.invoke(self.job_list)  # type: ignore

        record = await ctx.db.get_user_record(ctx.author.id)
        if record.job is None:
            mention, view = self._prepare_find_job_error(ctx)
            return f"You aren't hired yet. Get a job first! ({mention})", view, ERROR

        expires_at = record.job.cooldown_expires_at
        if expires_at and ctx.now < expires_at:
            return f'You recently worked your shift. You can work again {format_dt(expires_at, "R")}.', ERROR

        if record.alcohol_expiry:
            return (
                'You are too drunk to work right now. (You can\'t work while you have an active '
                f'{Items.alcohol.display_name} boost)',
                ERROR,
            )

        info = record.job.job
        minigame = info.random_minigame()

        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now, title=f'Working as {info.chunk}')
        embed.set_author(name=f'{ctx.author.name}: Work', icon_url=ctx.author.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji(info.emoji))

        breakdown = []
        multiplier_mention = ctx.bot.tree.get_app_command('multiplier').mention
        coin_multiplier = record.coin_multiplier_in_ctx(ctx)
        disclaimer = f' with +{coin_multiplier - 1:.1%} {multiplier_mention}' if coin_multiplier > 1 else ''

        extra = (
            f'\n{Emojis.Expansion.standalone} *This is a harder task!* '
            f'You will get {minigame.multiplier - 1:.0%} more coins.'
        ) if minigame.multiplier > 1 else ''

        salary = round(record.job.salary * minigame.multiplier)
        difference = salary - record.job.salary
        if difference > 0:
            breakdown.append(f'+{minigame.multiplier - 1:.1%} Minigame Multiplier: {Emojis.coin} **+{difference:,.0f}**')

        embed.description = (
            f'Expected salary{disclaimer}: {Emojis.coin} **{salary * coin_multiplier:,.0f}**{extra}\n'
            'To work, complete the minigame below'
        )

        await ctx.reply(f'{Emojis.loading} Working as {info.chunk}...')
        shortcuts = await Profit._get_command_shortcuts(ctx, record)

        await asyncio.sleep(random.uniform(2.0, 4.0))
        expiry = ctx.now + info.cooldown
        try:
            message = await minigame.callback(ctx, embed, info)
        except MinigameFailure as exc:
            async with ctx.db.acquire() as conn:
                await record.add(job_fails=1, connection=conn)
                await record.update(job_cooldown_expires_at=expiry, connection=conn)
                await record.add_random_exp(8, 15, ctx=ctx, connection=conn)
            return str(exc), shortcuts, REPLY

        message = message or ctx._message
        raise_amount = 0 if (record.job.hours + 1) % 5 else info.base_salary // 10
        quests = await record.quest_manager.wait()

        async with ctx.db.acquire() as conn:
            await record.add(job_hours=1, work_experience=1, job_salary=raise_amount, connection=conn)
            await record.update(job_cooldown_expires_at=expiry, connection=conn)
            await record.add_random_exp(12, 20, ctx=ctx, connection=conn)
            raise_text = (
                f'\n\u2934\ufe0f **You got a raise!** Your base salary is now {Emojis.coin} **{record.job.salary:,}**.'
                if raise_amount else ''
            )
            if raise_amount:
                if quest := quests.get_active_quest(QuestTemplates.work_raises):
                    await quest.add_progress(1, connection=conn)
            if quest := quests.get_active_quest(QuestTemplates.work_successes):
                await quest.add_progress(1, connection=conn)

            pets = await record.pet_manager.wait()
            if duck := pets.get_active_pet(Pets.duck):
                multiplier = 0.02 + duck.level * 0.005
                if extra := round(salary * multiplier):
                    salary += extra
                    breakdown.append(f'{Pets.duck.display}: {Emojis.coin} **+{extra:,}**')

            profit = await record.add_coins(salary, ctx=ctx, connection=conn)
            extra = profit - salary
            if extra:
                multiplier = coin_multiplier - 1
                breakdown.append(
                    f'+{multiplier:.1%} Coin Multiplier ({multiplier_mention}): {Emojis.coin} **+{extra:,}**',
                )

            item_text = ''
            item = random.choices(list(info.items), weights=list(info.items.values()))[0]
            if item:
                await record.inventory_manager.add_item(item, connection=conn)
                item_text = f' and {item.get_sentence_chunk()}'

        view = discord.ui.View()
        view.add_item(StaticCommandButton(
            command=self.job_view, label='View Job', style=discord.ButtonStyle.primary, emoji=self.emoji,  # type: ignore
        ))
        view.add_item(FollowUpButton(
            f'### {ctx.author.mention}\'s Profit Breakdown from working\n'
            f'{Emojis.coin} **+{record.job.salary:,}** (base salary)\n{expansion_list(breakdown)}',
            label='View Breakdown', style=discord.ButtonStyle.primary, emoji='\N{MONEY BAG}',
        ))
        for child in shortcuts.children:
            view.add_item(child)

        if ctx.interaction and ctx.interaction.is_user_integration():
            method = ctx.interaction.followup.send
        else:
            method = message.reply
        await method(
            content=f'{info.emoji} **SUCCESS!** You work as {info.chunk} and earn {Emojis.coin} **{profit:,}**{item_text}!' + raise_text,
            view=view,
        )

    @work.command('view', aliases={'v', 'me'}, hybrid=True)
    async def job_view(self, ctx: Context) -> CommandResponse:
        """View information regarding your current job."""
        record = await ctx.db.get_user_record(ctx.author.id)
        if record.job is None:
            mention, view = self._prepare_find_job_error(ctx)
            return f"You don't have a job yet. Get one first! ({mention})", view, ERROR

        job = record.job.job
        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.set_author(name=f'{ctx.author.name}: Job', icon_url=ctx.author.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji(job.emoji))
        embed.description = f'You currently work as {job.chunk_display}.\n*{job.description}*'
        embed.add_field(name='Salary', value=f'{Emojis.coin} **{record.job.salary:,}**')
        embed.add_field(
            name='Work Hours',
            value=f'**{record.job.hours:,}** (Total: {record.work_experience:,})',
        )
        total = record.job.hours + record.job.fails
        s = '' if record.job.fails == 1 else 's'
        embed.add_field(
            name='Failed Attempts',
            value=(
                (
                    f'{record.job.fails:,} failed attempt{s} (out of {total:,})\n'
                    f'**{1 - record.job.fails / total:.2%} success rate**'
                )
                if total else 'No attempts yet...\nWork to get started!'
            )
        )

        available = record.job.cooldown_expires_at is None or record.job.cooldown_expires_at <= ctx.now
        mention = ctx.bot.tree.get_app_command('work start').mention
        embed.add_field(
            name='Work Cooldown',
            value=(
                  f'\N{ALARM CLOCK} You can work now!\nRun {mention} to work' if available
                  else (
                      f'\N{ALARM CLOCK} Work again {format_dt(record.job.cooldown_expires_at, "R")}\n'
                      f'{Emojis.Expansion.standalone} Cooldown: {humanize_duration(job.cooldown.total_seconds())}'
                  )
            ),
        )
        # raise every 5 hours
        raise_in = (record.job.hours // 5 + 1) * 5 - record.job.hours
        embed.add_field(name='Raise', value=pluralize(f'Next raise in {raise_in} shift(s)'))

        view = discord.ui.View()
        view.add_item(StaticCommandButton(
            command=self.work, label='Work', style=discord.ButtonStyle.primary, emoji=self.emoji,
            disabled=not available,
        ))
        return embed, view, REPLY

    @work.command('list', aliases={'l', 'ls', 'offers', 'offerings', 'find'}, hybrid=True)
    async def job_list(self, ctx: Context) -> CommandResponse:
        """View available job offerings."""
        record = await ctx.db.get_user_record(ctx.author.id)
        embed = discord.Embed(
            color=Colors.primary,
            timestamp=ctx.now,
            description=f'You have worked **{record.work_experience:,}** total shifts.',
        )
        embed.set_author(name=f'{ctx.author.name}: Job Offerings', icon_url=ctx.author.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji(self.emoji))

        formatter = JobListFormatter(
            embed,
            list(sorted(walk_collection(Jobs, Job), key=lambda job: job.base_salary)),
            per_page=5,
        )
        return Paginator(ctx, formatter, other_components=[ActiveJobSelect(ctx)]), REPLY

    @work.command('info', aliases={'i', 'details'}, hybrid=True)
    async def job_info(self, ctx: Context, *, job: Annotated[Job, JobTransformer]) -> CommandResponse:
        """See details about a specific job."""
        embed = discord.Embed(color=Colors.primary, title=job.display, description=job.description, timestamp=ctx.now)
        embed.set_author(name=f'{ctx.author.name}: Job Info', icon_url=ctx.author.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji(job.emoji))

        embed.add_field(name='Base Salary', value=f'{Emojis.coin} **{job.base_salary:,}**')
        embed.add_field(name='Work Cooldown', value=humanize_duration(job.cooldown.total_seconds()))

        expanded = []
        if job.work_experience_required:
            expanded.append(f'\N{CRYSTAL BALL} Experience Required: {job.work_experience_required:,} total shifts')
        if job.intelligence_required:
            expanded.append(f'\N{BRAIN} Intelligence Required: **{job.intelligence_required:,} IQ**')
        if expanded:
            embed.add_field(name='Requirements', value=expansion_list(expanded))

        embed.add_field(name='Minigames', value=', '.join(m.name for m in job.minigames), inline=False)
        if job.keywords:
            embed.add_field(name='Keywords', value=', '.join(job.keywords), inline=False)
        return embed, REPLY

    @work.command('apply', aliases={'set', 'a'}, hybrid=True)
    @app_commands.describe(job='The job you want to apply for.')
    async def job_apply(self, ctx: Context, *, job: Annotated[Job, JobTransformer]) -> CommandResponse:
        """Apply for a new job."""
        record = await ctx.db.get_user_record(ctx.author.id)
        if record.job is not None:
            view = discord.ui.View()
            view.add_item(StaticCommandButton(
                command=self.job_resign, label='Resign', style=discord.ButtonStyle.danger, emoji='\U0001f4e4',  # type: ignore
            ))
            return f'You\'re already working as {record.job.job.chunk_display}!', view, ERROR

        if record.job_switch_cooldown_expiry and record.job_switch_cooldown_expiry > ctx.now:
            return (
                f'You\'re switching jobs too fast! '
                f'You can switch jobs again {format_dt(record.job_switch_cooldown_expiry, "R")}.',
                ERROR,
            )

        if record.work_experience < job.work_experience_required:
            return (
                f'**You don\'t have enough work experience to apply for this job!** '
                f'You need **{job.work_experience_required:,}** total shifts of work experience, '
                f'but you only have {record.work_experience:,}.',
                ERROR,
            )

        if record.iq < job.intelligence_required:
            trivia_mention = ctx.bot.tree.get_app_command('trivia').mention
            return (
                f'**You don\'t have enough IQ to apply for this job!** '
                f'You need **{job.intelligence_required:,}** IQ, but you only have {record.iq:,} IQ.\n'
                f'-# You can increase your IQ by playing {trivia_mention}.\n',
                ERROR,
            )
        
        await record.update(
            job=job.key,
            job_salary=job.base_salary,
            job_hours=0,
            job_fails=0,
            job_cooldown_expires_at=None,
            job_switch_cooldown_expires_at=ctx.now + datetime.timedelta(hours=6),
        )
        view = discord.ui.View()
        view.add_item(StaticCommandButton(
            command=self.work, label='Start Working', style=discord.ButtonStyle.primary, emoji=self.emoji,
        ))
        return f'**Hired!** You now work as {job.chunk_display}!', view, REPLY

    @work.command('resign', aliases={'retire', 'res', 'r'}, hybrid=True)
    async def job_resign(self, ctx: Context) -> CommandResponse:
        """Resign from your current job."""
        record = await ctx.db.get_user_record(ctx.author.id)
        if record.job is None:
            return 'You don\'t have a job to resign from!', ERROR

        if not await ctx.confirm(
            f'Are you sure you want to resign from your job as {record.job.job.chunk_display}?\n'
            + (
                f'{Emojis.Expansion.standalone} You have worked {record.job.hours:,} shifts in this job'
                + (
                    f' and accumulated {Emojis.coin} **{record.job.salary - record.job.job.base_salary:,}** in raises'
                    if record.job.hours >= 5 else ''
                )
                + '. You will lose these if you resign.'
                if record.job.hours > 0 else ''
            ),
            delete_after=True,
        ):
            return 'Alright, we will resign another day then', REPLY, EPHEMERAL

        display = record.job.job.chunk_display
        await record.update(job=None, job_salary=None, job_cooldown_expires_at=None, job_hours=None, job_fails=None)
        return f'You have resigned from your job as {display}.', REPLY


setup = JobsCog.simple_setup
