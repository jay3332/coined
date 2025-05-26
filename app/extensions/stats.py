from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timedelta
from io import BytesIO
from textwrap import dedent
from typing import Any, Iterable, Literal, TYPE_CHECKING

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext.commands import BadArgument
from discord.utils import format_dt
from PIL import Image

from app import Bot
from app.core import BAD_ARGUMENT, Cog, Context, HybridContext, NO_EXTRA, REPLY, command, group, simple_cooldown
from app.core.flags import Flags, flag, store_true
from app.data.items import ItemType, Items
from app.database import InventoryManager, Multiplier, UserHistoryEntry, UserRecord
from app.extensions.transactions import query_item_type
from app.util.common import converter, cutoff, humanize_duration, image_url_from_emoji, progress_bar
from app.util.converters import CaseInsensitiveMemberConverter, IntervalConverter
from app.util.graphs import send_graph_to
from app.util.pagination import FieldBasedFormatter, Formatter, LineBasedFormatter, Paginator
from app.util.structures import DottedDict
from app.util.views import ModalButton, StaticCommandButton, invoke_command
from config import Colors, Emojis, multiplier_guilds

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
            stat = (
                f'**Level {record.level:,}** \u2022 {record.exp:,} XP'
                if self.attr == 'total_exp'
                else f'{Emojis.coin} **{getattr(record, self.attr):,}**'
            )
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
        super().__init__(style=discord.ButtonStyle.secondary, label='Refresh')
        self.cog = cog
        self.user = user
        self.record = record
        self.color = color

    async def callback(self, interaction: TypedInteraction) -> None:
        embed, view = self.cog._generate_balance_stats(self.user, self.record, self.color)
        await interaction.response.edit_message(embed=embed, view=view)


class RefreshInventoryButton(discord.ui.Button):
    def __init__(self, ctx: Context, user: discord.User, inventory: InventoryManager, color: int) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label='Refresh', row=1)
        self.cog: Stats = ctx.cog  # type: ignore
        self.ctx = ctx
        self.user = user
        self.inventory = inventory
        self.color = color

    async def callback(self, interaction: TypedInteraction) -> Any:
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message(
                'You cannot refresh someone else\'s inventory view.', ephemeral=True,
            )
        paginator = self.cog._refresh_inventory_paginator(self.ctx, self.user, self.inventory, self.color)
        await paginator.start(edit=True, interaction=interaction)


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
        await ctx.invoke(ctx.command, user=user)

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

        view = discord.ui.View(timeout=60)
        view.add_item(StaticCommandButton(
            command=ctx.bot.get_command('multiplier'),
            label='View Multipliers', style=discord.ButtonStyle.primary, emoji='\U0001f4c8',
        ))
        return embed, view, REPLY, NO_EXTRA

    @level.define_app_command()
    @app_commands.describe(user='The user to view the level of.')
    async def level_app_command(self, ctx: HybridContext, user: discord.Member = None) -> None:
        await ctx.invoke(ctx.command, user=user)

    @staticmethod
    def _deconstruct(multipliers: Iterable[Multiplier]) -> tuple[str, float]:
        try:
            details, multipliers = zip(*(
                (multiplier.display, multiplier.multiplier)
                for multiplier in multipliers if multiplier.multiplier
            ))
        except ValueError:
            details, multipliers = (), ()

        return '\n'.join(details), sum(multipliers)

    @command(aliases={'mul', 'ml', 'mti', 'multi', 'multipliers'}, hybrid=True)
    @simple_cooldown(2, 5)
    async def multiplier(self, ctx: Context) -> CommandResponse:
        """View a detailed breakdown of all multipliers."""
        data = await ctx.db.get_user_record(ctx.author.id)

        embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.set_author(name=f"Multipliers: {ctx.author}", icon_url=ctx.author.display_avatar)
        embed.set_thumbnail(url=image_url_from_emoji('\U0001f4c8'))

        # XP Multi
        details, total = self._deconstruct(data.walk_exp_multipliers(ctx))
        embed.add_field(
            name=f"Total XP Multiplier: **{total:.1%}**",
            value=details or 'No XP multipliers applied.',
            inline=False,
        )

        # Coin Multi
        details, total = self._deconstruct(data.walk_coin_multipliers(ctx))
        embed.add_field(
            name=f"Total Coin Multiplier: **{total:.1%}**",
            value=details or 'No coin multipliers applied.',
            inline=False
        )

        # Bank space growth multi
        details, total = self._deconstruct(data.walk_bank_space_growth_multipliers())
        embed.add_field(
            name=f"Total Bank Space Growth Multiplier: **{total:.1%}**",
            value=details or 'No bank space multipliers applied.',
            inline=False
        )

        return embed, REPLY

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

        Valid arguments for `sort_by`: `wallet` (default), `bank`, `total`, or `level`.

        Flags:
        - `--global`: Show the global leaderboard instead of the server leaderboard. If specified, this will only show the top 100 users.
          This is by default off in servers but on for direct messages ad user-installed apps.
        """
        sort_by = sort_by or 'wallet'
        if not flags.is_global and not ctx.guild:
            flags.is_global = True

        assert sort_by in ('wallet', 'bank', 'total_coins', 'total_exp')
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
    ])
    async def leaderboard_app_command(
        self, ctx: HybridContext, sort_by: str = 'wallet', is_global: bool = False,
    ) -> None:
        flags = DottedDict(is_global=is_global)
        await ctx.invoke(ctx.command, sort_by=sort_by, flags=flags)  # type: ignore

    @staticmethod
    def _refresh_inventory_paginator(
        ctx: Context, user: discord.User, inventory: InventoryManager, color: int,
    ) -> Paginator:
        fields = [{
            'name': f'{item.display_name} — **{quantity:,}**',
            'value': f'Worth {Emojis.coin} **{item.price * quantity:,}**',
            'inline': False,
        } for item, quantity in inventory.cached.items() if quantity]

        worth = sum(item.price * quantity for item, quantity in inventory.cached.items())

        embed = discord.Embed(color=color, timestamp=ctx.now)
        owner = 'you' if user == ctx.author else 'they'
        embed.description = dedent(f"""
            {'Your' if user == ctx.author else f"{user.name}'s"} inventory is worth {Emojis.coin} **{worth:,}**.
            Additionally, {owner} own **{len(fields):,}** out of {len(list(Items.all())):,} unique items.
        """)
        embed.set_author(name=f'{user.name}\'s Inventory', icon_url=user.display_avatar)

        go_shopping = StaticCommandButton(
            command=ctx.bot.get_command('shop'),
            label='Go Shopping', style=discord.ButtonStyle.primary, emoji='\U0001f6d2', row=1,
        )
        refresh = RefreshInventoryButton(ctx, user, inventory, color)
        return Paginator(
            ctx,
            FieldBasedFormatter(embed, fields, per_page=5),
            other_components=[go_shopping, refresh],
            timeout=120,
        )

    @command(aliases={"inv", "items"}, hybrid=True, with_app_command=False)
    @simple_cooldown(1, 6)
    async def inventory(self, ctx: Context, *, user: CaseInsensitiveMemberConverter | None = None):
        """View your inventory, or optionally, someone elses."""
        user = user or ctx.author

        record = await ctx.db.get_user_record(user.id)
        inventory = await record.inventory_manager.wait()

        if all(not quantity for item, quantity in inventory.cached.items()):
            return f'{"You currently do" if user == ctx.author else f"{user.name} currently does"} not own any items.', REPLY

        paginator = self._refresh_inventory_paginator(ctx, user, inventory, Colors.primary)
        return paginator, REPLY, NO_EXTRA if ctx.author != user else None

    @inventory.define_app_command()
    @app_commands.describe(user='The user to view the inventory of.')
    async def inventory_app_command(self, ctx: HybridContext, user: discord.Member = None):
        await ctx.invoke(ctx.command, user=user)

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
        await ctx.invoke(ctx.command, rarity=(rarity or 'all').lower(), category=category and query_item_type(category))

    @group(aliases={"notifs", "notification", "notif", "nt"}, hybrid=True, fallback='list')
    @simple_cooldown(1, 6)
    async def notifications(self, ctx: Context) -> tuple[str | Paginator, Any]:
        """View your notifications."""
        record = await ctx.db.get_user_record(ctx.author.id)
        notifications = await record.notifications_manager.wait()

        await record.update(unread_notifications=0)

        fields = [{
            'name': (
                f'{idx}. {notification.data.emoji} **{notification.data.title}** \u2014 '
                f'{discord.utils.format_dt(notification.created_at, "R")}'
            ),
            'value': cutoff(notification.data.describe(ctx.bot)),
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
