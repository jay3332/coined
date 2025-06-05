from __future__ import annotations

import random
from datetime import timedelta
from string import ascii_letters
from textwrap import dedent
from typing import ClassVar, Final, TYPE_CHECKING, Type, Union

import discord
from discord import app_commands
from discord.ext.commands import BadArgument, Greedy

from app.core import (
    BAD_ARGUMENT,
    Cog,
    Context,
    HybridContext,
    REPLY,
    command,
    group,
    lock_transactions,
    simple_cooldown,
    user_max_concurrency,
)
from app.data.items import Item, ItemType, Items
from app.data.quests import QuestTemplates
from app.database import CropInfo, CropManager, UserRecord
from app.util.common import cutoff, humanize_duration, image_url_from_emoji, query_collection_many
from app.util.converters import query_crop
from app.util.types import CommandResponse, TypedInteraction
from app.util.views import StaticCommandButton, UserView
from config import Colors, Emojis

if TYPE_CHECKING:
    parse_position: Type[tuple[int, int]]


def parse_coordinate(argument: str) -> tuple[int, int]:
    try:
        if argument[1].isdigit():
            x, y = argument[0], int(argument[1:])

        elif len(argument) > 2 and argument[2].isdigit():
            x, y = argument[:2], int(argument[2:])

        else:
            raise TypeError

        if len(x) > 2 or x.isdigit():
            raise TypeError

        letters = ascii_letters[:26]
        x = x.lower()

        unit = letters.index(x[-1])
        if len(x) > 1:
            unit += 26 * (letters.index(x[0]) + 1)

        if not 0 <= unit <= 64 or not 0 <= y <= 64:
            raise BadArgument("Coordinate must be between `A1` and `BL64`.")

        return unit, y - 1

    except (IndexError, TypeError, ValueError):
        raise BadArgument(f"{argument!r} is not a valid coordinate. Try something such as `A1`, `D3`, or `BD13`.")


class LandView(UserView):
    _placeholder = discord.ui.button(label='\u200b', style=discord.ButtonStyle.secondary, disabled=True)

    LAND_EMOJI: Final[ClassVar[str]] = '<:l:1379665442792280105>'
    LAND_LOCKED_EMOJI: Final[ClassVar[str]] = '<:l:1379665444339843075>'
    REGIONAL_INDICATOR_OFFSET: Final[ClassVar[int]] = 127397
    NUMERIC_INDICATOR_SUFFIX: Final[ClassVar[str]] = '\ufe0f\u20e3'

    def __init__(self, ctx: Context, record: UserRecord):
        super().__init__(ctx.author)

        self.record: UserRecord = record
        self.boundary_x: int = 0
        self.boundary_y: int = 0

        self.embed: discord.Embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        self.embed.set_author(name=f'{ctx.author.name}\'s Farm', icon_url=ctx.author.display_avatar)
        self.embed.set_footer(text='Use the arrow buttons below to move around!')

        self.embed.add_field(name='Information', value=f'Use `{ctx.prefix}land buy <coordinate>` to buy a patch of land.')

        self.message: str
        self.update()

        self.add_item(StaticCommandButton(
            label='Harvest All',
            style=discord.ButtonStyle.green,
            emoji='\U0001f33e',
            row=2,
            command=ctx.bot.get_command('harvest'),  # ensure bound
        ))

    @property
    def crop_manager(self) -> CropManager:
        return self.record.crop_manager

    def update_buttons(self) -> None:
        self.left.disabled = self.boundary_x <= 0
        self.right.disabled = self.boundary_x >= 56
        self.up.disabled = self.boundary_y <= 0
        self.down.disabled = self.boundary_y >= 56

    @classmethod
    def _regional_indicator(cls, c: str) -> str:
        return chr(ord(c) + cls.REGIONAL_INDICATOR_OFFSET) + '\u200b'

    def update_embed(self) -> None:
        # Calculate heading
        top, bottom = Emojis.space, Emojis.space
        no_top = True
        for i in range(self.boundary_x, self.boundary_x + 8):
            letters = CropInfo.get_letters(i)
            if len(letters) == 2:
                top += self._regional_indicator(letters[0])
                bottom += self._regional_indicator(letters[1])
                no_top = False
                continue

            top += Emojis.space
            bottom += self._regional_indicator(letters)
        lines = [bottom] if no_top else [top, bottom]

        if pad := len(str(self.boundary_y + 8)) > 1:
            for i, line in enumerate(lines):
                lines[i] = Emojis.space + line

        for y in range(self.boundary_y, self.boundary_y + 8):
            _ = lambda val: str(val) + self.NUMERIC_INDICATOR_SUFFIX
            match pad, tuple(str(y + 1)):
                case True, (left, right):
                    chunks = [f'{_(left)}{_(right)}']
                case True, (single,):
                    chunks = [f'{Emojis.space}{_(single)}']
                case False, (single,):
                    chunks = [_(single)]
                case _:
                    chunks = []

            for x in range(self.boundary_x, self.boundary_x + 8):
                crop = self.crop_manager.get_crop_info(x, y)

                if crop is None:
                    chunks.append(self.LAND_LOCKED_EMOJI)
                    continue

                if crop.crop is None:
                    chunks.append(self.LAND_EMOJI)
                    continue

                chunks.append(crop.crop.emoji)

            lines.append(''.join(chunks))

        self.message = '\n'.join(lines)

    def update(self) -> None:
        self.update_embed()
        self.update_buttons()

    @_placeholder
    async def left_placeholder(self, _b, _i):
        pass

    @discord.ui.button(emoji='\u2b06', style=discord.ButtonStyle.primary)
    async def up(self, interaction: TypedInteraction, _) -> None:
        if self.boundary_y <= 0:
            await interaction.response.send_message('Cannot go any further', ephemeral=True)

        self.boundary_y -= 8
        self.update()

        await interaction.response.edit_message(content=self.message, view=self)

    @_placeholder
    async def right_placeholder(self, _b, _i):
        pass

    @discord.ui.button(emoji='\u2b05', style=discord.ButtonStyle.primary, row=1)
    async def left(self, interaction: TypedInteraction, _) -> None:
        if self.boundary_x <= 0:
            await interaction.response.send_message('Cannot go any further', ephemeral=True)

        self.boundary_x -= 8
        self.update()

        await interaction.response.edit_message(content=self.message, view=self)

    @discord.ui.button(emoji='\u2b07', style=discord.ButtonStyle.primary, row=1)
    async def down(self, interaction: TypedInteraction, _) -> None:
        if self.boundary_y >= 56:
            await interaction.response.send_message('Cannot go any further', ephemeral=True)

        self.boundary_y += 8
        self.update()

        await interaction.response.edit_message(content=self.message, view=self)

    @discord.ui.button(emoji='\u27a1', style=discord.ButtonStyle.primary, row=1)
    async def right(self, interaction: TypedInteraction, _) -> None:
        if self.boundary_x >= 56:
            await interaction.response.send_message('Cannot go any further', ephemeral=True)

        self.boundary_x += 8
        self.update()

        await interaction.response.edit_message(content=self.message, view=self)


class Farming(Cog):
    """Commands that assist with the farming and crop system of the bot."""

    emoji = '\U0001f468\u200d\U0001f33e'

    @group(aliases={'crops', 'fa', 'crop', 'land', 'area'}, hybrid=True, fallback='view', expand_subcommands=True)
    @simple_cooldown(2, 4)
    async def farm(self, ctx: Context) -> CommandResponse:
        """View your farm and it's crops.

        Buy land using the `land buy` command.
        Sell land using the `land sell` command.
        Plant crops using the `plant` command.
        Harvest crops using the `harvest` command.
        Water your crops (to level them up faster) using the `water` command.
        View information on a specific crop using the `crop info` command.
        """
        record = await ctx.db.get_user_record(ctx.author.id)
        await record.crop_manager.wait()

        view = LandView(ctx, record)
        return view.message, view.embed, view, REPLY

    @staticmethod
    def get_land_buy_price(x: int, y: int) -> int:
        return round(25 * 1.225 ** max(x, y)) * 100

    @farm.command('buy', aliases={'b', 'purchase', 'acquire'}, hybrid=True)
    @app_commands.describe(coordinate='The location of the area of land you want to buy, e.g. "A1" or "F6".')
    @simple_cooldown(4, 4)
    @user_max_concurrency(1)
    @lock_transactions
    async def buy(self, ctx: Context, coordinate: parse_coordinate):
        """Buy the area of land located at the given coordinate.

        The futher you go away from the origin (`A1`), the more you will have to pay.
        """
        record = await ctx.db.get_user_record(ctx.author.id)
        manager = await record.crop_manager.wait()

        if coordinate in manager.cached:
            return 'You already own this patch of land.', BAD_ARGUMENT

        x, y = coordinate
        if all(
            (nx, ny) not in manager.cached
            for nx, ny in (
                (x - 1, y),
                (x + 1, y),
                (x, y - 1),
                (x, y + 1),
            )
        ):
            return 'Unlock at least one neighboring section of land first before unlocking this one.', BAD_ARGUMENT

        price = self.get_land_buy_price(*coordinate)
        if record.wallet < price:
            return f'The price to buy this patch of land is {Emojis.coin} **{price:,}**, and you only have {Emojis.coin} **{record.wallet:,}**.', BAD_ARGUMENT

        readable = CropInfo.into_coordinates(*coordinate)
        if not await ctx.confirm(
            f'Are you sure you want to buy the section of land at **{readable}** for {Emojis.coin} **{price:,}**?',
            reference=ctx.message,
            delete_after=True,
        ):
            return 'Purchase cancelled.', BAD_ARGUMENT

        await record.add(wallet=-price)
        await record.add_random_exp(10, 15, chance=0.65)
        await manager.add_land(*coordinate)

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.set_author(name='Successful Purchase', icon_url=ctx.author.display_avatar)
        embed.description = f'Successfully purchased section of land at **{readable}** for {Emojis.coin} **{price:,}**.'

        return embed, REPLY

    @farm.command(aliases={'s', 'release', 'disown'}, hybrid=True)
    @app_commands.describe(coordinate='The location of the area of land you want to sell, e.g. "A1" or "F6".')
    @simple_cooldown(2, 4)
    @user_max_concurrency(1)
    @lock_transactions
    async def sell(self, ctx: Context, coordinate: parse_coordinate):
        """Sell a patch of land you own for 1/5 it's original price back."""
        record = await ctx.db.get_user_record(ctx.author.id)
        manager = await record.crop_manager.wait()

        if coordinate not in manager.cached:
            return 'You do not own this patch of land.', BAD_ARGUMENT

        x, y = coordinate
        if x < 4 or y < 4:
            return 'This patch of land was given to you by default; you cannot sell it.', BAD_ARGUMENT

        price = self.get_land_buy_price(*coordinate) // 5
        readable = CropInfo.into_coordinates(*coordinate)

        if not await ctx.confirm(
            f'Are you sure you want to sell the section of land at **{readable}** in exchange for {Emojis.coin} **{price:,}**?',
            reference=ctx.message,
            delete_after=True,
        ):
            return 'Transaction cancelled.', BAD_ARGUMENT

        await record.add(wallet=price)
        await record.add_random_exp(10, 15, chance=0.65)
        await manager.remove_land(*coordinate)

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.set_author(name='Successful Transaction', icon_url=ctx.author.display_avatar)
        embed.description = f'Successfully sold the section of land at **{readable}** in return for {Emojis.coin} **{price:,}**.'

        return embed, REPLY

    @farm.command(aliases={'view', 'v', 'i', 'information', 'details'}, hybrid=True, with_app_command=False)
    @simple_cooldown(2, 4)
    async def info(self, ctx: Context, coordinate_or_crop: Union[parse_coordinate, query_crop]):  # Must use Union here as | operator does not work for functions)
        """View information about a crop or a specific crop at a specific coordinate."""
        record = await ctx.db.get_user_record(ctx.author.id)
        manager = await record.crop_manager.wait()

        if isinstance(coordinate_or_crop, tuple):
            x, y = coordinate_or_crop
            try:
                crop = manager.cached[x, y].crop
            except KeyError:
                raise BadArgument(f'You do not own the land at {CropInfo.into_coordinates(x, y)}.')

            if crop is None:
                raise BadArgument(f'You have not planted at crop at {CropInfo.into_coordinates(x, y)} yet.')
        else:
            crop = coordinate_or_crop

        crop: Item
        is_coordinate = isinstance(coordinate_or_crop, tuple)

        embed = discord.Embed(
            title=crop.name, description=crop.description, color=Colors.primary, timestamp=ctx.now,
        )
        embed.set_thumbnail(url=image_url_from_emoji(crop.emoji))

        embed.add_field(name='General', value=dedent(f"""
            **Name:** {crop.display_name}
            **Query Key: `{crop.key}`**
            **Crop Price:** {Emojis.coin} **{crop.price:,}**
        """))

        lower, upper = crop.metadata.count
        count = f'x{lower:,}' if lower == upper else f'x{lower:,} - x{upper:,}'

        harvest_time = manager.get_harvest_time(crop)
        embed.add_field(name='Crop Information', value=dedent(f"""
            **Harvesting Time:** {humanize_duration(harvest_time)}
            **Harvesting Quantity:** {count}
            **Harvests:** {crop.metadata.item.get_display_name(bold=True)}
        """))

        if is_coordinate:
            embed.title += f' [{CropInfo.into_coordinates(*coordinate_or_crop)}]'

            info = manager.cached[coordinate_or_crop]
            level, xp, max_xp = info.level_data
            next_harvest = info.last_harvest + timedelta(seconds=harvest_time)

            embed.add_field(name='Specific Information', value=dedent(f"""
                **Level:** {level:,} ({xp:,} / {max_xp:,} XP)
                **Planted At:** {discord.utils.format_dt(info.created_at)}
                **Last Harvest:** {discord.utils.format_dt(info.last_harvest)}
                **Next Harvest:** {
                    discord.utils.format_dt(next_harvest, 'R')
                    if next_harvest > ctx.now
                    else 'Ready!'
                }
            """), inline=False)

        return embed, REPLY

    @info.define_app_command()
    @app_commands.describe(
        coordinate_or_crop='The crop to view information on. Can be a specific crop (e.g. "A1") or a crop name (e.g. "corn").'
    )
    async def info_app_command(self, ctx: HybridContext, coordinate_or_crop: str) -> None:
        try:
            coordinate_or_crop = parse_coordinate(coordinate_or_crop)
        except BadArgument:
            coordinate_or_crop = query_crop(coordinate_or_crop)
        await ctx.invoke(ctx.command, coordinate_or_crop=coordinate_or_crop)  # type: ignore

    @info.autocomplete('coordinate_or_crop')
    async def into_autocomplete(self, _: TypedInteraction, current: str):
        return [
            app_commands.Choice(name=cutoff(current, 100), value=current[:100]),
            *(
                app_commands.Choice(name=crop.name, value=crop.key) for crop in Items.all()
                if crop.type == ItemType.crop and crop.name.lower().startswith(current.lower())
            )
        ]

    @command(aliases={'pl', 'grow'}, hybrid=True, with_app_command=False)
    @simple_cooldown(2, 4)
    @user_max_concurrency(1)
    @lock_transactions
    async def plant(self, ctx: Context, coordinates: Greedy[parse_coordinate], *, crop: query_crop):
        """Plant a crop (or multiple) in your farm."""
        if not coordinates:
            return 'You must specify at least one coordinate to plant at.', BAD_ARGUMENT

        coordinates = list(set(coordinates))
        record = await ctx.db.get_user_record(ctx.author.id)
        manager = await record.crop_manager.wait()

        for coordinate in coordinates:
            if coordinate not in manager.cached:
                return (
                    f'You do not own the land at coordinates {CropInfo.into_coordinates(*coordinate)}, '
                    f'disallowing you to plant there.',
                    BAD_ARGUMENT,
                )

        inventory = await record.inventory_manager.wait()
        owned = inventory.cached.quantity_of(crop)
        if owned < len(coordinates):
            if len(coordinates) == 1:
                return f'You do not have any {crop.get_display_name(plural=True)} in your inventory.', BAD_ARGUMENT
            return (
                f'You only have {owned} {crop.get_display_name(plural=True)} in your inventory,'
                f' which is not enough to plant over {len(coordinates)} coordinates.',
                BAD_ARGUMENT,
            )

        message = None
        if len(coordinates) == 1:
            coordinate = coordinates[0]
            maybe_crop = manager.cached[coordinate].crop
            if maybe_crop is not None:
                message = (
                    f'You already have {maybe_crop.get_sentence_chunk(1)} planted in this spot. '
                    'If you plant this crop here, the previous crop will be disposed of without any refund. '
                    'Are you sure you want to do this?'
                )
        elif conflicts := sum(manager.cached[coord].crop is not None for coord in coordinates):
            message = (
                f'You are trying to plant {len(coordinates)} crops, but {conflicts} of those coordinates already have '
                f'crops planted in them. Are you sure you want to dispose {conflicts} crops without refund?'
            )

        if message is not None and not await ctx.confirm(message, delete_after=True, reference=ctx.message):
            return 'Alright, I guess not', BAD_ARGUMENT

        await inventory.add_item(crop, -len(coordinates))
        await record.add_random_exp(8, 12, chance=0.65)
        await manager.plant_crop(coordinates, crop)

        ctx.bot.loop.create_task(ctx.thumbs())
        if len(coordinates) > 1:
            return f'Planted {crop.get_sentence_chunk(len(coordinates))} at {len(coordinates)} coordinates.', REPLY

        coordinate = coordinates[0]
        return f'Planted {crop.get_sentence_chunk(1)} at coordinate **{CropInfo.into_coordinates(*coordinate)}**.', REPLY

    @plant.define_app_command()
    @app_commands.describe(
        coordinates='The coordinates to plant at. Can be a single coordinate ("A1") or multiple coordinates ("A1 A2 A3").',
        crop='The name of the crop to plant.'
    )
    async def plant_app_command(self, ctx: HybridContext, coordinates: str, crop: str) -> None:
        crop = query_crop(crop)
        await ctx.invoke(ctx.command, coordinates=[parse_coordinate(c) for c in coordinates.split()], crop=crop)

    @plant.autocomplete('crop')
    async def plant_autocomplete(self, _: TypedInteraction, current: str):
        return [
            app_commands.Choice(name=crop.name, value=crop.key)
            for crop in query_collection_many(Items, Item, current, prioritizer=lambda c: c.type is ItemType.crop)
            if crop.type is ItemType.crop
        ]

    @command(aliases={'har', 'ha', 'hv', 'gather', 'collect'}, hybrid=True, with_app_command=False)
    @simple_cooldown(1, 15)
    @user_max_concurrency(1)
    async def harvest(self, ctx: Context, *crops: Union[parse_coordinate, query_crop]):  # Must use Union here as | operator does not work for functions
        """Harvest crops from your farm.

        **Examples:**
        `.harvest` - Harvest all crops from your farm.
        `.harvest tomato` - Harvest only tomatoes from your farm.
        `.harvest A5` - Harvest the crop from A5 if possible.
        `.harvest tomato corn wheat` - Harvest all tomatoes, corn, and wheat from your farm.
        `.harvest A1 A2 A3 A4` - Harvest all crops from A1, A2, A3, and A4.
        `.harvest A1 tomato` - Harvest the crop from A1 and all tomatoes.
        """
        record = await ctx.db.get_user_record(ctx.author.id)
        manager = await record.crop_manager.wait()

        crops: list[tuple[int, int] | Item] = list(set(crops))

        for i, crop in enumerate(crops):
            if isinstance(crop, Item):
                crops.pop(i)
                crops.extend(k for k, v in manager.cached.items() if v.crop == crop)

        if not len(crops):
            crops = list(manager.cached.keys())

        level_ups, harvested = await manager.harvest(crops)

        if not harvested:
            return 'Could not harvest anything.', REPLY

        await record.add_random_exp(10, 15)
        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.set_author(name='Successful Harvest', icon_url=ctx.author.display_avatar)

        embed.add_field(name='Harvested:', value='\n'.join(
            f'{item.get_display_name()} x{quantity:,}' for item, quantity in harvested.items()
        ), inline=False)

        if len(level_ups):
            message = '\n'.join(
                f'{item.get_display_name()} at {CropInfo.into_coordinates(x, y)}: Now level {new:,}!'
                for _, ((x, y), (item, new)) in zip(range(10), level_ups.items())
            )
            if len(level_ups) > 10:
                message += f'\n*{len(level_ups) - 10:,} more...*'

            embed.add_field(name='Leveled up crops:', value=message)

        return embed, REPLY

    @harvest.define_app_command()
    async def harvest_app_command(self, ctx: HybridContext):
        await ctx.invoke(ctx.command)

    @command(aliases={'wat', 'flourish'})
    @simple_cooldown(2, 4)
    @user_max_concurrency(1)
    @lock_transactions
    async def water(self, ctx: Context, coordinates: parse_coordinate):
        """Water the crop at the given coordinate to increase its EXP.
        The more EXP a crop has, the higher level it will become, and the faster it will take to harvest.

        You must have a Watering Can in your inventory.
        """
        record = await ctx.db.get_user_record(ctx.author.id)
        manager = await record.crop_manager.wait()
        quests = await record.quest_manager.wait()

        try:
            crop = manager.cached[coordinates]
        except KeyError:
            return f'You don\'t own the land at {CropInfo.into_coordinates(*coordinates)}.', BAD_ARGUMENT

        if not crop.crop:
            return f'There is no crop at {CropInfo.into_coordinates(*coordinates)}.', BAD_ARGUMENT

        inventory = await record.inventory_manager.wait()
        if not inventory.cached.quantity_of('watering_can'):
            return f'You must have a {Items.watering_can.get_display_name(bold=True)} in order to water crops.', BAD_ARGUMENT

        await inventory.add_item(Items.watering_can, -1)

        gain = random.randint(15, 40)
        message = (
            f'Watered the {crop.crop.display_name} at **{CropInfo.into_coordinates(*coordinates)}**, gaining **{gain:,} EXP**.'
        )

        if await manager.add_crop_exp(*coordinates, gain):
            message += f' Your crop is now **Level {manager.cached[coordinates].level}**!'

        if quest := quests.get_active_quest(QuestTemplates.water_crops):
            await quest.add_progress(1)

        await record.add_random_exp(8, 12, chance=0.8)
        embed = discord.Embed(color=Colors.success, description=message, timestamp=ctx.now)
        embed.set_author(name='Successful Watering', icon_url=ctx.author.display_avatar)

        return embed, REPLY


setup = Farming.simple_setup
