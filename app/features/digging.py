from __future__ import annotations

import random
from asyncio import gather
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from math import ceil
from io import BytesIO
from typing import ClassVar, TypeAlias

from discord import ButtonStyle, Embed, File, HTTPException, MediaGalleryItem, Message, ui
from discord.utils import MISSING, format_dt, utcnow
from PIL import Image, ImageDraw

from app.core import Context
from app.data.backpacks import Backpack
from app.data.pets import Pets
from app.data.quests import QuestTemplates
from app.database import InventoryManager, PetManager, QuestManager, UserRecord
from app.data.biomes import Biome, Biomes
from app.data.items import ItemType, Items, Item, ToolMetadata
from app.util.common import executor_function, humanize_duration, image_url_from_emoji, progress_bar, weighted_choice
from app.util.types import TypedInteraction
from app.util.views import UserLayoutView
from config import Colors, Emojis

RGB: TypeAlias = tuple[int, int, int]


@dataclass
class Cell:
    coins: int
    item: Item | None
    dirt_index: int
    hp: float = 0


class NavigationRow(ui.ActionRow['DiggingView']):
    @ui.button(label='Left', style=ButtonStyle.secondary, emoji='\u2b05')
    async def left(self, itx: TypedInteraction, _button: ui.Button) -> None:
        x, y = self.view.session.position
        cell = self.view.session.grid[y][x - 1]
        if y < 0 or cell is None:
            self.view.session.position = (x - 1, y)
        else:
            self.view.session.target = DiggingSession.Target.left
        self.view.container.update()

        image = await self.view.generate_image()
        await itx.response.edit_message(view=self.view, attachments=[image])

    @ui.button(label='Dig Deeper', style=ButtonStyle.secondary, emoji='\u23ec')
    async def down(self, itx: TypedInteraction, _button: ui.Button) -> None:
        self.view.session.target = DiggingSession.Target.down
        cell = self.view.session.target_cell
        if cell is None or cell.hp <= 0:
            self.view.session.move()
        self.view.container.update()

        image = await self.view.generate_image()
        await itx.response.edit_message(view=self.view, attachments=[image])

    @ui.button(label='Right', style=ButtonStyle.secondary, emoji='\u27a1')
    async def right(self, itx: TypedInteraction, _button: ui.Button) -> None:
        x, y = self.view.session.position
        cell = self.view.session.grid[y][x + 1]
        if y < 0 or cell is None:
            self.view.session.position = (x + 1, y)
            self.view.container.update()
        else:
            self.view.session.target = DiggingSession.Target.right
        self.view.container.update()

        image = await self.view.generate_image()
        await itx.response.edit_message(view=self.view, attachments=[image])


class TargetActionRow(ui.ActionRow['DiggingView']):
    @ui.button(style=ButtonStyle.secondary)
    async def dig(self, itx: TypedInteraction, _button: ui.Button) -> None:
        session = self.view.session
        cell = session.target_cell

        tool = session.pickaxe if cell.item and cell.item.type is ItemType.ore else session.shovel
        hp_dealt = min(
            (tool.metadata.strength if tool else 1) * session.hp_multiplier,
            cell.hp,
        )
        cell.hp -= hp_dealt
        session.stamina -= 1

        if quest := session.quests.get_active_quest(QuestTemplates.dig_hp):
            await quest.add_progress(int(hp_dealt))

        if session.target_cell.hp <= 0:
            if session.target is DiggingSession.Target.down:
                session.move()
                while session.target_cell is None:
                    session.move()
            else:
                session.collect_target()
            self.view.container.update()
            attachments = [await self.view.generate_image()]
        else:
            self.view.container.update_header()
            self.view.container.update_target_info()
            attachments = MISSING

        if random.random() < 0.7:
            self.view.session.xp_earned += random.randint(2, 4)
            self.view.session.bank_space_earned += random.randint(1, 3)
        await itx.response.edit_message(view=self.view, attachments=attachments)


class HeaderRow(ui.ActionRow['DiggingView']):
    def __init__(self, *, parent: DiggingContainer) -> None:
        self.parent: DiggingContainer = parent
        super().__init__()

    def update(self) -> None:
        self.show_collected_coins.label = format(self.parent.session.collected_coins, ',')
        self.show_collected_items.emoji = self.parent.session.backpack.emoji
        self.show_collected_items.label = f'{sum(self.parent.session.collected_items.values())} items'
        self.show_collected_items.disabled = not self.parent.session.collected_items

        if self.view and self.view.is_finished():
            self.show_collected_coins.disabled = True
            self.show_collected_items.disabled = True

    @ui.button(style=ButtonStyle.secondary, emoji=Emojis.coin)
    async def show_collected_coins(self, itx: TypedInteraction, _) -> None:
        base = self.parent.session.collected_coins
        multiplier = self.parent.session.record.coin_multiplier
        multipliers_mention = self.parent.ctx.bot.tree.get_app_command('multiplier').mention
        extra = '' if multiplier <= 1 else (
            f'\n{Emojis.Expansion.standalone} You will receive {Emojis.coin} **{round(base * multiplier):,}** because '
            f'you have a **+{multiplier - 1:.1%} coin multiplier** ({multipliers_mention})'
        )

        await itx.response.send_message(
            content=f'You have collected {Emojis.coin} **{base:,}**' + extra,
            ephemeral=True,
        )

    @ui.button(style=ButtonStyle.secondary)
    async def show_collected_items(self, itx: TypedInteraction, _) -> None:
        display = self.parent.session.collected_display
        display = f'### You have collected:\n{display}' if display else 'No items collected yet.'
        await itx.response.send_message(
            content=display + f'\n-# Using **{self.parent.session.backpack.display}**',
            ephemeral=True,
        )


class DiggingContainer(ui.Container['DiggingView']):
    def __init__(self, parent: DiggingView) -> None:
        self.parent: DiggingView = parent
        self._header: ui.Section = ui.Section(accessory=ui.Thumbnail(
            media=self.ctx.author.display_avatar.with_size(64).url
        ))
        self._header_items_display = ui.TextDisplay('')
        self._header_row = HeaderRow(parent=self)
        self._navigation_row: NavigationRow = NavigationRow()
        self._target_info: ui.Section | None = ui.Section(accessory=MISSING)
        self._target_row: TargetActionRow = TargetActionRow()
        super().__init__()

    @property
    def session(self) -> DiggingSession:
        return self.parent.session

    @property
    def ctx(self) -> Context:
        return self.parent.ctx

    def update_header(self) -> None:
        session = self.session
        stamina_pbar = progress_bar(session.stamina / session.max_stamina)
        backpack_pbar = progress_bar(session.backpack_occupied / session.backpack.capacity)

        items_collected = session.collected_display
        if items_collected:
            items_collected += '\n'

        self._header.clear_items()

        entries = list(session.collected_items.items())
        condensed = '\n'.join(
            '-# ' + ' '.join(f'{item.emoji} x{quantity:,}' for item, quantity in entries[i:i + 5])
            for i in range(0, len(entries), 5)
        )
        condensed = f'\n{condensed}' if condensed else ''
        for item in (
            f'## {self.ctx.author.display_name}\'s Digging Session',
            f'{Emojis.bolt} {stamina_pbar} {session.stamina:,}/{session.max_stamina:,}',
            f'{session.backpack.emoji} {backpack_pbar} {session.backpack_occupied:,}/{session.backpack.capacity:,}'
            + condensed,
        ):
            self._header.add_item(item)

        self._header_row.update()

    def update_navigation_row(self) -> None:
        row = self._navigation_row
        grid = self.session.grid
        x, y = self.session.position

        row.left.disabled = self.session.x == 0
        row.right.disabled = self.session.x == self.session.GRID_WIDTH - 1

        left_cell = grid[y][x - 1] if x > 0 else None
        right_cell = grid[y][x + 1] if x < self.session.GRID_WIDTH - 1 else None
        down_cell = grid[y + 1][x]

        if down_cell and down_cell.hp > 0:
            if self.session.target is DiggingSession.Target.down:
                row.down.label = 'See Below'
                row.down.disabled = True
            else:
                row.down.label = 'View Bottom'
                row.down.disabled = False
        else:
            row.down.label = 'Dig Deeper'

        if y >= 0:
            row.left.label = 'View Left' if left_cell else 'Move Left'
            row.right.label = 'View Right' if right_cell else 'Move Right'
        else:
            row.left.label = 'Move Left'
            row.right.label = 'Move Right'

    def update_target_info(self) -> None:
        cell = self.session.target_cell
        if cell is None or not cell.item and not cell.coins:
            self._target_info: ui.Section | None = None
            return

        if not self._target_info:
            self._target_info = ui.Section(accessory=MISSING)
        self._target_info.clear_items()

        if cell.item:
            self._target_info.add_item('## ' + f'{cell.item.name} {cell.item.rarity.emoji}')
        if cell.hp > 0 and cell.item is not None:
            s = 's' if cell.item.volume > 1 else ''
            formatted = f'{cell.hp:.1f}'.removesuffix('.0')
            self._target_info.add_item(
                f'{Emojis.hp} {progress_bar(cell.hp / cell.item.hp)} {formatted}/{cell.item.hp:,}\n'
                f'-# Occupies {cell.item.volume:,} storage unit{s}'
            )

        self._target_info.accessory = ui.Thumbnail(media=image_url_from_emoji(cell.item.emoji)) if cell.item else None

        btn = self._target_row.dig
        if cell.item and cell.item.type is ItemType.ore:
            tool = self.session.pickaxe
            if tool is None:
                btn.disabled = True
                btn.label = 'You need a pickaxe to mine this ore!'
                btn.emoji = None
                return
            verb = 'Mine'
        else:
            tool = self.session.shovel
            verb = 'Dig'

        btn.disabled = False
        if self.session.stamina <= 0:
            btn.label = 'You are too tired to dig!'
            btn.disabled = True
            btn.emoji = None
        elif cell.coins:
            btn.label = f'Collect {cell.coins:,} coins'
            btn.emoji = Emojis.coin
        elif self.session.backpack_occupied + cell.item.volume > self.session.backpack.capacity:
            btn.label = 'Your backpack is too full!'
            btn.disabled = True
            btn.emoji = None
        else:
            btn.label = f'{verb} with {tool.name if tool else "bare hands"}'
            btn.emoji = tool and tool.emoji

    def update(self) -> None:
        self.clear_items()
        self.update_header()

        self.add_item(self._header).add_item(self._header_row).add_item(ui.Separator())
        self.add_item(ui.MediaGallery(MediaGalleryItem(media='attachment://digging.png')))

        s = '' if self.session.y == 0 else 's'
        self.add_item(ui.TextDisplay(
            f'-# \U0001f5bc\ufe0f Biome: **{self.session.biome.name}** \u2022 '
            f'Depth: {(self.session.y + 1):,} meter{s}'
        ))

        if not self.view.is_finished():
            self.update_navigation_row()
            self.add_item(self._navigation_row)
            self.update_target_info()
            if self._target_info is not None:
                self.add_item(ui.Separator())
                if self._target_info.accessory is not None:
                    self.add_item(self._target_info)
                else:
                    for item in self._target_info.children:
                        self.add_item(item)
                self.add_item(self._target_row)
        else:
            elapsed = utcnow() - self.ctx.now
            self.add_item(ui.TextDisplay(f'-# \N{STOPWATCH}\ufe0f {humanize_duration(elapsed)}'))

        self.view.action_row.update()


class DiggingActionRow(ui.ActionRow['DiggingView']):
    def __init__(self) -> None:
        super().__init__()
        self._message: Message | None = None

    @property
    def session(self) -> DiggingSession:
        return self.view.session

    async def edit_or_send(self, itx: TypedInteraction, **kwargs):
        if msg := self._message:
            try:
                await itx.followup.edit_message(msg.id, **kwargs)
            except HTTPException:
                pass
            else:
                return
        self._message = await itx.followup.send(**kwargs, ephemeral=True, wait=True)

    async def base_surface(self) -> dict:
        self.view.stop()
        self.view.remove_item(self)
        self.view.container.update()

        depth = self.session.y + 1
        self.session.position = (self.session.x, -1)

        async with self.session.ctx.db.acquire() as conn:
            if depth > self.session.record.deepest_dig:
                await self.session.record.update(deepest_dig=depth, connection=conn)

            if quest := self.session.quests.get_active_quest(QuestTemplates.dig_to_depth):
                if depth > quest.progress:
                    await quest.set_progress(depth, connection=conn)

            kwargs = {
                item.key: quantity for item, quantity in self.session.collected_items.items()
            }
            if quest := self.session.quests.get_active_quest(QuestTemplates.dig_coins):
                await quest.add_progress(self.session.collected_coins, connection=conn)
            if quest := self.session.quests.get_active_quest(QuestTemplates.dig_items):  # non-dirt
                await quest.add_progress(
                    sum(
                        quantity
                        for item, quantity in self.session.collected_items.items()
                        if item.type is not ItemType.dirt
                    ),
                    connection=conn,
                )
            # if quest := self.session.quests.get_active_quest(QuestTemplates.dig_single_item):
            #     await quest.add_progress(kwargs.get(quest.quest.extra, 0), connection=conn)
            if quest := self.session.quests.get_active_quest(QuestTemplates.dig_ores):
                await quest.add_progress(
                    sum(
                        quantity
                        for item, quantity in self.session.collected_items.items()
                        if item.type is ItemType.ore
                    ),
                    connection=conn,
                )
            if quest := self.session.quests.get_active_quest(QuestTemplates.dig_stamina):
                await quest.add_progress(self.session.max_stamina - self.session.stamina, connection=conn)

            self.session.collected_coins = await self.session.record.add_coins(
                self.session.collected_coins, connection=conn,
            )
            await self.session.inventory.add_bulk(**kwargs, connection=conn)

        if self.session.collected_coins or self.session.collected_items:
            self.view.container.accent_colour = Colors.success

        return dict(view=self.view, attachments=[await self.view.generate_image()])

    @ui.button(label='Surface', style=ButtonStyle.secondary, emoji='\u23eb')
    async def surface(self, itx: TypedInteraction, _btn):
        if (
            self.session.stamina / self.session.max_stamina > 0.5
            and self.session.backpack_occupied / self.session.backpack.capacity < 0.5
        ) and not await self.session.ctx.confirm(
            'Are you sure you want to surface now? You still have stamina left and space in your backpack.\n'
            '-# You will have to wait a bit before digging again.',
            true='Yes, end my digging session and surface!',
            false='No, I want to keep digging.',
            interaction=itx,
            delete_after=True,
            ephemeral=True,
        ):
            return

        meth = itx.message.edit if itx.response.is_done() else itx.response.edit_message
        await meth(**await self.base_surface())

        display = self.session.collected_display
        embed = Embed(timestamp=utcnow(), color=Colors.success)
        if display:
            embed.description = '### You earned:\n' + display
            embed.set_author(name='Successful digging session')
        else:
            embed.description = 'You surfaced empty-handed.'
            embed.colour = Colors.warning
        embed.set_footer(text=f'Session lasted for {humanize_duration(utcnow() - self.session.ctx.now, depth=2)}')
        await self.edit_or_send(itx, embed=embed)

    @ui.button(label='Use Railgun', style=ButtonStyle.secondary, emoji=Items.railgun.emoji)
    async def railgun(self, itx: TypedInteraction, _btn: ui.Button):
        if self.session.inventory.cached.quantity_of(Items.railgun) < 1:
            return await itx.response.send_message('Cooked', ephemeral=True)
        if self.session.record.railgun_expiry and utcnow() < self.session.record.railgun_expiry:
            return await itx.response.send_message('Cooldown active', ephemeral=True)

        await self.session.record.update(railgun_cooldown_expires_at=utcnow() + timedelta(hours=1))
        added_coins, added_items = self.session.cascading_dig(30)  # TODO: upgradable railgun
        display = self.session.get_collected_display(coins=added_coins, items=added_items)
        display += f'\n-# You can use this again {format_dt(self.session.record.railgun_expiry, "R")}'

        embed = Embed(timestamp=utcnow(), color=Colors.success)
        embed.set_author(name='Used Railgun!')
        embed.set_thumbnail(url=image_url_from_emoji(Items.railgun.emoji))
        embed.add_field(name='You collected:', value=display, inline=False)

        self.view.container.update()
        await itx.response.edit_message(view=self.view, attachments=[await self.view.generate_image()])
        await self.edit_or_send(itx, embed=embed)

    @ui.button(style=ButtonStyle.secondary, emoji=Items.dynamite.emoji)
    async def dynamite(self, itx: TypedInteraction, _btn: ui.Button):
        inventory = self.session.inventory
        if inventory.cached.quantity_of(Items.dynamite) <= 0:
            return await itx.response.send_message('You have no dynamite left!', ephemeral=True)

        await inventory.add_item(Items.dynamite, -1)
        total_hp, added_coins, added_items = self.session.surrounding_dig(10)  # TODO: upgradable dynamite

        display = self.session.get_collected_display(coins=added_coins, items=added_items)
        embed = Embed(timestamp=utcnow(), color=Colors.success)
        embed.description = f'\U0001f4a5 Dealt **{round(total_hp)} HP** to surrounding blocks!'
        embed.set_author(name='Used Dynamite!')
        embed.set_thumbnail(url=image_url_from_emoji(Items.dynamite.emoji))
        embed.add_field(name='You collected:', value=display, inline=False)

        self.view.container.update()
        await itx.response.edit_message(view=self.view, attachments=[await self.view.generate_image(draw_hp=True)])
        await self.edit_or_send(itx, embed=embed)

    def update(self) -> None:
        self.clear_items()
        if self.view.is_finished():
            self.view.remove_item(self)
            return

        self.add_item(self.surface)

        target_exceeds_volume = (
            self.session.target_cell and self.session.target_cell.item
            and self.session.backpack_occupied + self.session.target_cell.item.volume > self.session.backpack.capacity
        )
        self.surface.style = (
             ButtonStyle.success
             if self.session.stamina <= 0 or target_exceeds_volume
             else ButtonStyle.secondary
        )

        if self.session.inventory.cached.quantity_of(Items.railgun) > 0:
            self.add_item(self.railgun)
            down = self.session.grid[self.session.y + 1][self.session.x]
            self.railgun.disabled = (
                self.session.record.railgun_expiry and self.session.record.railgun_expiry > utcnow()
                or (
                    down
                    and down.item
                    and self.session.backpack_occupied + down.item.volume > self.session.backpack.capacity
                )
            )

        dynamite = self.session.inventory.cached.quantity_of(Items.dynamite)
        self.add_item(self.dynamite)

        self.dynamite.label = str(dynamite) if dynamite > 0 else None
        self.dynamite.disabled = dynamite <= 0 or self.session.backpack_occupied == self.session.backpack.capacity


class DiggingView(UserLayoutView):
    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx.author, timeout=600)
        self.ctx: Context = ctx
        self.session: DiggingSession = DiggingSession(ctx)
        self.container: DiggingContainer = DiggingContainer(self)

        self.add_item(self.container)
        self.add_item(row := DiggingActionRow())
        self.action_row = row

    async def prepare(self) -> None:
        """Prepares the digging session and its assets."""
        await self.session.prepare()
        self.container.update()

    async def generate_image(self, *, draw_hp: bool = False) -> File:
        """Generates the digging session image."""
        return await self.session.generate_image(active=not self.is_finished(), draw_hp=draw_hp)

    async def on_timeout(self) -> None:
        await self.ctx.maybe_edit(**await self.action_row.base_surface())


class DiggingSession:
    DUG_COLOR: RGB = (88, 50, 15)
    BG_COLOR: RGB = (72, 48, 13, 150)  # Transparent background

    GRID_WIDTH: int = 9
    CELL_WIDTH: int = 48
    IMAGE_WIDTH: int = GRID_WIDTH * CELL_WIDTH
    IMAGE_HEIGHT: int = IMAGE_WIDTH
    GRAIN_WIDTH: int = CELL_WIDTH // 16
    Y_OFFSET: int = IMAGE_HEIGHT // 2 - CELL_WIDTH // 2  # ...of current position

    OVERLAY_WIDTH: int = CELL_WIDTH * 8 // 10
    OVERLAY_PADDING: int = (CELL_WIDTH - OVERLAY_WIDTH) // 2
    BG_OFFSET: int = CELL_WIDTH // 2

    AVATAR_MASK: Image.Image = Image.open('assets/digging/avatar_rounded_mask.png').convert('L').resize(
        (OVERLAY_WIDTH, OVERLAY_WIDTH),
        Image.NEAREST,
    )
    VISIBILITY: int = 2
    GEN_IMAGES_PER_DIRT: int = 8

    # Cache for generated images
    coin_image: ClassVar[Image.Image] = Image.open('assets/digging/padded_coin.png').convert('RGBA').resize(
        (OVERLAY_WIDTH, OVERLAY_WIDTH),
        Image.NEAREST,
    )
    backdrops: ClassVar[dict[Biome, Image.Image]] = {}
    image_cache: ClassVar[dict[Item, Image.Image]] = {}
    dirt_images: ClassVar[dict[Item, list[Image.Image]]] = {}

    class Target(Enum):
        down = 0
        left = 1
        right = 2

    def __init__(self, context: Context, biome: Biome = Biomes.backyard) -> None:
        self.ctx: Context = context
        self.biome: Biome = biome

        self.position: tuple[int, int] = (self.GRID_WIDTH // 2, -1)
        self.target: DiggingSession.Target = self.Target.down
        self.grid: list[list[Cell | None]] = []
        self.explored = set()

        self.collected_coins: int = 0
        self.collected_items: defaultdict[Item, int] = defaultdict(int)

        self.xp_earned: int = 0
        self.bank_space_earned: int = 0

    async def fetch_bytes(self, url: str) -> BytesIO:
        async with self.ctx.bot.session.get(url) as response:
            if response.status != 200:
                raise Exception(f'Failed to fetch image from {url}, status: {response.status}')
            return BytesIO(await response.read())

    @executor_function
    def open_sized(self, fp: BytesIO, size: tuple[int, int]) -> Image.Image:
        """Open an image from a file-like object with a specific size."""
        image = Image.open(fp).convert('RGBA')
        return image.resize(size, Image.NEAREST)

    @property
    def backpack_occupied(self) -> int:
        """Returns the number of storage units occupied in the backpack."""
        return sum(quantity * item.volume for item, quantity in self.collected_items.items())

    async def prepare(self) -> None:
        """Prepares the digging session by ensuring dirt and avatar images are cached."""
        self.record: UserRecord = await self.ctx.fetch_author_record()
        self.inventory: InventoryManager = await self.record.inventory_manager.wait()
        self.pets: PetManager = await self.record.pet_manager.wait()
        self.quests: QuestManager = await self.record.quest_manager.wait()

        self.backpack: Backpack = self.record.equipped_backpack
        self.shovel: Item[ToolMetadata] | None = next(
            filter(self.inventory.cached.quantity_of, Items.__shovels__), None
        )
        self.pickaxe: Item[ToolMetadata] | None = next(
            filter(self.inventory.cached.quantity_of, Items.__pickaxes__), None
        )

        self.coin_multiplier: float = 1.0
        self.hp_multiplier: float = 1.0
        self.max_stamina: int = 100 + 20 * self.record.prestige

        if hamster := self.pets.get_active_pet(Pets.hamster):
            self.coin_multiplier += 0.02 + hamster.level * 0.002
        if armadillo := self.pets.get_active_pet(Pets.armadillo):
            self.max_stamina += armadillo.level + 1
        if jaguar := self.pets.get_active_pet(Pets.jaguar):
            self.hp_multiplier += 0.05 + jaguar.level * 0.01
        if tiger := self.pets.get_active_pet(Pets.tiger):
            self.max_stamina += tiger.level * 2 + 2
            self.hp_multiplier += 0.1 + tiger.level * 0.02

        self.stamina: int = self.max_stamina  # TODO: unified stamina system

        fp = await self.fetch_bytes(self.ctx.author.display_avatar.with_size(64).with_format('png').url)
        self.avatar_image: Image.Image = await self.open_sized(fp, (self.OVERLAY_WIDTH, self.OVERLAY_WIDTH))

        mask = self.AVATAR_MASK.copy()
        for x in range(self.OVERLAY_WIDTH):
            for y in range(self.OVERLAY_WIDTH):
                r, g, b, a = self.avatar_image.getpixel((x, y))
                mask_a = mask.getpixel((x, y))
                mask.putpixel((x, y), min(a, mask_a))
        self.avatar_image.putalpha(mask)

        await self.prepare_backdrop()
        await self.prepare_assets()
        self.grid: list[list[Cell | None]] = [self.generate_row(y) for y in self.y_range if y >= 0]

    SPAWN_COUNT: ClassVar[dict[int, float]] = {
        0: 4,
        1: 6,
        2: 4,
        3: 2,
        4: 1,
    }

    def generate_row(self, y: int) -> list[Cell | None]:
        # TODO: different types as dirt as we go deeper + better chance at rarer items
        layer = self.biome.get_layer(y)
        base = [
            Cell(
                coins=0,
                item=layer.dirt,
                dirt_index=random.randrange(0, self.GEN_IMAGES_PER_DIRT),
                hp=layer.dirt.hp,
            )
            for _ in range(self.GRID_WIDTH)
        ]
        if y == 0:
            return base

        # Add some items to the row
        spawns = weighted_choice(self.SPAWN_COUNT)
        positions = random.sample(range(self.GRID_WIDTH), spawns)

        for pos in positions:
            idx = random.randrange(0, self.GEN_IMAGES_PER_DIRT)
            item = weighted_choice(layer.items)
            if item is None:
                coins = round(random.uniform(10 * y, 20 * y) * self.coin_multiplier)
                base[pos] = Cell(coins=coins, item=None, dirt_index=idx)
            else:
                base[pos] = Cell(coins=0, item=item, dirt_index=idx, hp=item.hp)

        return base

    @property
    def current(self) -> Cell:
        return self.grid[self.y][self.x]

    @property
    def target_xy(self) -> tuple[int, int]:
        """GRID coordinates of the target cell."""
        match self.target:
            case self.Target.down:
                return self.x, self.y + 1
            case self.Target.left:
                return self.x - 1, self.y
            case self.Target.right:
                return self.x + 1, self.y

    @property
    def target_cell(self) -> Cell | None:
        x, y = self.target_xy
        return self.grid[y][x]

    @property
    def x(self) -> int:
        """GRID x coordinate of the current position."""
        return self.position[0]

    @property
    def y(self) -> int:
        """GRID y coordinate of the current position."""
        return self.position[1]

    @property
    def y_range(self) -> range:
        """Min and max GRID y values we can actually see within view."""
        # Remember, each cell is CELL_WIDTH pixels tall and the full image is self.IMAGE_HEIGHT pixels tall
        effective_y = max(0, self.y)
        available_above = ceil(self.Y_OFFSET / self.CELL_WIDTH)  # exclude current position
        available_below = ceil((self.IMAGE_HEIGHT - self.Y_OFFSET) / self.CELL_WIDTH)  # include current position
        return range(effective_y - available_above, effective_y + available_below)

    @property
    def greatest_visible_y(self) -> int:
        """The greatest GRID y value we can see."""
        return self.y_range.stop - 1

    @property
    def collected_display(self) -> str:
        return self.get_collected_display(coins=self.collected_coins, items=self.collected_items)

    @staticmethod
    def get_collected_display(*, coins: int, items: Mapping[Item, int]) -> str:
        out = '\n'.join(
            f'- **{item.display_name}** x{quantity}'
            for item, quantity in items.items() if quantity > 0
        )
        if coins:
            out = f'- {Emojis.coin} **{coins:,}**\n' + out

        return out

    @executor_function
    def prepare_backdrop(self) -> None:
        if self.biome in self.backdrops:
            self._backdrop = self.backdrops[self.biome]
            return

        w = self.IMAGE_WIDTH
        backdrop = Image.open(self.biome.backdrop_path).convert('RGBA')
        self.backdrops[self.biome] = backdrop.resize((w, backdrop.height * w // backdrop.width), Image.NEAREST)
        self._backdrop: Image.Image = self.backdrops[self.biome]

    async def prepare_assets(self) -> None:
        for layer in set(self.biome.get_layer(y) for y in (self.y_range.start, self.greatest_visible_y)):
            if layer.dirt not in self.dirt_images:
                tasks = [
                    layer.generate_dirt_sample(self.CELL_WIDTH, self.GRAIN_WIDTH)
                    for _ in range(self.GEN_IMAGES_PER_DIRT)
                ]
                self.dirt_images[layer.dirt] = list(await gather(*tasks))

        for row in self.grid[self.y_range.start:self.y_range.stop]:
            for cell in row:
                if cell and cell.item is not None and cell.item not in self.image_cache:
                    fp = await self.fetch_bytes(image_url_from_emoji(cell.item.emoji))
                    image = await self.open_sized(fp, (self.OVERLAY_WIDTH, self.OVERLAY_WIDTH))
                    self.image_cache[cell.item] = image

    def grid_to_image_coords(self, x: int, y: int) -> tuple[int, int]:
        """Convert GRID coordinates to pixel coordinates in the image."""
        px = x * self.CELL_WIDTH
        py = (y - self.y_range.start) * self.CELL_WIDTH
        return px, py

    def is_coordinates_visible(self, x: int, y: int) -> bool:
        if y < 2:
            return True

        v_sq = self.VISIBILITY ** 2
        return any(
            (ex - x) ** 2 + (ey - y) ** 2 <= v_sq for ex, ey in self.explored)  # TODO: this is an expensive check

    @executor_function
    def _generate_image(self, *, active: bool = True, draw_hp: bool = False) -> BytesIO:
        image = Image.new('RGBA', (self.IMAGE_WIDTH, self.IMAGE_HEIGHT), self.BG_COLOR)
        draw = ImageDraw.Draw(image)

        for y in self.y_range:
            if y < 0:
                continue
            for x in range(self.GRID_WIDTH):
                cell = self.grid[y][x]
                px, py = self.grid_to_image_coords(x, y)

                if cell is None:
                    draw.rectangle(
                        (px, py, px + self.CELL_WIDTH, py + self.CELL_WIDTH),
                        fill=self.DUG_COLOR,
                    )
                    continue

                # Not visible (in the future, add visibility upgrade)
                if active and y >= self.y and not self.is_coordinates_visible(x, y):
                    continue

                base = self.biome.get_layer(y).dirt
                image.paste(self.dirt_images[base][cell.dirt_index], (px, py))
                if cell.coins or cell.item and cell.item.type is not ItemType.dirt:
                    cell_image = self.coin_image if cell.coins else self.image_cache[cell.item]
                    image.paste(cell_image, (px + self.OVERLAY_PADDING, py + self.OVERLAY_PADDING), cell_image)

                if draw_hp and cell.item:
                    alpha = int(255 * max(0.0, cell.hp / cell.item.hp + 0.2))
                    if alpha > 255:
                        continue

                    for qx in range(px, px + self.CELL_WIDTH):
                        for qy in range(py, py + self.CELL_WIDTH):
                            r, g, b, a = image.getpixel((qx, qy))
                            image.putpixel((qx, qy), (r, g, b, alpha))

        # Draw the background ONLY if there is space up top
        true_origin = self.Y_OFFSET - max(0, self.y) * self.CELL_WIDTH
        if true_origin > -self.BG_OFFSET:
            image.paste(self._backdrop, (0, true_origin - self._backdrop.height + self.BG_OFFSET), self._backdrop)

        # Draw avatar
        ax, ay = self.grid_to_image_coords(self.x, self.y)
        image.paste(
            self.avatar_image,
            (ax + self.OVERLAY_PADDING, ay + self.OVERLAY_PADDING),
            self.avatar_image,
        )
        # Draw box around target
        tx, ty = self.grid_to_image_coords(*self.target_xy)
        draw.rectangle(
            (tx, ty, tx + self.CELL_WIDTH, ty + self.CELL_WIDTH),
            outline=(255, 0, 0),  # Red outline for target?
            width=2,
        )

        image.save(buffer := BytesIO(), format='PNG')
        buffer.seek(0)
        return buffer

    async def generate_image(self, *, active: bool = True, draw_hp: bool = False) -> File:
        """Generates the digging session image."""
        await self.prepare_assets()
        return File(await self._generate_image(active=active, draw_hp=draw_hp), filename='digging.png')

    # We can either simply "dig/mine" left/right targets, or OPTIONALLY move into the space after we have dug it.
    # However, after digging/mining a down target, we MUST move into that space (we "fall").

    def _cleanup_explored(self) -> None:
        self.explored = {(x, y) for x, y in self.explored if y >= self.y_range.start}

    def collect(self, x: int, y: int) -> int | Item | None:
        cell = self.grid[y][x]
        self.grid[y][x] = None

        out = None
        if cell is not None:
            if cell.coins:
                self.collected_coins += cell.coins
                out = cell.coins
            if cell.item:
                self.collected_items[cell.item] += 1
                out = cell.item

        self.explored.add((x, y))
        return out

    def collect_target(self) -> int | Item | None:
        x, y = self.target_xy
        if y < 0 or x < 0 or x >= self.GRID_WIDTH:
            return None

        return self.collect(x, y)

    def move(self) -> int | Item | None:
        """Moves into the target cell. Typically called after the target has successfully been "dug"."""
        x, y = self.target_xy
        out = self.collect_target() if y >= 0 else None

        if self.target is self.Target.down:
            self.grid.append(self.generate_row(len(self.grid)))
            self._cleanup_explored()

        self.position = self.target_xy
        return out

    def cascading_dig(self, total_hp: int) -> tuple[int, defaultdict[Item, int]]:
        # keep digging down and collecting until total_hp is 0.
        remaining = total_hp
        added_coins = 0
        added_items = defaultdict(int)
        self.target = self.Target.down

        while remaining > 0:
            if not self.target_cell or remaining >= self.target_cell.hp:
                if self.target_cell:
                    remaining -= self.target_cell.hp
                    if (
                        self.target_cell.item
                        and self.backpack_occupied + self.target_cell.item.volume > self.backpack.capacity
                    ):
                        break
                out = self.move()

                if isinstance(out, int):
                    added_coins += out
                elif isinstance(out, Item):
                    added_items[out] += 1
            else:
                self.target_cell.hp -= remaining
                remaining = 0

        if quest := self.quests.get_active_quest(QuestTemplates.dig_hp):
            self.ctx.bot.loop.create_task(quest.add_progress(total_hp - remaining))

        return added_coins, added_items

    def surrounding_dig(self, base_hp: int, *, radius: int = 2) -> tuple[int, int, defaultdict[Item, int]]:
        """Deals damage to all cells within the radius, collecting coins and items if possible.

        Note that `base_hp` damage is dealt to cells that are neighboring the target cell (distance 1), then
        HP dealt is multiplied by (1 / R^2) for cells that are further away, where R is the distance from the current
        position.
        """
        added_coins = total_hp = 0
        added_items = defaultdict(int)

        for y in range(self.y - radius, self.y + radius + 1):
            if y < 0 or y >= len(self.grid):
                continue
            for x in range(self.x - radius, self.x + radius + 1):
                if x < 0 or x >= self.GRID_WIDTH:
                    continue

                distance = (x - self.x) ** 2 + (y - self.y) ** 2
                if distance > radius ** 2:
                    continue

                cell = self.grid[y][x]
                if not cell or cell.item and self.backpack_occupied + cell.item.volume > self.backpack.capacity:
                    continue

                damage = base_hp / distance
                total_hp += min(cell.hp, damage)
                cell.hp -= damage

                if cell.hp <= 0:
                    out = self.collect(x, y)
                    if isinstance(out, int):
                        added_coins += out
                    elif isinstance(out, Item):
                        added_items[out] += 1

        # Normalize
        self.target = self.Target.down
        while self.target_cell is None:
            self.move()

        if quest := self.quests.get_active_quest(QuestTemplates.dig_hp):
            self.ctx.bot.loop.create_task(quest.add_progress(total_hp))

        return total_hp, added_coins, added_items
