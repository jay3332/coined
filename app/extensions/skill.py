from __future__ import annotations

import asyncio
import datetime
import random
from textwrap import dedent
from typing import Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.utils import format_dt

from app.core import BAD_ARGUMENT, Cog, Context, REPLY, command, group, lock_transactions, simple_cooldown, \
    user_max_concurrency
from app.data.skills import Skill as SkillObject, Skills, TrainingFailure
from app.util.common import humanize_duration, image_url_from_emoji, query_collection_many, walk_collection
from app.util.converters import query_skill
from app.util.pagination import FieldBasedFormatter, Paginator
from app.util.types import TypedInteraction
from config import Colors, Emojis

if TYPE_CHECKING:
    from app.database import SkillInfo, UserRecord


class Skill(Cog):
    """Commands for the skill and training system."""

    emoji = '\U0001f52e'

    # noinspection PyTypeChecker
    @group(aliases={'skill', 'sk'}, hybrid=True, expand_subcommands=True)
    @simple_cooldown(2, 2)
    async def skills(self, ctx: Context, *, skill: query_skill = None) -> tuple[Paginator, Any] | None:
        """View a dashboard of all of your skills, or information on a specific skill."""
        if skill is not None:
            await ctx.invoke(self.skills_view, skill=skill)
            return

        record = await ctx.db.get_user_record(ctx.author.id)
        skills = await record.skill_manager.wait()

        fields = []

        for skill in walk_collection(Skills, SkillObject):
            if skill_record := skills.get_skill(skill):
                s = 's' if skill_record.points != 1 else ''
                fields.append({
                    'name': f'**{skill.display}** \u2014 {skill_record.points:,} Skill Point{s}',
                    'value': dedent(f"""
                        {skill.description}
                        -# {Emojis.Expansion.first} {skill.benefit(skill_record.points)}
                        -# {Emojis.Expansion.last} *Train this skill by running `{ctx.clean_prefix}train {skill.key}`.*
                    """),
                    'inline': False,
                })
            elif record.level < skill.level_unlocked:
                fields.append({
                    'name': f'{skill.display} (Unlocked at Level {skill.level_unlocked})',
                    'value': (
                        f'{skill.description}\n-# {skill.benefit_per_point}\n'
                        '-# *You do not meet the level requirement to unlock this skill.*'
                    ),
                    'inline': False
                })
            else:
                if record.wallet < skill.price:
                    value = 'You cannot afford to unlock this skill.'
                else:
                    value = f'Buy this skill by running `{ctx.clean_prefix}skill buy {skill.key}`.'

                fields.append({
                    'name': f'{skill.display} (Unlock for {Emojis.coin} {skill.price:,})',
                    'value': f'{skill.description}\n-# {skill.benefit_per_point} per point\n-# *{value}*',
                    'inline': False,
                })

        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.set_thumbnail(url=image_url_from_emoji(self.emoji))
        embed.set_author(name=f'{ctx.author.name}\'s Skills', icon_url=ctx.author.avatar)
        embed.description = f'You have unlocked {len(skills.cached):,} skills.'

        return Paginator(ctx, FieldBasedFormatter(embed, fields, per_page=3)), REPLY

    @skills.app_command.command(name='list', description='View a list of all available skills')
    async def skills_list(self, interaction: TypedInteraction) -> None:
        ctx = await self.bot.get_context(interaction)
        ctx.command = self.skills

        if not await self.skills.can_run(ctx):
            return
        await ctx.invoke(self.skills)

    @staticmethod
    def get_maximum_skill_points(record: UserRecord, skill_record: SkillInfo) -> int:
        acc = None

        for points, level in skill_record.into_skill().level_requirement_mapping.items():
            if record.level >= level:
                acc = points
            else:
                return acc

        return skill_record.into_skill().max_points

    @skills.command('view', aliases={'i', 'info'}, hybrid=True)
    @app_commands.describe(skill='The skill to view information on.')
    @simple_cooldown(2, 2)
    async def skills_view(self, ctx: Context, *, skill: query_skill) -> tuple[discord.Embed, Any]:
        """View information on a specific skill."""
        record = await ctx.db.get_user_record(ctx.author.id)
        skills = await record.skill_manager.wait()

        embed = discord.Embed(color=Colors.primary, description=skill.description, title=skill.name, timestamp=ctx.now)
        embed.set_author(name=f'Skill: {skill.name}', icon_url=ctx.author.avatar)
        embed.set_thumbnail(url=image_url_from_emoji(skill.emoji))

        embed.add_field(name='General', value=dedent(f"""
            Name: **{skill.name}**
            Query Key: **`{skill.key}`**
            Maximum Skill Points: **{skill.max_points or 'Unlimited'}**
            Training Cooldown: **{humanize_duration(skill.training_cooldown)}**
        """))

        embed.add_field(name='Requirements', value=dedent(f"""
            Level Unlocked: **{skill.level_unlocked:,}**
            Price to Unlock: {Emojis.coin} **{skill.price:,}**
        """))

        if skill_record := skills.get_skill(skill):
            embed.add_field(name=f'{self.emoji} Benefit', value=skill.benefit(skill_record.points), inline=False)
            if skill_record.cooldown_until and ctx.now < skill_record.cooldown_until:
                embed.add_field(
                    name='\u23f0 Training Cooldown',
                    value=f'You may train again {format_dt(skill_record.cooldown_until, "R")}',
                    inline=False,
                )
            else:
                embed.add_field(
                    name='\u23f0 Train this Skill',
                    value=f'Run `{ctx.clean_prefix}train {skill.key}` to train this skill.',
                    inline=False,
                )

            embed.add_field(name='Skill Points', value=f'{skill_record.points:,}')
            embed.add_field(name='Maximum Skill Points', value=f'{self.get_maximum_skill_points(record, skill_record):,}')

            embed.set_footer(text='Maximum skill points scale with level.')
        else:
            embed.description += '\n*You have not unlocked this skill yet.*'
            embed.add_field(name=f'{self.emoji} Benefit', value=skill.benefit_per_point + ' per point', inline=False)

        return embed, REPLY

    @skills.command('buy', aliases={'purchase', 'b', 'unlock'}, hybrid=True)
    @app_commands.describe(skill='The skill to buy.')
    @simple_cooldown(1, 6)
    @user_max_concurrency(1)
    @lock_transactions
    async def skills_buy(self, ctx: Context, *, skill: query_skill) -> tuple[discord.Embed | str, Any]:
        """Purchase a skill."""
        record = await ctx.db.get_user_record(ctx.author.id)
        skills = await record.skill_manager.wait()

        if skills.has_skill(skill):
            return 'You have already unlocked this skill.', BAD_ARGUMENT

        if record.level < skill.level_unlocked:
            return 'You do not meet the level requirement in order unlock this skill.', BAD_ARGUMENT

        if record.wallet < skill.price:
            return 'Insufficient funds: You do not have enough coins to purchase this skill.', BAD_ARGUMENT

        if not await ctx.confirm(
            f'Are you sure you want to buy the **{skill.name}** skill for {Emojis.coin} **{skill.price:,}**? '
            'You will not be able to refund this transaction.',
            reference=ctx.message,
            delete_after=True,
        ):
            return 'Purchase cancelled.', REPLY

        async with ctx.db.acquire() as conn:
            await record.add(wallet=-skill.price, connection=conn)
            await skills.add_skill(skill, connection=conn)

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.set_author(name='Purchase Successful', icon_url=ctx.author.display_avatar)
        embed.description = f'Successfully unlocked the **{skill.name}** skill for {Emojis.coin} {skill.price:,}.'

        return embed, REPLY

    @skills.command('issue', hidden=True, hybrid=True, with_app_command=False)
    async def skill_issue(self, _: Context) -> tuple[str, Any]:
        """skill issue"""
        return 'https://tenor.com/view/skillissue-skill-issue-gif-22125481', REPLY

    @command('train', aliases={'t', 'tr'}, hybrid=True)
    @app_commands.describe(skill='The skill to train.')
    @user_max_concurrency(1)
    async def train(self, ctx: Context, *, skill: query_skill) -> Any:
        """Train a skill. You must have unlocked the skill first."""
        record = await ctx.db.get_user_record(ctx.author.id)
        skills = await record.skill_manager.wait()

        if not skills.has_skill(skill):
            yield 'You have not unlocked this skill yet.', BAD_ARGUMENT
            return

        skill_record = skills.get_skill(skill)
        maximum = self.get_maximum_skill_points(record, skill_record)

        if skill_record.points >= maximum:
            if skill.max_points <= maximum:
                yield 'You have already reached the maximum skill points for this skill.', BAD_ARGUMENT
            else:
                yield 'You cannot train this skill any further as your level is too low for it.', BAD_ARGUMENT
            return

        if skill_record.cooldown_until and ctx.now < skill_record.cooldown_until:
            yield (
                f'You may train this skill again {format_dt(skill_record.cooldown_until, "R")}.',
                BAD_ARGUMENT,
            )
            return

        yield f'{Emojis.loading} Training {skill.name}...', REPLY
        await asyncio.sleep(random.uniform(2, 4))

        try:
            await skill.run_training(ctx)
        except TrainingFailure as exc:
            embed = discord.Embed(color=Colors.error, timestamp=ctx.now)
            embed.set_author(name='Training Failed', icon_url=ctx.author.display_avatar)
            embed.add_field(name='You failed training!', value=str(exc))

            yield embed, REPLY
            return
        finally:
            await skills.add_skill_cooldown(skill, datetime.timedelta(seconds=skill.training_cooldown))

        await skills.add_skill_points(skill, 1)

        # it doesn't update in place due to the fact that they're namedtuples
        skill_record = skills.get_skill(skill)

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.set_author(name='Training Successful', icon_url=ctx.author.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji(skill.emoji))

        embed.description = dedent(f"""
            Successfully trained the **{skill.name}** skill.
            Added 1 skill point to this skill - you now have **{skill_record.points:,}** skill points for {skill.name}.
        """)

        embed.add_field(name=f'{self.emoji} Updated Benefit', value=skill.benefit(skill_record.points))

        yield embed, REPLY

    @skills_view.autocomplete('skill')
    @skills_buy.autocomplete('skill')
    @train.autocomplete('skill')
    async def autocomplete_skill(self, _, current: str):
        current = current.lower()
        return [
            app_commands.Choice(name=skill.name, value=skill.key)
            for skill in query_collection_many(Skills, SkillObject, current)
        ]


setup = Skill.simple_setup
