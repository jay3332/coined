from __future__ import annotations

import asyncio
import datetime
from collections import defaultdict
from functools import partial
from textwrap import dedent
from typing import Any, Callable, Literal, NamedTuple, ValuesView, TYPE_CHECKING, TypeAlias

import discord
from discord import app_commands
from discord.ext import commands

from app.core import (
    Cog,
    Command,
    Context,
    EDIT,
    ERROR, HybridCommand,
    HybridContext, NO_EXTRA,
    REPLY,
    command,
    lock_transactions,
    simple_cooldown,
    user_max_concurrency,
)
from app.core.flags import Flags, store_true
from app.core.helpers import check_transaction_lock, get_transaction_lock
from app.data.backpacks import Backpack, Backpacks
from app.data.items import Item, ItemRarity, ItemType, Items
from app.data.pets import Pets
from app.data.quests import QuestTemplates
from app.data.recipes import Recipe, Recipes
from app.database import InventoryManager, NotificationData, UserRecord
from app.util.common import (
    converter,
    cutoff,
    get_by_key,
    image_url_from_emoji,
    humanize_duration,
    humanize_list,
    progress_bar,
    query_collection,
    query_collection_many,
    walk_collection,
)
from app.util.converters import (
    BUY,
    BankTransaction,
    CaseInsensitiveMemberConverter,
    DEPOSIT,
    DROP,
    DropAmount,
    ItemAndQuantityConverter,
    SELL,
    USE,
    WITHDRAW,
    get_amount,
    query_item,
    query_recipe,
    query_repairable_item,
    transform_item_and_quantity,
)
from app.util.pagination import ActiveRow, FieldBasedFormatter, Formatter, LineBasedFormatter, Paginator
from app.util.structures import DottedDict, LockWithReason
from app.util.views import CommandInvocableModal, ConfirmationView, ModalButton, StaticCommandButton, UserLayoutView, \
    UserView
from config import Colors, Emojis

if TYPE_CHECKING:
    from app.core.timers import Timer
    from app.util.types import CommandResponse, TypedInteraction


class ItemTransformer(app_commands.Transformer):
    @classmethod
    async def convert(cls, _, value: str) -> Item:
        return query_item(value)

    async def transform(self, _, value: str) -> Item:
        return query_item(value)

    async def autocomplete(self, _, value: str) -> list[app_commands.Choice]:
        return [
            app_commands.Choice(name=item.name, value=item.key)
            for item in query_collection_many(Items, Item, value)
        ][:25]


@converter
async def RarityConverter(_, value: str) -> ItemRarity:
    try:
        return ItemRarity[value.lower()]
    except KeyError:
        raise commands.BadArgument(f'Invalid rarity {value!r}')


@converter
async def ItemTypeConverter(_, value: str) -> ItemType:
    if result := query_collection(ItemType, ItemType, value, get_key=lambda v: v.name):
        return result
    raise commands.BadArgument(f'Invalid item category {value!r}')


@converter
async def _SellBulkInvalidCatcher(_, value: str) -> commands.BadArgument:
    if value.startswith('-'):
        raise commands.BadArgument('flag starter')
    return commands.BadArgument(f'Invalid rarity or category {value!r}')


class DropView(discord.ui.View):
    def __init__(self, ctx: Context, embed: discord.Embed, entity: str, record: UserRecord) -> None:
        super().__init__(timeout=120)

        self.ctx: Context = ctx
        self.embed: discord.Embed = embed
        self.entity: str = entity
        self.record: UserRecord = record

        self.winner: discord.Member | None = None
        self._lock: asyncio.Lock = asyncio.Lock()

        self.embed.set_footer(text='')

    @discord.ui.button(label='Claim!', style=discord.ButtonStyle.success)
    async def claim(self, interaction: TypedInteraction, button: discord.ui.Button) -> None:
        if self.winner:
            await interaction.response.send_message('This drop has already been claimed!', ephemeral=True)

        async with self._lock:
            self.winner = interaction.user
            button.disabled = True

            embed = self.embed
            embed.colour = Colors.success
            embed.description = (
                f'{self.ctx.author.mention} reclaimed their own drop of {self.entity}.'
                if self.winner == self.ctx.author
                else f'{self.winner.mention} was the first one to click the button! They have received {self.entity}.'
            )
            embed.set_author(name=f'Winner: {self.winner}', icon_url=self.winner.avatar.url)

            await interaction.response.edit_message(embed=embed, view=self)
            self.stop()

    async def on_timeout(self) -> None:
        assert self.children

        child = self.children[0]
        assert isinstance(child, discord.ui.Button)

        child.disabled = True


class RecipeSelect(discord.ui.Select['RecipeView']):
    def __init__(self, default: Recipe | None = None) -> None:
        super().__init__(
            placeholder='Choose a recipe...',
            options=[
                discord.SelectOption(
                    label=recipe.name,
                    value=recipe.key,
                    emoji=recipe.emoji,
                    description=cutoff(recipe.description, max_length=50, exact=True),
                    default=default == recipe,
                )
                for recipe in walk_collection(Recipes, Recipe)
            ],
            row=0,
        )

    async def callback(self, interaction: TypedInteraction) -> Any:
        try:
            recipe = get_by_key(Recipes, self.values[0])
        except (KeyError, IndexError):
            return await interaction.response.send_message('Could not resolve that recipe for some reason.', ephemeral=True)

        self.view.current = recipe
        self.view.update()
        await interaction.response.edit_message(embed=self.view.build_embed(), view=self.view)


class RecipeView(UserView):
    def __init__(self, ctx: Context, record: UserRecord, default: Recipe | None = None) -> None:
        self.ctx: Context = ctx
        self.record: UserRecord = record

        self.current: Recipe = default or next(walk_collection(Recipes, Recipe))
        self.input_lock: asyncio.Lock = asyncio.Lock()

        super().__init__(ctx.author, timeout=60)
        self.add_item(RecipeSelect(default=default))

        self.update()

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.current.name, description=self.current.description, color=Colors.primary, timestamp=self.ctx.now,
        )
        embed.set_author(name='Recipes', icon_url=self.ctx.author.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji(self.current.emoji))

        embed.add_field(name='General', value=dedent(f"""
            **Name:**: {self.current.name}
            **Query Key: `{self.current.key}`**
            **Price:** {Emojis.coin} {self.current.price:,}
        """), inline=False)

        embed.add_field(name='Ingredients', value='\n'.join(
            (f'{item.display_name} x{quantity}' for item, quantity in self.current.ingredients.items())
        ))
        return embed

    def update(self) -> None:
        amount = self._get_max()
        toggle = amount > 0

        for child in self.children:
            if child is self.stop_button or not isinstance(child, discord.ui.Button):
                continue

            child.disabled = not toggle
            child.style = discord.ButtonStyle.primary

        self.craft_max.label = f'Craft Max ({amount:,})' if amount > 0 else 'Craft Max'

    async def _craft(self, amount: int = 1, *, interaction: TypedInteraction = None) -> Any:
        respond_error = partial(interaction.response.send_message, ephemeral=True) if interaction else self.ctx.reply
        respond = interaction.response.send_message if interaction else self.ctx.reply
        extra = f' ({Emojis.coin} **{self.current.price * amount:,}** for {amount})' if amount > 1 else ''

        if self.record.wallet < self.current.price * amount:
            return await respond_error(
                f'Insufficient funds: Crafting one of this item costs {Emojis.coin} **{self.current.price:,}**{extra}, '
                f'you only have {Emojis.coin} **{self.record.wallet:,}**.',
            )

        manager = self.record.inventory_manager
        quantity_of = manager.cached.quantity_of

        if any(quantity_of(item) < quantity * amount for item, quantity in self.current.ingredients.items()):
            extra = ', maybe try a lower amount?' if amount > 1 else ''

            return await respond_error(
                f"You don't have enough of the required ingredients to craft this recipe{extra}",
            )

        async with self.record.db.acquire() as conn:
            await self.record.add(wallet=-self.current.price * amount, connection=conn)

            for item, quantity in self.current.ingredients.items():
                await manager.add_item(item, -quantity * amount, connection=conn)

            for item, quantity in self.current.result.items():
                await manager.add_item(item, quantity * amount, connection=conn)

        embed = discord.Embed(color=Colors.success, timestamp=self.ctx.now)
        embed.set_author(name='Crafted Successfully', icon_url=self.ctx.author.display_avatar)

        embed.add_field(
            name='Crafted',
            value='\n'.join(f'{item.display_name} x{quantity * amount:,}' for item, quantity in self.current.result.items()),
            inline=False
        )

        embed.add_field(
            name='Ingredients Used',
            value=f'{Emojis.coin} {self.current.price * amount:,}\n' + '\n'.join(
                f'{item.display_name} x{quantity * amount:,}' for item, quantity in self.current.ingredients.items()
            ),
            inline=False,
        )
        await respond(embed=embed)

    def _get_max(self) -> int:
        inventory = self.record.inventory_manager

        item_max = min(inventory.cached.quantity_of(item) // quantity for item, quantity in self.current.ingredients.items())
        return min(item_max, self.record.wallet // self.current.price)

    @discord.ui.button(label='Craft One', style=discord.ButtonStyle.primary, row=1)
    async def craft_one(self, interaction: TypedInteraction, _) -> None:
        await self._craft(1, interaction=interaction)

    @discord.ui.button(label='Craft Max', style=discord.ButtonStyle.primary, row=1)
    async def craft_max(self, interaction: TypedInteraction, _) -> None:
        await self._craft(self._get_max(), interaction=interaction)

    @discord.ui.button(label='Craft Custom', style=discord.ButtonStyle.primary, row=1)
    async def craft_custom(self, interaction: TypedInteraction, _) -> Any:
        async with self.input_lock:
            await interaction.response.send_message(
                'How many of this item/recipe do you want to craft? Send a valid quantity in chat, e.g. "3" or "half".',
            )

            try:
                response = await self.ctx.bot.wait_for(
                    'message', timeout=30, check=lambda m: m.author == interaction.user,
                )
            except asyncio.TimeoutError:
                return await self.ctx.reply("You took too long to respond, cancelling.")

            maximum = self._get_max()
            try:
                await self._craft(get_amount(maximum, 1, maximum, response.content))
            except Exception as e:
                await self.ctx.reply(f'Error: {e.__class__.__name__}')

    @discord.ui.button(label='Stop', style=discord.ButtonStyle.danger, row=1)
    async def stop_button(self, interaction: TypedInteraction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True

            if child is not button and isinstance(child, discord.ui.Button):
                child.style = discord.ButtonStyle.secondary

        self.stop()
        await interaction.response.edit_message(view=self)


if TYPE_CHECKING:
    query_item_type: TypeAlias = ItemType | None
else:
    def query_item_type(arg: str) -> ItemType | None:
        if arg.lower() in ('all', '*'):
            return None
        return query_collection(ItemType, ItemType, arg, get_key=lambda value: value.name)


TITLE = 0
DESCRIPTION = 1


def shop_paginator(
    ctx: Context,
    *,
    record: UserRecord,
    inventory: InventoryManager,
    type: ItemType | None = None,
    query: str | None = None,
) -> Paginator:
    fields = []
    embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
    query = query and query.lower()
    offset = query and len(query)

    for i in walk_collection(Items, Item):
        if not i.buyable:
            continue
        if type is not None and i.type is not type:
            continue

        loc = match_loc = None
        if query is not None:
            if (loc := i.name.lower().find(query)) != -1:
                match_loc = TITLE
            elif i.key.find(query) != -1:
                pass
            elif (loc := i.description.lower().find(query)) != -1 and len(i.description) + offset < 100:
                match_loc = DESCRIPTION
            else:
                continue

        embed.set_author(name='Item Shop', icon_url=ctx.author.display_avatar)
        embed.description = (
            f'To buy an item, use `{ctx.clean_prefix}buy`.\n'
            f'To view information on an item, use `{ctx.clean_prefix}shop <item>`.'
        )

        comment = '*You cannot afford this item.*\n' if i.price > record.wallet else ''
        owned = inventory.cached.quantity_of(i)
        owned = f'(You own {owned:,})' if owned else ''

        description = cutoff(i.brief, max_length=100)

        end = loc and loc + offset
        name = i.name

        if match_loc == TITLE:
            name = f'{name[:loc]}**{name[loc:end]}**{name[end:]}'
        elif match_loc == DESCRIPTION:
            description = f'{description[:loc]}**{description[loc:end]}**{description[end:]}'

        fields.append({
            'name': f'{i.emoji} {name} — {Emojis.coin} {i.price:,} {owned}',
            'value': comment + description,
            'inline': False,
        })
    fields = fields or [{
        'name': 'No items found!',
        'value': f'No items found for query: `{query}`',
        'inline': False,
    }]

    return Paginator(
        ctx,
        FieldBasedFormatter(embed, fields, per_page=5),
        other_components=[ShopCategorySelect(ctx, record=record, inventory=inventory)],
        row=1,
    )


class ShopSearchModal(discord.ui.Modal):
    query = discord.ui.TextInput(
        label='Search Query',
        placeholder='Enter a search query for the item shop... (e.g. "spinning coin")',
        min_length=2,
        max_length=50,
    )

    def __init__(self) -> None:
        super().__init__(timeout=60, title='Item Search')
        self.interaction: TypedInteraction | None = None

    async def on_submit(self, interaction: TypedInteraction) -> None:
        self.interaction = interaction


class ShopCategorySelect(discord.ui.Select):
    OPTIONS = [
        discord.SelectOption(label='All Items', value='all'),
        *(
            discord.SelectOption(label=category.name.title(), value=str(category.value))
            for category in walk_collection(ItemType, ItemType)
            if any(item.type is category and item.buyable for item in walk_collection(Items, Item))
        ),
        discord.SelectOption(label='Search...', value='search'),
    ]

    def __init__(
        self,
        ctx: Context,
        *,
        record: UserRecord,
        inventory: InventoryManager,
    ) -> None:
        super().__init__(
            placeholder='Filter by category...',
            options=self.OPTIONS,
            row=0,
        )
        self.ctx = ctx
        self.record = record
        self.inventory = inventory

    async def callback(self, interaction: TypedInteraction) -> None:
        value = self.values[0]
        if value == 'all':
            query_type = None
            query_search = None
        elif value == 'search':
            query_type = None
            modal = ShopSearchModal()
            await interaction.response.send_modal(modal)
            await modal.wait()
            query_search = modal.query.value
            interaction = modal.interaction
        else:
            query_type = ItemType(int(value))
            query_search = None

        paginator = shop_paginator(
            self.ctx, record=self.record, inventory=self.inventory,
            type=query_type, query=query_search,
        )
        await paginator.start(edit=True, interaction=interaction)


class BankTransactionModal(CommandInvocableModal):
    amount = discord.ui.TextInput(
        label='How many coins do you want to {}?',
        placeholder='Enter a number or shorthand like "500", "10k", or "all"...',
        required=True,
        min_length=1,
        max_length=20,
    )

    def __init__(self, cmd: Command | HybridCommand, *, title: str, transaction: Literal[0, 1]) -> None:
        super().__init__(title=title, command=cmd)
        self.amount.label = self.amount.label.format('withdraw' if transaction == WITHDRAW else 'deposit')
        self.converter = BankTransaction(transaction)()

    async def on_submit(self, interaction: TypedInteraction, /) -> None:
        ctx = await self.get_context(interaction)
        try:
            value = await self.converter.convert(ctx, self.amount.value)
        except commands.CommandError as exc:
            await ctx.command.dispatch_error(ctx, exc)
        else:
            await self.invoke(ctx, amount=value)


class EquipBackpack(discord.ui.Button['BackpacksView']):
    def __init__(self, backpack: Backpack, container: BackpacksContainer) -> None:
        super().__init__()
        self.backpack: Backpack = backpack
        self.container: BackpacksContainer = container

    def update(self, *, equipped: bool) -> None:
        if equipped:
            self.label = 'Equipped!'
            self.style = discord.ButtonStyle.secondary
            self.disabled = True
        else:
            self.label = 'Equip'
            self.style = discord.ButtonStyle.primary
            self.disabled = False

    async def callback(self, interaction: TypedInteraction) -> Any:
        container: BackpacksContainer = self.container
        record = container.record

        if self.backpack not in record.unlocked_backpacks:
            await interaction.response.send_message(
                f'You do not own the **{self.backpack.name}** backpack.',
                ephemeral=True,
            )

        current = record.equipped_backpack
        self.update(equipped=True)
        if btn := container._btn_mapping.get(current):
            if isinstance(btn, EquipBackpack):
                btn.update(equipped=False)

        await record.update(backpack=self.backpack.key)
        await interaction.response.edit_message(view=self.view)
        return await interaction.followup.send(
            f'Equipped **{self.backpack.display}**',
            ephemeral=True,
        )


class UnlockBackpack(discord.ui.Button['BackpacksView']):
    def __init__(self, backpack: Backpack, container: BackpacksContainer, **kwargs) -> None:
        super().__init__(**kwargs)
        self.backpack: Backpack = backpack
        self.container: BackpacksContainer = container

    async def callback(self, interaction: TypedInteraction) -> Any:
        container: BackpacksContainer = self.container
        record = container.record

        if record.wallet < self.backpack.price:
            return await interaction.response.send_message(
                f'You do not have enough coins in your wallet to unlock **{self.backpack.name}**.',
                ephemeral=True,
            )

        if self.backpack in record.unlocked_backpacks:
            return await interaction.response.send_message('weird', ephemeral=True)

        if not await check_transaction_lock(container.ctx):
            return

        if not await container.ctx.confirm(
            f'Are you sure you want to unlock **{self.backpack.display}** '
            f'for {Emojis.coin} **{self.backpack.price:,}**?',
            delete_after=True,
            interaction=interaction,
        ):
            return await interaction.followup.send('Cancelled.', ephemeral=True)

        async with get_transaction_lock(container.ctx, update_jump_url=True):
            updated = list(set(b.key for b in record.unlocked_backpacks) | {self.backpack.key})
            async with container.ctx.db.acquire() as conn:
                await record.add(wallet=-self.backpack.price, connection=conn)
                await record.update(unlocked_backpacks=updated, connection=conn)

            container.update()
            await interaction.message.edit(view=self.view)
            return await interaction.followup.send(
                f'Unlocked **{self.backpack.display}** for {Emojis.coin} **{self.backpack.price:,}** coins.\n'
                f'You now have {Emojis.coin} **{record.wallet:,}** coins left in your wallet.',
                ephemeral=True,
            )


class BackpacksContainer(discord.ui.Container['BackpacksView']):
    def __init__(self, parent: BackpacksView) -> None:
        self.parent: BackpacksView = parent
        self.record: UserRecord = parent.ctx.db.get_user_record(parent.ctx.author.id, fetch=False)

        super().__init__(accent_color=Colors.primary)
        self.update()

    def update(self) -> None:
        self.clear_items()
        self.add_item(discord.ui.TextDisplay(f'## Backpack Shop'))
        self._btn_mapping: dict[Backpack, discord.ui.Button] = {}

        for backpack in walk_collection(Backpacks, Backpack, method=vars):
            self.add_item(discord.ui.Separator()).add_item(self.render_backpack(backpack))

    def render_backpack(self, backpack: Backpack) -> discord.ui.Section:
        if backpack in self.record.unlocked_backpacks:
            accessory = EquipBackpack(backpack, container=self)
            accessory.update(equipped=backpack is self.record.equipped_backpack)
        else:
            accessory = UnlockBackpack(
                backpack,
                container=self,
                label=f'Unlock ({backpack.price:,} coins)',
                style=discord.ButtonStyle.success,
                emoji=Emojis.coin,
                disabled=self.record.wallet < backpack.price,
            )
        self._btn_mapping[backpack] = accessory
        return discord.ui.Section(
            f'### **{backpack.display}**\n-# {backpack.description}',
            f'- **Price to Unlock:** {Emojis.coin} **{backpack.price:,}**\n'
            f'- **Capacity:** {backpack.capacity:,} storage units',
            accessory=accessory,
        )

    @property
    def ctx(self) -> Context:
        return self.parent.ctx


class BackpacksView(UserLayoutView):
    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx.author, timeout=60)
        self.ctx: Context = ctx
        inventory_mention = ctx.bot.tree.get_app_command('inventory').mention
        self.add_item(discord.ui.TextDisplay(
            f'**Looking for your permanent inventory?** Use {inventory_mention} instead.')
        )
        self.add_item(BackpacksContainer(self))


class SellBulkFlags(Flags):
    all_rarities = store_true(name='all-rarities', aliases=('ar', 'allrarities', 'all-rarity', 'rarity'), short='r')
    all_categories = store_true(
        name='all-categories', aliases=('ac', 'allcategories', 'all-category', 'category'), short='c',
    )
    all = store_true(short='a')
    keep_one = store_true(name='keep-one', aliases=('ko', 'keepone', 'keep-1', 'keep1', 'k1'), short='k')


class ActiveRepairJob(NamedTuple):
    item: Item
    start: datetime.datetime
    end: datetime.datetime


class Transactions(Cog):
    """Commands that handle transactions between the bank or other users."""

    emoji = '\U0001f91d'

    def __setup__(self) -> None:
        self._fetch_active_repair_jobs_task = self.bot.loop.create_task(self._fetch_active_repair_jobs())
        self.active_repair_jobs: defaultdict[int, dict[int, ActiveRepairJob]] = defaultdict(dict)

    async def _fetch_active_repair_jobs(self) -> None:
        query = """
                SELECT
                    id,
                    (metadata->'user_id')::BIGINT AS user_id,
                    metadata->>'item' AS item,
                    created_at,
                    expires
                FROM timers
                WHERE
                    event = 'repair'
                """

        await self.bot.db.wait()
        for record in await self.bot.db.fetch(query):
            self.active_repair_jobs[record['user_id']][record['id']] = ActiveRepairJob(
                item=get_by_key(Items, record['item']),
                start=record['created_at'],
                end=record['expires'],
            )

    @discord.utils.cached_property
    def withdraw_modal(self) -> Callable[[TypedInteraction], BankTransactionModal]:
        return lambda _: BankTransactionModal(self.withdraw, title='Withdraw Coins', transaction=WITHDRAW)

    @discord.utils.cached_property
    def deposit_modal(self) -> Callable[[TypedInteraction], BankTransactionModal]:
        return lambda _: BankTransactionModal(self.deposit, title='Deposit Coins', transaction=DEPOSIT)

    # noinspection PyTypeChecker
    @command(aliases={"w", "with", "wd"}, hybrid=True)
    @app_commands.describe(
        amount='The amount of coins to withdraw from your bank. Use "all" to withdraw all coins.'
    )
    @simple_cooldown(1, 8)
    @lock_transactions
    async def withdraw(self, ctx: Context, *, amount: BankTransaction(WITHDRAW)) -> Any:
        """Withdraw coins from your bank."""
        data = await ctx.db.get_user_record(ctx.author.id)

        async with ctx.typing():
            await data.add(wallet=amount, bank=-amount)

        embed = discord.Embed(color=Colors.primary)
        embed.set_author(name=f"Successful Transaction: {ctx.author}", icon_url=ctx.author.avatar)

        embed.description = f"Withdrew {Emojis.coin} **{amount:,}** from your bank."
        embed.add_field(name="Updated Balance", value=dedent(f"""
            Wallet: {Emojis.coin} **{data.wallet:,}**
            Bank: {Emojis.coin} **{data.bank:,}**
        """))

        view = discord.ui.View(timeout=60)
        view.add_item(ModalButton(
            modal=self.withdraw_modal, label='Withdraw More Coins', style=discord.ButtonStyle.primary,
            disabled=not data.bank,
        ))
        view.add_item(ModalButton(modal=self.deposit_modal, label='Deposit Coins', style=discord.ButtonStyle.primary))

        return embed, view, REPLY

    # noinspection PyTypeChecker
    @command(aliases={"d", "dep"}, hybrid=True)
    @app_commands.describe(
        amount='The amount of coins to deposit into your bank. Use "all" to deposit all coins.'
    )
    @simple_cooldown(1, 8)
    @lock_transactions
    async def deposit(self, ctx: Context, *, amount: BankTransaction(DEPOSIT)) -> Any:
        """Deposit coins from your wallet into your bank."""
        data = await ctx.db.get_user_record(ctx.author.id)

        async with ctx.typing():
            await data.add(wallet=-amount, bank=amount)

        embed = discord.Embed(color=Colors.primary)
        embed.set_author(name=f"Successful Transaction: {ctx.author}", icon_url=ctx.author.avatar)

        embed.description = f"Deposited {Emojis.coin} **{amount:,}** into your bank."
        embed.add_field(name="Updated Balance", value=dedent(f"""
            Wallet: {Emojis.coin} **{data.wallet:,}**
            Bank: {Emojis.coin} **{data.bank:,}**
        """))

        view = discord.ui.View(timeout=60)
        view.add_item(ModalButton(
            modal=self.deposit_modal, label='Deposit More Coins', style=discord.ButtonStyle.primary,
            disabled=not data.wallet,
        ))
        view.add_item(ModalButton(
            modal=self.withdraw_modal, label='Withdraw Coins', style=discord.ButtonStyle.primary,
        ))

        return embed, view, REPLY

    @command(aliases={"store", "market", "sh", "iteminfo", "ii", "item"}, hybrid=True)
    @app_commands.describe(item='The item to view specific information on.')
    @simple_cooldown(4, 6)
    async def shop(self, ctx: Context, *, item: query_item = None) -> CommandResponse:
        """View the item shop, or view information on a specific item.

        Arguments:
        - `item`: The item to view information on. Leave blank to the view the item shop.
        """
        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        record = await ctx.db.get_user_record(ctx.author.id)

        inventory = record.inventory_manager
        await inventory.wait()

        if not item:
            paginator = shop_paginator(ctx, record=record, inventory=inventory)
            return paginator, REPLY

        item: Item
        owned = inventory.cached.quantity_of(item)

        embed.title = f'{item.display_name} ({owned:,} owned)'
        embed.description = item.description
        embed.set_thumbnail(url=image_url_from_emoji(item.emoji))

        embed.add_field(name='General', value=dedent(f"""
            Name: {item.get_display_name(bold=True)}
            Query Key: **`{item.key}`**
            Type: **{item.type.name.title()}**
            Rarity: {item.rarity.emoji} **{item.rarity.name.title()}**
        """))

        then = '\n' + Emojis.Expansion.standalone
        buy_text = f"""\
            {'Buy Price' if item.buyable else 'Reference Value'}: {Emojis.coin} **{item.price:,}** per unit \
            {f'{then} Total {Emojis.coin} **{item.price * owned:,}** for the {owned:,} you own' if owned else ''}\
        """ if not item.sellable or item.price != item.sell else ''

        embed.add_field(name='Pricing', value=dedent(f"""
            {buy_text}
            {f'Sell Value: {Emojis.coin} **{item.sell:,}** per unit' if item.sellable else ''} \
            {f'{then} Total {Emojis.coin} **{item.sell * owned:,}** for the {owned:,} you own' if item.sellable and owned else ''}
        """), inline=False)

        allowed = []
        forbidden = []
        for verb, value in (
            ('buy', item.buyable),
            ('sell', item.sellable),
            ('use', item.usable),
            ('remove', item.removable),
            ('gift', item.giftable),
        ):
            target = allowed if value else forbidden
            target.append(verb)

        flexibility = []
        if allowed:
            flexibility.append(f'You can {humanize_list([f"**{verb}**" for verb in allowed])} this item.')
        if forbidden:
            flexibility.append(f'You *cannot* {humanize_list(forbidden, joiner="or")} this item.')
        embed.add_field(name='Flexibility', value='\n'.join(flexibility), inline=False)

        view = discord.ui.View(timeout=120)
        check = lambda itx: itx.user == ctx.author
        if item.sellable:
            view.add_item(button := StaticCommandButton(
                command=self.sell, command_kwargs=dict(item_and_quantity=(item, 1)), check=check,
                label='Sell One', style=discord.ButtonStyle.primary, disabled=not owned,
            ))
            if owned > 1:
                view.add_item(StaticCommandButton(
                    command=self.sell, command_kwargs=dict(item_and_quantity=(item, owned)), check=check,
                    label='Sell All', style=discord.ButtonStyle.primary,
                ))
            else:
                button.label = 'Sell'

        if item.usable:
            view.add_item(StaticCommandButton(
                command=self.use, command_kwargs=dict(item_and_quantity=(item, 1)), check=check,
                label='Use', style=discord.ButtonStyle.primary, disabled=not owned,
            ))

        return embed, view, REPLY

    @staticmethod
    def _bool_to_human(b: bool) -> str:
        return 'Yes' if b else 'No'

    @command(alias='purchase', hybrid=True, with_app_command=False)
    @simple_cooldown(3, 8)
    @user_max_concurrency(1)
    @lock_transactions
    async def buy(self, ctx: Context, *, item_and_quantity: ItemAndQuantityConverter(BUY)) -> tuple[discord.Embed | str, Any]:
        """Buy items!"""
        item, quantity = item_and_quantity
        price = item.price * quantity

        if not await ctx.confirm(
            f'Are you sure you want to buy {item.get_sentence_chunk(quantity)} for {Emojis.coin} **{price:,}**?',
            delete_after=True,
            reference=ctx.message,
        ):
            return 'Cancelled purchase.', REPLY

        record = await ctx.db.get_user_record(ctx.author.id)
        inventory = record.inventory_manager

        money_back = 0
        money_back_text = []

        pets = record.pet_manager
        if hamster := pets.get_active_pet(Pets.hamster):
            factor = 0.005 + hamster.level * 0.001
            money_back += round(price * factor)
            money_back_text.append(
                f'Your **{Pets.hamster.display}** finds you {Emojis.coin} **{money_back:,}** coins back!',
            )

        async with ctx.db.acquire() as conn:
            await record.add_random_exp(10, 15, chance=0.5, ctx=ctx, connection=conn)
            await record.add_random_bank_space(10, 15, chance=0.5, connection=conn)

            await record.add(wallet=-price + money_back, connection=conn)
            await inventory.add_item(item, quantity, connection=conn)

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.description = f'You bought {item.get_sentence_chunk(quantity)} for {Emojis.coin} **{price:,}** coins.'
        embed.set_author(name=f'Successful Purchase: {ctx.author}', icon_url=ctx.author.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji(item.emoji))

        if money_back and money_back_text:
            embed.add_field(name='Money Back', value='\n'.join(f'- {line}' for line in money_back_text), inline=False)

        return embed, REPLY

    @buy.define_app_command()
    @app_commands.describe(
        item='The item to purchase.',
        quantity='How many of the item to purchase. Use "max" to buy as many as possible.',
    )
    async def buy_app_command(
        self,
        ctx: HybridContext,
        item: app_commands.Transform[Item, ItemTransformer],
        quantity: str = '1',
    ) -> None:
        transformed = await transform_item_and_quantity(ctx, BUY, item, quantity)
        await ctx.invoke(ctx.command, item_and_quantity=transformed)

    @command(alias='s', hybrid=True, with_app_command=False)
    @simple_cooldown(3, 8)
    @user_max_concurrency(1)
    @lock_transactions
    async def sell(self, ctx: Context, *, item_and_quantity: ItemAndQuantityConverter(SELL)) -> tuple[discord.Embed | str, Any]:
        """Sell items from your inventory for coins."""
        item, quantity = item_and_quantity
        value = item.sell * quantity

        if not await ctx.confirm(
            f'Are you sure you want to sell {item.get_sentence_chunk(quantity)} in exchange for {Emojis.coin} **{value:,}**?',
            delete_after=True,
            reference=ctx.message,
        ):
            return 'Cancelled transaction.', REPLY

        record = await ctx.db.get_user_record(ctx.author.id)
        inventory = record.inventory_manager
        quests = await record.quest_manager.wait()

        async with ctx.db.acquire() as conn:
            await record.add_random_exp(10, 15, chance=0.4, ctx=ctx, connection=conn)
            await record.add_random_bank_space(10, 15, chance=0.4, connection=conn)

            await record.add(wallet=value, connection=conn)
            await inventory.add_item(item, -quantity, connection=conn)

            if quest := quests.get_active_quest(QuestTemplates.sell_items):
                await quest.add_progress(value, connection=conn)

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.description = f'You sold {item.get_sentence_chunk(quantity)} in exchange for {Emojis.coin} **{value:,}** coins.'
        embed.set_author(name=f'Successful Transaction: {ctx.author}', icon_url=ctx.author.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji(item.emoji))

        return embed, REPLY

    @sell.define_app_command()
    @app_commands.describe(
        item='The item to sell',
        quantity='How many of the item to sell. Use "all" to sell all.',
    )
    async def sell_app_command(
        self,
        ctx: HybridContext,
        item: app_commands.Transform[Item, ItemTransformer],
        quantity: str = '1',
    ) -> None:
        transformed = await transform_item_and_quantity(ctx, SELL, item, quantity)
        await ctx.invoke(ctx.command, item_and_quantity=transformed)

    @command(
        'sell-bulk', aliases={'sellall', 'sell-all', 'sa', 'sb', 'sellbulk', 'bulksell', 'bulk-sell'},
        hybrid=True, with_app_command=False,
    )
    @simple_cooldown(2, 10)
    @user_max_concurrency(1)
    @lock_transactions
    async def sell_bulk(
        self,
        ctx: Context,
        entities: commands.Greedy[RarityConverter | ItemTypeConverter | _SellBulkInvalidCatcher],
        *,
        flags: SellBulkFlags,
    ) -> CommandResponse:
        """Sell items in your inventory in bulk by rarity and/or category.

        By default, this command will sell all items that meet the following constraints:
        - the item has a rarity below epic (common, uncommon, or rare)
        - the item is not a crop, collectible, tool, net, or crate

        Likewise, the first constraint above is the default rarity constraint if none is provided, and the second
        constraint is the default category constraint if none is provided.

        When using this command via slash commands, you may only explicitly specify one single rarity and one single
        category at a time due to Discord limitations. This is subject to change in the future.

        Flags:
        - `--all-rarities`: Sell items of all rarities, overriding the rarity constraint (`-r`)
        - `--all-categories`: Sell items of all categories, overriding the category constraint (`-c`)
        - `--all`: Sell all items, overriding both the rarity and category constraints (`-a`)
        - `--keep-one`: Keep one of every item that would otherwise be sold (`-k`)

        Examples:
        - `{PREFIX}sell-bulk`: Sell by default constraints
        - `{PREFIX}sell-bulk common`: Sell only common items that are not crops, collectibles, tools, or crates
        - `{PREFIX}sell-bulk common uncommon tool`: Sell only common and uncommon tools
        - `{PREFIX}sell-bulk crop -r`: Sell all crops, regardless of rarity
        - `{PREFIX}sell-bulk --all`: Sell all items that can possibly be sold
        - `{PREFIX}sell-bulk --all --keep-one`: Sell all items except for one of every item that would otherwise be sold
        """
        record = await ctx.db.get_user_record(ctx.author.id)
        inventory = await record.inventory_manager.wait()

        if all(not quantity for quantity in inventory.cached.values()):
            return 'You don\'t have any items to sell.', REPLY

        if error := discord.utils.find(lambda e: isinstance(e, commands.BadArgument), entities):
            raise error
        rarities: set[ItemRarity] = set(r for r in entities if isinstance(r, ItemRarity))
        categories: set[ItemType] = set(c for c in entities if isinstance(c, ItemType))

        if flags.all or flags.all_rarities:
            rarities = set(ItemRarity)
        elif flags.all or flags.all_categories:
            categories = set(ItemType)

        rarities = rarities or {ItemRarity.common, ItemRarity.uncommon, ItemRarity.rare}
        categories = categories or (
            set(ItemType) - {ItemType.crop, ItemType.collectible, ItemType.tool, ItemType.net, ItemType.crate}
        )
        keep = 1 if flags.keep_one else 0

        items = {
            item: quantity - keep
            for item, quantity in inventory.cached.items()
            if item.sellable and quantity > keep and item.rarity in rarities and item.type in categories
        }
        if not items:
            return 'You do not have any sellable items in your inventory that match the provided constraints.', REPLY

        total = sum(item.sell * quantity for item, quantity in items.items())
        count = sum(items.values())

        embed = discord.Embed(color=Colors.warning, timestamp=ctx.now)
        embed.set_author(name=f'Confirm Bulk Sell: {ctx.author}', icon_url=ctx.author.display_avatar)

        friendly = [
            f'- {item.get_sentence_chunk(quantity)} worth {Emojis.coin} **{item.sell * quantity:,}**'
            for item, quantity in items.items()
        ]
        s = 's' if count != 1 else ''
        description = (
            f'You are about to sell **{count:,}** item{s} in bulk:\n{{}}\nTotal: {Emojis.coin} **{total:,}**'
        )
        paginator = Paginator(ctx, LineBasedFormatter(embed, friendly, description, per_page=15))
        if not await ctx.confirm(paginator=paginator, true='Confirm Bulk Sell'):
            return 'Alright, looks like we won\'t bulk sell today.', EDIT, dict(view=None)

        async with ctx.db.acquire() as conn:
            payload = {item.key: -quantity for item, quantity in items.items()}
            await record.inventory_manager.add_bulk(**payload, connection=conn)
            await record.add(wallet=total, connection=conn)

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.set_author(name=f'Successful Transaction: {ctx.author}', icon_url=ctx.author.display_avatar)
        description = (
            f'Successfully sold **{count:,}** item{s} for {Emojis.coin} **{total:,}**:\n{{}}'
        )

        paginator = Paginator(ctx, LineBasedFormatter(embed, friendly, description, per_page=15))
        return paginator, EDIT

    @sell_bulk.define_app_command()
    @app_commands.describe(
        rarity='The rarity of the items to sell. Defaults to common/uncommon/rare.',
        category='The category of items to sell. See /help command sell-bulk for more information on defaults.',
        keep_one='Whether to keep one of every item that would otherwise be sold.',
    )
    @app_commands.choices(
        rarity=[app_commands.Choice(name=rarity.name.title(), value=rarity.name) for rarity in ItemRarity],
        category=[app_commands.Choice(name=cat.name.title(), value=cat.name) for cat in ItemType],
    )
    @app_commands.rename(keep_one='keep-one')
    async def sell_bulk_app_command(
        self,
        ctx: HybridContext,
        rarity: str = None,
        category: str = None,
        keep_one: bool = False,
    ):
        if rarity:
            rarity = ItemRarity[rarity.lower()]
        if category:
            category = ItemType[category.lower()]

        flags = DottedDict(all_rarities=False, all_categories=False, all=False, keep_one=keep_one)
        await ctx.invoke(ctx.command, (rarity, category), flags=flags)  # type: ignore

    @command(aliases={'u', 'consume', 'activate', 'open'}, hybrid=True, with_app_command=False)
    @simple_cooldown(2, 10)
    @user_max_concurrency(1)
    @lock_transactions
    async def use(self, ctx: Context, *, item_and_quantity: ItemAndQuantityConverter(USE)):
        """Use the items you own!"""
        item, quantity = item_and_quantity
        record = await ctx.db.get_user_record(ctx.author.id)

        async with ctx.db.acquire() as conn:
            await record.add_random_exp(10, 15, chance=0.5, ctx=ctx, connection=conn)
            await record.add_random_bank_space(10, 15, chance=0.4, connection=conn)

            quantity = await item.use(ctx, quantity)

            if quantity > 0 and item.dispose:
                await record.inventory_manager.add_item(item, -quantity, connection=conn)

        await ctx.thumbs()

    @use.define_app_command()
    @app_commands.describe(
        item='The item to use',
        quantity='How many of the item to use. Note that some items may not be usable in bulk.',
    )
    async def use_app_command(
        self,
        ctx: HybridContext,
        item: app_commands.Transform[Item, ItemTransformer],
        quantity: str = '1',
    ) -> None:
        transformed = await transform_item_and_quantity(ctx, USE, item, quantity)
        await ctx.invoke(ctx.command, item_and_quantity=transformed)

    @command(aliases={'rm', 'dispose', 'deactivate', 'discard'}, hybrid=True)
    @simple_cooldown(2, 10)
    @user_max_concurrency(1)
    @lock_transactions
    async def remove(self, ctx: Context, *, item: query_item):
        """Remove the effects of active items."""
        record = await ctx.db.get_user_record(ctx.author.id)

        async with ctx.db.acquire() as conn:
            await record.add_random_exp(10, 15, chance=0.4, ctx=ctx, connection=conn)
            await record.add_random_bank_space(10, 15, chance=0.4, connection=conn)

            await item.remove(ctx)

        await ctx.thumbs()

    @shop.autocomplete('item')
    @remove.autocomplete('item')
    async def item_autocomplete(self, _, current: str) -> list[app_commands.Choice]:
        return [
            app_commands.Choice(name=item.name, value=item.key)
            for item in query_collection_many(Items, Item, current)
        ]

    @command(aliases={'bp', 'backpack'}, hybrid=True)
    @simple_cooldown(3, 6)
    async def backpacks(self, ctx: Context) -> CommandResponse:
        """View the backpack shop and manage your backpacks."""
        await ctx.db.get_user_record(ctx.author.id)
        return BackpacksView(ctx), REPLY

    @command(aliases={'rep', 'fix', 'repairshop'}, hybrid=True)
    @simple_cooldown(3, 6)
    @user_max_concurrency(1)
    @lock_transactions
    async def repair(self, ctx: Context, *, item: query_repairable_item = None) -> CommandResponse:
        """Repair a repairable item at the repair shop."""
        item: Item
        record = await ctx.db.get_user_record(ctx.author.id)
        inventory = await record.inventory_manager.wait()
        if item is None:
            formatter = RepairListFormatter(record, self.active_repair_jobs[ctx.author.id].values())
            return Paginator(ctx, formatter, other_components=[ActiveRepairRow()]), REPLY

        if inventory.cached.quantity_of(item) <= 0:
            return f'You do not have a **{item.display_name}** in your inventory to repair.', ERROR

        damage = inventory.damage.get(item, item.durability)
        remaining = item.durability - damage
        if remaining <= 0:
            return f'Your **{item.display_name}** is not damaged.', ERROR

        price = item.repair_rate * remaining
        if record.wallet < price:
            return (
                f'Repairing your {item.display_name} would cost {Emojis.coin} **{price:,}**, but you only have '
                f'{Emojis.coin} **{record.wallet:,}** in your wallet.',
                ERROR,
            )

        time = item.repair_time * remaining
        last_line = (
            f'{Emojis.Expansion.last} The *{item.name}* will be temporarily removed from your inventory while it is being repaired.'
        )
        if not await ctx.confirm(
            f'Are you sure you want to repair your **{item.display_name}** for {Emojis.coin} **{price:,}**?\n'
            f'{Emojis.Expansion.first} The repair will take **{humanize_duration(time)}** to complete.\n' + last_line,
            reference=ctx.message,
            delete_after=True,
        ):
            return 'Looks like we won\'t be repairing anything today.', REPLY

        async with ctx.db.acquire() as conn:
            await record.add(wallet=-price, connection=conn)
            await inventory.add_item(item, -1, connection=conn)
            await inventory.reset_damage(item, connection=conn)

        timer = await ctx.bot.timers.create(time, 'repair', user_id=ctx.author.id, item=item.key)
        self.active_repair_jobs[ctx.author.id][timer.id] = (
            ActiveRepairJob(item=item, start=timer.created_at, end=timer.expires)
        )

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.set_author(name=f'Repairing Item: {ctx.author.name}', icon_url=ctx.author.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji(item.emoji))
        embed.description = (
            f'**{item.display_name}** is now being repaired for {Emojis.coin} **{price:,}**.\n'
            f'{Emojis.Expansion.first} The repair will finish {discord.utils.format_dt(timer.expires, "R")}.\n'
            + last_line
        )
        view = discord.ui.View(timeout=120).add_item(
            StaticCommandButton(label='View Repairs', style=discord.ButtonStyle.primary, command=ctx.command)
        )
        return embed, view, REPLY

    @Cog.listener()
    async def on_repair_timer_complete(self, timer: Timer) -> Any:
        user_id = timer.metadata['user_id']
        item = get_by_key(Items, key := timer.metadata['item'])

        record = await self.bot.db.get_user_record(user_id)
        await record.inventory_manager.add_item(item, 1)
        self.active_repair_jobs[user_id].pop(timer.id, None)

        await record.notifications_manager.add_notification(NotificationData.RepairFinished(key))

    @repair.autocomplete('item')
    async def repair_autocomplete(self, _, current: str) -> list[app_commands.Choice]:
        return [
            app_commands.Choice(name=item.name, value=item.key)
            for item in query_collection_many(Items, Item, current)
            if item.durability is not None
        ]

    @command(aliases={'give', 'gift', 'donate', 'pay'}, hybrid=True, with_app_command=False)
    @simple_cooldown(1, 30)
    @user_max_concurrency(1)
    @lock_transactions
    async def share(self, ctx: Context, user: CaseInsensitiveMemberConverter, *, entity: DropAmount | ItemAndQuantityConverter(DROP)):
        """Share coins or items from your inventory with another user."""
        if user.bot:
            return 'You cannot share with bots.', REPLY

        if user == ctx.author:
            return 'Sharing with yourself, that sounds kinda funny', REPLY

        if isinstance(entity, int):
            entity_human = f'{Emojis.coin} **{entity:,}**'
        else:
            item, quantity = entity
            entity_human = item.get_sentence_chunk(quantity)

        if not await ctx.confirm(
            f"Are you sure you want to give {entity_human} to {user.mention}?",
            allowed_mentions=discord.AllowedMentions.none(),
            reference=ctx.message,
            delete_after=True,
        ):
            return 'Cancelled transaction.', REPLY

        record = await ctx.db.get_user_record(ctx.author.id)
        their_record = await ctx.db.get_user_record(user.id)

        async with ctx.db.acquire() as conn:
            if isinstance(entity, int):
                await record.add(wallet=-entity, connection=conn)
                await their_record.add(wallet=entity, connection=conn)

                updated = f'{Emojis.coin} **{record.wallet:,}**', f'{Emojis.coin} **{their_record.wallet:,}**'
            else:
                # noinspection PyUnboundLocalVariable
                await record.inventory_manager.add_item(item, -quantity, connection=conn)
                await their_record.inventory_manager.add_item(item, quantity, connection=conn)

                updated = (
                    f'{item.emoji} {item.name} x{record.inventory_manager.cached.quantity_of(item):,}',
                    f'{item.emoji} {item.name} x{their_record.inventory_manager.cached.quantity_of(item):,}',
                )

            await their_record.notifications_manager.add_notification(
                (
                    NotificationData.ReceivedCoins(user_id=ctx.author.id, coins=entity)
                    if isinstance(entity, int)
                    else NotificationData.ReceivedItems(user_id=ctx.author.id, item=item.key, quantity=quantity)
                ),
                connection=conn,
            )

        embed = discord.Embed(color=Colors.success, timestamp=ctx.now)
        embed.description = f'You gave {entity_human} to {user.mention}.'
        embed.set_author(name=f'Successful Transaction: {ctx.author}', icon_url=ctx.author.display_avatar)

        us, them = updated
        embed.add_field(name='Updated Values', value=f'{ctx.author.name}: {us}\n{user.name}: {them}')

        return embed, REPLY

    @share.define_app_command()
    @app_commands.describe(
        user='The user to give coins or items to.',
        quantity='How many coins or how many of the item to share',
        item='The item to share. If not provided, will share coins.',
    )
    async def share_app_command(
        self,
        ctx: Context,
        user: discord.Member,
        quantity: str,
        item: app_commands.Transform[Item, ItemTransformer] = None,
    ) -> None:
        if item is None:
            entity = await DropAmount().convert(ctx, quantity)
        else:
            entity = await transform_item_and_quantity(ctx, DROP, item, quantity)
        await ctx.invoke(ctx.command, user, entity=entity)

    @command(aliases={'giveaway'}, hybrid=True, with_app_command=False)
    @simple_cooldown(1, 30)
    @user_max_concurrency(1)
    async def drop(self, ctx: Context, *, entity: DropAmount | ItemAndQuantityConverter(DROP)):
        """Drop coins or items from your inventory into the chat.

        The first one to click the button will retrieve your coins!
        If no one clicks the button within 120 seconds, your coins/items will be returned.
        """
        record = await ctx.db.get_user_record(ctx.author.id)

        if isinstance(entity, int):
            await record.add(wallet=-entity)
        else:
            item, quantity = entity
            inventory = await record.inventory_manager.wait()
            await inventory.add_item(item, -quantity)

        # noinspection PyUnboundLocalVariable
        entity_human = f"{Emojis.coin} **{entity:,}**" if isinstance(entity, int) else item.get_sentence_chunk(quantity)
        entity_type = 'coins' if isinstance(entity, int) else 'items'

        if not await ctx.confirm(
            f'Are you sure you want to drop {entity_human}?\n'
            f'This will make the {entity_type} available for anyone in this channel to claim.',
            edit=False,
            replace_interaction=True,
        ):
            if isinstance(entity, int):
                await record.add(wallet=entity)
            else:
                # noinspection PyUnboundLocalVariable
                await inventory.add_item(item, quantity)
            yield 'I guess we aren\'t dropping anything today then', dict(view=None), EDIT, NO_EXTRA
            return

        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.set_author(name=f'{ctx.author.name} has dropped {entity_type}!', icon_url=ctx.author.display_avatar)
        embed.description = f'{ctx.author.mention} has dropped {entity_human}!'
        embed.set_footer(text=f'Click the button below to retrieve your {entity_type}!')

        view = DropView(ctx, embed, entity_human, record)
        yield '', embed, view, EDIT, NO_EXTRA

        embed.set_footer(text='')

        await view.wait()
        if not view.winner:
            if isinstance(entity, int):
                await record.add(wallet=entity)
            else:
                # noinspection PyUnboundLocalVariable
                await inventory.add_item(item, quantity)

            embed.description = f'No one clicked the button! Your {entity_type} have been returned.'
            embed.colour = Colors.error

            await ctx.maybe_edit(embed=embed, view=view)
            return

        winner_record = await ctx.db.get_user_record(view.winner.id)
        if isinstance(entity, int):
            await winner_record.add(wallet=entity)
        else:
            # noinspection PyUnboundLocalVariable
            await winner_record.inventory_manager.add_item(item, quantity)

    @drop.define_app_command()
    @app_commands.describe(
        quantity='How many coins or how many of the item to drop',
        item='The item to drop. If not provided, will drop coins.',
    )
    async def drop_app_command(
        self,
        ctx: Context,
        quantity: str,
        item: app_commands.Transform[Item, ItemTransformer] = None,
    ) -> None:
        if item is None:
            entity = await DropAmount().convert(ctx, quantity)
        else:
            entity = await transform_item_and_quantity(ctx, DROP, item, quantity)
        await ctx.invoke(ctx.command, entity=entity)

    @command(aliases={'recipes', 'exchange', 'recipe', 'rc'}, hybrid=True)
    @simple_cooldown(1, 4)
    async def craft(self, ctx: Context, *, recipe: query_recipe = None):
        """Craft items from your inventory to make new ones!"""
        record = await ctx.db.get_user_record(ctx.author.id)
        await record.inventory_manager.wait()

        view = RecipeView(ctx, record, default=recipe)
        yield view.build_embed(), view, REPLY

        await view.wait()

    @craft.autocomplete('recipe')
    async def recipe_autocomplete(self, _, current: str) -> list[app_commands.Choice]:
        return [
            app_commands.Choice(name=recipe.name, value=recipe.key)
            for recipe in query_collection_many(Recipes, Recipe, current)
        ]

    PRESTIGE_WHAT_DO_I_LOSE = (
        '- Your wallet, bank, and bank space will be wiped.\n'
        '- Your inventory will be wiped, except for:\n'
        '  - Any collectibles,\n'
        '  - Any backpacks,\n'
        '  - Any crates, and\n'
        '  - Any items of **Mythic** or **Unobtainable** rarity.\n'
        '- All crops will be wiped on your farm, however you will keep all claimed land.'
    )
    PRESTIGE_WHAT_DO_I_KEEP = (
        '- You keep the aforementioned subset of items in your inventory,\n'
        '- All claimed land on your farm,\n'
        '- All skills and training progress,\n'
        '- All pets and their levels,\n'
        '- All crafting recipes you have discovered, and\n'
        '- Any non-tangible entities such as notifications and cooldowns.'
    )

    @command(aliases={'pres', 'pr', 'prest', 'rebirth'}, hybrid=True)
    @simple_cooldown(1, 10)
    async def prestige(self, ctx: Context) -> CommandResponse:
        """Prestige and start over in exchange for long-term multipliers."""
        record = await ctx.db.get_user_record(ctx.author.id)
        inventory = await record.inventory_manager.wait()

        current_emoji = Emojis.get_prestige_emoji(record.prestige, trailing_ws=True)
        next_emoji = Emojis.get_prestige_emoji(next_prestige := record.prestige + 1, trailing_ws=True)

        level_requirement = next_prestige * 20
        meets_level = record.level >= level_requirement

        bank_requirement = next_prestige * 50_000
        meets_bank = record.bank >= bank_requirement

        unique_items = sum(value > 0 for value in inventory.cached.values())
        unique_items_requirement = min(48 + next_prestige * 2, len(list(Items.all())) - 4)
        meets_unique_items = unique_items >= unique_items_requirement

        _ = lambda b: Emojis.enabled if b else Emojis.disabled
        progress = lambda ratio: f'{progress_bar(ratio)} ({min(ratio, 1.0):.1%})'
        embed = discord.Embed(
            color=Colors.primary,
            timestamp=ctx.now,
            description=(
                f'Current prestige level: {current_emoji} **{record.prestige}**\n'
                f'Next prestige level: {next_emoji} **{next_prestige}**'
            ),
        )
        embed.set_author(name=f'Prestige: {ctx.author}', icon_url=ctx.author.display_avatar)
        embed.add_field(
            name=f'{_(meets_level)} Level **{record.level}**/{level_requirement:,}',
            value=progress(record.level / level_requirement),
            inline=False,
        )
        embed.add_field(
            name=f'{_(meets_bank)} Coins in Bank: {Emojis.coin} **{record.bank:,}**/{bank_requirement:,}',
            value=progress(record.bank / bank_requirement),
            inline=False,
        )
        embed.add_field(
            name=f'{_(meets_unique_items)} Unique Items: **{unique_items}**/{unique_items_requirement}',
            value=progress(unique_items / unique_items_requirement),
            inline=False,
        )
        if meets_level and meets_bank and meets_unique_items:
            embed.set_footer(text='You meet all requirements to prestige!')
            view = PrestigeView(ctx, record=record, next_prestige=next_prestige)
            return embed, view, REPLY

        view = discord.ui.View(timeout=1)  # timeout=0 gives weird problems
        view.add_item(
            discord.ui.Button(
                label='You do not meet prestige requirements yet.',
                style=discord.ButtonStyle.secondary,
                disabled=True,
            ),
        )
        return embed, view, REPLY


class RepairListFormatter(Formatter[Item | ActiveRepairJob]):
    def __init__(self, record: UserRecord, active_repairs: ValuesView[ActiveRepairJob]) -> None:
        assert record.inventory_manager._task.done(), 'inventory must be fetched'
        self.record = record

        self._cached = cached = record.inventory_manager.cached
        self._damage = damage = record.inventory_manager.damage

        now = discord.utils.utcnow()
        # sort by damage ratio
        entries = sorted(active_repairs, key=lambda r: r.end - now) + sorted(
            (item for item, quantity in cached.items() if item.durability is not None and quantity > 0),
            key=lambda item: damage[item] / item.durability if damage.get(item) else 1,
        )

        super().__init__(entries, per_page=5)

    async def format_page(self, paginator: Paginator, entry: list[Item | ActiveRepairJob]) -> discord.Embed:
        embed = discord.Embed(color=Colors.secondary, timestamp=paginator.ctx.now)
        embed.description = (
            '**Welcome to the repair shop!**\nHere you can repair your damaged items for a small fee.\n'
        )

        embed.set_author(name=f'Repair Shop: {paginator.ctx.author}', icon_url=paginator.ctx.author.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji('\U0001f6e0'))

        if not entry:
            embed.description += 'You do not have any repairable items in your inventory.'
            return embed
        else:
            embed.description += 'All repairable items you own are listed below.'

        for item in entry:
            if isinstance(item, ActiveRepairJob):
                elapsed = paginator.ctx.now - item.start
                duration = item.end - item.start
                embed.add_field(
                    name=f'**Currently Repairing:** {item.item.display_name}',
                    value=f'\N{ALARM CLOCK} {progress_bar(elapsed / duration, length=6)} ({discord.utils.format_dt(item.end, "R")})',
                    inline=False,
                )
                continue

            damage = self._damage.get(item, item.durability)
            remaining = item.durability - damage
            if remaining <= 0:
                text = f'{Emojis.Expansion.standalone} This item is not damaged yet.'
                provider = Emojis.GreenProgressBars
            else:
                text = (
                    f'{Emojis.Expansion.first} Repair Price: {Emojis.coin} **{item.repair_rate * remaining:,}**\n'
                    f'{Emojis.Expansion.last} Repair Time: **{humanize_duration(item.repair_time * remaining)}**'
                )
                provider = Emojis.RedProgressBars

            ratio = damage / item.durability
            embed.add_field(
                name=f'{item.display_name}',
                value=f'Condition: {progress_bar(ratio, length=6, provider=provider)} ({ratio:.0%})\n{text}',
                inline=False,
            )

        return embed


class ActiveRepairButton(StaticCommandButton):
    def __init__(self, paginator: Paginator, item: Item) -> None:
        disabled = paginator.formatter._damage.get(item, item.durability) >= item.durability  # type: ignore
        super().__init__(
            style=discord.ButtonStyle.success, label=f'Repair {item.name}', emoji=item.emoji, disabled=disabled,
            command=paginator.ctx.bot.get_command('repair'), command_kwargs={'item': item},
            check=lambda itx: itx.user.id == paginator.ctx.author.id,
        )
        self.item = item


class ActiveRepairRow(ActiveRow):
    def __init__(self) -> None:
        super().__init__(row=2)

    async def active_update(self, paginator: Paginator, entry: list[Item | ActiveRepairJob]) -> list[discord.ui.Button]:
        return [ActiveRepairButton(paginator, item) for item in entry if isinstance(item, Item)]


class PrestigeView(UserView):
    def __init__(self, ctx: Context, *, record: UserRecord, next_prestige: int) -> None:
        super().__init__(ctx.author, timeout=60)
        self.ctx = ctx
        self.record: UserRecord = record
        self.inventory: InventoryManager = record.inventory_manager
        self.next_prestige = next_prestige
        self.prestige.emoji = self.emoji = Emojis.get_prestige_emoji(next_prestige)

    @discord.ui.button(label='Prestige!', style=discord.ButtonStyle.primary)
    async def prestige(self, interaction: TypedInteraction, _button: discord.ui.Button) -> Any:
        lock = self.ctx.bot.transaction_locks.setdefault(self.ctx.author.id, LockWithReason())
        if lock.locked():
            reason = f' ({lock.reason})' if lock.reason else ''
            return await interaction.response.send_message(
                f'You are already performing a transaction{reason}. '
                'Please finish it or wait until it is finished before prestiging.',
                ephemeral=True,
            )

        async with lock:
            await self._prestige(interaction, _button)

    async def _prestige(self, interaction: TypedInteraction, _button: discord.ui.Button) -> Any:
        crate = Items.mythic_crate if self.next_prestige % 5 == 0 else Items.legendary_crate
        receive = (
            f'- {Items.banknote.get_sentence_chunk(self.next_prestige)},\n'
            f'- {crate.get_sentence_chunk()},\n'
            f'- {self.next_prestige * 50}% faster bank space gain,\n'
            f'- {self.next_prestige * 25}% XP multiplier,\n'
            f'- {self.next_prestige * 25}% coin multiplier,\n'
            f'- {self.next_prestige * 20}% extra space from banknotes,\n'
            f'- 20 more :zap: stamina when digging and diving, and\n'
            f'- {self.emoji} **Prestige {self.next_prestige}** badge'
        )
        message = (
            f'You are about to prestige to {self.emoji} **Prestige {self.next_prestige}**!\n\n'
            'Prestiging is required to get far into the economy. '
            'With it, you gain perks, multipliers, and increased limits that are unobtainable without doing so.\n'
            '## What will I lose?\n'
            f'{Transactions.PRESTIGE_WHAT_DO_I_LOSE}\n'
            '## What will I keep?\n'
            f'{Transactions.PRESTIGE_WHAT_DO_I_KEEP}\n'
            f'## What will I get in exchange for prestiging?\n{receive}'
        )

        view = ConfirmationView(user=self.ctx.author, true="Yes, let's prestige!", false='Maybe next time', timeout=120)
        if not await self.ctx.confirm(message, interaction=interaction, view=view):
            self.stop()
            return await view.interaction.response.send_message('Okay, we will postpone your prestige.', ephemeral=True)

        async with self.ctx.db.acquire() as conn:
            keep = {
                item.key: quantity
                for item, quantity in self.inventory.cached.items()
                if quantity > 0 and (
                    item.type in (ItemType.collectible, ItemType.crate)
                    or item.rarity in (ItemRarity.mythic, ItemRarity.unobtainable)
                )
            }
            await self.record.update(
                wallet=0, bank=0, max_bank=100, prestige=self.next_prestige, connection=conn,
            )
            inventory = self.record.inventory_manager
            await inventory.wipe(connection=conn)
            await self.record.crop_manager.wipe_keeping_land(connection=conn)

            # Replenish promised items
            await inventory.update(**keep)
            await inventory.add_item(Items.banknote, self.next_prestige, connection=conn)
            await inventory.add_item(crate, 1, connection=conn)

        self.stop()
        await view.interaction.response.send_message(
            f'\U0001f389 What a legend, after prestiging you are now {self.emoji} **Prestige {self.next_prestige}**.\n'
            f'## You have received:\n{receive}'
        )


setup = Transactions.simple_setup
