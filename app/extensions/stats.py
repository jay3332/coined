from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta
from math import ceil
from io import BytesIO
from textwrap import dedent
from typing import Any, Iterable, Literal, TYPE_CHECKING

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext.commands import BadArgument
from PIL import Image

from app import Bot
from app.core import BAD_ARGUMENT, Cog, Context, HybridContext, NO_EXTRA, REPLY, command, group, simple_cooldown
from app.core.flags import Flags, flag, store_true
from app.data.items import ItemRarity, ItemType, Item, Items, LEVEL_REWARDS
from app.database import (
    InventoryManager,
    Multiplier,
    NotificationData,
    NotificationsManager,
    UserHistoryEntry,
    UserRecord,
    aggregate_multipliers,
)
from app.extensions.transactions import query_item_type
from app.util.common import converter, cutoff, humanize_duration, image_url_from_emoji, progress_bar
from app.util.converters import CaseInsensitiveMemberConverter, IntervalConverter
from app.util.graphs import send_graph_to
from app.util.pagination import (
    FieldBasedFormatter,
    Formatter,
    LineBasedFormatter,
    NavigableItem,
    NavigationRow,
    Paginator,
)
from app.util.structures import DottedDict
from app.util.views import ModalButton, StaticCommandButton, UserLayoutView, invoke_command
from config import Colors, Emojis

if TYPE_CHECKING:
    from app.extensions.transactions import Transactions
    from app.util.types import CommandResponse, TypedInteraction

_LB_SORT_BY_MAPPING: dict[str | None, str] = {
    None: 'wallet',
    'wallet': 'wallet',
    'w': 'wallet',
    'pocket': 'wallet',
    'bank': 'bank',
    'b': 'bank',
    'total': 'total_coins',
    'total_coins': 'total_coins',
    't': 'total_coins',
    'xp': 'total_exp',
    'level': 'total_exp',
    'lvl': 'total_exp',
    'l': 'total_exp',
    'exp': 'total_exp',
    'v': 'votes_this_month',
    'votes': 'votes_this_month',
    'vote': 'votes_this_month',
    'dig': 'deepest_dig',
    'd': 'deepest_dig',
    'depth': 'deepest_dig',
    'deepest': 'deepest_dig',
}


@converter
async def LeaderboardSortByConverter(_ctx: Context, argument: str) -> str:
    if alias := _LB_SORT_BY_MAPPING.get(argument.lower()):
        return alias
    raise BadArgument()


class LeaderboardFlags(Flags):
    is_global = store_true(
        name='global', short='g',
        description='Show the global leaderboard instead of the server leaderboard.',
    )


class LeaderboardFormatter(Formatter[tuple[UserRecord, discord.Member]]):
    def __init__(
        self,
        records: list[tuple[UserRecord, discord.Member]],
        *,
        per_page: int,
        is_global: bool,
        attr: str,
    ) -> None:
        self.is_global = is_global
        self.attr = attr

        super().__init__(records, per_page=per_page)
        self.records = records

    ATTR_TEXT: dict[str, str] = {
        'wallet': 'Sorted by coins in wallet',
        'bank': 'Sorting by coins in bank',
        'total_coins': 'Sorted by total coins',
        'total_exp': 'Sorted by level and EXP',
        'votes_this_month': 'Sorted by votes this month',
        'deepest_dig': 'Sorted by deepest dig (any biome)',
    }

    async def format_page(self, paginator: Paginator, entries: list[tuple[UserRecord, discord.Member]]) -> discord.Embed:
        result = []

        for i, (record, user) in enumerate(entries, start=paginator.current_page * 10):
            match i:
                case 0:
                    start = '\U0001f3c6'
                case 1:
                    start = '\U0001f948'
                case 2:
                    start = '\U0001f949'
                case _:
                    start = '<:bullet:934890293902327838>'

            record: UserRecord
            anonymize = self.is_global and record.anonymous_mode and not (
                paginator.ctx.guild and record.user_id in paginator.ctx.guild._members
                or record.user_id == paginator.ctx.author.id
            )
            name = '*Anonymous User*' if anonymize else discord.utils.escape_markdown(str(user))

            match self.attr:
                case 'wallet' | 'bank' | 'total_coins':
                    stat = f'{Emojis.coin} **{getattr(record, self.attr):,}**'
                case 'total_exp':
                    stat = f'**Level {record.level:,}** \u2022 {record.exp:,} XP'
                case 'votes_this_month':
                    stat = f'**{record.votes_this_month:,} votes**'
                case 'deepest_dig':
                    stat = f'**{record.deepest_dig:,} meters**'
                case _:
                    raise ValueError(f'Unknown leaderboard attribute: {self.attr}')

            result.append(
                f'{start} {stat} \u2014 {name} {Emojis.get_prestige_emoji(record.prestige)}'
            )

        embed = discord.Embed(color=Colors.primary, description='\n'.join(result), timestamp=paginator.ctx.now)
        # noinspection PyTypeChecker
        if self.is_global:
            embed.set_author(name='Coined: Global Leaderboard (Top 100)')
        else:
            embed.set_author(name=f'Leaderboard: {paginator.ctx.guild.name}', icon_url=paginator.ctx.guild.icon)

        embed.set_footer(text=self.ATTR_TEXT[self.attr])
        return embed


class GraphFlags(Flags):
    total = store_true(
        aliases=('total-coins', 'tot'), short='t', description='Show total coins instead of wallet coins.',
    )
    duration: IntervalConverter = flag(
        short='d',
        aliases=('dur', 'time', 'interval', 'lookback', 'timespan', 'span'),
        default=timedelta(minutes=15), description='How far back to look for data.',
    )


class GuildGraphFlags(GraphFlags):
    duration: IntervalConverter = flag(
        short='d',
        aliases=('dur', 'time', 'interval', 'lookback', 'timespan', 'span'),
        description='How far back to look for data.',
    )


class RefreshBalanceButton(discord.ui.Button):
    def __init__(self, cog: Stats, *, user: discord.User, record: UserRecord, color: int) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, emoji=Emojis.refresh)
        self.cog = cog
        self.user = user
        self.record = record
        self.color = color

    async def callback(self, interaction: TypedInteraction) -> None:
        embed, view = self.cog._generate_balance_stats(self.user, self.record, self.color)
        await interaction.response.edit_message(embed=embed, view=view)


class RefreshInventoryButton(discord.ui.Button):
    def __init__(self, parent: InventoryContainer) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, emoji=Emojis.refresh)
        self.parent = parent

    async def callback(self, interaction: TypedInteraction) -> Any:
        self.parent.update()
        await interaction.response.edit_message(view=self.view)


class InventorySortBy(discord.ui.Select['InventoryView']):
    def __init__(self, parent: InventoryContainer) -> None:
        super().__init__(
            placeholder='Sort by...',
            options=[
                discord.SelectOption(label='Sort by Name (A-Z)', value='name', default=True),
                discord.SelectOption(label='Sort by Individual Price', value='price'),
                discord.SelectOption(label='Sort by Total Sell Value', value='sell'),
                discord.SelectOption(label='Sort by Quantity Owned', value='quantity'),
            ],
        )
        self.parent = parent

    @property
    def value(self) -> str:
        if not self.values:
            return 'name'
        return self.values[0]

    async def callback(self, interaction: TypedInteraction) -> Any:
        self.parent.recompute_entries()
        self.parent.update()
        await interaction.response.edit_message(view=self.view)


class InventoryFilterByRarity(discord.ui.Select['InventoryView']):
    def __init__(self, parent: InventoryContainer) -> None:
        super().__init__(
            placeholder='Filter by rarity...',
            options=[
                discord.SelectOption(
                    label=rarity.name.title(), value=rarity.name.lower(),
                    emoji=rarity.emoji,
                )
                for rarity in ItemRarity
            ],
            min_values=0,
            max_values=len(ItemRarity),
        )
        self.parent = parent

    async def callback(self, interaction: TypedInteraction) -> Any:
        self.parent.recompute_entries()
        self.parent.update()
        await interaction.response.edit_message(view=self.view)


class InventoryFilterByType(discord.ui.Select['InventoryView']):
    def __init__(self, parent: InventoryContainer) -> None:
        super().__init__(
            placeholder='Filter by type...',
            options=[
                discord.SelectOption(
                    label=category.name.title(),
                    value=category.name.lower(),
                )
                for category in ItemType
            ],
            min_values=0,
            max_values=len(ItemType),
        )
        self.parent = parent

    async def callback(self, interaction: TypedInteraction) -> Any:
        self.parent.recompute_entries()
        self.parent.update()
        await interaction.response.edit_message(view=self.view)


class InventoryFilterByFunction(discord.ui.Select['InventoryView']):
    def __init__(self, parent: InventoryContainer) -> None:
        super().__init__(
            placeholder='Filter by function...',
            options=[
                discord.SelectOption(label='Buyable', value='buyable'),
                discord.SelectOption(label='Sellable', value='sellable'),
                discord.SelectOption(label='Usable', value='usable'),
                discord.SelectOption(label='Giftable', value='giftable'),
                discord.SelectOption(label='Disposable', value='removable'),
            ],
            min_values=0,
            max_values=5,
        )
        self.parent = parent

    async def callback(self, interaction: TypedInteraction) -> Any:
        self.parent.recompute_entries()
        self.parent.update()
        await interaction.response.edit_message(view=self.view)


class InventoryToggleRow(discord.ui.ActionRow['InventoryView']):
    def __init__(self, parent: InventoryContainer) -> None:
        super().__init__()
        self.parent = parent

    def update(self) -> None:
        self.clear_items()
        self.add_item(self.toggle_compact).add_item(self.toggle_filters)
        if self.parent._show_filters:
            self.add_item(self.clear_filters)

    @discord.ui.button(label='Compact View')
    async def toggle_compact(self, interaction: TypedInteraction, button: discord.ui.Button) -> Any:
        self.parent._compact_view = not self.parent._compact_view
        button.label = 'Cozy View' if self.parent._compact_view else 'Compact View'
        self.parent.update()
        await interaction.response.edit_message(view=self.view)

    @discord.ui.button(label='Show Filters')
    async def toggle_filters(self, interaction: TypedInteraction, button: discord.ui.Button) -> Any:
        self.parent._show_filters = not self.parent._show_filters
        button.label = 'Hide Filters' if self.parent._show_filters else 'Show Filters'
        self.parent.update()
        await interaction.response.edit_message(view=self.view)

    @discord.ui.button(label='Clear Filters', style=discord.ButtonStyle.danger)
    async def clear_filters(self, interaction: TypedInteraction, _) -> Any:
        self.parent.reset_filters()
        self.parent.recompute_entries()
        self.parent.update()
        await interaction.response.edit_message(view=self.view)


class InventoryContainer(discord.ui.Container['InventoryView'], NavigableItem):
    def __init__(self) -> None:
        super().__init__(accent_color=Colors.primary)
        self.reset_filters()

        self._current_page = 0
        self._show_filters = False
        self._nav = NavigationRow(self)
        self._toggle_row = InventoryToggleRow(self)
        self._compact_view = False  # Whether to show a compact view of the inventory
        self.entries: list[tuple[Item, int]] = []  # Ordered list of (item, quantity) tuples

    def reset_filters(self) -> None:
        self._sort_by = InventorySortBy(self)
        self._filter_by_type = InventoryFilterByType(self)
        self._filter_by_rarity = InventoryFilterByRarity(self)
        self._filter_by_function = InventoryFilterByFunction(self)

        self._filters: list[discord.ui.ActionRow] = [
            discord.ui.ActionRow().add_item(item)
            for item in (
                self._sort_by,
                self._filter_by_type,
                self._filter_by_rarity,
                self._filter_by_function,
            )
        ]

    @property
    def ctx(self) -> Context:
        return self.view.ctx

    @property
    def user(self) -> discord.User:
        return self.view.user

    @property
    def inventory(self) -> InventoryManager:
        return self.view.inventory

    @property
    def current_page(self) -> int:
        return self._current_page

    @property
    def per_page(self) -> int:
        return 15 if self._compact_view else 6

    @property
    def max_pages(self) -> int:
        return max(1, ceil(len(self.entries) / self.per_page))

    @property
    def inventory_worth(self) -> int:
        return sum(item.price * quantity for item, quantity in self.inventory.cached.items())

    @property
    def unique_count(self) -> int:
        return len(self.inventory.cached)

    def recompute_entries(self) -> None:
        match self._sort_by.value:
            case 'name':
                sort_predicate = lambda pair: pair[0].key.lower()
            case 'price':
                sort_predicate = lambda pair: -pair[0].price
            case 'sell':
                sort_predicate = lambda pair: -pair[0].sell * pair[1]
            case 'quantity':
                sort_predicate = lambda pair: -pair[1]
            case _:
                raise ValueError(f'Unknown sort by value: {self._sort_by.values[0]}')

        filter_predicates = []
        if self._filter_by_type.values:
            filter_predicates.append(lambda item: item.type.name.lower() in self._filter_by_type.values)
        if self._filter_by_rarity.values:
            filter_predicates.append(lambda item: item.rarity.name.lower() in self._filter_by_rarity.values)
        if self._filter_by_function.values:
            filter_predicates.append(
                lambda item: any(getattr(item, func) for func in self._filter_by_function.values)
            )

        self.entries = sorted(
            (
                (item, quantity) for item, quantity in self.inventory.cached.items()
                if quantity > 0 and all(predicate(item) for predicate in filter_predicates)
            ),
            key=sort_predicate,
        )

        # this is in case the client refreshes
        for select in (self._filter_by_type, self._filter_by_rarity, self._filter_by_function):
            for option in select.options:
                option.default = option.value in select.values

    def get_page_entries(self) -> list[tuple[Item, int]]:
        start = self._current_page * self.per_page
        end = start + self.per_page
        return self.entries[start:end]

    async def set_page(self, interaction: TypedInteraction, page: int) -> Any:
        self._current_page = page
        self.update()
        await interaction.response.edit_message(view=self.view)

    def update(self) -> None:
        self.clear_items()

        self._current_page = min(self._current_page, self.max_pages - 1)
        your_inventory = 'Your inventory' if self.user == self.ctx.author else f"{self.user.name}'s inventory"
        you_own = 'you own' if self.user == self.ctx.author else 'they own'
        self.add_item(discord.ui.Section(
            f'## {self.user}\'s Inventory',
            f'-# {your_inventory} is worth {Emojis.coin} **{self.inventory_worth:,}**.\n'
            f'-# Additionally, {you_own} **{self.unique_count:,}** out of {len(list(Items.all())):,} unique items.',
            accessory=RefreshInventoryButton(self),
        ))
        self.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

        if not sum(self.inventory.cached.values()):
            self.add_item(discord.ui.TextDisplay('You currently do not own any items. Maybe buy some?'))
            return

        entries = self.get_page_entries()
        if not self.entries:
            self.add_item(discord.ui.TextDisplay(
                'You currently do not own any items that match the selected filters.'
            ))
        elif self._compact_view:
            self.add_item(discord.ui.TextDisplay('\n'.join(
                f'{item.get_display_name(bold=True)} \u2014 {quantity:,}' for item, quantity in entries
            )))
        else:
            for i, (item, quantity) in enumerate(entries):
                extra = (
                    f'Sell all for {Emojis.coin} **{item.sell * quantity:,}**'
                    if self._sort_by.value == 'sell'
                    else f'Worth {Emojis.coin} **{item.price * quantity:,}**'
                )
                self.add_item(discord.ui.TextDisplay(
                    f'{item.get_display_name(bold=True)} \u2014 {quantity:,}\n'
                    f'\u2002{Emojis.Expansion.standalone} {extra}'
                ))
                if i < len(entries) - 1 or not self._show_filters:
                    self.add_item(discord.ui.Separator(visible=False))

        if self._show_filters:
            self.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
            for item in self._filters:
                self.add_item(item)

        self._toggle_row.update()
        self.add_item(self._toggle_row)

        self._nav.update()
        if self.max_pages > 1:
            self.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large)).add_item(self._nav)


class InventoryView(UserLayoutView):
    def __init__(self, ctx: Context, user: discord.User, inventory: InventoryManager) -> None:
        super().__init__(ctx.author, timeout=300)
        self.ctx = ctx
        self.user = user
        self.inventory = inventory

        self.add_item(container := InventoryContainer())
        self.add_item(discord.ui.ActionRow().add_item(StaticCommandButton(
            command=ctx.bot.get_command('shop'),
            label='Go Shopping', style=discord.ButtonStyle.primary, emoji='\U0001f6d2',
        )))
        self.container = container


class Stats(Cog):
    """Useful statistical commands. These commands do not have any action behind them."""

    emoji = '\U0001f4ca'

    def __init__(self, bot: Bot) -> None:
        super().__init__(bot)

        self._balance_context_menu = app_commands.ContextMenu(
            name='View Balance', callback=self._balance_context_menu_callback,
        )
        bot.tree.add_command(self._balance_context_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self._balance_context_menu.name, type=self._balance_context_menu.type)

    def _generate_balance_stats(
        self, user: discord.User, data: UserRecord, color: int,
    ) -> tuple[discord.Embed, discord.ui.View]:
        embed = discord.Embed(color=color, timestamp=discord.utils.utcnow())
        prestige_text = (
            f'{Emojis.get_prestige_emoji(data.prestige)} Prestige {data.prestige}' if data.prestige else 'Coins'
        )
        embed.set_author(name=f"Balance: {user}", icon_url=user.avatar)
        embed.add_field(name=prestige_text, value=dedent(f"""
            - Wallet: {Emojis.coin} **{data.wallet:,}**
            - Bank: {Emojis.coin} **{data.bank:,}**/{data.max_bank:,} *[{data.bank_ratio:.1%}]*
            - Total: {Emojis.coin} **{data.wallet + data.bank:,}**
        """))
        embed.set_thumbnail(url=user.avatar)

        transactions: Transactions = self.bot.get_cog('Transactions')  # type: ignore
        view = discord.ui.View(timeout=60)
        view.add_item(ModalButton(
            modal=transactions.withdraw_modal, label='Withdraw Coins', style=discord.ButtonStyle.primary,
            disabled=not data.bank,
        ))
        view.add_item(ModalButton(
            modal=transactions.deposit_modal, label='Deposit Coins', style=discord.ButtonStyle.primary,
            disabled=not data.wallet,
        ))
        view.add_item(RefreshBalanceButton(self, user=user, record=data, color=color))
        return embed, view

    # noinspection PyTypeChecker
    @command(aliases={"bal", "coins", "stats", "b", "wallet"}, hybrid=True, with_app_command=False)
    @simple_cooldown(2, 5)
    async def balance(self, ctx: Context, *, user: CaseInsensitiveMemberConverter | None = None) -> CommandResponse:
        """View your wallet and bank balance, or optionally, someone elses."""
        user = user or ctx.author
        data = await ctx.db.get_user_record(user.id)

        embed, view = self._generate_balance_stats(user, data, Colors.primary)
        return embed, view, REPLY, NO_EXTRA if ctx.author != user else None

    @balance.define_app_command()
    @app_commands.describe(user='The user to view the balance of.')
    async def balance_app_command(self, ctx: HybridContext, user: discord.Member = None) -> None:
        await ctx.invoke(ctx.command, user=user)  # type: ignore

    async def _balance_context_menu_callback(self, interaction: TypedInteraction, user: discord.Member) -> None:
        await invoke_command(self.balance, interaction, args=(), kwargs={'user': user})

    @command(aliases={'lvl', 'lv', 'l', 'xp', 'exp'}, hybrid=True, with_app_command=False)
    @simple_cooldown(2, 5)
    async def level(self, ctx: Context, *, user: CaseInsensitiveMemberConverter | None = None) -> CommandResponse:
        """View your current level and experience, or optionally, someone elses."""
        user = user or ctx.author
        data = await ctx.db.get_user_record(user.id)

        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.set_author(name=f"Level: {user}", icon_url=user.avatar.url)

        level, exp, requirement = data.level_data
        extra = ''
        if multi := data.exp_multiplier_in_ctx(ctx) - 1:
            extra = f'-# XP Multiplier: **+{multi:.1%}**'

        embed.add_field(
            name=f"Level {level:,}",
            value=f'{exp:,}/{requirement:,} XP ({exp / requirement:.1%})\n{progress_bar(exp / requirement)}\n' + extra,
        )

        not_earned = ((milestone, reward) for milestone, reward in LEVEL_REWARDS.items() if level < milestone)
        next_reward = min(not_earned, default=(None, None), key=lambda x: x[0])
        if ctx.author == user and next_reward[0]:
            milestone, reward = next_reward
            embed.add_field(
                name=f'\U0001f3c5 Next Milestone: Level {milestone:,}',
                value=f'Upon reaching this level, you will be rewarded:\n{reward}',
                inline=False,
            )

        view = discord.ui.View(timeout=60)
        view.add_item(StaticCommandButton(
            command=ctx.bot.get_command('multiplier'),
            label='View Multipliers', style=discord.ButtonStyle.primary, emoji='\U0001f4c8',
        ))
        return embed, view, REPLY, NO_EXTRA

    @level.define_app_command()
    @app_commands.describe(user='The user to view the level of.')
    async def level_app_command(self, ctx: HybridContext, user: discord.Member = None) -> None:
        await ctx.invoke(ctx.command, user=user)  # type: ignore

    @staticmethod
    def _deconstruct(multipliers: Iterable[Multiplier]) -> tuple[str, float]:
        multipliers = list(multipliers)
        return '\n'.join(m.display for m in multipliers if m.multiplier), aggregate_multipliers(multipliers)

    @command(aliases={'mul', 'ml', 'mti', 'multi', 'multipliers'}, hybrid=True, with_app_command=False)
    @simple_cooldown(2, 5)
    async def multiplier(self, ctx: Context, *, user: CaseInsensitiveMemberConverter | None = None) -> CommandResponse:
        """View a detailed breakdown of all multipliers."""
        user = user or ctx.author
        data = await ctx.db.get_user_record(user.id)

        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.set_author(name=f"Multipliers: {user}", icon_url=user.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji('\U0001f4c8'))

        # XP Multi
        details, total = self._deconstruct(data.walk_exp_multipliers(ctx))
        embed.add_field(
            name=f"Total XP Multiplier: **{total - 1:.1%}**",
            value=details or 'No XP multipliers applied.',
            inline=False,
        )

        # Coin Multi
        details, total = self._deconstruct(data.walk_coin_multipliers(ctx))
        embed.add_field(
            name=f"Total Coin Multiplier: **{total - 1:.1%}**",
            value=details or 'No coin multipliers applied.',
            inline=False
        )

        # Bank space growth multi
        details, total = self._deconstruct(data.walk_bank_space_growth_multipliers())
        embed.add_field(
            name=f"Total Bank Space Growth Multiplier: **{total - 1:.1%}**",
            value=details or 'No bank space multipliers applied.',
            inline=False
        )

        return embed, REPLY

    @multiplier.define_app_command()
    @app_commands.describe(user='The user to view the multipliers of.')
    async def multiplier_app_command(self, ctx: HybridContext, user: discord.Member = None) -> None:
        await ctx.invoke(ctx.command, user=user)  # type: ignore

    @command(aliases={"rich", "lb", "top", "richest", "wealthiest"}, hybrid=True, with_app_command=False)
    @simple_cooldown(2, 5)
    async def leaderboard(
        self,
        ctx: Context,
        sort_by: LeaderboardSortByConverter | None = None,
        *,
        flags: LeaderboardFlags,
    ) -> CommandResponse:
        """View the richest people in terms of coins (or level) in your server.

        A few things to note:
        - In a server, this leaderboard defaults to *server only* unless `--global` is specified.
        - This leaderboard only shows *cached users*: if a user has not used the bot since the last startup, they will not be shown here.
        - This leaderboard shows the richest users by their *wallet* unless specified otherwise.

        Valid arguments for `sort_by`: `wallet` (default), `bank`, `total`, `level`, `votes`, or `dig`.

        Flags:
        - `--global`: Show the global leaderboard instead of the server leaderboard. If specified, this will only show the top 100 users.
          This is by default off in servers but on for direct messages ad user-installed apps.
        """
        sort_by = sort_by or 'wallet'
        if not flags.is_global and not ctx.guild:
            flags.is_global = True

        assert sort_by in ('wallet', 'bank', 'total_coins', 'total_exp', 'votes_this_month', 'deepest_dig')
        population = (
            ctx.db.user_records.values()
            if flags.is_global
            else (ctx.db.user_records[id] for id in ctx.guild._members if id in ctx.db.user_records)
        )
        records = sorted(
            (
                (record, ctx.guild and ctx.guild.get_member(record.user_id) or ctx.bot.get_user(record.user_id))
                for record in population if getattr(record, sort_by) > 0
            ),
            key=lambda r: getattr(r[0], sort_by),
            reverse=True,
        )
        if sort_by == 'votes_this_month':
            records = [
                (record, user) for record, user in records
                if record.last_dbl_vote and record.last_dbl_vote.month == ctx.now.month
            ]
        if flags.is_global:
            records = records[:100]  # TODO: perf improvements here

        if not records:
            message = "I don't see anyone in the cache with any coins"
            if not flags.is_global:
                message += " who is in this server"
            return message + '.'

        fmt = LeaderboardFormatter(records, per_page=10, is_global=flags.is_global, attr=sort_by)
        return Paginator(ctx, fmt, timeout=120), REPLY

    @leaderboard.define_app_command()
    @app_commands.rename(is_global='global')
    @app_commands.describe(
        sort_by='Sorts by this field (default: Wallet)',
        is_global='Whether to show the global leaderboard instead of the server leaderboard.',
    )
    @app_commands.choices(sort_by=[
        Choice(name='Wallet', value='wallet'),
        Choice(name='Bank', value='bank'),
        Choice(name='Total', value='total_coins'),
        Choice(name='Level/EXP', value='total_exp'),
        Choice(name='Votes this Month', value='votes_this_month'),
        Choice(name='Deepest Dig', value='deepest_dig'),
    ])
    async def leaderboard_app_command(
        self, ctx: HybridContext, sort_by: str = 'wallet', is_global: bool = False,
    ) -> None:
        flags = DottedDict(is_global=is_global)
        await ctx.invoke(ctx.command, sort_by=sort_by, flags=flags)  # type: ignore

    @command(aliases={"inv", "items"}, hybrid=True, with_app_command=False)
    @simple_cooldown(1, 6)
    async def inventory(self, ctx: Context, *, user: CaseInsensitiveMemberConverter | None = None):
        """View your inventory, or optionally, someone elses."""
        user = user or ctx.author
        record = await ctx.db.get_user_record(user.id)
        inventory = await record.inventory_manager.wait()

        view = InventoryView(ctx, user, inventory)
        view.container.recompute_entries()
        view.container.update()

        yield view, REPLY, NO_EXTRA
        await view.wait()

    @inventory.define_app_command()
    @app_commands.describe(user='The user to view the inventory of.')
    async def inventory_app_command(self, ctx: HybridContext, user: discord.Member = None):
        await ctx.invoke(ctx.command, user=user)  # type: ignore

    @command(aliases={"itembook", "uniqueitems", "discovered", "ib"}, hybrid=True, with_app_command=False)
    @simple_cooldown(2, 6)
    async def book(
        self,
        ctx: Context,
        rarity: Literal['common', 'uncommon', 'rare', 'epic', 'legendary', 'mythic', 'all'] | None = 'all',
        category: query_item_type = None,
    ):
        """View a summary of all unique items you have discovered (and what you are missing)."""
        record = await ctx.db.get_user_record(ctx.author.id)
        inventory = await record.inventory_manager.wait()
        quantity = inventory.cached.quantity_of

        rarity = rarity.lower()

        lines = [
            f'{item.get_display_name(bold=quantity(item) > 0)} ({item.rarity.name.title()}) x{quantity(item):,}'
            for item in Items.all()
            if rarity in ('all', item.rarity.name.lower())
            and (category is None or item.type is category)
        ]

        count = sum(quantity > 0 for quantity in inventory.cached.values())

        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.set_author(name=f'{ctx.author.name}\'s Item Book', icon_url=ctx.author.display_avatar)
        embed.description = f'You own **{count:,}** out of {len(list(Items.all())):,} unique items.'

        if rarity != 'all':
            count = sum(quantity > 0 for item, quantity in inventory.cached.items() if item.rarity.name.lower() == rarity)
            embed.description += f'\nYou have also discovered {count:,} out of {len(lines):,} **{rarity.lower()}** items.'

        return Paginator(ctx, LineBasedFormatter(embed, lines, field_name='\u200b'), timeout=120), REPLY

    @book.define_app_command()
    @app_commands.describe(
        rarity='Show only items of this rarity.',
        category='Show only items from this category.',
    )
    @app_commands.choices(category=[Choice(name=cat.name.title(), value=cat.name) for cat in list(ItemType)])
    async def book_app_command(
        self,
        ctx: HybridContext,
        rarity: Literal['Common', 'Uncommon', 'Rare', 'Epic', 'Legendary', 'Mythic'] = None,
        category: str = None,
    ):
        await ctx.invoke(ctx.command, rarity=(rarity or 'all').lower(), category=category and query_item_type(category))  # type: ignore

    @group(aliases={"notifs", "notification", "notif", "nt"}, hybrid=True, fallback='list')
    @simple_cooldown(1, 6)
    async def notifications(self, ctx: Context) -> tuple[str | Paginator, Any]:
        """View your notifications."""
        record = await ctx.db.get_user_record(ctx.author.id)
        notifications = await record.notifications_manager.wait()

        await record.update(unread_notifications=0)

        newline = '\n'
        fields = [{
            'name': (
                f'{idx}. {notification.data.emoji} **{notification.data.title}** \u2014 '
                f'{discord.utils.format_dt(notification.created_at, "R")}'
            ),
            'value': (
                f'{notification.data.describe(ctx.bot).split(newline)[1].removeprefix("-# ")}\n'
                f'-# Run `{ctx.clean_prefix}notifications view {idx}` for the changelog'
                if isinstance(notification.data, NotificationData.BotUpdate)
                else cutoff(notification.data.describe(ctx.bot).splitlines()[0], max_length=256)
            ),
            'inline': False,
        } for idx, notification in enumerate(notifications.cached, start=1)]

        if not len(fields):
            return 'You currently do not have any notifications.', REPLY

        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.description = (
            f'Run `{ctx.clean_prefix}notifications view <index>` to view a specific notification.\n'
            f'Likewise, run `{ctx.clean_prefix}notifications clear` to clear all notifications.'
        )
        embed.set_author(name=f'{ctx.author.name}\'s Notifications', icon_url=ctx.author.display_avatar)

        return Paginator(ctx, FieldBasedFormatter(embed, fields, per_page=5), timeout=120), REPLY

    @notifications.command(name='view', aliases={"v", "read", "info"}, hybrid=True)
    @app_commands.describe(index='The index of the notification to view.')
    @simple_cooldown(2, 3)
    async def notifs_view(self, ctx: Context, index: int) -> tuple[discord.Embed | str, Any]:
        """View information on a specific notification."""
        if index < -1:
            return 'Notification index must be positive.', BAD_ARGUMENT

        record = await ctx.db.get_user_record(ctx.author.id)
        notifications = await record.notifications_manager.wait()
        try:
            notification = notifications.cached[index - 1]
        except IndexError:
            return 'Invalid notification index.', BAD_ARGUMENT

        embed = discord.Embed(
            color=notification.data.color, description=notification.data.describe(ctx.bot), timestamp=ctx.now
        )
        embed.set_author(name=notification.data.title, icon_url=ctx.author.display_avatar)
        if isinstance(notification.data, NotificationData.BotUpdate):
            if notification.data.image_path:
                embed.set_image(
                    url=f'{NotificationsManager.CHANGELOG_IMAGES_BASE_URL}/{notification.data.image_path}?raw=true'
                )
        else:
            embed.set_thumbnail(url=image_url_from_emoji(notification.data.emoji))

        fmt = lambda f: discord.utils.format_dt(notification.created_at, f)
        embed.add_field(name='Created', value=f'{fmt("R")} ({fmt("f")})')
        return embed, REPLY

    @notifications.command(name='clear', aliases={"c", "wipe"}, hybrid=True)
    @simple_cooldown(1, 10)
    async def notifs_clear(self, ctx: Context) -> tuple[str, Any]:
        """Clear all of your notifications."""
        await ctx.db.execute('DELETE FROM notifications WHERE user_id = $1', ctx.author.id)

        record = await ctx.db.get_user_record(ctx.author.id)
        notifications = await record.notifications_manager.wait()
        notifications.cached.clear()

        return 'Cleared all of your notifications.', REPLY

    @command(aliases={'chart', 'coinhistory', 'coingraph', 'cg'})
    @simple_cooldown(2, 6)
    async def graph(self, ctx: Context, *, flags: GraphFlags) -> CommandResponse | None:
        """View a graph of your wallet over time.

        Flags:
        - `--total`: Graph your total coins instead of your wallet.
        - `--timespan <duration>`: How far back to look for data. Defaults to 15 minutes.

        Examples:
        - `{PREFIX}graph --total --timespan 1h`: Graph your total coins over the past hour.
        - `{PREFIX}graph --timespan 1d`: Graph your wallet over the past day.
        """
        if flags.duration > timedelta(days=14):
            return 'You may only graph up to 14 days of data.', BAD_ARGUMENT
        if flags.duration < timedelta(minutes=2):
            return 'You must graph at least 2 minutes of data.', BAD_ARGUMENT

        record = await ctx.db.get_user_record(ctx.author.id)

        threshold = ctx.now - flags.duration
        position = bisect_left(record.history, threshold, key=lambda entry: entry[0])
        history = record.history[position:]
        if not history:
            return 'No data to graph. Try specifying a larger timespan.', REPLY

        history.append((ctx.now, UserHistoryEntry(record.wallet, record.total_coins)))
        dates, values = zip(*history)
        wallet, total = zip(*values)
        values = total if flags.total else wallet  # This could be compressed into zip(*values)[flags.total]
        target = 'Total Coins' if flags.total else 'Coins in Wallet'

        with Image.new("RGB", (30, 30), (0, 0, 0)) as background:
            buffer = BytesIO()
            background.save(buffer, format="PNG")
            buffer.seek(0)

        color = discord.Color.from_rgb(255, 255, 255)
        await send_graph_to(
            ctx,
            buffer,
            dates,
            values,
            content=(
                f'**{target}** over the past {humanize_duration(flags.duration.total_seconds())}:\n'
                f'*Note, this is an experimental command.*'
            ),
            y_axis=target,
            color=color,
        )

    @command(aliases={'guildgraph', 'guildhistory', 'guildchart', 'gg'})
    @simple_cooldown(2, 6)
    async def guilds(self, ctx: Context, *, flags: GuildGraphFlags) -> CommandResponse | None:
        """View a graph of this bot's growth over time."""
        if flags.duration and flags.duration < timedelta(minutes=2):
            return 'You must graph at least 2 minutes of data.', BAD_ARGUMENT

        entries = await ctx.db.fetch(
            'SELECT guild_count, timestamp FROM guild_count_graph_data WHERE timestamp >= $1 ORDER BY timestamp',
            ctx.now - flags.duration if flags.duration else datetime.utcfromtimestamp(0),
        )
        if not entries:
            return 'No data to graph. Try specifying a larger timespan.', REPLY

        history = [(entry['timestamp'], entry['guild_count']) for entry in entries]
        history.append((ctx.now, current := len(ctx.bot.guilds)))

        dates, values = zip(*history)
        with Image.new("RGB", (30, 30), (0, 0, 0)) as background:
            buffer = BytesIO()
            background.save(buffer, format="PNG")
            buffer.seek(0)

        color = discord.Color.from_rgb(255, 255, 255)
        label = f'the past {humanize_duration(flags.duration.total_seconds())}' if flags.duration else 'time'
        await send_graph_to(
            ctx,
            buffer,
            dates,
            values,
            content=(
                f'**Guild Count** over {label}: (Currently in **{current:,} guilds**)\n'
                f'*Note, this is an experimental command.*'
            ),
            y_axis='Guild Count',
            color=color,
        )

    @Cog.listener('on_guild_join')
    @Cog.listener('on_guild_remove')
    async def update_guild_count(self, _) -> None:
        await self.bot.db.execute(
            'INSERT INTO guild_count_graph_data (guild_count) VALUES ($1)',
            len(self.bot.guilds),
        )


setup = Stats.simple_setup
