from __future__ import annotations

import asyncio
import datetime
import random
from collections import defaultdict, deque
from datetime import timedelta
from html import unescape
from textwrap import dedent
from typing import Any, Generic, Literal, NamedTuple, Sequence, TypeVar, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import BadArgument
from discord.utils import format_dt

from app.core import (
    BAD_ARGUMENT,
    Cog,
    Context,
    EDIT,
    HybridContext,
    NO_EXTRA, REPLY,
    command,
    database_cooldown,
    group, lock_transactions,
    simple_cooldown,
    user_max_concurrency
)
from app.core.helpers import EPHEMERAL, cooldown_message
from app.data.items import EnemyRef, FishingPoleMetadata, Item, ItemRarity, ItemType, Items
from app.data.pets import Pet, Pets
from app.data.quests import QUEST_PASS_CURVE, QUEST_PASS_REWARDS, QuestTemplates, reward_for_achieving_tier
from app.data.skills import RobberyTrainingButton
from app.database import NotificationData, QuestManager, QuestRecord, RobFailReason, UserRecord
from app.extensions.misc import _get_retry_after
from app.features.battles import PvEBattleView
from app.features.digging import DiggingView
from app.util.common import (
    expansion_list,
    humanize_list,
    image_url_from_emoji,
    progress_bar,
    weighted_choice,
)
from app.util.converters import CaseInsensitiveMemberConverter, Investment
from app.util.structures import LockWithReason
from app.util.views import AnyUser, StaticCommandButton, UserLayoutView, UserView
from config import Colors, Emojis

if TYPE_CHECKING:
    from app.util.types import CommandResponse, TypedInteraction


class SearchArea(NamedTuple):
    minimum: int
    maximum: int
    success_chance: float = 1
    death_chance_if_fail: float = 0
    success_responses: list[str] = []  # We can use a list literal here as these are defined as constants and will never be appended to.
    failure_responses: list[str] = []
    death_responses: list[str] = []
    items: dict[Item | None, float] = {None: 1}  # Similar situation with list literals


class CrimeData(NamedTuple):
    minimum: int
    maximum: int
    image: str = ''

    success_chance: float = 1
    death_chance_if_fail: float = 0
    success_responses: list[str] = []
    failure_responses: list[str] = []
    death_responses: list[str] = []

    item_chance: float = 0
    item_count: tuple[int, int] = 1, 1
    items: dict[Item, float] = {}


class SearchButton(discord.ui.Button['SearchView']):
    def __init__(self, name: str) -> None:
        super().__init__(label=name, style=discord.ButtonStyle.primary)

    async def callback(self, interaction: TypedInteraction) -> None:
        for button in self.view.children:
            if not isinstance(button, discord.ui.Button):
                continue

            button.style = discord.ButtonStyle.primary if button.label == self.label else discord.ButtonStyle.secondary
            button.disabled = True

        self.view.choice = self.label, self.view.mapping[self.label]
        await interaction.response.edit_message(view=self.view)
        self.view.stop()


T = TypeVar('T', SearchArea, CrimeData)
I = TypeVar('I')


class SearchView(UserView, Generic[T]):
    def __init__(self, ctx: Context, choices: list[str], mapping: dict[str, T]) -> None:
        super().__init__(ctx.author, timeout=30)
        self.ctx: Context = ctx

        for choice in choices:
            self.add_item(SearchButton(choice))

        self.choice: tuple[str, T] | None = None
        self.mapping: dict[str, T] = mapping

    async def on_timeout(self) -> None:
        await self.ctx.send('Timed out.', reference=self.ctx.message)


class RobData(NamedTuple):
    timestamp: datetime.datetime
    robbed_by: AnyUser
    victim: AnyUser
    amount: int


class TriviaQuestion(NamedTuple):
    category: str
    type: Literal['multiple', 'boolean']
    difficulty: Literal['easy', 'medium', 'hard']
    question: str
    correct_answer: str
    incorrect_answers: list[str]

    @property
    def answers(self) -> list[str]:
        if self.type == 'multiple':
            entities = [self.correct_answer] + self.incorrect_answers
            random.shuffle(entities)
            return entities

        return ['True', 'False']

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> TriviaQuestion:
        data['question'] = unescape(data['question'])
        data['correct_answer'] = unescape(data['correct_answer'])
        data['incorrect_answers'] = [unescape(answer) for answer in data['incorrect_answers']]

        return cls(**data)


def active_pet(pet: Pet, energy: int, verb: str):
    async def predicate(ctx: Context) -> bool:
        record = await ctx.db.get_user_record(ctx.author.id)
        pets = await record.pet_manager.wait()
        entry = pets.get_active_pet(pet)
        if not entry:
            if entry := pets.cached.get(pet):
                if not entry.equipped:
                    raise BadArgument(
                        f'You have a **{pet.display}**, but it is not equipped. '
                        f'Equip it with `{ctx.clean_prefix}pets equip {pet.key}`.',
                    )
                raise BadArgument(
                    f'Your **{pet.display}** does not have enough energy to {verb}! '
                    f'Feed it with `{ctx.clean_prefix}feed {pet.key}`.\n'
                    f'({Emojis.bolt} **{energy:,}** Energy required, but only {Emojis.bolt} {entry.energy} available)',
                )
            hunt_mention = ctx.bot.tree.get_app_command('hunt').mention
            raise BadArgument(
                f"You don't have a **{pet.display}** to {verb}! Hunt for one using {hunt_mention}"
            )
        return True

    return commands.check(predicate)


class Profit(Cog):
    """Commands you use to grind for profit."""

    emoji = '\U0001f4b0'

    def __setup__(self) -> None:
        self._recent_robs: dict[int, RobData] = {}
        self._trivia_questions: deque[TriviaQuestion] = deque(maxlen=50)
        self._trivia_questions_fetch_lock: asyncio.Lock = asyncio.Lock()

    BEG_INITIAL_MESSAGES = (
        "Alright, begging...",
        "Hold on, let me just beg *for you*...",
        "Begging...",
    )

    BEG_PEOPLE = (
        "jay3332",
        "your mother",
        "your father",
        "your left arm",
        "your right arm",
        "your left leg",
        "your right leg",
        "a worm",
        "a bird",
        "a homeless guy",
        "the president of the United States",
        "Joe Biden",
        "Donald Trump",
        "George Washington",
        "Barack Obama",
        "John Cena",
        "The Rock",
        "a rock",
        "Aagames",
        "an apple",
        "Tim Cook",
        "Steve Jobs",
        "Bill Gates",
        "Jeff Bezos",
        "Elon Musk",
        "a robot",
        "a cat",
        "a dog",
        "your nose",
        "me",
        "Jason Citron",
        'the popular video game "Among Us"',
        "a tree",
        "some guy on the street",
        "a businessman",
        "LeBron James",
        "Martin Luther King",
        "Bill Clinton",
        'Dwayne "The Rock" Johnson',
    )

    BEG_FAIL_MESSAGES = (
        "lol! {} didn't give you anything because they didn't feel like it.",
        "funny, {} told you to get a job.",
        "ouch, {} simply denied your request.",
        "{} does not give to homeless people. Kinda rude wouldn't you say?",
        "{}: go away you filthy beggar!",
        "{}: I don't have money either...",
        "{}: I'm broke too.",
        '{} says "YOU WISH" and then skidaddles away',
        'After a bit of consideration, {} decides to not give you anything.',
    )

    BEG_SUCCESS_MESSAGES = (
        "Cool, {0} gave you {1}.",
        "{0} handed you {1} without hesitation.",
        "Nice stuff, you received {1} from {0}.",
        "{0} scooped up {1} from the toilet and handed it to you.",
        "{0} made {1} magically appear into your possesion.",
        "{0} vomitted out {1} and gave it to you.",
        "{0} pulls out {1} from their rectum and gives it to you. Gross.",
        "{0}: Poor soul. Here's {1}",
        "{0} gave you {1} because they felt bad for you.",
        "{0} gave you {1} because they felt like it.",
        "{1} just fell from the sky. Just kidding, {0} gave it to you.",
        "After a bit of consideration, {0} finally decides to give you {1}.",
    )

    BEG_ITEMS = {
        Items.stick: 0.1,
        Items.padlock: 0.1,
        Items.cheese: 0.05,
        Items.banknote: 0.05,
        Items.common_crate: 0.03,
        Items.uncommon_crate: 0.005,
        Items.nineteen_dollar_fortnite_card: 0.001,
    }

    @staticmethod
    def _capitalize_first(s: str, /) -> str:
        if not len(s):
            return s

        return s[0].upper() + s[1:]

    _SHORTCUT_CANDIDATES: list[str] = ['beg', 'search', 'hunt', 'trivia', 'fish', 'dig', 'quests']
    _COOLDOWN_ONLY_CANDIDATES: list[str] = ['hourly', 'daily', 'weekly']
    _TOOL_MAPPING: dict[Item | tuple[Item, ...], str] = {
        Items.axe: 'chop',
    }

    @staticmethod
    def weighted_sample(population: Sequence[I], weights: Sequence[int], k: int = 1) -> list[I]:
        weights = list(weights)
        positions = range(len(population))
        indices = []
        while True:
            needed = k - len(indices)
            if not needed:
                break
            for i in random.choices(positions, weights, k=needed):
                if weights[i]:
                    weights[i] = 0.0
                    indices.append(i)
        return [population[i] for i in indices]

    @classmethod
    async def _get_command_shortcuts(cls, ctx: Context, record: UserRecord) -> discord.ui.View:
        candidates = cls._SHORTCUT_CANDIDATES[:]
        weights = [1] * len(candidates)

        # if no job or job cooldown is over, add job
        if record.job is None or record.job.cooldown_expires_at is None or record.job.cooldown_expires_at <= ctx.now:
            candidates.append('job')
            weights.append(1 if record.job is None else 3)

        # if user can vote, add vote
        vote_again = record.last_dbl_vote is None or record.last_dbl_vote + datetime.timedelta(hours=12) <= ctx.now
        if vote_again:
            candidates.append('vote')
            weights.append(2)

        inventory = await record.inventory_manager.wait()
        # if level 2+ OR lifesaver in inventory, add crime and dive
        if record.level >= 2 or inventory.cached.quantity_of(Items.lifesaver):
            candidates.extend(['crime', 'dive'])
            weights.extend([2, 2])

        # for every tool-based command, add the tool to the candidates
        for items, name in cls._TOOL_MAPPING.items():
            if isinstance(items, Item):
                items = (items,)
            if any(record.inventory_manager.cached.quantity_of(item) for item in items):
                candidates.append(name)
                weights.append(4)

        try:
            index = candidates.index(ctx.command.qualified_name)
            candidates.pop(index)
            weights.pop(index)
        except ValueError:
            pass

        candidates = [ctx.bot.get_command(name) for name in candidates]
        for i, candidate in enumerate(candidates):
            # less than 0.5 seconds in cooldown? favor this command
            if await _get_retry_after(ctx, candidate) <= 0.5:
                weights[i] *= 100

        for candidate in cls._COOLDOWN_ONLY_CANDIDATES:
            if await _get_retry_after(ctx, cmd := ctx.bot.get_command(candidate)) <= 0.5:
                candidates.append(cmd)
                weights.append(150)

        view = discord.ui.View(timeout=120)
        for cmd in cls.weighted_sample(candidates, weights, k=3):
            view.add_item(StaticCommandButton(label=f'/{cmd.qualified_name}', command=cmd, row=1))
        return view

    # noinspection PyTypeChecker
    @command(aliases={"plead"}, hybrid=True)
    @simple_cooldown(1, 15)
    @user_max_concurrency(1)
    async def beg(self, ctx: Context):
        """Beg for coins.

        There is a chance that you can get nothing, and a small chance that you can obtain some items.
        """
        yield f"{Emojis.loading} {random.choice(self.BEG_INITIAL_MESSAGES)}", REPLY
        person = random.choice(self.BEG_PEOPLE)

        embed = discord.Embed(timestamp=ctx.now)
        embed.set_author(name=f"Beg: {ctx.author}", icon_url=ctx.author.display_avatar)

        await asyncio.sleep(random.uniform(2, 4))

        record = await ctx.db.get_user_record(ctx.author.id)
        view = await self._get_command_shortcuts(ctx, record)
        await record.add_random_exp(4, 7, ctx=ctx)
        await record.add_random_bank_space(10, 15, chance=0.45)

        if random.random() < 0.4:
            embed.colour = Colors.error
            embed.description = self._capitalize_first(random.choice(self.BEG_FAIL_MESSAGES).format(f'**{person}**'))

            yield '', embed, view, EDIT
            return

        base = random.randint(150, 450)
        multiplier = 1
        multiplier_text = []
        item_chance = 0.06

        skills = await record.skill_manager.wait()
        if begging_skill := skills.get_skill('begging'):
            multiplier += (extra := begging_skill.points * 0.02)
            item_chance += begging_skill.points * 0.005
            multiplier_text.append(
                f'{begging_skill.into_skill().display} Skill: {Emojis.coin} **+{extra * base:,.0f}**',
            )

        pets = await record.pet_manager.wait()
        if dog := pets.get_active_pet(Pets.dog):
            multiplier += (extra := 0.01 + dog.level * 0.003)
            multiplier_text.append(f'{dog.pet.display}: {Emojis.coin} **+{extra * base:,.0f}**')

        if cow := pets.get_active_pet(Pets.cow):
            multiplier += (extra := 0.02 + cow.level * 0.005)
            multiplier_text.append(f'{cow.pet.display}: {Emojis.coin} **+{extra * base:,.0f}**')

        if record.coin_multiplier > 1:
            extra = record.coin_multiplier - 1
            multiplier_mention = ctx.bot.tree.get_app_command('multiplier').mention
            multiplier_text.append(
                f'+{extra:.1%} Coin Multiplier ({multiplier_mention}): {Emojis.coin} **+{extra * base:,.0f}**',
            )

        async with ctx.db.acquire() as conn:
            profit = await record.add_coins(base * multiplier, connection=conn)
            message = f'{Emojis.coin} **{profit:,}**'

            if random.random() < item_chance:
                item = random.choices(list(self.BEG_ITEMS), list(self.BEG_ITEMS.values()))[0]

                message += f' and {item.get_sentence_chunk(1)}'
                await record.inventory_manager.add_item(item, 1, connection=conn)

        embed.colour = Colors.success
        embed.description = self._capitalize_first(
            random.choice(self.BEG_SUCCESS_MESSAGES).format(person, message)
        )

        button = discord.ui.Button(label='View Breakdown', emoji='\U0001f4b0', style=discord.ButtonStyle.primary)

        async def callback(itx: TypedInteraction) -> None:
            await itx.response.send_message(
                f'### {ctx.author.mention}\'s Profit Breakdown from begging\n'
                f'{Emojis.coin} **+{base:,.0f}** (base profit)\n' + expansion_list(multiplier_text),
                ephemeral=True,
            )

        button.callback = callback
        view.add_item(button)

        yield '', embed, view, EDIT
        return

    @command(aliases={"investment", "iv", "in"}, hybrid=True)
    @simple_cooldown(1, 20)
    @user_max_concurrency(1)
    @lock_transactions
    async def invest(self, ctx: Context, *, amount: Investment()):
        """Invest your coins and potentially get more money.

        There is a chance that you could fail and lose your investment.

        Although this command carries gamble-like properties, it is not considered a gambling command, so alcohol is
        not applied to this command.
        """
        record = await ctx.db.get_user_record(ctx.author.id)
        await record.add_random_exp(4, 7, ctx=ctx)
        await record.add_random_bank_space(10, 15, chance=0.45)
        await record.add(wallet=-amount)

        def make_embed(c: int = Colors.primary) -> discord.Embed:
            e = discord.Embed(timestamp=ctx.now, color=c)
            # noinspection PyTypeChecker
            e.set_author(name=f"{ctx.author.name}'s Investment", icon_url=ctx.author.avatar)
            return e

        multiplier = 0
        yield f'{Emojis.loading} Please wait...', REPLY

        for _ in range(5):
            if random.random() > 0.15:
                multiplier += random.uniform(.13, .19)

                embed = make_embed()
                embed.description = f'{Emojis.loading} Investing...'

                embed.add_field(name="Earnings", value=dedent(f"""
                    {multiplier:,.1%} of initial value
                    {Emojis.Expansion.standalone} {Emojis.coin} +{round(amount * multiplier):,}
                """), inline=False)

                embed.add_field(name="Total Return", value=f"{Emojis.coin} {amount * (1 + multiplier):,.0f}")

                yield embed, EDIT

            else:
                embed = make_embed(Colors.error)
                embed.description = "You failed to invest properly. Lol."

                yield "", embed, EDIT
                return

            await asyncio.sleep(2)

        profit = await record.add_coins(round(amount * (1 + multiplier)))

        embed = make_embed(Colors.success)
        embed.description = 'Success! Your investment succeeded.'

        embed.add_field(name="Earnings", value=dedent(f"""
            {multiplier:,.1%} of initial value
            {Emojis.Expansion.standalone} {Emojis.coin} +{amount * multiplier:,.0f}
        """), inline=False)

        embed.add_field(name="Total Return", value=f"{Emojis.coin} {profit:,}")
        yield "", embed, EDIT

    SEARCH_AREAS = {
        'bathroom': SearchArea(
            minimum=200,
            maximum=320,
            success_chance=0.8,
            death_chance_if_fail=0.1,
            success_responses=[
                'You found {} in the toilet. Was it really worth it though?',
                'You found {} in the bathtub.',
                'You dug through the unflushed toilet and found {}. Disgusting you.'
            ],
            failure_responses=[
                'You put your hand deep in the toilet only to come out with no coins.',
                'You simply could not find anything in the bathroom.',
            ],
            death_responses=[
                'You got stuck in the toilet and drowned yourself - wtf?',
                'You drown in the bathtub, nice job.',
            ],
        ),
        'trash can': SearchArea(
            minimum=50,
            maximum=550,
            success_chance=0.65,
            success_responses=[
                'You now really stink, but at least you found {} in the trash can.',
                'You simply find {} in the trash can.'
            ],
            failure_responses=[
                'You got stuck in the trash can, lmao',
                'Not only do you stink now, but you found absolutely nothing in the trash can.',
            ],
            items={
                None: 0.94,
                Items.stick: 0.04,
                Items.cheese: 0.02,
                Items.dynamite: 0.02,
            }
        ),
        'car': SearchArea(
            minimum=100,
            maximum=500,
            success_chance=0.7,
            death_chance_if_fail=0.15,
            success_responses=[
                'You find {} inside of your car.',
                'You find {} on top of the passenger seat.',
            ],
            failure_responses=[
                'You try going to your car to find some coins, but then it hits you. You don\'t own a car! Silly you.',
                'You could not find anything inside of your __brand new__ car.',
            ],
            death_responses=[
                'You look under your car, but you left it in driving mode. Your car runs you over.',
                'You were held at gunpoint for driving a hijacked car. Reluctant to comply, you were shot and killed by the police.',
            ],
            items={
                None: 0.97,
                Items.banknote: 0.03,
                Items.key: 0.03,
                Items.dynamite: 0.01,
            },
        ),
        'bank': SearchArea(
            minimum=200,
            maximum=800,
            success_chance=0.45,
            death_chance_if_fail=0.4,
            success_responses=[
                'You find {} at the bank.',
                'You sneak into the bank at 3 in the morning. You find {} and get out without a trace.',
            ],
            failure_responses=[
                'Lol, the bank was closed.',
                'You did not find anything at the bank.',
            ],
            death_responses=[
                'You were caught breaking into the bank. You were shot and killed by the police.',
            ],
            items={
                None: 0.85,
                Items.banknote: 0.15,
                Items.dynamite: 0.1,
            },
        ),
        'house': SearchArea(  # credit: Clammerz
            minimum=200,
            maximum=700,
            success_chance=0.6,
            death_chance_if_fail=0.2,
            success_responses=[
                'You stole {} from the dresser.',
                'You found {} in the spare room.',
                'You stole {} from a childs piggy bank.',
                'Unexpectedly, their cat helped you find {}.',
                'You sneak into the house at 2 in the morning. You find {} and get out without causing a ruckus.',
            ],
            failure_responses=[
                'Lol, the doors and windows were locked.',
                'Their dog started barking and you swiftly ran away.',
                'The police caught you breaking in, but you got away just in time.',
                'You dropped a cereal bowl causing the owner to wake up, you got away before they saw you.',
            ],
            death_responses=[
                'You were caught breaking into someones house (in America). You were shot and killed by the owner.',
                'The police saw you breaking in; you weren\'t fast enough and ended up getting shot by the police.',
                'While scrounging for money, the owner knocked you out and tortured you til you met your demise.',
                'You punctured an artery on the broken window and bled out soon after.',
            ],
            items={
                None: 0.91,
                Items.padlock: 0.06,
                Items.banknote: 0.03,
            },
        ),
        'shoe': SearchArea(
            minimum=300,
            maximum=700,
            success_chance=0.44,
            death_chance_if_fail=0.03,
            success_responses=[
                'Your shoe had {} in it???',
                '{} was hiding in your shoe.',
                'There was {} in your shoe, must\'ve been uncomfortable.',
            ],
            failure_responses=[
                'There was nothing in your shoe.',
                'Why would there be money in your shoe?',
                'Your shoe isn\'t your wallet.',
                'Maybe ask your sock, it might have some coins.',
                'What were you expecting? It\'s your shoe not a bank.',
            ],
            death_responses=[
                'The shoe literally ate you.',
            ],
        ),
        'sock': SearchArea(
            minimum=200,
            maximum=500,
            success_chance=0.45,
            death_chance_if_fail=0.1,
            success_responses=[
                'Okay now, who put {} coins inside of your sock?',
                'Your sock was holding {} coins hostage.',
                'There was {} in your sock, how did you wear this thing..?',
                'You found {} inside of your sock. Yeah, I know - who *doesn\'t* put coins inside of their socks?',
            ],
            failure_responses=[
                'Sadly, your sock had no coins to offer.',
                'I wonder why there are no coins in a sock.',
                'Maybe ask your shoe, it might have some coins.',
                'Who puts coins in their socks?',
            ],
            death_responses=[
                'The sock captured you and fed you to the shoe.',
            ],
        ),
    }

    @command(aliases={'se', 'sch', 'scout'}, hybrid=True)
    @simple_cooldown(1, 20)
    @user_max_concurrency(1)
    async def search(self, ctx: Context):
        """Search for coins."""
        view: SearchView[SearchArea] = SearchView(ctx, random.sample(list(self.SEARCH_AREAS), 3), self.SEARCH_AREAS)
        yield f'\U0001f50d {ctx.author.mention}, Where would you like to search?', view, REPLY

        await view.wait()
        if not view.choice:
            return

        record = await ctx.db.get_user_record(ctx.author.id)
        await record.add_random_exp(10, 16, ctx=ctx)
        await record.add_random_bank_space(18, 24, chance=0.6)

        name, choice = view.choice
        embed = discord.Embed(timestamp=ctx.now)
        embed.set_author(name=f'Search: {ctx.author}', icon_url=ctx.author.display_avatar)
        embed.set_footer(text=f'Search area: {name}')

        accumulated = 0
        weights = choice.items.copy()
        pets = await record.pet_manager.wait()
        if dog := pets.get_active_pet(Pets.dog):
            accumulated += 0.01 + dog.level * 0.004
        if mouse := pets.get_active_pet(Pets.mouse):
            accumulated += 0.01 + mouse.level * 0.004
        if armadillo := pets.get_active_pet(Pets.armadillo):
            accumulated += 0.02 + armadillo.level * 0.005

        weights[None] *= 1 - accumulated
        cont = await self._get_command_shortcuts(ctx, record)

        if random.random() > choice.success_chance:
            embed.colour = Colors.error

            if random.random() < choice.death_chance_if_fail:
                cause = random.choice(choice.death_responses)
                await record.make_dead(reason=f'While searching for coins, {cause}')

                embed.add_field(name='You died!', value=cause)

                yield embed, cont, REPLY
                return

            message = random.choice(choice.failure_responses)
            embed.add_field(name='You found nothing!', value=message)

            yield embed, cont, REPLY
            return

        gain = random.randint(choice.minimum, choice.maximum)
        gain_multiplier = 1.0
        if cow := pets.get_active_pet(Pets.cow):
            gain_multiplier += 0.02 + cow.level * 0.005
        if weasel := pets.get_active_pet(Pets.weasel):
            gain_multiplier += 0.02 + weasel.level * 0.005

        gain *= gain_multiplier
        async with ctx.db.acquire() as conn:
            profit = await record.add_coins(gain, connection=conn)
            message = f'{Emojis.coin} **{profit:,}**'

            if item := random.choices(list(weights), weights=list(weights.values()))[0]:
                message += f' and {item.get_sentence_chunk(1)}'
                await record.inventory_manager.add_item(item, 1, connection=conn)

        embed.colour = Colors.success
        embed.add_field(name='Profit!', value=random.choice(choice.success_responses).format(message))

        yield embed, cont, REPLY

    CRIMES = {
        'shoplift': CrimeData(
            minimum=100,
            maximum=300,
            image='https://cdn.discordapp.com/attachments/935327142332465222/942470170562138202/Untitled352_20220213120854.png',
            success_chance=0.4,
            death_chance_if_fail=0.3,
            success_responses=[
                'You stole {} from the shop!',
                'You were caught stealing {} from the shop, but you got away just in time.',
            ],
            failure_responses=[
                'The store was closed, maybe try shoplifting when the store is open next time.',
                'You were caught stealing from the shop, but you got away just in time while having to drop your items.',
            ],
            death_responses=[
                'You were caught stealing from the shop and you were reluctant to comply with the police; so they shot you instead.',
                'You slipped on a banana peel while trying to run out of the shop and fell head first into concrete. You died.',
            ],
            item_chance=0.75,
            item_count=(1, 2),
            items={
                Items.cup: 1.1,
                Items.tomato: 1,
                Items.corn: 1,
                Items.bread: 0.8,
                Items.padlock: 0.7,
                Items.cheese: 0.6,
                Items.lifesaver: 0.5,
                Items.banknote: 0.15,
                Items.fishing_pole: 0.1,
            },
        ),
        'pickpocket': CrimeData(
            minimum=400,
            maximum=900,
            image='https://cdn.discordapp.com/attachments/935327142332465222/942470170348244992/Untitled352_20220213121146.png',
            success_chance=0.35,
            death_chance_if_fail=0.45,
            success_responses=[
                'You stealthily take {} out of the victim\'s pocket.',
                'You distract the victim and steal {} from their pocket.',
            ],
            failure_responses=[
                'The victim had nothing in their pocket, lol.',
                'You were caught stealing from the victim, but you got away just in time.',
            ],
            death_responses=[
                'The victim caught you trying to steal from them and shot you in the head in self-defense.',
                'You pickpocket a mine which explodes in your hand, killing you.'
            ],
            item_chance=0.4,
            items={
                Items.tobacco: 0.5,
                Items.padlock: 0.3,
                Items.key: 0.3,
                Items.banknote: 0.1,
            },
        ),
        'rob': CrimeData(
            minimum=500,
            maximum=800,
            image='https://cdn.discordapp.com/attachments/935327142332465222/942470172286001203/Untitled347_20220212181014.png',
            success_chance=0.4,
            death_chance_if_fail=0.3,
            success_responses=[
                'You robbed an old lady on the street for {}.',
                "You steal someone's paycheck which contained {}.",
            ],
            failure_responses=[
                'Maybe don\'t try robbing a bank with a banana next time.',
                'You tried robbing someone with a nerf gun, lol.',
            ],
            death_responses=[
                'You were caught robbing a bank and got shot by the police.',
                'You were beaten to death for trying to steal from the elderly.',
            ],
            item_chance=0.42,
            items={
                Items.tobacco: 0.7,
                Items.key: 0.2,
                Items.banknote: 0.2,
                Items.dynamite: 0.1,
            },
        ),
        'arson': CrimeData(
            minimum=500,
            maximum=800,
            image='https://cdn.discordapp.com/attachments/935327142332465222/942470172529291376/Untitled347_20220212180424.png',
            success_chance=0.55,
            death_chance_if_fail=0.5,
            success_responses=[
                'You burn down the house and get paid a bounty of {}.',
                'You watch the building burn in flames and somehow receive {}.',
            ],
            failure_responses=[
                'You burned down a house, now what?',
                'You tried to burn down a fireproof building.',
            ],
            death_responses=[
                'You tried to burn down a police station and ended up getting shot by the police.',
                'You were caught in the fire you created and died.'
            ],
            item_chance=0.35,
            items={
                Items.fish: 0.8,
                Items.padlock: 0.2,
                Items.banknote: 0.1,
                Items.fishing_pole: 0.1,
                Items.key: 0.1,
                Items.dynamite: 0.1,
            },
        ),
    }

    @command(aliases={'ci', 'cri', 'felony', 'criminal'}, hybrid=True)
    @simple_cooldown(1, 25)
    @user_max_concurrency(1)
    async def crime(self, ctx: Context):
        """Commit a crime and hope for profit."""
        view: SearchView[CrimeData] = SearchView(ctx, random.sample(list(self.CRIMES), 3), self.CRIMES)
        yield f'\U0001f92b {ctx.author.mention}, Which crime would you like to commit?', view, REPLY, EPHEMERAL

        await view.wait()
        if not view.choice:
            return

        record = await ctx.db.get_user_record(ctx.author.id)
        await record.add_random_exp(10, 16, ctx=ctx)
        await record.add_random_bank_space(18, 24, chance=0.6)

        name, choice = view.choice
        embed = discord.Embed(timestamp=ctx.now)
        embed.set_author(name=f'Crime: {ctx.author}', icon_url=ctx.author.display_avatar)
        embed.set_footer(text=f'Crime committed: {name}')
        embed.set_thumbnail(url=choice.image)

        cont = await self._get_command_shortcuts(ctx, record)
        pets = await record.pet_manager.wait()

        chance_multiplier = 1.0
        if weasel := pets.get_active_pet(Pets.weasel):
            chance_multiplier += 0.01 + weasel.level * 0.005

        if random.random() > choice.success_chance * chance_multiplier:
            embed.colour = Colors.error

            if random.random() < choice.death_chance_if_fail:
                cause = random.choice(choice.death_responses)
                await record.make_dead(reason=f'While committing a crime, {cause}')

                embed.add_field(name='You died!', value=cause)

                yield embed, cont, REPLY
                return

            message = random.choice(choice.failure_responses)
            embed.add_field(name='You got nothing!', value=message)

            yield embed, cont, REPLY
            return

        gain = random.randint(choice.minimum, choice.maximum)
        gain_multiplier = 1.0

        if cow := pets.get_active_pet(Pets.cow):
            gain_multiplier += 0.02 + cow.level * 0.005
        if weasel := pets.get_active_pet(Pets.weasel):
            gain_multiplier += 0.02 + weasel.level * 0.005

        gain *= gain_multiplier

        async with ctx.db.acquire() as conn:
            profit = await record.add_coins(gain, connection=conn)
            message = [f'{Emojis.coin} **{profit:,}**']

            if random.random() < choice.item_chance:
                items = random.choices(list(choice.items), list(choice.items.values()), k=random.randint(*choice.item_count))
                message.extend(item.get_sentence_chunk(1) for item in items)

                kwargs = {item.key: 1 for item in items}
                await record.inventory_manager.add_bulk(connection=conn, **kwargs)

        embed.colour = Colors.success
        embed.add_field(name='Profit!', value=random.choice(choice.success_responses).format(humanize_list(message)))

        yield embed, cont, REPLY

    @command(aliases={'f', 'cast', 'fishing', 'fishingpole'}, hybrid=True)
    @simple_cooldown(1, 40)
    @user_max_concurrency(1)
    async def fish(self, ctx: Context):
        """Use your fishing pole to fish for fish and sell them for profit!"""
        record = await ctx.db.get_user_record(ctx.author.id)
        await record.inventory_manager.wait()
        await record.pet_manager.wait()

        await record.add_random_exp(15, 20, chance=0.8, ctx=ctx)
        await record.add_random_bank_space(12, 20, chance=0.6)

        game = FishingView(ctx, record=record)
        message = await ctx.reply(
            f'{Emojis.loading} Casting your {game.tool.display_name}...'
            if game.tool else f'{Emojis.loading} Fishing with your bare hands...'
        )
        await asyncio.sleep(random.uniform(2., 4.))

        await game.remove_bait()
        await ctx.maybe_edit(message, content='', embeds=[game.make_embed(), game.prompt_embed()], view=game)

        if not game.current:
            await asyncio.sleep(1.2)
            await game.advance(ctx)  # type: ignore

        await game.wait()

    @command(aliases={'shovel', 'di', 'mine', 'pickaxe', 'm'}, hybrid=True)
    @simple_cooldown(1, 240)
    @user_max_concurrency(1)
    async def dig(self, ctx: Context) -> CommandResponse:
        """Dig up items from the ground and sell them for profit!"""

        view = DiggingView(ctx)
        await view.prepare()
        yield await view.session.generate_image(), view, REPLY, NO_EXTRA
        await view.wait()

        async with ctx.db.acquire() as conn:
            record = view.session.record
            await record.add_exp(view.session.xp_earned, ctx=ctx, connection=conn)
            await record.add_bank_space(view.session.bank_space_earned, connection=conn)

    ABUNDANCE_FOREST_WOOD_CHANCES = {
        None: 1,
        Items.wood: 0.3,
        Items.redwood: 0.03,
        Items.blackwood: 0.0025,
    }

    EXOTIC_FOREST_WOOD_CHANCES = {
        None: 1,
        Items.wood: 0.5,
        Items.redwood: 0.09,
        Items.blackwood: 0.0085,
    }

    @command(aliases={'c', 'ch', 'axe'}, hybrid=True)
    @simple_cooldown(1, 25)
    @user_max_concurrency(1)
    async def chop(self, ctx: Context):
        """Chop down trees for wood! Wood can be sold for profit, or used to craft many items."""
        record = await ctx.db.get_user_record(ctx.author.id)
        inventory = await record.inventory_manager.wait()

        if not inventory.cached.quantity_of('axe'):
            yield f'You need {Items.axe.get_sentence_chunk(1)} to chop down trees.', BAD_ARGUMENT
            return

        view = ChopView(ctx)
        yield (
            "Choose a forest to chop trees from:\n"
            "- **Abundance Forest**: lower chance of getting rarer wood, zero risk of dying\n"
            "- **Exotic Forest**: higher chance of getting rarer wood, but you have a chance of dying",
            view,
            REPLY,
        )

        await view.wait()
        if view.choice is None:
            yield 'Timed out.', REPLY
            return

        # random.random() is INCLUSIVE of 0, but EXCLUSIVE of 1
        success_chance = 0.95 if view.choice == view.EXOTIC else 1
        mapping = self.ABUNDANCE_FOREST_WOOD_CHANCES if view.choice == view.ABUNDANCE else self.EXOTIC_FOREST_WOOD_CHANCES
        mapping = mapping.copy()

        pets = await record.pet_manager.wait()
        extra_weight = 1

        if panda := pets.get_active_pet(Pets.panda):
            extra_weight += 0.02 + panda.level * 0.005
        if jaguar := pets.get_active_pet(Pets.jaguar):
            extra_weight += 0.03 + jaguar.level * 0.01

        mapping[Items.redwood] *= extra_weight
        mapping[Items.blackwood] *= extra_weight

        wood = random.choices(list(mapping), weights=list(mapping.values()), k=13)
        wood = {item: wood.count(item) for item in set(wood) if item is not None}

        await record.add_random_exp(12, 18, chance=0.8, ctx=ctx)
        await record.add_random_bank_space(10, 15, chance=0.6)

        area = 'Abundance Forest' if view.choice == view.ABUNDANCE else 'Exotic Forest'
        yield f'{Emojis.loading} Chopping down trees in **{area}**...', dict(view=None), EDIT

        view = await self._get_command_shortcuts(ctx, record)
        await asyncio.sleep(random.uniform(2., 4.))

        if not len(wood):
            yield 'You couldn\'t chop down any trees, lol.', view, EDIT
            return

        if random.random() > success_chance:
            await record.make_dead(reason='A tree fell on your head while chopping trees.')
            yield 'How exotic! A tree fell on your head while you were chopping it down, killing you instantly.', view, EDIT
            return

        # TODO: way to make user lose their axe?

        async with ctx.db.acquire() as conn:
            for item, count in wood.items():
                await inventory.add_item(item, count, connection=conn)

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.add_field(name='You generated:', value='\n'.join(f'{item.get_display_name(bold=True)} x{count}' for item, count in wood.items()))
        embed.set_author(name=f'Chopping: {ctx.author}', icon_url=ctx.author.display_avatar)

        yield '', embed, view, EDIT

    async def pop_trivia_question(self) -> TriviaQuestion:
        try:
            return self._trivia_questions.popleft()
        except IndexError:
            pass

        async with self._trivia_questions_fetch_lock:
            async with self.bot.session.get('https://opentdb.com/api.php?amount=50') as response:
                if not response.ok:
                    raise RuntimeError('failed to retrieve trivia question')

                data = await response.json(encoding='utf-8')

            if data['response_code'] != 0:
                raise RuntimeError('failed to retrieve trivia question')

            self._trivia_questions.extend(TriviaQuestion.from_data(q) for q in data['results'])
            return self._trivia_questions.popleft()

    TRIVIA_PRIZE_MAPPING = {
        'easy': (100, 150),
        'medium': (200, 325),
        'hard': (360, 500),
    }

    @command(aliases={'triv', 'tv'}, hybrid=True)
    @simple_cooldown(1, 20)
    @user_max_concurrency(1)
    async def trivia(self, ctx: Context):
        """Answer trivia questions to gain coins and intelligence!"""
        question = await self.pop_trivia_question()
        prize = random.randint(*self.TRIVIA_PRIZE_MAPPING[question.difficulty])

        embed = discord.Embed(color=Colors.primary, description=question.question, timestamp=ctx.now)
        embed.set_author(name=f'Trivia: {ctx.author}', icon_url=ctx.author.display_avatar)
        embed.set_footer(text='Answer using the buttons below!')

        embed.add_field(name='Difficulty', value=question.difficulty.title())
        embed.add_field(name='Category', value=question.category)
        embed.add_field(name='Prize', value=f'{Emojis.coin} **{prize:,}**')

        view = TriviaView(ctx, embed, question)
        yield embed, view, REPLY

        await view.wait()
        if not view.choice:
            yield f"You didn't answer in time! The correct answer was **{question.correct_answer}**", REPLY
            return

        record = await ctx.db.get_user_record(ctx.author.id)
        cont = await self._get_command_shortcuts(ctx, record)

        async with ctx.db.acquire() as conn:
            await record.add_random_bank_space(10, 15, chance=0.5, connection=conn)
            await record.add_random_exp(10, 15, chance=0.65, ctx=ctx, connection=conn)

            if view.choice == question.correct_answer:
                async with ctx.db.acquire() as conn:
                    profit = await record.add_coins(prize, connection=conn)
                    await record.add(iq=1, connection=conn)

                yield (
                    f'Correct! You earned {Emojis.coin} **{profit:,}**.\n'
                    f'-# \U0001f9e0 **+1 IQ** \u2014 You now have **{record.iq:,}** IQ.'
                ), cont, REPLY
                return

        yield f'Wrong, the correct answer was **{question.correct_answer}**', cont, REPLY

    @command(aliases={'dv', 'submerge'}, hybrid=True)
    @user_max_concurrency(1)
    @lock_transactions
    @simple_cooldown(1, 60)
    @cooldown_message("You're too tired out from diving.")
    async def dive(self, ctx: Context) -> CommandResponse:
        """Dive underwater for treasure!

        The deeper you dive, the more likely you are to either lose all earnings, or die from water pressure or drowning.
        """
        record = await ctx.db.get_user_record(ctx.author.id)
        await record.inventory_manager.wait()

        view = DivingView(ctx, record=record)
        yield view, view.make_embed(message='You begin your dive at the surface.'), REPLY
        await view.wait()

    HOURLY_CRATE_WEIGHTS: dict[Item, float] = {
        Items.common_crate: 1.0,
        Items.uncommon_crate: 0.1,
        Items.rare_crate: 0.01,
    }

    @command(aliases={'claim', 'redeem', 'hr', 'hour'}, hybrid=True)
    @database_cooldown(3_600)
    @user_max_concurrency(1)
    @cooldown_message('This command is named hourly for a reason.')
    async def hourly(self, ctx: Context) -> CommandResponse:
        """Claim your hourly crate."""
        record = await ctx.db.get_user_record(ctx.author.id)
        inventory = record.inventory_manager

        weights = self.HOURLY_CRATE_WEIGHTS.copy()
        pets = await record.pet_manager.wait()

        if duck := pets.get_active_pet(Pets.duck):
            k = 1.01 + duck.level * 0.0025
            for item in (Items.uncommon_crate, Items.rare_crate):
                weights[item] *= k

        item = weighted_choice(self.HOURLY_CRATE_WEIGHTS)

        async with ctx.db.acquire() as conn:
            await inventory.add_item(item, 1, connection=conn)
            await record.add_random_exp(15, 25, ctx=ctx, connection=conn)
            await record.add_random_bank_space(20, 35, chance=0.8, connection=conn)

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.set_author(name=f'{ctx.author}: Claim Hourly', icon_url=ctx.author.display_avatar)
        embed.description = f'You claimed your hourly {item.get_display_name(bold=True)}!'

        view = await self._get_command_shortcuts(ctx, record)
        return embed, view, REPLY

    @command(aliases={'da', 'day'}, hybrid=True)
    @database_cooldown(86_400)
    @user_max_concurrency(1)
    @cooldown_message('This command is named daily for a reason.')
    async def daily(self, ctx: Context) -> CommandResponse:
        """Claim your daily reward!"""
        record = await ctx.db.get_user_record(ctx.author.id)
        cooldowns = await record.cooldown_manager.wait()

        previous = cooldowns.cached['daily'].previous_expiry

        if previous and ctx.now - previous <= timedelta(days=1):  # Give one day of breathing room
            await record.add(daily_streak=1)
        else:
            await record.update(daily_streak=0)

        streak_benefit = record.daily_streak * 250
        profit = 5000 + streak_benefit
        profit = await record.add_coins(profit)

        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.set_author(name=f'{ctx.author.name}: Claim Daily', icon_url=ctx.author.display_avatar)

        if streak_benefit:
            embed.add_field(name='Streak Bonus', value=f'+{Emojis.coin} **{streak_benefit:,}** [Streak: {record.daily_streak}]')

        embed.description = f'You claimed your daily reward of {Emojis.coin} **{profit:,}**.'

        view = await self._get_command_shortcuts(ctx, record)
        return embed, view, REPLY

    @command(aliases={'week', 'wk'}, hybrid=True)
    @database_cooldown(604_800)
    @user_max_concurrency(1)
    @cooldown_message('This command is named weekly for a reason.')
    async def weekly(self, ctx: Context) -> CommandResponse:
        """Claim your weekly reward!"""
        record = await ctx.db.get_user_record(ctx.author.id)
        cooldowns = await record.cooldown_manager.wait()

        previous = cooldowns.cached['weekly'].previous_expiry

        if previous and ctx.now - previous <= timedelta(days=2):  # Give two days of breathing room
            await record.add(weekly_streak=1)
        else:
            await record.update(weekly_streak=0)

        streak_benefit = record.weekly_streak * 2000
        profit = 20000 + streak_benefit
        profit = await record.add_coins(profit)

        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.set_author(name=f'{ctx.author.name}: Claim Weekly', icon_url=ctx.author.display_avatar)

        if streak_benefit:
            embed.add_field(name='Streak Bonus', value=f'+{Emojis.coin} **{streak_benefit:,}** [Streak: {record.weekly_streak}]')

        embed.description = f'You claimed your weekly reward of {Emojis.coin} **{profit:,}**.'

        view = await self._get_command_shortcuts(ctx, record)
        return embed, view, REPLY

    @staticmethod
    async def _pet_claim(ctx: Context, *, pet: Pet, item: Item, energy: int) -> None:
        record = await ctx.fetch_author_record()
        inventory = await record.inventory_manager.wait()

        pet_record = record.pet_manager.cached[pet]
        async with ctx.db.acquire() as conn:
            await pet_record.add_energy(-energy, connection=conn)
            await inventory.add_item(item, connection=conn)
            await ctx.add_random_exp(10, 15, connection=conn)

    @command(aliases={'hon', 'bee'}, hybrid=True)
    @database_cooldown(3600)
    @user_max_concurrency(1)
    @cooldown_message('Your bee needs time to make more honey!')
    @active_pet(Pets.bee, energy=60, verb='produce honey')
    async def honey(self, ctx: Context) -> CommandResponse:
        """Claim honey from your bee."""
        item = Items.honey
        await self._pet_claim(ctx, pet=Pets.bee, item=item, energy=60)
        return (
            f'{Pets.bee.emoji} Your **bee** produces {item.get_sentence_chunk()} and stores it in your inventory.',
            REPLY,
        )

    @command(aliases={'cow'}, hybrid=True)
    @database_cooldown(3600)
    @user_max_concurrency(1)
    @cooldown_message('Your cow needs time to make more milk!')
    @active_pet(Pets.cow, energy=100, verb='produce milk')
    async def milk(self, ctx: Context) -> CommandResponse:
        """Claim milk from your cow."""
        item = Items.milk
        await self._pet_claim(ctx, pet=Pets.cow, item=item, energy=100)
        return (
            f'{Pets.cow.emoji} Your **cow** produces {item.get_sentence_chunk()} and stores it in your inventory.',
            REPLY,
        )

    @command(aliases={'berry', 'bry'}, hybrid=True)
    @database_cooldown(3600)
    @user_max_concurrency(1)
    @cooldown_message('Your fox needs time to find berries!')
    @active_pet(Pets.fox, energy=200, verb='produce berries')
    async def berries(self, ctx: Context) -> CommandResponse:
        """Claim berries from your fox."""
        item = Items.berries
        await self._pet_claim(ctx, pet=Pets.fox, item=item, energy=200)
        return (
            f'{Pets.fox.emoji} Your **fox** produces {item.get_sentence_chunk()} and stores it in your inventory.',
            REPLY,
        )

    def store_rob(self, ctx: Context, victim: AnyUser, amount: int) -> RobData:
        self._recent_robs[victim.id] = entry = RobData(
            timestamp=ctx.utcnow(), robbed_by=ctx.author, victim=victim, amount=amount,
        )
        return entry

    @command(aliases={'steal', 'ripoff'}, hybrid=True, with_app_command=False)
    @simple_cooldown(1, 300)
    @user_max_concurrency(1)
    @lock_transactions
    async def rob(self, ctx: Context, *, user: CaseInsensitiveMemberConverter):
        # sourcery no-metrics skip: merge-nested-ifs
        """Attempt to rob someone of their coins!

        There is a chance that you might fail and pay a fine, or even die.
        """
        if user == ctx.author:
            yield 'What are you trying to do? Rob yourself? Sounds kinda dumb to me.', BAD_ARGUMENT
            return

        if user.bot:
            yield 'You cannot rob bot accounts.', BAD_ARGUMENT
            return

        if not ctx.channel.permissions_for(user).view_channel:
            yield f'{user.name} can\'t even see this channel, that would be pretty unfair.', BAD_ARGUMENT
            return

        if isinstance(ctx.channel, discord.Thread):
            yield 'Robbing in threads is disabled as of this moment.', BAD_ARGUMENT
            return

        if entry := self._recent_robs.get(user.id):
            if ctx.now - entry.timestamp < timedelta(minutes=5):
                yield 'That user has recently been robbed, let\'s give them a break.', BAD_ARGUMENT
                return

        lock = ctx.bot.transaction_locks.setdefault(user.id, LockWithReason())

        if lock.locked():
            yield f'{user.name} is currently being robbed, lmao', BAD_ARGUMENT
            return

        their_record = await ctx.db.get_user_record(user.id)

        if their_record.wallet < 500:
            yield f"The person you're trying to rob is pretty poor, try robbing people with more than {Emojis.coin} 500 next time.", BAD_ARGUMENT
            return

        if their_record.level < 5:
            yield 'You cannot rob people under level 5, that\'s just cruel.', BAD_ARGUMENT
            return

        record = await ctx.db.get_user_record(ctx.author.id)
        if record.wallet < 500:
            yield f'You must have {Emojis.coin} 500 in your wallet in order to rob someone.', BAD_ARGUMENT
            return

        if record.level < 5:
            yield 'You must be at least level 5 to rob others.', BAD_ARGUMENT
            return

        skills = await record.skill_manager.wait()
        their_skills = await their_record.skill_manager.wait()

        has_alcohol = record.alcohol_expiry is not None
        they_have_alcohol = their_record.alcohol_expiry is not None
        success_chance = (
            50
            + skills.points_in('robbery')
            - their_skills.points_in('defense') * 1.5
            + 15 * has_alcohol
            - 20 * they_have_alcohol
        )
        success_chance = max(min(success_chance, 100), 2) / 100

        if not await ctx.confirm(
            f'Are you sure you want to rob **{user.name}**? (Success chance: {success_chance:.0%})',
            delete_after=True,
            reference=ctx.message,
        ):
            yield 'Looks like we won\'t rob today.', BAD_ARGUMENT
            return

        async with lock.with_reason(
            f"Someone else ({ctx.author.mention}) is currently trying to rob you - view your notifications to find out more details!"
        ):
            notify = their_record.notifications_manager.add_notification

            async with ctx.db.acquire() as conn:
                await record.add_random_bank_space(10, 15, chance=0.6, connection=conn)
                await record.add_random_exp(12, 17, chance=0.7, ctx=ctx, connection=conn)

            yield f'{Emojis.loading} Robbing {user.name}...', REPLY
            await asyncio.sleep(random.uniform(1.5, 3.5))

            their_pets = await their_record.pet_manager.wait()
            if bee := their_pets.get_active_pet(Pets.bee):
                if random.random() < 0.02 + bee.level * 0.0025:
                    fine = record.wallet * random.uniform(0.05, 0.2)
                    await record.add(wallet=-fine)

                    yield (
                        f'{Pets.bee.emoji} Ouch! You were stung by {user.name}\'s pet **bee**!\n'
                        f'Stunned, the police caught you and fined you {Emojis.coin} **{fine:,}**.'
                    )
                    await notify(NotificationData.RobFailure(
                        user_id=ctx.author.id, guild_name=ctx.guild.name, reason=RobFailReason.bee_sting,
                    ))
                    return

            padlock_worked = False

            if their_record.padlock_active:
                padlock_worked = True
                inventory = await record.inventory_manager.wait()

                if inventory.cached.quantity_of('key') > 0 and await ctx.confirm(
                    f'{Items.padlock.emoji} {user.name} has a padlock active!\n'
                    f'You have a {Items.key.get_display_name(bold=True)} in your inventory, do you want to use it to potentially open the padlock?',
                    reference=ctx.message,
                ):
                    await inventory.add_item('key', -1)
                    if random.random() < 0.25:
                        padlock_worked = False
                        await their_record.update(padlock_active=False)
                        yield f'{Items.padlock.emoji} Unlocked {user.name}\'s padlock!', REPLY

                        await notify(NotificationData.PadlockOpened(
                            user_id=ctx.author.id, guild_name=ctx.guild.name, device='key',
                        ))
                    else:
                        yield f'{Items.padlock.emoji} Failed to unlock {user.name}\'s padlock! (You also consumed your key)', REPLY

            if padlock_worked:
                fine_percent = random.uniform(.05, .25)
                fine = max(500, round(record.wallet * fine_percent))

                fine_percent = fine / record.wallet
                yield (
                    f'{Items.padlock.emoji} {user.name} had a padlock active. '
                    f'You were instantly caught trying to get rid of the padlock and you pay a fine of {Emojis.coin} '
                    f'**{fine:,}** ({fine_percent:.1%} of your wallet).',
                    EDIT,
                )
                await record.add(wallet=-fine)
                await their_record.update(padlock_active=False)

                await notify(NotificationData.RobFailure(
                    user_id=ctx.author.id, guild_name=ctx.guild.name, reason=RobFailReason.padlock_active,
                ))
                return

            await notify(NotificationData.RobInProgress(user_id=ctx.author.id, guild_name=ctx.guild.name))

            embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
            embed.set_author(name=f'{ctx.author.name}: Robbing {user.name}', icon_url=ctx.author.display_avatar)

            code = random.randint(100000, 999999)
            embed.description = (
                "Robbing isn't as always as easy as it seems. Quick! Type in the following combination onto the keypad below "
                f"before time runs out to rob {user.mention} of their coins!\n\n"
                f"{user.mention}, you can press the **CATCH!** button before {ctx.author.name} finishes entering in the "
                f"combination in order to catch them and automatically fail their attempt."
            )

            embed.add_field(name='Enter the following combination:', value=str(code), inline=False)
            view = RobbingKeypad(ctx, user, embed, code)

            yield '', embed, view, EDIT

            try:
                await asyncio.wait_for(view.wait(), timeout=20)
            except asyncio.TimeoutError:
                fine_percent = random.uniform(.1, .5)
                fine = max(500, round(record.wallet * fine_percent))

                fine_percent = fine / record.wallet
                yield (
                    'Looks like you took too long to enter in the combination. '
                    f'You were caught trying to break into {user.name}\'s wallet and you pay a fine of '
                    f'{Emojis.coin} **{fine:,}** ({fine_percent:.1%} of your wallet).',
                    REPLY,
                )
                await record.add(wallet=-fine)

                await notify(NotificationData.RobFailure(
                    user_id=ctx.author.id, guild_name=ctx.guild.name, reason=RobFailReason.code_failure,
                ))
                return

            if view.caught:
                fine_percent = random.uniform(.2, .6)
                fine = max(500, round(record.wallet * fine_percent))

                fine_percent = fine / record.wallet
                yield (
                    f'{user.name} caught you trying to break into their wallet and immediately call the cops on you. '
                    f'You pay a fine of {Emojis.coin} **{fine:,}** ({fine_percent:.1%} of your wallet).',
                    REPLY,
                )
                await record.add(wallet=-fine)
                return  # Don't notify here since that person MUST have been present

            if str(code) != view.entered:
                fine_percent = random.uniform(.1, .5)
                fine = max(500, round(record.wallet * fine_percent))

                fine_percent = fine / record.wallet
                yield (
                    f'You entered in the wrong combination. '
                    f'The police are alerted about your attempt and you pay a fine of {Emojis.coin} **{fine:,}** ({fine_percent:.1%} of your wallet).',
                    REPLY,
                )
                await record.add(wallet=-fine)

                await notify(NotificationData.RobFailure(
                    user_id=ctx.author.id, guild_name=ctx.guild.name, reason=RobFailReason.code_failure,
                ))
                return

            embed.colour = Colors.success
            yield embed, EDIT

            death_chance = max(10 - skills.points_in('robbery') / 2 + their_skills.points_in('defense') / 2, 0) / 100

            if random.random() < success_chance:
                quests = await record.quest_manager.wait()
                payout_percent = min(
                    random.uniform(.3, .8) + min(skills.points_in('robbery') * .02, .5),
                    record.wallet * 3 / their_record.wallet,
                    1,
                )
                payout = round(their_record.wallet * payout_percent)

                await record.add(wallet=payout)
                await their_record.add(wallet=-payout)

                yield (
                    f"**SUCCESS!** You stole {Emojis.coin} **{payout:,}** ({payout_percent:.1%}) from {user.name}'s wallet.\n"
                    f"You now have {Emojis.coin} **{record.wallet:,}**.",
                    REPLY,
                )

                self.store_rob(ctx, user, payout)

                await notify(NotificationData.RobSuccess(
                    user_id=ctx.author.id, guild_name=ctx.guild.name, percent=payout_percent, amount=payout,
                ))
                if quest := quests.get_active_quest(QuestTemplates.rob_coins):
                    await quest.add_progress(payout)
                if quest := quests.get_active_quest(QuestTemplates.rob_successes):
                    await quest.add_progress(1)
                return

            if random.random() < death_chance:
                await record.make_dead()
                yield (
                    f"While trying your best not to make a noise, you are spotted by police while trying to rob {user.name}.\n"
                    "You refuse arrest causing the police to fatally shoot you. You died.",
                    REPLY,
                )

                await notify(NotificationData.RobFailure(
                    user_id=ctx.author.id, guild_name=ctx.guild.name, reason=RobFailReason.spotted_by_police,
                ))
                return

            # highest fines are here
            fine_percent = random.uniform(.2, .7)
            fine = max(500, round(record.wallet * fine_percent))

            fine_percent = fine / record.wallet
            yield (
                f'While so stealthily trying to rob {user.name}, you are spotted by police, '
                f'who force you to pay a fine of {Emojis.coin} **{fine:,}** '
                f'({fine_percent:.1%} of your wallet) to {user.name}.',
                REPLY,
            )
            await record.add(wallet=-fine)
            await their_record.add(wallet=fine)

            await notify(NotificationData.RobFailure(
                user_id=ctx.author.id, guild_name=ctx.guild.name, reason=RobFailReason.spotted_by_police,
                received=fine,
            ))
            return

    @rob.define_app_command()
    @app_commands.describe(victim='The victim of your robbery.')
    @app_commands.allowed_installs(guilds=True, users=False)
    async def rob_app_command(self, ctx: HybridContext, victim: discord.Member) -> None:
        await ctx.invoke(ctx.command, user=victim)

    @group(aliases={'q', 'qu', 'quest', 'pass', 'battlepass'}, hybrid=True, fallback='view', expand_subcommands=True)
    @simple_cooldown(2, 7)
    @user_max_concurrency(1)
    async def quests(self, ctx: Context) -> CommandResponse:
        """View your current quests and progress."""
        if ctx.invoked_with in ('pass', 'battlepass'):
            return await ctx.invoke(self.quest_pass)  # type: ignore

        record = await ctx.db.get_user_record(ctx.author.id)
        await record.quest_manager.wait()

        view = QuestsView(ctx, record)
        await view.update()
        return view, REPLY, NO_EXTRA

    @quests.command(name='pass', aliases={'bp', 'battlepass', 'rewards', 'reward', 'p'}, hybrid=True)
    @simple_cooldown(2, 7)
    @user_max_concurrency(1)
    async def quest_pass(self, ctx: Context) -> CommandResponse:
        """View your quest pass tier and quest rewards!"""
        record = await ctx.db.get_user_record(ctx.author.id)
        await record.quest_manager.wait()

        view = QuestPassView(ctx, record)
        await view.update()
        return view, REPLY, NO_EXTRA


class ChopView(UserView):
    ABUNDANCE = 0
    EXOTIC = 1

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx.author, timeout=20)

        self.choice: Literal[0, 1] | None = None
        self._ctx: Context = ctx

    def _disable_buttons(self) -> None:
        for button in self.children:
            assert isinstance(button, discord.ui.Button)
            button.disabled = True

    @discord.ui.button(label='Abundance Forest')
    async def abundance(self, interaction: TypedInteraction, button: discord.ui.Button) -> None:
        self.choice = self.ABUNDANCE

        self._disable_buttons()
        button.style = discord.ButtonStyle.primary

        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label='Exotic Forest')
    async def exotic(self, interaction: TypedInteraction, button: discord.ui.Button) -> None:
        self.choice = self.EXOTIC

        self._disable_buttons()
        button.style = discord.ButtonStyle.primary

        await interaction.response.edit_message(view=self)
        self.stop()


class TriviaButton(discord.ui.Button['TriviaView']):
    async def callback(self, interaction: TypedInteraction) -> None:
        self.view.choice = self.label

        color = Colors.success if self.view.correct == self.label else Colors.error
        self.view.embed.colour = color

        for button in self.view.children:
            assert isinstance(button, discord.ui.Button)

            if button.label == self.view.correct:
                button.style = discord.ButtonStyle.success

            elif button.label == self.label:
                button.style = discord.ButtonStyle.danger

            else:
                button.style = discord.ButtonStyle.secondary

            button.disabled = True

        await interaction.response.edit_message(embed=self.view.embed, view=self.view)

        self.view.stop()


class TriviaView(UserView):
    def __init__(self, ctx: Context, embed: discord.Embed, question: TriviaQuestion) -> None:
        super().__init__(ctx.author, timeout=15)

        if question.type == 'boolean':
            self.add_item(TriviaButton(label='True', style=discord.ButtonStyle.success))
            self.add_item(TriviaButton(label='False', style=discord.ButtonStyle.danger))
        else:
            for answer in question.answers:
                self.add_item(TriviaButton(label=answer, style=discord.ButtonStyle.primary))

        self.embed: discord.Embed = embed
        self.correct: str = question.correct_answer
        self.choice: str | None = None


class PlaceholderKeypadButton(discord.ui.Button['RobbingKeypad']):
    def __init__(self, *, row: int | None = None) -> None:
        super().__init__(label='\u200b', disabled=True, row=row)


class RobbingKeypad(discord.ui.View):
    def __init__(self, ctx: Context, opponent: AnyUser, embed: discord.Embed, code: int) -> None:
        super().__init__()

        self.ctx: Context = ctx
        self.opponent: AnyUser = opponent

        self.code: int = code
        self.embed: discord.Embed = embed
        self.entered: str = ''

        self.caught: bool = False
        self.dangling_interaction: discord.Interaction | None = None

        self.clear_button = discord.ui.Button(label='Clear', style=discord.ButtonStyle.danger, row=4)
        self.submit_button = discord.ui.Button(label='Submit!', style=discord.ButtonStyle.success, row=4)
        self.catch_button = discord.ui.Button(label='CATCH!', style=discord.ButtonStyle.danger, row=4)

        self.clear_button.callback = self.clear_callback
        self.submit_button.callback = self.submit_callback
        self.catch_button.callback = self.catch_callback

        self.add_buttons()

    def update(self) -> None:
        self.embed.remove_field(1)

        if self.entered:
            self.embed.add_field(name='You entered:', value=f'```py\n{self.entered}```', inline=False)

    def add_buttons(self) -> None:
        buttons = (
            (1, 2, 3),
            (4, 5, 6),
            (7, 8, 9),
            (None, 0, None),
        )

        self.clear_items()
        for i, row in enumerate(buttons):
            for button in row:
                self.add_item(
                    PlaceholderKeypadButton(row=i)
                    if button is None
                    else RobberyTrainingButton(button, row=i, user=self.ctx.author)
                )

        self.add_item(self.clear_button)
        self.add_item(self.submit_button)
        self.add_item(self.catch_button)

    async def clear_callback(self, interaction: TypedInteraction):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message('nope', ephemeral=True)

        self.entered = ''
        self.update()

        await interaction.response.edit_message(embed=self.embed, view=self)

    async def submit_callback(self, interaction: TypedInteraction):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message('nope', ephemeral=True)

        self.dangling_interaction = interaction
        self.stop()

    async def catch_callback(self, interaction: TypedInteraction):
        if interaction.user != self.opponent:
            return await interaction.response.send_message(f'Only {self.opponent.mention} can use this button.', ephemeral=True)

        self.caught = True
        self.dangling_interaction = interaction
        self.stop()


class FishingButton(discord.ui.Button['FishingView']):
    def __init__(self, fish: Item) -> None:
        super().__init__(label=fish.name, style=discord.ButtonStyle.primary)
        self.fish: Item = fish

    async def callback(self, interaction: TypedInteraction) -> None:
        if self.fish is not self.view.current:
            self.view.stop()
            for button in self.view.children:
                assert isinstance(button, discord.ui.Button)
                button.disabled = True
                button.style = (
                    discord.ButtonStyle.success
                    if isinstance(button, FishingButton) and button.fish is self.view.current
                    else discord.ButtonStyle.secondary
                )
            self.style = discord.ButtonStyle.danger

            shortcuts = await self.view._shortcuts()
            for child in shortcuts.children:
                child.row = 1
                self.view.add_item(child)

            self.view.embed_color = Colors.secondary
            embed = discord.Embed(color=Colors.secondary, timestamp=interaction.created_at)
            extra = await self.view.damage_tool(1) if self.view.tool is not None else ''
            embed.add_field(
                name=f'Wrong, that was {self.view.current.get_sentence_chunk()}!',
                value=(
                    f'The {self.view.current.name} got away and your fishing session ended.\n'
                    f'You can fish again later by using {self.view.fish_mention}.\n\n{extra}'
                ),
            )
            await self.view.give_prizes()
            return await interaction.response.edit_message(embeds=[self.view.make_embed(), embed], view=self.view)

        await self.view.advance(interaction)


class FishingView(UserView):
    FISH: list[Item] = [
        item for item in Items.all() if item.type is ItemType.fish and item.rarity is not ItemRarity.unobtainable
    ]
    BASE_FISH_CHANCES: dict[Item | None, float] = {
        None: 1.0,
        Items.fish: 0.4,
        Items.anchovy: 0.3,
        Items.sardine: 0.3,
        Items.angel_fish: 0.2,
        Items.goldfish: 0.2,
        Items.blowfish: 0.15,
        Items.crab: 0.1,
        Items.lobster: 0.05,
        Items.octopus: 0.03,
        Items.seahorse: 0.02,
        Items.axolotl: 0.015,
        Items.dolphin: 0.005,
        Items.shark: 0.003,
        Items.whale: 0.0015,
    }

    def __init__(self, ctx: Context, *, record: UserRecord) -> None:
        super().__init__(ctx.author, timeout=30)

        self.ctx: Context = ctx
        self.record: UserRecord = record
        self.collected: defaultdict[Item, int] = defaultdict(int)
        self.count: int = 0
        self.embed_color: int = Colors.secondary

        inventory = record.inventory_manager
        assert inventory._task.done(), 'inventory_manager must be fetched'

        pets = record.pet_manager
        assert pets._task.done(), 'pet_manager must be fetched'

        fishing_poles = (item for item in Items.__fishing_poles__ if inventory.cached.quantity_of(item) > 0)
        self.tool: Item[FishingPoleMetadata] | None = next(fishing_poles, None)
        self.max_count: int = 4 if not self.tool else self.tool.metadata.iterations  # TODO: will be upgradable with prestige tokens

        weights = self.tool.metadata.weights if self.tool else self.BASE_FISH_CHANCES
        self.weights: dict[Item | None, float] = weights.copy()

        # Apply pet multipliers
        extra = 1.0
        if cat := pets.get_active_pet(Pets.cat):
            extra += 0.01 + cat.level * 0.002

        if tortoise := pets.get_active_pet(Pets.tortoise):
            extra += 0.01 + tortoise.level * 0.005

        for item in self.weights:
            if item and item.rarity < ItemRarity.rare:
                continue
            self.weights[item] *= extra

        # Apply bait multipliers
        self.weights_with_bait: dict[Item | None, float] = self.weights.copy()

        for item in self.weights_with_bait:
            if item and item.rarity < ItemRarity.rare:
                continue
            self.weights_with_bait[item] *= 1.2  # Constant +20% chance with bait

        self.update()

    @property
    def fish_mention(self) -> str:
        return self.ctx.bot.tree.get_app_command('fish').mention

    def update(self):
        self.count += 1
        self.previously_used_bait: bool = self.record.inventory_manager.cached.quantity_of(Items.fish_bait) > 0
        weights = self.weights_with_bait if self.previously_used_bait else self.weights

        self.current: Item | None = random.choices(list(weights), weights=list(weights.values()))[0]
        self.clear_items()

        if current := self.current:
            choices = random.sample(self.FISH, k=3)
            if current not in choices:
                choices[random.randrange(0,  len(choices))] = current

            for choice in choices:
                self.add_item(FishingButton(choice))
        else:
            self.previously_used_bait = False
            self.add_item(self.continue_fishing)

        self.add_item(self.quit_fishing)

    async def damage_tool(self, damage: int, *, extra: str = '') -> str:
        inventory = self.record.inventory_manager
        remaining, broken = await inventory.deal_damage(self.tool, damage)
        quantity = inventory.cached.quantity_of(self.tool)
        repair = self.ctx.bot.tree.get_app_command('repair').mention
        # format the text differently if the pole broke
        return (
            f'**Your {self.tool.display_name} was damaged{extra} and it snapped in half!**\n'
            f'{Emojis.Expansion.standalone} You have '
            f'{self.tool.get_sentence_chunk(quantity or f"no more {self.tool.emoji} {self.tool.plural}")} remaining.'
            if broken else (
                f'**Your {self.tool.display_name} was damaged{extra}!**\n'
                f'{Emojis.Expansion.first} {damage:,} damage taken {Emojis.arrow} '
                f'**{remaining:,}/{self.tool.durability:,}** damage remaining '
                f'({remaining / self.tool.durability:.1%})\n'
                f'{Emojis.Expansion.last} Repair the tool in the repair shop: {repair}'
            )
        )

    async def advance(self, interaction: TypedInteraction) -> Any:
        if self.is_finished():
            return

        if self.current and isinstance(self.current.metadata, EnemyRef):
            game = PvEBattleView(
                self.ctx,
                {self.ctx.author: self.record},
                opponent=self.current.metadata.resolved,
                level=1,  # TODO: should this scale with the user's level?
                description=f'The {self.current.display_name} challenges you to a battle! Defeat it to catch it!',
                embeds=[self.make_embed()],
            )
            await interaction.response.edit_message(embeds=game.make_public_embeds(), view=game)

            self.timeout = None
            await game.wait()
            await asyncio.sleep(1)
            self.timeout = 60

            if not game.won:
                self.stop()
                # bare hands? 75% chance they survive and pay medical fees. 25% chance they die
                if not self.tool or random.random() < 0.2:
                    if random.random() < 0.75:
                        debt = max(
                            random.randint(500, 2500), int(self.record.wallet * random.uniform(0.05, 0.3)),
                        )
                        text = f'The fish defeats you and bites your hand! You paid {Emojis.coin} **{debt:,}** in medical fees.'
                        await self.record.add(wallet=-debt)
                    else:
                        text = f'The fish defeats you and bites your head off! You died.'
                        await self.record.make_dead(reason='A fish bit your hand off')
                # otherwise, 80% chance the fish damages the fishing pole
                else:
                    damage = random.randint(*self.current.metadata.damage)
                    text = await self.damage_tool(damage, extra=' in the battle')

                embed = discord.Embed(color=Colors.error, timestamp=interaction.created_at)
                embed.add_field(name=f'You were defeated by the {self.current.display_name}!', value=text)

                return await interaction.edit_original_response(
                    embeds=[self.make_embed(), embed], view=await self._shortcuts(),
                )

        if self.current:
            self.collected[self.current] += 1

        self.update()
        caller = interaction.maybe_edit if isinstance(interaction, Context) else (
            interaction.response.edit_message if not interaction.response.is_done() else interaction.edit_original_response
        )
        if self.count > self.max_count:
            self.stop()
            self.embed_color = Colors.success
            await self.give_prizes()
            return await caller(embed=self.make_embed(), view=await self._shortcuts())

        await self.remove_bait()
        await caller(embeds=[self.make_embed(), self.prompt_embed()], view=self)

        if not self.current:
            await asyncio.sleep(1.2)
            await self.advance(interaction)

    async def remove_bait(self) -> None:
        if not self.previously_used_bait:
            return

        await self.record.inventory_manager.add_item(Items.fish_bait, -1)

    def make_embed(self) -> discord.Embed:
        ctx = self.ctx
        embed = discord.Embed(color=self.embed_color, timestamp=ctx.now if self.is_finished() else None)
        embed.set_author(name=f'{ctx.author.name}\'s Fishing Session', icon_url=ctx.author.display_avatar)
        if self.collected:
            embed.add_field(
                name='You caught:' if self.is_finished() else 'You\'ve collected:',
                value='\n'.join(f'{item.display_name} x{quantity}' for item, quantity in self.collected.items() if item),
                inline=False,
            )
        else:
            embed.description = 'You haven\'t caught anything yet.' if not self.is_finished() else 'You didn\'t catch any fish.'

        if not self.is_finished():
            previous_count = self.count - 1
            embed.add_field(
                name=f'Progress \u2014 {previous_count}/{self.max_count}',
                value=progress_bar(previous_count / self.max_count),
            )
        return embed

    def prompt_embed(self) -> discord.Embed:
        embed = discord.Embed(
            color=Colors.secondary if self.current else Colors.warning,
            timestamp=discord.utils.utcnow(),
        )
        if current := self.current:
            embed.add_field(
                name='You caught a fish!',
                value=f'Identify the fish you caught to collect it and continue fishing!',
                inline=False,
            )
            embed.set_thumbnail(url=image_url_from_emoji(current.emoji))
        else:
            embed.add_field(
                name='Nothing!',
                value='You cast your line but nothing bit. Try again!',
            )
        if self.previously_used_bait:
            remaining = self.record.inventory_manager.cached.quantity_of(Items.fish_bait)
            embed.add_field(
                name=f'{Items.fish_bait.emoji} Fish Bait \u2014 **{remaining:,}** remaining',
                value='*Bait increased the chance of catching rarer fish.*',
                inline=False,
            )
        return embed

    async def _shortcuts(self) -> discord.ui.View:
        return await Profit._get_command_shortcuts(self.ctx, self.record)

    async def give_prizes(self) -> None:
        self.embed_color = Colors.success
        kwargs = {item.key: quantity for item, quantity in self.collected.items() if item}
        await self.record.inventory_manager.add_bulk(**kwargs)

        quests = await self.record.quest_manager.wait()
        if quest := quests.get_active_quest(QuestTemplates.catch_fish):
            await quest.add_progress(sum(self.collected.values()))
        if quest := quests.get_active_quest(QuestTemplates.catch_specific_fish):
            await quest.add_progress(kwargs.get(quest.quest.extra, 0))

    @discord.ui.button(
        label='Fishing in Progress...', style=discord.ButtonStyle.primary, emoji=Emojis.loading, disabled=True,
    )
    async def continue_fishing(self, interaction: TypedInteraction, _button: discord.ui.Button) -> None:
        pass

    @discord.ui.button(label='Quit Fishing', style=discord.ButtonStyle.danger)
    async def quit_fishing(self, interaction: TypedInteraction, _button: discord.ui.Button) -> None:
        self.stop()
        self.embed_color = Colors.success

        embed = discord.Embed(
            color=Colors.warning, timestamp=interaction.created_at,
            description=f'You ended your fishing session. Fish again by running {self.fish_mention}!',
        )
        await self.give_prizes()
        await interaction.response.edit_message(embeds=[self.make_embed(), embed], view=await self._shortcuts())

    async def on_timeout(self) -> None:
        embed = discord.Embed(
            color=Colors.error, timestamp=self.ctx.now,
            description=f'You ran out of time! You can fish again by running {self.fish_mention}.',
        )
        embed.set_author(name=f'{self.ctx.author.name}\'s Fishing Session', icon_url=self.ctx.author.display_avatar)
        await self.ctx.maybe_edit(embed=embed, view=await self._shortcuts())


class DivingView(UserView):
    def __init__(self, ctx: Context, *, record: UserRecord) -> None:
        self.record = record
        self.ctx = ctx
        super().__init__(ctx.author, timeout=30)

        self._depth: int = 0
        self._oxygen: int = 50

        self._profit: int = 0
        self._multipliers_applied: bool = False
        self._items: defaultdict[Item, int] = defaultdict(int)

    def make_embed(self, *, message: str | None = None, error: bool = False, emoji: str = '\u23ec') -> discord.Embed:
        embed = discord.Embed(color=Colors.warning, timestamp=self.ctx.now)
        embed.set_author(name=f'{self.ctx.author.name}: Diving', icon_url=self.ctx.author.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji(emoji))

        embed.add_field(name='Depth', value=f'{self._depth}m' if self._depth else 'Surface')
        embed.add_field(
            name=f'Oxygen: **{max(0, self._oxygen):,}**/50',
            value=f'{progress_bar(self._oxygen / 50, length=6)}',
        )

        if message is not None:
            embed.description = message
        if error:
            embed.colour = Colors.error
            return embed
        if self._depth >= 50:
            embed.set_footer(
                text=f'Dive Deeper: {self.calculate_pressure_chance(depth=self._depth + 50):.02%} chance of dying from pressure',
            )

        earnings = []
        if self._profit:
            with_multi = (
                f' (applied {self.record.coin_multiplier - 1:.1%} coin multiplier)'
                if self._multipliers_applied and self.record.coin_multiplier > 1 else ''
            )
            earnings.append(f'- {Emojis.coin} **{self._profit:,}**{with_multi}')
        for item, quantity in self._items.items():
            earnings.append(f'- {item.get_display_name(bold=True)} x{quantity}')

        embed.insert_field_at(0, name='Earnings', value='\n'.join(earnings) or 'Nothing yet!', inline=False)
        return embed

    async def suspend(self, interaction: TypedInteraction | None, message: str | None = None) -> None:
        self.stop()
        for button in self.children:
            button.disabled = True

        if interaction is not None:
            return await interaction.response.edit_message(
                embed=self.make_embed(message=message, error=True), view=self,
            )
        await self.ctx.maybe_edit(self.ctx.message, embed=self.make_embed(message=message), view=self)

    async def make_dead(self, interaction: TypedInteraction, message: str) -> None:
        self.stop()
        for button in self.children:
            button.disabled = True

        await self.record.make_dead(reason=message)
        await interaction.response.edit_message(embed=self.make_embed(message=message, error=True), view=self)

    def calculate_pressure_chance(self, *, depth: int | None = None) -> float:
        """Calculate the chance of dying due to water pressure.

        For any "death resistance" factor k, the chance can be calculated as: ::

            -1 / (0.02 * x ** k + 1) + 1

        See <https://www.desmos.com/calculator/bors91xu3x>
        """
        depth = depth if depth is not None else self._depth

        k = 0.46  # this constant will change based on submarine
        return -1 / (0.02 * depth ** k + 1) + 1

    LOSS_MESSAGES = (
        'You got lost and surface back up without any coins or items.',
        'The strong current pulls away your coins and items and you come back empty-handed.',
        'A shark attacks you and you drop all your coins and items while trying to escape. You come back empty-handed.',
    )
    DEATH_MESSAGES = (
        'You got lost and couldn\'t find your way back to the surface. You died.',
        'You got attacked by a shark and died.',
        'You got attacked by a giant squid and died.',
        'You were eaten by a whale. You died.',
    )
    # this could maybe also change based on submarine
    ITEMS = {
        Items.fish: 0.15,
        Items.fish_bait: 0.15,
        Items.crab: 0.15,
        Items.shark: 0.15,
        Items.fishing_pole: 0.1,
        Items.padlock: 0.1,
        Items.banknote: 0.1,
        Items.key: 0.098,
        Items.dynamite: 0.07,
        Items.eel: 0.002,
    }

    @discord.ui.button(label='Dive Deeper', style=discord.ButtonStyle.primary, emoji='\u23ec')
    async def dive_deeper(self, interaction: TypedInteraction, _) -> None:
        self._depth += 50
        self._oxygen -= random.randint(5, 15)

        if self._oxygen <= 0:
            return await self.make_dead(interaction, 'You ran out of oxygen and drowned. You died.')
        # death due to pressure:
        if self._depth > 50 and random.random() < self.calculate_pressure_chance():
            return await self.make_dead(
                interaction, 'You dive a bit too deep and the water pressure crushes you. You died.',
            )
        # general loss chance
        if random.random() < 0.01:  # this number will change based on submarine
            return await self.make_dead(interaction, random.choice(self.DEATH_MESSAGES))
        if random.random() < 0.13:  # this number will change based on submarine
            return await self.suspend(interaction, random.choice(self.LOSS_MESSAGES))

        profit = random.randint(100, 250)
        self._profit += profit

        found = f'{Emojis.coin} **{profit:,}**'

        if random.random() < 0.2:  # item chance. this number will change based on submarine
            item = random.choices(list(self.ITEMS.keys()), weights=list(self.ITEMS.values()))[0]
            self._items[item] += 1
            found += f' and {item.get_sentence_chunk(bold=True)}'

        message = f'You dive deeper into the ocean. At **{self._depth:,} meters** deep you find an additional {found}.'
        await interaction.response.edit_message(embed=self.make_embed(message=message))

    @discord.ui.button(label='Surface', style=discord.ButtonStyle.success, emoji='\u23eb')
    async def surface(self, interaction: TypedInteraction, button: discord.ui.Button):
        self._profit = await self.record.add_coins(self._profit)
        self._multipliers_applied = True

        async with self.record.db.acquire() as conn:
            for item, quantity in self._items.items():
                await self.record.inventory_manager.add_item(item, quantity, connection=conn)

        embed = self.make_embed(
            message='You come back up to the surface safely. Your dive was successful!',
            emoji=str(button.emoji),
        )
        embed.colour = Colors.success

        self.stop()
        for button in self.children:
            button.disabled = True

        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        self.stop()
        await self.ctx.maybe_edit(self.ctx.message, embed=self.make_embed(message='You ran out of time!'), view=self)


class QuestsNavRow(discord.ui.ActionRow['QuestsView']):
    def __init__(self, parent: QuestsContainer) -> None:
        super().__init__()
        self.parent: QuestsContainer = parent

    def update(self) -> None:
        if self.parent.showing_daily_quests:
            self.toggle_daily_quests.label = 'See Recurring Quests'
        else:
            self.toggle_daily_quests.label = 'See Daily Quests'

    @discord.ui.button(label='See Daily Quests', style=discord.ButtonStyle.primary)
    async def toggle_daily_quests(self, interaction: TypedInteraction, _button: discord.ui.Button) -> Any:
        self.parent.showing_daily_quests = not self.parent.showing_daily_quests
        await self.parent.update()

        await interaction.response.edit_message(view=self.view)


class RefreshQuestsButton(discord.ui.Button['QuestsView']):
    def __init__(self, container: QuestsContainer) -> None:
        super().__init__(emoji=Emojis.refresh)
        self.container: QuestsContainer = container

    async def callback(self, interaction: TypedInteraction) -> None:
        await self.container.update()
        await interaction.response.edit_message(view=self.container.view)


class QuestsContainer(discord.ui.Container['QuestsView']):
    def __init__(self) -> None:
        super().__init__(accent_color=Colors.secondary)
        self.showing_daily_quests: bool = False
        self.nav = QuestsNavRow(self)

    @property
    def ctx(self) -> Context:
        return self.view.ctx

    @property
    def quests(self) -> QuestManager:
        return self.view.quests

    async def update(self) -> None:
        self.clear_items()

        subtext = (
            'Showing daily quests' if self.showing_daily_quests else 'Showing recurring quests'
        )
        self.add_item(discord.ui.Section(
            f'### {self.ctx.author.name}\'s Quests\n-# {subtext}\n'
            f'-# You have a total of {Emojis.ticket} **{self.view.record.tickets:,}**',
            accessory=RefreshQuestsButton(self),
        ))
        self.add_item(discord.ui.Separator(spacing=discord.SeparatorSize.large))

        quests = await self.quests.refresh_slots()
        if self.showing_daily_quests:
            show = quests.vote, quests.daily_1, quests.daily_2
        else:
            show = quests.vote, quests.recurring_easy, quests.recurring_mid, quests.recurring_hard

        for i, entry in enumerate(show):
            if isinstance(entry, QuestRecord):
                title = entry.quest.title
                exp = [
                    f'{progress_bar(entry.progress / entry.quest.max_progress)} {entry.progress:,}/{entry.quest.max_progress:,}',
                    f'Reward: {Emojis.ticket} **{entry.quest.tickets:,}**',
                ]
                if entry.expires_at:
                    exp.append(f'Expires {format_dt(entry.expires_at, "R")}')
            else:
                recent = self.quests.get_most_recent_quest_for_slot(entry.slot)
                if not recent:
                    continue
                title = recent.quest.title
                exp = [
                    f'{progress_bar(1.0, length=8, provider=Emojis.GreenProgressBars)} Completed!',
                    f'You received {Emojis.ticket} **{recent.quest.tickets:,}**',
                    f'Refreshes {format_dt(entry.refreshes_at, "R")}',
                ]
            self.add_item(discord.ui.TextDisplay(f'**{title}**\n{expansion_list(exp)}'))
            if i < len(show) - 1:
                self.add_item(discord.ui.Separator(visible=False))

        self.nav.update()
        self.add_item(discord.ui.Separator(visible=False)).add_item(self.nav)
        self.add_item(discord.ui.Separator(spacing=discord.SeparatorSize.large))

        tier, n, d = self.view.record.quest_pass_tier_data
        self.add_item(discord.ui.Section(
            f'### Quest Pass',
            f'{Emojis.quest_pass} **Tier {tier}**\n'
            f'{Emojis.Expansion.first} {Emojis.ticket} {progress_bar(n / d, provider=Emojis.RedProgressBars)} {n:,}/{d:,}\n'
            f'{Emojis.Expansion.last} {QUEST_PASS_REWARDS[tier].short} at next tier',
            accessory=StaticCommandButton(
                command=self.ctx.bot.get_command('quests pass'),
                label='Quest Pass',
                emoji=Emojis.ticket,
            )
        ))


class QuestsView(UserLayoutView):
    def __init__(self, ctx: Context, record: UserRecord) -> None:
        super().__init__(ctx.author, timeout=300)
        self.ctx = ctx
        self.record = record
        self.quests = record.quest_manager
        self.add_item(container := QuestsContainer())
        self._container = container

    async def update(self) -> None:
        await self._container.update()


import math
from app.extensions.pets import NavigableItem, NavigationRow  # TODO put into its own place


class QuestPassContainer(discord.ui.Container['QuestPassView'], NavigableItem):
    REWARDS_PER_PAGE: int = 5

    def __init__(self, ctx: Context, record: UserRecord) -> None:
        super().__init__(accent_color=Colors.secondary)
        self.ctx = ctx
        self.record = record
        self.nav = NavigationRow(self)

        tier, _, _ = self.record.quest_pass_tier_data
        self.page = min(self.max_pages, tier // self.REWARDS_PER_PAGE)

    @property
    def current_page(self) -> int:
        return self.page

    @property
    def max_pages(self) -> int:
        return math.ceil(len(QUEST_PASS_REWARDS) / self.REWARDS_PER_PAGE)

    async def set_page(self, itx: TypedInteraction, page: int):
        self.page = page
        await self.update()
        await itx.response.edit_message(view=self.view)

    @staticmethod
    def _get_reward_emoji(tier: int) -> str:
        out = None
        if reward := reward_for_achieving_tier(tier):
            out = reward.principal_emoji
        return out or Emojis.space

    async def update(self) -> None:
        self.clear_items()

        large_sep = lambda: discord.ui.Separator(spacing=discord.SeparatorSize.large)
        self.add_item(discord.ui.TextDisplay(
            f'### {self.ctx.author.name}\'s Quest Pass\n'
            f'-# You have a total of {Emojis.ticket} **{self.record.tickets:,}**\n'
        )).add_item(large_sep())

        tier, n, d = self.record.quest_pass_tier_data
        emojis = ' '.join(self._get_reward_emoji(t) for t in range(tier - 2, tier + 5))
        chevron = (Emojis.space + ' ') * 2 + '\U0001f53a'
        self.add_item(discord.ui.TextDisplay(f'## {emojis}\n## {chevron}')).add_item(large_sep())
        self.add_item(discord.ui.TextDisplay(
            f'{Emojis.quest_pass} **Tier {tier}**\n'
            f'{Emojis.Expansion.first} {Emojis.ticket} {progress_bar(n / d, provider=Emojis.RedProgressBars)} {n:,}/{d:,}\n'
            f'{Emojis.Expansion.last} Next tier reward: {QUEST_PASS_REWARDS[tier].short}'
        )).add_item(large_sep())

        for i, reward in enumerate(
            QUEST_PASS_REWARDS[self.page * self.REWARDS_PER_PAGE:(self.page + 1) * self.REWARDS_PER_PAGE],
            start=self.page * self.REWARDS_PER_PAGE + 1,
        ):
            if reward is None:
                continue
            self.add_item(discord.ui.TextDisplay(
                f'**Tier {i}** \u2014 {Emojis.ticket} {QUEST_PASS_CURVE.total_exp_needed_to_complete(i - 1):,} total tickets\n'
                f'{Emojis.Expansion.standalone} {reward.short}'
            ))

        self.nav.update()
        self.add_item(large_sep()).add_item(self.nav)


class QuestPassView(UserLayoutView):
    def __init__(self, ctx: Context, record: UserRecord) -> None:
        super().__init__(ctx.author, timeout=300)
        self.ctx = ctx
        self.record = record
        self.add_item(container := QuestPassContainer(ctx, record))
        self._container = container

    async def update(self) -> None:
        await self._container.update()


setup = Profit.simple_setup
