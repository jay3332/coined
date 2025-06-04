from __future__ import annotations

import asyncio
import datetime
import random
import time
from math import ceil
from typing import Any, Final, Iterator, TYPE_CHECKING, cast

import aiohttp
import discord
from discord import app_commands, ui
from discord.ext import commands
from discord.ext.commands import BadArgument
from discord.utils import format_dt

from app.core import Cog, Context, command, group, simple_cooldown
from app.core.helpers import BAD_ARGUMENT, EDIT, REPLY, cooldown_message, user_max_concurrency
from app.database import PetManager, PetRecord
from app.data.items import Item, ItemType, Items, NetMetadata
from app.data.pets import DEFAULT_PET_WEIGHTS, Pet, PetRarity, Pets
from app.extensions.profit import Profit
from app.util import converters
from app.util.common import (
    get_by_key,
    image_url_from_emoji,
    ordinal,
    progress_bar,
    query_collection,
    query_collection_many,
    walk_collection,
    weighted_choice,
)
from app.util.converters import get_amount, try_query_item
from app.util.pagination import LineBasedFormatter, Paginator
from app.util.views import StaticCommandButton, UserView
from config import Colors, Emojis

if TYPE_CHECKING:
    from app.database import PetRecord, UserRecord
    from app.util.types import CommandResponse, TypedInteraction

    query_pet = Pet
else:
    def query_pet(query: str) -> Pet:
        if pet := query_collection(Pets, Pet, query):
            return pet
        raise BadArgument(f'No pet named {query!r} found.')


def has_operations(count: int):
    async def predicate(ctx: Context) -> bool:
        record = await ctx.db.get_user_record(ctx.author.id)
        expiry = record.pet_operations_cooldown_start + PetsCog.PET_MAX_OPERATIONS_WINDOW
        if ctx.now > expiry or record.pet_operations + count <= PetsCog.PET_MAX_OPERATIONS_COUNT:
            return True

        raise BadArgument(
            f'You have no more pet swaps remaining. They will replenish {format_dt(expiry, "R")}.'
        )
    return commands.check(predicate)


class HuntHelpButton(discord.ui.Button):
    def __init__(self, ctx: Context) -> None:
        super().__init__(style=discord.ButtonStyle.primary, label='View the Hunting Guide', row=2)
        self.ctx = ctx

    async def callback(self, interaction: TypedInteraction) -> None:
        self.ctx.interaction = interaction
        await self.ctx.send_guide('hunt')


class PetsCog(Cog, name='Pets'):
    """Hunt, manage, and raise your pets!"""

    emoji = '\U0001f415\u200d\U0001f9ba'

    PET_MAX_OPERATIONS_COUNT: Final[int] = 10  # 5 swaps
    PET_MAX_OPERATIONS_WINDOW: Final[datetime.timedelta] = datetime.timedelta(hours=2)

    def __setup__(self) -> None:
        self._last_measured_api_latency: float | None = None
        self._last_measured_api_latency_minute: int | None = None

    async def get_api_latency(self, default: float = 0.1) -> float:
        # discordstatus refreshes API latency every minute, so we can cache the value for a minute
        perf_minute = time.perf_counter_ns() // 60_000_000_000  # determine the current minute
        if perf_minute == self._last_measured_api_latency_minute and self._last_measured_api_latency is not None:
            # if we've already measured the latency this minute, return the last measured value
            return self._last_measured_api_latency

        api_latency = default  # this is a graceful method, assume a default latency
        timeout = aiohttp.ClientTimeout(total=3)
        try:
            # endpoint to discordstatus which can tell us how the REST API is performing today
            async with self.bot.session.get(
                'https://discordstatus.com/metrics-display/5k2rt9f7pmny/day.json',
                timeout=timeout,
            ) as resp:
                data = await resp.json()
                api_latency = self._last_measured_api_latency = data['metrics'][0]['summary']['mean'] / 1000
                self._last_measured_api_latency_minute = perf_minute
        except asyncio.TimeoutError:
            pass
        return api_latency

    async def get_tolerable_wait_time(self, *, window: float, min: float = 0.5) -> float:
        # within the allowed window of time to react and click:
        # 1. bot sends request to REST
        # 2. user receives request through gateway (assuming they use official discord client)
        # 3. user reacts
        # 4. user clicks button, sending interaction through REST
        # 5. bot receives interaction through gateway
        # in total, a response takes the time of two WS messages plus two REST messages plus reaction time
        api_latency = await self.get_api_latency(default=0.1)  # default to 0.1s if we can't get the latency
        predicted = (self.bot.average_latency + api_latency) * 2  # Predicted response time
        return window + max(
            predicted,
            min,  # If Discord is performing well today, (as if it ever does), a lower bound can be specified
        )

    @command(aliases={'catch', 'hu', 'ct', 'h'}, hybrid=True)
    @simple_cooldown(1, 90)
    @cooldown_message("There aren't unlimited animals to hunt!")
    @user_max_concurrency(1)
    async def hunt(self, ctx: Context) -> None:
        """Hunt for animals, catch them, and raise them as pets for special powers!"""
        record = await ctx.db.get_user_record(ctx.author.id)
        inventory = await record.inventory_manager.wait()

        available: Iterator[Item[NetMetadata]] = (
            item for item, quantity in inventory.cached.items() if quantity > 0 and item.type is ItemType.net
        )
        net: Item[NetMetadata] | None = None
        extra = 'with your bare hands'
        tip = f'\n\U0001f4a1 **Tip:** Buy nets from the shop to hunt rarer pets!'
        try:
            net = max(available, key=lambda item: item.metadata.priority)
            extra = f'with your {net.get_display_name(bold=True)}'
            weights = net.metadata.weights.copy()
            tip = ''
        except ValueError:
            weights = DEFAULT_PET_WEIGHTS.copy()

        rare_multiplier = 1.0
        pets = await record.pet_manager.wait()
        if jaguar := pets.get_active_pet(Pets.jaguar):
            rare_multiplier += 0.01 + jaguar.level * 0.005
        if tiger := pets.get_active_pet(Pets.tiger):
            rare_multiplier += 0.02 + tiger.level * 0.01

        for key in weights:
            if key and key.rarity >= PetRarity.rare:
                weights[key] *= rare_multiplier

        yield f'{Emojis.loading} Hunting for pets {extra}...{tip}', REPLY
        cont = await Profit._get_command_shortcuts(ctx, record)

        pet = weighted_choice(weights)
        await asyncio.sleep(random.uniform(2, 4))

        if pet is None:
            yield f"You went hunting for pets, but couldn't spot any.", cont, EDIT
            return

        view = HuntView(ctx, record, cont)
        message = (
            f'{ctx.author.mention}, you went hunting for pets and you spotted something...\n'
            'When you see the pet below, click the button to catch it!'
        )
        yield message, view, EDIT
        await asyncio.sleep(random.uniform(3, 6))

        # discord.py has a bit of a problem individually setting a child
        children = view.children  # the children getter implicitly does an implicit copy
        view.clear_items()
        position, bomb_position = random.sample(range(len(children)), k=2)
        if view.is_finished():
            return

        button = None
        for i, child in enumerate(children):
            if i == position:
                view.add_item(button := HuntTargetButton(pet, row=child.row))
                continue
            if i == bomb_position:
                view.add_item(HuntBombButton(net, row=child.row))
                continue
            view.add_item(child)

        sleep_time = await self.get_tolerable_wait_time(window=1.35)
        yield message, view, EDIT
        await asyncio.sleep(sleep_time)

        if view.is_finished():
            return

        view.finish()
        button.style = discord.ButtonStyle.primary
        yield 'You were too slow and the pet escaped! Better luck next time.', view, cont, EDIT

    @group(aliases={'pet', 'zoo', 'p'}, hybrid=True, expand_subcommands=True)
    async def pets(self, ctx: Context, *, pet: query_pet = None) -> None:
        """View and manage equipped pets."""
        if pet is not None:
            return await ctx.invoke(self.pets_info, pet=pet)  # type: ignore

        await ctx.invoke(self.pets_view)  # type: ignore

    @pets.command(name='view', aliases={'equipped'}, hybrid=True, hidden=True)
    async def pets_view(self, ctx: Context) -> CommandResponse:
        """View and manage equipped pets."""
        record = await ctx.db.get_user_record(ctx.author.id)
        pets = await record.pet_manager.wait()

        if not pets.cached:
            mention = ctx.bot.tree.get_app_command(self.hunt.app_command.qualified_name).mention
            return f'You have no pets! Go hunt for some with {mention}.'

        view = ActivePetsView(ctx)
        await view.container.prepare()
        return view, REPLY

    @pets.command(name='all', aliases={'a', '*', 'discovered'}, hybrid=True)
    async def pets_all(self, ctx: Context) -> CommandResponse:
        """List all discovered pets."""
        view = PetsAllLayout(ctx)
        await view.prepare()
        return view, REPLY

        record = await ctx.db.get_user_record(ctx.author.id)
        pets = await record.pet_manager.wait()

        if not pets.cached:
            mention = ctx.bot.tree.get_app_command(self.hunt.app_command.qualified_name).mention
            return f'You have no pets! Go hunt for some with {mention}.'

        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.description = (
            f'You have discovered **{len(pets.cached)}** out of {sum(1 for _ in walk_collection(Pets, Pet))} pets.'
        )
        embed.set_author(name=f"{ctx.author.name}'s Pets", icon_url=ctx.author.avatar)
        embed.set_thumbnail(url=image_url_from_emoji('\U0001f43e'))

        formatter = LineBasedFormatter(
            embed=embed,
            field_name=f'Discovered Pets ({len(pets.cached)})',
            lines=[
                f'**{entry.pet.display}** {entry.pet.rarity.emoji} \u2014 Level {entry.level:,} '
                + ('[EQUIPPED]' if entry.equipped else '')
                for entry in sorted(pets.cached.values(), key=lambda entry: (-entry.pet.rarity.value, entry.pet.name))
            ],
            per_page=10,
        )

        return Paginator(ctx, formatter), REPLY

    @pets.command(name='info', aliases={'i', 'details', 'stats'}, hybrid=True)
    @app_commands.describe(pet='The pet to view information on.')
    async def pets_info(self, ctx: Context, *, pet: query_pet) -> CommandResponse:
        """View information on a particular pet."""
        record = await ctx.db.get_user_record(ctx.author.id)
        pets = await record.pet_manager.wait()
        entry = pets.cached.get(pet)

        evolution = f' (Evolution {entry.evolution})' if entry and entry.evolution else ''
        embed = discord.Embed(
            title=pet.display + evolution, description=pet.description, color=Colors.primary,
            timestamp=ctx.now,
        )
        embed.set_author(name='Pet Info', icon_url=ctx.author.avatar)
        embed.set_thumbnail(url=image_url_from_emoji(pet.emoji))

        embed.add_field(
            name='General',
            value=(
                f'Name: **{pet.display}**\n'
                f'Query Key: `{pet.key}`\n'
                f'Rarity: **{pet.rarity.name.title()}** {pet.rarity.emoji}\n'
                f'Energy Usage: {Emojis.bolt} **{pet.energy_per_minute * 60:,.1f}/hour**'
            ),
        )
        level = 0 if entry is None else entry.level
        embed.add_field(name=f'\U0001f634 Passive Abilities', value=pet.benefit(level), inline=False)
        if abilities := pet.abilities:
            embed.add_field(name=f'\U0001f3c3\u200d\u2642\ufe0f Active Abilities', value=abilities(level), inline=False)

        if entry is None:
            embed.description += '\n*You have not discovered this pet yet.*'
            embed.insert_field_at(
                index=1,
                name='Base Stats',
                value=(
                    f'Max Level: **{pet.max_level:,}**\n'
                    f'Max Energy: {Emojis.bolt} **{pet.max_energy:,}**'
                ),
            )
            return embed, REPLY

        _, exp, requirement = entry.level_data
        ratio = exp / requirement
        embed.add_field(
            name=f'\u2728 **Level {entry.level:,}** ({exp:,}/{requirement} XP)',
            value=(
                f'{progress_bar(ratio, length=8)}  {ratio:.1%}'
                if entry.level < pet.max_level
                else 'MAX LEVEL!'
            ),
            inline=False,
        )
        if entry.equipped:
            ratio = entry.energy / entry.max_energy
            feed_mention = ctx.bot.tree.get_app_command('feed').mention
            embed.add_field(
                name=(
                    f'{Emojis.bolt} **{entry.energy:,}**/{entry.max_energy:,} Energy'
                    + (
                        f' (\u23f0 Runs out {format_dt(entry.exhausts_at, "R")})'
                        if entry.energy > 0 else ''
                    )
                ),
                value=(
                    f'{progress_bar(ratio, length=8)}  {ratio:.1%}'
                    + (
                        f'\n\n\u26a0\ufe0f **No energy left!** Feed this pet to restore energy. ({feed_mention})\n'
                        '*Pet abilities only apply when they have enough energy.*'
                        if entry.energy <= 0 else ''
                    )
                ),
            )
        else:
            embed.add_field(
                name=f'{Emojis.neutral} Unequipped',
                value=(
                    f'\u26a0\ufe0f Abilities are not active for this pet because it is not equipped.\n'
                    f'To equip this pet, run `{ctx.clean_prefix}pets equip {pet.key}`.'
                )
            )
        embed.insert_field_at(
            index=1,
            name='Stats',
            value=(
                f'Duplicates: **{entry.duplicates:,}**\n'
                f'Max Level: **{pet.max_level:,}**\n'
                f'Max Energy: {Emojis.bolt} **{pet.max_energy:,}**'
            )
        )

        view = discord.ui.View()
        if entry.equipped:
            view.add_item(StaticCommandButton(
                command=self.feed,
                command_kwargs=dict(pet=pet),
                label='Feed',
                emoji=Emojis.bolt,
                style=discord.ButtonStyle.primary,
            ))
            view.add_item(StaticCommandButton(
                command=self.pets_unequip,  # type: ignore
                command_kwargs=dict(pet=pet),
                label='Unequip this Pet',
                style=discord.ButtonStyle.danger,
            ))
        else:
            view.add_item(StaticCommandButton(
                command=self.pets_equip,  # type: ignore
                command_kwargs=dict(pet=pet),
                label='Equip this Pet',
                style=discord.ButtonStyle.primary,
            ))

        return embed, view, REPLY

    @classmethod
    async def refresh_operations(cls, ctx: Context, record: UserRecord, operations: int, *, connection) -> str:
        if record.pet_operations_cooldown_start + cls.PET_MAX_OPERATIONS_WINDOW < ctx.now:
            await record.update(pet_operations_cooldown_start=ctx.now, pet_operations=operations, connection=connection)
        else:
            await record.add(pet_operations=operations, connection=connection)

        remaining = cls.PET_MAX_OPERATIONS_COUNT - record.pet_operations
        return f'You have {remaining / 2:g} pet swaps remaining.'

    @pets.command(name='equip', aliases={'+', 'e', 'eq', 'activate', 'add'}, hybrid=True)
    @app_commands.describe(pet='The pet to equip.')
    @has_operations(1)
    async def pets_equip(self, ctx: Context, *, pet: query_pet = None) -> CommandResponse:
        """Equip a pet. This will activate its effects and abilities."""
        record = await ctx.db.get_user_record(ctx.author.id)
        pets = await record.pet_manager.wait()
        if pet is None:
            item = EquipPetSelect(ctx, record)
            view = UserView(ctx.author, timeout=120).add_item(item)
            return EquipMorePets.MESSAGE, view, REPLY

        if pet not in pets.cached:
            return f'You have not discovered a **{pet.display}** yet.', BAD_ARGUMENT

        entry = pets.cached[pet]
        if entry.equipped:
            return f'Your **{pet.display}** is already equipped.', BAD_ARGUMENT

        if pets.equipped_count >= record.max_equipped_pets:
            swap_mention = ctx.bot.tree.get_app_command('pets swap').mention
            return (
                f'You have filled all {record.max_equipped_pets} equipped pet slots.\n'
                f'Use {swap_mention} to swap out a pet instead.'
            ), BAD_ARGUMENT

        async with ctx.db.acquire() as conn:
            await entry.update(equipped=True, connection=conn)
            swaps = await self.refresh_operations(ctx, record, 1, connection=conn)

        ctx.bot.loop.create_task(ctx.thumbs())
        view = ui.View().add_item(StaticCommandButton(
            command=self.feed,
            command_kwargs=dict(pet=pet),
            label='Feed this Pet',
            emoji=Emojis.bolt,
            style=discord.ButtonStyle.primary,
        ))
        return f'Equipped your **{pet.display}**.\n{swaps}', view, REPLY

    @pets.command(name='unequip', aliases={'-', 'u', 'deactivate', 'remove'}, hybrid=True)
    @app_commands.describe(pet='The pet to unequip.')
    @has_operations(1)
    async def pets_unequip(self, ctx: Context, *, pet: query_pet) -> CommandResponse:
        """Unequip a pet. This will remove its effects and abilities."""
        record = await ctx.db.get_user_record(ctx.author.id)
        pets = await record.pet_manager.wait()
        if pet not in pets.cached:
            return f'You have not discovered a **{pet.display}** yet.', BAD_ARGUMENT

        entry = pets.cached[pet]
        if not entry.equipped:
            return f'Your **{pet.display}** is not equipped.', BAD_ARGUMENT

        async with ctx.db.acquire() as conn:
            await entry.update(equipped=False)
            swaps = await self.refresh_operations(ctx, record, 1, connection=conn)

        ctx.bot.loop.create_task(ctx.thumbs())
        return f'Unequipped your **{pet.display}**.\n{swaps}', REPLY

    @pets.command(name='swap', aliases={'s', 'switch', 'change', '~'}, hybrid=True)
    @app_commands.describe(to_unequip='The pet to unequip and replace.', to_equip='The pet to equip.')
    @has_operations(2)
    async def pets_swap(self, ctx: Context, to_unequip: query_pet, *, to_equip: query_pet) -> CommandResponse:
        """Swap an equipped pet with an unequipped pet."""
        if to_unequip == to_equip:
            return 'You cannot swap a pet with itself.', REPLY

        record = await ctx.db.get_user_record(ctx.author.id)
        pets = await record.pet_manager.wait()
        if to_unequip not in pets.cached:
            return f'You have not discovered a **{to_unequip.display}** yet.', BAD_ARGUMENT
        if to_equip not in pets.cached:
            return f'You have not discovered a **{to_equip.display}** yet.', BAD_ARGUMENT

        unequip_entry = pets.cached[to_unequip]
        equip_entry = pets.cached[to_equip]
        if not unequip_entry.equipped:
            return f'Your **{to_unequip.display}** is not equipped.', BAD_ARGUMENT
        if equip_entry.equipped:
            return f'Your **{to_equip.display}** is already equipped.', BAD_ARGUMENT

        async with ctx.db.acquire() as conn:
            await unequip_entry.update(equipped=False, connection=conn)
            await equip_entry.update(equipped=True, connection=conn)
            swaps = await self.refresh_operations(ctx, record, 2, connection=conn)

        ctx.bot.loop.create_task(ctx.thumbs())
        return f'Swapped your **{to_unequip.display}** with your **{to_equip.display}**.\n{swaps}', REPLY

    @command(aliases={'fe'}, hybrid=True)
    @app_commands.describe(pet='The pet to feed.')
    @simple_cooldown(1, 10)
    async def feed(self, ctx: Context, *, pet: query_pet = None) -> CommandResponse:
        """Feed a pet food to give it energy."""
        record = await ctx.db.get_user_record(ctx.author.id)
        await record.inventory_manager.wait()
        pets = await record.pet_manager.wait()
        if pet is not None and pet not in pets.cached:
            return f'You have not discovered a **{pet.display}** yet.', BAD_ARGUMENT
        if not pets.equipped_count:
            equip_mention = ctx.bot.tree.get_app_command('pets equip').mention
            return (
                f'You have no equipped pets to feed. Equip a pet using {equip_mention}, then use this command again!',
                BAD_ARGUMENT,
            )

        entry = next(entry for entry in pets.cached.values() if entry.equipped) if pet is None else pets.cached[pet]  # type: ignore
        if not entry.equipped:
            return f'Your **{pet.display}** is not equipped.', BAD_ARGUMENT  # type: ignore
        view = FeedView(ctx, record, entry)
        return *view.make_embeds(), view, REPLY

    @pets_info.autocomplete('pet')
    @pets_equip.autocomplete('pet')
    @pets_unequip.autocomplete('pet')
    @pets_swap.autocomplete('to_unequip')
    @pets_swap.autocomplete('to_equip')
    @feed.autocomplete('pet')
    async def autocomplete_pet(self, _interaction: TypedInteraction, current: str):
        return [
            app_commands.Choice(name=pet.name, value=pet.key)
            for pet in query_collection_many(Pets, Pet, current)
        ]


def _format_level_data(record: PetRecord) -> str:
    level, current, required = record.level_data
    if level >= record.pet.max_level:
        return f'Level {level} (MAX)'
    return f'Level {level} ({current:,}/{required:,} XP)'


class EquipPetSelect(ui.Select):
    def __init__(self, ctx: Context, record: UserRecord, *, parent: ActivePetsContainer | None = None) -> None:
        super().__init__(placeholder='Select pets to equip', min_values=0, max_values=record.max_equipped_pets)

        self.ctx: Context = ctx
        self.record: UserRecord = record
        self.pets: PetManager = record.pet_manager
        self.parent: ActivePetsContainer | None = parent

        for pet, record in self.pets.cached.items():
            self.add_option(
                label=pet.name,
                emoji=pet.emoji,
                value=pet.key,
                description=f'Level {record.level} \u2022 {record.energy} Energy',
                default=record.equipped,
            )

        for i in range(max(0, 3 - len(self.pets.cached))):  # fill empty slots
            self.add_option(label='\u200b', value=f'null:{i}', default=False)

    async def callback(self, interaction: TypedInteraction) -> Any:
        values = set(get_by_key(Pets, key) for key in self.values if not key.startswith('null:'))
        equipped = set(pet for pet, record in self.pets.cached.items() if record.equipped)

        added = values - equipped
        removed = equipped - values

        operations = len(added) + len(removed)
        if operations == 0:
            return await interaction.response.send_message('No modifications made.', ephemeral=True)

        expiry = self.record.pet_operations_cooldown_start + PetsCog.PET_MAX_OPERATIONS_WINDOW
        if self.ctx.now <= expiry and self.record.pet_operations + operations > PetsCog.PET_MAX_OPERATIONS_COUNT:
            return await interaction.response.send_message(
                f'You have no more pet swaps remaining. They will replenish {format_dt(expiry, "R")}.',
                ephemeral=True,
            )

        remaining = (
            (PetsCog.PET_MAX_OPERATIONS_COUNT - self.record.pet_operations)
            or PetsCog.PET_MAX_OPERATIONS_COUNT
        )
        if not await self.ctx.confirm(
            'Are you want sure you want to update your equipped pets?\n'
            f'This will cost you **{operations / 2}** pet swaps (you have {remaining / 2} remaining).',
            interaction=interaction,
            ephemeral=True,
            edit=False,
            timeout=30,
            replace_interaction='next_interaction',
        ):
            # if the user doesn't confirm, we need to reset the select menu
            self.view.clear_items()
            self.view.add_item(self)
            return await getattr(self.ctx, 'next_interaction').response.edit_message(
                content='Looks like we won\'t be swapping pets today.',
                view=None,
            )

        cog: PetsCog = cast(self.ctx.cog, PetsCog)

        async with self.ctx.db.acquire() as conn:
            out = await cog.refresh_operations(self.ctx, self.record, operations, connection=conn)
            for pet in added:
                await self.pets.cached[pet].update(equipped=True, connection=conn)
            for pet in removed:
                await self.pets.cached[pet].update(equipped=False, connection=conn)

        if self.parent:
            await self.parent.update()
        self.disabled = True

        view = discord.ui.View().add_item(StaticCommandButton(
            command=self.ctx.bot.get_command('pets view'),
            label='View Equipped Pets',
            style=discord.ButtonStyle.primary,
        ))
        await getattr(self.ctx, 'next_interaction').response.edit_message(
            content=f'Successfully updated your equipped pets using **{operations / 2}** pet swaps.\n-# {out}\n',
            view=view,
        )
        if self.parent:
            await self.ctx.maybe_edit(view=self.parent.view)


class EquipMorePets(discord.ui.Button):
    MESSAGE = (
        'Select the pets you want to equip or keep equipped.\n'
        '-# **Note:** You are limited to set number of pet swaps every few hours. Choose wisely.'
    )

    def __init__(self, container: 'ActivePetsContainer') -> None:
        super().__init__(label='Equip More Pets')
        self.container: ActivePetsContainer = container

    async def callback(self, interaction: TypedInteraction) -> None:
        self.original: discord.Message = interaction.message
        item = EquipPetSelect(self.container.ctx, self.container.record, parent=self.container)
        view = discord.ui.View().add_item(item)

        await interaction.response.send_message(self.MESSAGE, ephemeral=True, view=view)


class UnequipButton(StaticCommandButton):
    def __init__(self, ctx: Context, record: PetRecord, container: 'ActivePetsContainer') -> None:
        super().__init__(
            command=ctx.bot.get_command('pets unequip'),
            command_kwargs=dict(pet=record.pet),
            label='Unequip',
            style=discord.ButtonStyle.secondary,
        )
        self.parent = container

    async def callback(self, interaction: TypedInteraction) -> None:
        self.original: discord.Message = interaction.message
        await super().callback(interaction)

        await self.parent.update()
        await self.original.edit(view=self.view)


class RefreshPetsButton(ui.Button):
    def __init__(self, container: 'ActivePetsContainer') -> None:
        super().__init__(emoji=Emojis.refresh, style=discord.ButtonStyle.secondary)
        self.container: ActivePetsContainer = container

    async def callback(self, interaction: TypedInteraction) -> None:
        await self.container.update()
        await interaction.response.edit_message(view=self.view)


class ActivePetsContainer(ui.Container):
    def __init__(self, ctx: Context):
        super().__init__()
        self.ctx: Context = ctx
        self._accent_color = Colors.secondary

    async def prepare(self) -> None:
        self.record: UserRecord = await self.ctx.fetch_author_record()
        self.pets: PetManager = await self.record.pet_manager.wait()
        await self.update()

    async def update(self) -> None:
        self.clear_items()

        equip_mention = self.ctx.bot.tree.get_app_command('pets equip').mention
        swap_mention = self.ctx.bot.tree.get_app_command('pets swap').mention

        if not self.pets.equipped_count:
            self.accent_color = None
            self.add_item(ui.TextDisplay(
                f"You haven't equipped any pets yet.\nPets must be equipped to activate their abilities!\n"
                f"Use {equip_mention} to equip pets."
            ))
            self.add_item(ui.Separator()).add_item(ui.ActionRow().add_item(EquipMorePets(self)))
            return

        self.accent_color = self._accent_color
        self.add_item(
            ui.Section(
                f'### {self.ctx.author.display_name}\'s Equipped Pets',
                f'-# You have equipped **{self.pets.equipped_count}**/{self.record.max_equipped_pets} pets.',
                (
                    f'-# Use {equip_mention} to equip more pets.'
                    if self.pets.equipped_count < self.record.max_equipped_pets
                    else f'-# You have filled all pet slots! Unequip or {swap_mention} some pets.'
                ),
                accessory=RefreshPetsButton(self),
            )
        )

        equipped = (pet for pet in self.pets.cached.values() if pet.equipped)
        for i, record in enumerate(sorted(equipped, key=lambda entry: entry.pet.rarity.value, reverse=True)):
            self.add_item(ui.Separator(
                spacing=discord.SeparatorSize.large if i == 0 else discord.SeparatorSize.small,
            ))
            self.add_item(ui.TextDisplay(
                record.pet.jumbo_display(
                    f'**{record.pet.name}** {record.pet.rarity.emoji}',
                    _format_entry(record),
                ),
                # accessory=discord.ui.Thumbnail(media=image_url_from_emoji(record.pet.emoji)),
            ))
            self.add_item(ui.ActionRow().add_item(
                StaticCommandButton(
                    command=self.ctx.bot.get_command('feed'),
                    command_kwargs=dict(pet=record.pet),
                    label='Feed',
                    emoji=Emojis.bolt,
                    style=discord.ButtonStyle.primary,
                )
            ).add_item(
                StaticCommandButton(
                    command=self.ctx.bot.get_command('pets info'),
                    command_kwargs=dict(pet=record.pet),
                    label='Info',
                    style=discord.ButtonStyle.secondary,
                )
            ).add_item(UnequipButton(self.ctx, record, self)))

        self.add_item(ui.Separator(spacing=discord.SeparatorSize.large))
        self.add_item(
            ui.ActionRow().add_item(
                StaticCommandButton(command=self.ctx.bot.get_command('pets all'), label='View All Pets')
            ).add_item(EquipMorePets(self))
        )


class ActivePetsView(ui.LayoutView):
    def __init__(self, ctx: Context) -> None:
        super().__init__(timeout=120)
        self.add_item(container := ActivePetsContainer(ctx))
        self.container = container


class HuntMissButton(discord.ui.Button['HuntView']):
    def __init__(self, ctx: Context, *, row: int) -> None:
        super().__init__(emoji=Emojis.space, row=row)
        self.ctx = ctx

    async def callback(self, itx: TypedInteraction) -> None:
        self.style = discord.ButtonStyle.red
        self.view.finish()

        self.view.followup_view.add_item(HuntHelpButton(self.ctx))
        await itx.response.edit_message(view=self.view)
        await itx.followup.send(
            'You missed the pet and it ran away! Better aim next time.', view=self.view.followup_view,
        )


class HuntBombButton(discord.ui.Button['HuntView']):
    def __init__(self, net: Item[NetMetadata], *, row: int) -> None:
        super().__init__(emoji='\U0001f4a3', row=row)
        self.net = net

    async def callback(self, itx: TypedInteraction) -> None:
        self.style = discord.ButtonStyle.red
        self.view.finish()

        lost = f'lost your {self.net.get_display_name(bold=True)} and ' if self.net else ''
        async with self.view.ctx.db.acquire() as conn:
            await self.view.record.inventory_manager.add_item(self.net, -1, connection=conn)
            await self.view.record.make_dead(
                reason='A bomb exploded in your hand while hunting, killing you instantly.', connection=conn,
            )

        await itx.response.edit_message(view=self.view)
        await itx.followup.send(
            f'{self.emoji} You clicked the bomb and you explode. You {lost}died.',
            view=self.view.followup_view,
        )


class HuntTargetButton(discord.ui.Button['HuntView']):
    def __init__(self, pet: Pet, *, row: int) -> None:
        super().__init__(emoji=pet.emoji, row=row)
        self.pet = pet

    async def callback(self, itx: TypedInteraction) -> None:
        self.style = discord.ButtonStyle.green
        self.view.finish()

        embed = discord.Embed(color=Colors.success)
        embed.set_author(name=f'{itx.user.name}: Caught a pet!', icon_url=itx.user.avatar)
        embed.set_thumbnail(url=image_url_from_emoji(str(self.emoji)))
        embed.description = f'You caught a {self.pet.rarity.name} **{self.pet.display}**!\n*{self.pet.description}*'

        pets = await self.view.record.pet_manager.wait()
        if pet_record := pets.cached.get(self.pet):
            previous_level = pet_record.level

            exp = 0 if pet_record.is_max_level else random.randint(5, 15)
            await pet_record.add(exp=exp, duplicates=1)

            embed.description += f'\nThis is your **{ordinal(pet_record.duplicates)}** duplicate.'
            new_level = pet_record.level

            text = (
                '**MAX LEVEL**' if pet_record.is_max_level
                else f'{self.pet.name} XP **+{exp:,}** ({pet_record.exp:,}/{pet_record.exp_requirement:,})'
            )
            embed.add_field(
                name='\U0001f4cb Duplicate Pet!',
                value=f'You already have a **{self.pet.display}**.\n\u2728 {text}',
                inline=False
            )
            if new_level > previous_level:
                embed.add_field(
                    name=f'\U0001f52e **LEVEL UP!** {previous_level} {Emojis.arrow} {new_level}',
                    value=f'**Updated Abilities:**\n{self.pet.full_abilities(new_level)}',
                    inline=False,
                )
        else:
            await pets.add_pet(self.pet)
            embed.add_field(name=f'\U0001f634 Passive Abilities', value=self.pet.benefit(0), inline=False)
            if abilities := self.pet.abilities:
                embed.add_field(name=f'\U0001f3c3\u200d\u2642\ufe0f Active Abilities', value=abilities(0), inline=False)

        embed.set_footer(text=f'Use {self.view.ctx.clean_prefix}pets info {self.pet.key} to see more details!')
        await itx.response.edit_message(view=self.view)

        self.view.followup_view.add_item(StaticCommandButton(
            command=self.view.ctx.bot.get_command('pets equip'),
            command_kwargs=dict(pet=self.pet),
            label='Equip Pet',
            style=discord.ButtonStyle.primary,
            row=0,
        ))
        self.view.followup_view.add_item(StaticCommandButton(
            command=self.view.ctx.bot.get_command('pets info'),
            command_kwargs=dict(pet=self.pet),
            label='View Pet Info',
            style=discord.ButtonStyle.secondary,
            row=0,
        ))
        await itx.followup.send(embed=embed, view=self.view.followup_view)


class HuntView(UserView):  # CHANGE TO UserView
    def __init__(self, ctx: Context, record: UserRecord, continuation: discord.ui.View | None = None) -> None:
        super().__init__(ctx.author, timeout=15)  # if this doesn't time out in 15 seconds, an error likely occured
        for i in range(20):
            self.add_item(HuntMissButton(ctx, row=i % 5))
        self.ctx = ctx
        self.record = record
        self.followup_view: discord.ui.View | None = continuation

    def finish(self) -> None:
        self.stop()
        for button in self.children:
            button.disabled = True


def _format_entry(entry: PetRecord) -> str:
    warning = (
        f'(exhausts {format_dt(entry.exhausts_at, "R")})'
        if entry.energy > 0 else '\u26a0\ufe0f'
    )

    exp = Emojis.Expansion
    energy_pbar = progress_bar(entry.energy / entry.max_energy, length=6)
    return (
        f'\u2728 {_format_level_data(entry)}\n'
        f'{Emojis.space * 2}  {Emojis.bolt} {entry.energy:,}/{entry.max_energy:,} '
        f'{energy_pbar} {warning}'
    )


class FeedPetButton(discord.ui.Button['FeedView']):
    def __init__(self, entry: PetRecord) -> None:
        super().__init__(emoji=entry.pet.emoji, style=discord.ButtonStyle.secondary, row=0, label=entry.pet.name)
        self.entry = entry

    async def callback(self, interaction: TypedInteraction) -> None:
        if self.view.entry == self.entry:
            return await interaction.response.defer()

        self.view.entry = self.entry
        self.view.update_view()
        await interaction.response.edit_message(embeds=self.view.make_embeds(), view=self.view)


class SearchItemModal(discord.ui.Modal):
    item = discord.ui.TextInput(
        label='Which item do you want to feed to your pet?',
        placeholder='Enter an item name... (e.g. "fish")',
        min_length=2,
        max_length=32,
        required=True,
    )

    def __init__(self, parent: FeedView) -> None:
        super().__init__(title='Feed Item')
        self.parent = parent

    async def on_submit(self, interaction: TypedInteraction, /) -> Any:
        if item := try_query_item(self.item.value):
            # use linear search instead of binary search in case item quantities change, which would change the sort key
            try:
                self.parent.index = self.parent.items.index(item)
            except ValueError:
                return await interaction.response.send_message(
                    f'{item.display_name} is not a feedable item.', ephemeral=True,
                )
            self.parent.update_view()
            return await interaction.response.edit_message(embeds=self.parent.make_embeds(), view=self.parent)

        await interaction.response.send_message(
            f'Could not find an item named "{self.item.value}".', ephemeral=True,
        )


class FeedCustomModal(discord.ui.Modal):
    quantity = discord.ui.TextInput(
        label='How many of this item do you want to feed?',
        placeholder='Enter a quantity, e.g. a number like "5" or "half"...',
        min_length=1,
        max_length=7,
        required=True,
    )

    def __init__(self, parent: FeedView) -> None:
        super().__init__(timeout=60, title='Feed Item')
        self.parent = parent

    async def on_submit(self, interaction: TypedInteraction, /) -> None:
        max = self.parent.max
        item = self.parent.current_item
        owned = self.parent.inventory.cached.quantity_of(item)
        try:
            quantity = get_amount(owned, 1, max, self.quantity.value)
        except converters.PastMinimum:
            raise BadArgument(f'You must feed at least one {item.display_name}.')
        except converters.ZeroQuantity:
            raise BadArgument(f"You don't have any {item.get_display_name(plural=True)} item anymore.")
        except converters.NotAnInteger:
            raise BadArgument(
                f'Invalid quantity {self.quantity.value!r}. Make sure you pass in a positive integer.',
            )
        except converters.NotEnough:
            raise BadArgument(f'You only have {item.get_sentence_chunk(owned, bold=False)}.')
        except ZeroDivisionError:
            raise BadArgument('Very funny, division by 0.')

        await self.parent.do_feed(interaction, quantity)


class FeedView(UserView):
    def __init__(self, ctx: Context, record: UserRecord, entry: PetRecord) -> None:
        super().__init__(ctx.author, timeout=60)
        self.ctx = ctx
        self.inventory = record.inventory_manager
        self.pets = record.pet_manager
        self.entry: PetRecord = entry
        self.items: list[Item] = sorted(
            (item for item in Items.all() if item.energy),
            key=lambda item: (
                (quantity := self.inventory.cached.quantity_of(item)) > 0,
                -item.rarity.value,
                quantity * item.energy,
            ),
            reverse=True,
        )
        self.index: int = 0
        self._color = Colors.primary

        self._equipped_pets = equipped_pets = [pet for pet in self.pets.cached.values() if pet.equipped]
        if self.pets.equipped_count <= 1:
            self.remove_item(self.select_pet)  # type: ignore
        elif self.pets.equipped_count < 3:
            self.remove_item(self.select_pet)  # type: ignore
            for entry in equipped_pets:
                self.add_item(FeedPetButton(entry))

        self.update_view()

    @property
    def current_item(self) -> Item:
        return self.items[self.index % len(self.items)]

    @property
    def max(self) -> int:
        energy_needed = self.entry.max_energy - self.entry.energy
        return min(
            ceil(energy_needed / self.current_item.energy),
            self.inventory.cached.quantity_of(self.current_item),
        )

    def update_view(self) -> None:
        it: discord.ui.Select = self.select_pet  # type: ignore
        it.options = [
            discord.SelectOption(
                label=entry.pet.name,
                description=f'{entry.energy:,}/{entry.max_energy:,} Energy',
                value=entry.pet.key,
                emoji=entry.pet.emoji,
                default=entry == self.entry,
            )
            for entry in self._equipped_pets
        ]

        for child in self.children:
            if isinstance(child, FeedPetButton):
                selected = child.entry == self.entry
                child.style = discord.ButtonStyle.primary if selected else discord.ButtonStyle.secondary
                child.label = child.entry.pet.name

                if child.entry.energy >= child.entry.max_energy:
                    child.label += ' [FULL]'
                    child.disabled = True

        next_item = self.items[(self.index + 1) % len(self.items)]
        self.next.emoji = next_item.emoji
        self.next.label = f'Next ({next_item.name})'

        max = self.max
        self.feed_one.disabled = max <= 0
        self.feed_max.disabled = single = max <= 1
        self.feed_custom.disabled = single
        self.feed_max.label = f'Feed Max ({max:,})' if max > 1 else 'Feed Max'

    def make_embeds(self) -> list[discord.Embed]:
        pet_embed = discord.Embed(color=self._color)
        pet_embed.set_author(name=f'{self.ctx.author.name}: Feeding', icon_url=self.ctx.author.avatar)
        pet_embed.set_thumbnail(url=image_url_from_emoji(str(self.entry.pet.emoji)))
        pet_embed.description = (
            f'You are feeding your **{self.entry.pet.display}**.\n'
            'Use the buttons below to find food to feed your pet.'
        )
        ratio = self.entry.energy / self.entry.max_energy
        pet_embed.add_field(
            name=(
                f'{Emojis.bolt} {self.entry.energy:,}/{self.entry.max_energy:,} Energy'
                + (
                    f' (Runs out {format_dt(self.entry.exhausts_at, "R")})'
                    if self.entry.energy > 0 else ''
                )
            ),
            value=f'{progress_bar(ratio, length=8)}  {ratio:.1%}',
        )

        quantity = self.inventory.cached.quantity_of(self.current_item)
        item_embed = discord.Embed(color=self._color, timestamp=self.ctx.now, title=self.current_item.display_name)
        item_embed.set_thumbnail(url=image_url_from_emoji(str(self.current_item.emoji)))
        item_embed.description = self.current_item.brief
        item_embed.add_field(
            name='Energy',
            value=(
                f'{Emojis.bolt} **+{self.current_item.energy:,}** each'
                + (
                    f'\n{Emojis.max_bolt} **+{self.current_item.energy * quantity:,}** total'
                    if quantity > 1 else ''
                )
            ),
        )
        item_embed.add_field(name='Quantity', value=f'**{quantity:,}** owned in inventory')
        return [pet_embed, item_embed]

    @discord.ui.select(placeholder='Select a pet to feed', row=0)
    async def select_pet(self, interaction: TypedInteraction, select: discord.ui.Select) -> None:
        self.entry = next(entry for pet, entry in self.pets.cached.items() if pet.key == select.values[0])
        self.update_view()
        await interaction.response.edit_message(embeds=self.make_embeds(), view=self)

    @discord.ui.button(label='Previous', emoji=Emojis.Arrows.previous, style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: TypedInteraction, _) -> None:
        self.index -= 1
        self.update_view()
        await interaction.response.edit_message(embeds=self.make_embeds(), view=self)

    @discord.ui.button(label='Next', style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: TypedInteraction, _) -> None:
        self.index += 1
        self.update_view()
        await interaction.response.edit_message(embeds=self.make_embeds(), view=self)

    @discord.ui.button(label='Change Item', emoji='\U0001f50e', style=discord.ButtonStyle.secondary, row=1)
    async def change_item(self, interaction: TypedInteraction, _) -> None:
        await interaction.response.send_modal(SearchItemModal(self))

    async def do_feed(self, interaction: TypedInteraction, quantity: int) -> None:
        async with self.ctx.db.acquire() as conn:
            await self.inventory.add_item(self.current_item, -quantity, connection=conn)
            energy = min(self.current_item.energy * quantity, self.entry.max_energy - self.entry.energy)
            await self.entry.add_energy(energy, connection=conn)

            # Give 1 XP for every 3 energy fed
            exp = energy // 3
            previous_level = self.entry.level
            if random.random() < (energy / 3) % 1:
                exp += 1
            await self.entry.add(exp=exp, connection=conn)
            new_level = self.entry.level

        self.update_view()
        await interaction.response.edit_message(embeds=self.make_embeds(), view=self)
        await interaction.followup.send(
            f'You fed your **{self.entry.pet.display}** {self.current_item.get_sentence_chunk(quantity)}.\n'
            f'{Emojis.bolt} **+{energy:,}** {Emojis.arrow} {self.entry.energy:,} Energy'
            + (f'\n\u2728 **+{exp:,} XP**' if exp > 0 else '')
            + (
                f'\n{Emojis.Expansion.standalone} \U0001f52e **LEVEL UP!** **{previous_level}** {Emojis.arrow} **{new_level}**'
                if new_level > previous_level
                else (
                    f' {Emojis.arrow} {self.entry.exp:,}/{self.entry.total_exp:,} XP'
                    if exp > 0 else ''
                )
            ),
            ephemeral=True
        )

    @discord.ui.button(label='Feed One', emoji=Emojis.bolt, style=discord.ButtonStyle.blurple, row=2)
    async def feed_one(self, interaction: TypedInteraction, _) -> None:
        await self.do_feed(interaction, 1)

    @discord.ui.button(label='Feed Max', emoji=Emojis.max_bolt, style=discord.ButtonStyle.blurple, row=2)
    async def feed_max(self, interaction: TypedInteraction, _) -> None:
        await self.do_feed(interaction, self.max)

    @discord.ui.button(label='Feed Custom', style=discord.ButtonStyle.blurple, row=2)
    async def feed_custom(self, interaction: TypedInteraction, _) -> None:
        await interaction.response.send_modal(FeedCustomModal(self))


from abc import ABC, abstractmethod  # TODO: migrate this to paginators


class NavigableItem(ABC):
    # Note: pages are zero-indexed, but max-pages is normal

    @property
    @abstractmethod
    def max_pages(self) -> int:
        """The maximum number of pages this item can have."""
        raise NotImplementedError

    @property
    @abstractmethod
    def current_page(self) -> int:
        """The current page of this item."""
        raise NotImplementedError

    @abstractmethod
    async def set_page(self, interaction: TypedInteraction, page: int) -> Any:
        """Set the current page of this item."""
        raise NotImplementedError


class JumpToPageModal(ui.Modal):
    page = ui.TextInput(
        label='Page Number',
        placeholder='...',
        min_length=1,
        max_length=4,
        required=True,
        style=discord.TextStyle.short,
    )

    def __init__(self, row: NavigationRow) -> None:
        super().__init__(title='Jump to Page')
        self.row: NavigationRow = row
        self.target: NavigableItem = row.target
        self.update()

    def update(self) -> None:
        self.page.placeholder = f'Enter the page you want to jump to (1-{self.target.max_pages})'

    async def on_submit(self, interaction: TypedInteraction, /) -> Any:
        try:
            page = int(self.page.value) - 1  # Convert to zero-indexed
        except ValueError:
            return await interaction.response.send_message(
                'Invalid page number. Please enter a valid integer.', ephemeral=True,
            )

        if not (0 <= page < self.target.max_pages):
            return await interaction.response.send_message(
                f'Page number must be between 1 and {self.target.max_pages}.', ephemeral=True,
            )

        await self.target.set_page(interaction, page)
        self.row.update()


class NavigationRow(ui.ActionRow):
    def __init__(self, target: NavigableItem) -> None:
        super().__init__()
        self.target: NavigableItem = target

    def update(self) -> None:
        self.clear_items()
        need_ff = self.target.max_pages <= 4  # Good enough threshold?

        if need_ff:
            self.add_item(self.first)
            self.first.disabled = self.target.current_page == 0

        self.previous.disabled = self.target.current_page == 0
        self.last.disabled = self.target.current_page == self.target.max_pages - 1
        self.jump.label = f'{self.target.current_page + 1}/{self.target.max_pages}'
        self.add_item(self.previous).add_item(self.jump).add_item(self.next)

        if need_ff:
            self.add_item(self.last)
            self.last.disabled = self.target.current_page == self.target.max_pages - 1

    @ui.button(emoji=Emojis.Arrows.first)
    async def first(self, interaction: TypedInteraction, _) -> None:
        await self.target.set_page(interaction, 0)
        self.update()

    @ui.button(emoji=Emojis.Arrows.previous)
    async def previous(self, interaction: TypedInteraction, _) -> None:
        await self.target.set_page(interaction, max(0, self.target.current_page - 1))
        self.update()

    @ui.button(style=discord.ButtonStyle.primary)
    async def jump(self, interaction: TypedInteraction, _) -> None:
        modal = JumpToPageModal(self)
        await interaction.response.send_modal(modal)

    @ui.button(emoji=Emojis.Arrows.forward)
    async def next(self, interaction: TypedInteraction, _) -> None:
        await self.target.set_page(interaction, min(self.target.max_pages - 1, self.target.current_page + 1))
        self.update()

    @ui.button(emoji=Emojis.Arrows.last)
    async def last(self, interaction: TypedInteraction, _) -> None:
        await self.target.set_page(interaction, self.target.max_pages - 1)
        self.update()


class PetsAllContainer(ui.Container, NavigableItem):
    PER_PAGE: int = 6
    PETS_COUNT: int = sum(1 for _ in walk_collection(Pets, Pet))

    def __init__(self, ctx: Context) -> None:
        super().__init__()
        self.ctx = ctx
        self.page: int = 0
        self._accent_color = Colors.secondary
        self._nav: NavigationRow = NavigationRow(self)

    async def prepare(self) -> None:
        self.record: UserRecord = await self.ctx.fetch_author_record()
        self.pets: PetManager = await self.record.pet_manager.wait()
        self.update()

    @property
    def max_pages(self) -> int:
        return ceil(len(self.pets.cached) / self.PER_PAGE)

    @property
    def current_page(self) -> int:
        return self.page

    async def set_page(self, interaction: TypedInteraction, page: int) -> Any:
        self.page = page
        self.update()
        await interaction.response.edit_message(view=self.view)

    def update(self) -> None:
        self.clear_items()
        self.accent_color = self._accent_color

        self.add_item(ui.TextDisplay(
            f'### {self.ctx.author.name}\'s Discovered Pets\n'
            f'-# You have discovered **{len(self.pets.cached)}** out of {self.PETS_COUNT} pets.'
        ))

        if not self.pets.cached:
            hunt_mention = self.ctx.bot.tree.get_app_command('hunt').mention
            self.add_item(ui.Separator())
            self.add_item(ui.TextDisplay(
                f'You haven\'t discovered any pets yet!\n'
                f'Discover some pets using {hunt_mention} and then come back.'
            ))
            return

        start_idx = self.page * self.PER_PAGE
        pets = sorted(self.pets.cached.values(), key=lambda p: p.pet.rarity, reverse=True)[
           start_idx:start_idx + self.PER_PAGE
       ]

        for i, entry in enumerate(pets):
            self.add_item(ui.Separator(
                spacing=discord.SeparatorSize.large if i == 0 else discord.SeparatorSize.small,
                visible=i == 0,
            ))
            s = '' if entry.duplicates == 1 else 's'
            xp = f'{entry.exp:,}/{entry.total_exp:,} XP'
            self.add_item(ui.Section(
                entry.pet.jumbo_display(
                    f'**{entry.pet.name}** {entry.pet.rarity.emoji}',
                    f'Level {entry.level:,} ({xp})  \u2022  {entry.duplicates:,} Duplicate{s}',
                ),
                accessory=StaticCommandButton(
                    command=self.ctx.bot.get_command('pets info'),
                    command_kwargs=dict(pet=entry.pet),
                    label='Info',
                    style=discord.ButtonStyle.secondary,
                ),
            ))

        if self.max_pages > 1:
            self._nav.update()
            self.add_item(ui.Separator(spacing=discord.SeparatorSize.large)).add_item(self._nav)


class PetsAllLayout(ui.LayoutView):
    def __init__(self, ctx: Context) -> None:
        super().__init__(timeout=300)
        self.add_item(container := PetsAllContainer(ctx))
        self.add_item(ui.ActionRow().add_item(StaticCommandButton(
            command=ctx.bot.get_command('pets view'),
            label='Manage Equipped Pets',
            style=discord.ButtonStyle.primary,
        )))
        self.container = container

    async def prepare(self) -> None:
        await self.container.prepare()


setup = PetsCog.simple_setup
