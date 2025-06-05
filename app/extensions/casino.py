from __future__ import annotations

import asyncio
import functools
import random
from collections import defaultdict
from enum import Enum
from math import comb
from textwrap import dedent
from typing import Any, ClassVar, Final, Generic, Iterable, Iterator, NamedTuple, TYPE_CHECKING, TypeVar

import discord
from discord import app_commands
from discord.ext.commands import max_concurrency, BucketType
from discord.utils import format_dt

from app.core import (
    BAD_ARGUMENT, Cog, Context, EDIT, HybridContext, REPLY, command, lock_transactions, simple_cooldown,
    user_max_concurrency,
)
from app.core.flags import Flags, flag
from app.data.items import Items
from app.data.quests import QuestTemplates
from app.util.common import pluralize
from app.util.converters import CasinoBet
from app.util.structures import DottedDict
from app.util.types import CommandResponse, TypedInteraction
from app.util.views import FollowUpButton, UserView
from config import Colors, Emojis

if TYPE_CHECKING:
    from app.database import UserRecord

CardT = TypeVar('CardT', bound='Card')


class ScratchCell(Enum):
    empty = 0
    lose = 1
    coin = 2
    medal = 3
    trophy = 4
    crown = 5
    star = 6
    spinning_coin = 7


class ScratchCellInfo(NamedTuple):
    emoji: str | None
    chance: float
    max_occurences: int | None = None
    style: discord.ButtonStyle = discord.ButtonStyle.primary
    profit: float | None = None


class ScratchButton(discord.ui.Button['ScratchView']):
    def __init__(self, cell: ScratchCell, info: ScratchCellInfo, row: int):
        self.cell: ScratchCell = cell
        self.info: ScratchCellInfo = info

        super().__init__(style=discord.ButtonStyle.secondary, row=row, label='\u200b')

    async def callback(self, interaction: TypedInteraction) -> None:
        self.view.scratches -= 1

        match self.cell, self.info:
            case ScratchCell.empty, _:
                message = 'Empty cell.'
            case ScratchCell.lose, _:
                message = f'{self.info.emoji} You scratched a skull and you lose your bet.'
            case _, info:
                self.view.multiplier += info.profit
                message = f'Scratched {info.emoji} {Emojis.arrow} {Emojis.coin} **+{self.view.bet * info.profit:,.0f}** (+{info.profit:.0%})'
            case _:
                raise

        self.emoji = self.info.emoji
        self.style = self.info.style

        self.view.description.append(f'{5 - self.view.scratches}. {message}')
        self.view.update_embed(self.cell)

        if self.view.scratches <= 0 or self.cell is ScratchCell.lose:
            self.view.disable_items()

            if self.cell is ScratchCell.lose:
                self.view.multiplier = 0
                message = 'You scratched off a skull emoji.'
            else:
                message = 'You did not scratch off any profitable cells.'

            if not self.view.multiplier:
                self.view.embed.colour = Colors.error

                self.view.embed.clear_fields()
                self.view.embed.add_field(name='You got nothing!', value=message + f'\nYou now have {Emojis.coin} **{self.view.record.wallet:,}**.')
                await _handle_quests(self.view.ctx, self.view.record, bet=self.view.bet)
            else:
                self.view.embed.clear_fields()
                gain = self.view.bet * self.view.multiplier
                await self.view.record.add(wallet=gain)
                profit = gain - self.view.bet
                await _handle_quests(self.view.ctx, self.view.record, bet=self.view.bet, profit=profit)

                self.view.embed.colour = Colors.success if profit > 0 else Colors.warning

                self.view.embed.add_field(
                    name=f'You got {Emojis.coin} **{gain:,}**! ({self.view.multiplier:.0%})',
                    value=f'Profit: {Emojis.coin} **{profit:+,}**\nYou now have {Emojis.coin} **{self.view.record.wallet:,}**.'
                )

            self.view.stop()

        await interaction.response.edit_message(embed=self.view.embed, view=self.view)


class ScratchView(UserView):
    SCRATCH_CELLS: Final[ClassVar[dict[ScratchCell, ScratchCellInfo]]] = {
        ScratchCell.empty: ScratchCellInfo(emoji=None, chance=0.5),
        ScratchCell.lose: ScratchCellInfo(emoji='\U0001f480', chance=0, max_occurences=1, style=discord.ButtonStyle.red),  # add manually
        ScratchCell.coin: ScratchCellInfo(emoji='<:coin:896432147152400394>', chance=0.24, max_occurences=8, profit=0.3),
        ScratchCell.medal: ScratchCellInfo(emoji='\U0001f3c5', chance=0.1, max_occurences=4, profit=0.5),
        ScratchCell.trophy: ScratchCellInfo(emoji='\U0001f3c6', chance=0.04, max_occurences=3, profit=0.8),
        ScratchCell.crown: ScratchCellInfo(emoji='\U0001f451', chance=0.015, max_occurences=3, profit=1.2),
        ScratchCell.star: ScratchCellInfo(emoji='\U0001f320', chance=0.0045, max_occurences=2, profit=2),
        ScratchCell.spinning_coin: ScratchCellInfo(emoji='<a:spinning_coin:939937188836147240>', chance=0.001, max_occurences=1, profit=4),
    }

    if TYPE_CHECKING:
        cells: list[list[ScratchCell]]

    def __init__(self, ctx: Context, bet: int) -> None:
        super().__init__(ctx.author, timeout=120)
        self.fill_cells()
        self.update_buttons()
        self.ctx: Context = ctx
        self.record = ctx.db.get_user_record(ctx.author.id, fetch=False)

        self.scratches: int = 5
        self.multiplier: float = 0
        self.bet: int = bet
        self.description: list[str] = []

        self.embed = embed = discord.Embed(color=Colors.primary, timestamp=ctx.now)
        embed.description = f'Press a button to scratch it off! You have {self.scratches} scratches left.'
        embed.set_author(name=f'{ctx.author.name}: Scratch-off ticket', icon_url=ctx.author.display_avatar)

    def fill_cells(self):
        cells = []
        occurences = defaultdict(int)

        for _ in range(5):
            row = []
            for _ in range(3):
                cell = random.choices(*zip(*(
                    (key, info.chance) for key, info in self.SCRATCH_CELLS.items()
                    if info.max_occurences is None or occurences[key] < info.max_occurences
                )))[0]

                occurences[cell] += 1
                row.append(cell)

            cells.append(row)

        cells[random.randint(0, 4)][random.randint(0, 2)] = ScratchCell.lose
        self.cells = cells

    @discord.utils.cached_property
    def table_text(self) -> str:
        def emoji_of(cell: ScratchCell) -> str:
            return self.SCRATCH_CELLS[cell].emoji

        return dedent(f"""
            {emoji_of(ScratchCell.lose)} = Automatic loss
            {emoji_of(ScratchCell.coin)} = +30% of bet
            {emoji_of(ScratchCell.medal)} = +50% of bet
            {emoji_of(ScratchCell.trophy)} = +80% of bet
            {emoji_of(ScratchCell.crown)} = +120% of bet
            {emoji_of(ScratchCell.star)} = +200% (2x) of bet
            {emoji_of(ScratchCell.spinning_coin)} = +400% (4x) of bet
        """)

    def update_buttons(self) -> None:
        self.clear_items()

        for i, row in enumerate(self.cells):
            for cell in row:
                self.add_item(ScratchButton(cell, self.SCRATCH_CELLS[cell], i))

        self.add_item(FollowUpButton(self.table_text, label='View Scratch Key', style=discord.ButtonStyle.green, row=4))

    def update_embed(self, cell: ScratchCell | None = None) -> None:
        self.embed.description = '\n'.join(self.description)
        if self.scratches and (cell is None or cell is not ScratchCell.lose):
            self.embed.description += pluralize(f'\nYou have {self.scratches} scratch(es) left.')

        if self.multiplier:
            self.embed.remove_field(0)
            self.embed.add_field(name='Return', value=f'{Emojis.coin} {self.bet * self.multiplier:,.0f} ({self.multiplier:.0%})')

    def disable_items(self) -> None:
        for child in self.children:
            if not isinstance(child, ScratchButton):
                continue

            if not child.disabled:
                child.disabled = True
                child.emoji = child.info.emoji


class CardSuit(Enum):
    spades   = 0
    hearts   = 1
    diamonds = 2
    clubs    = 3

    def __str__(self) -> str:
        return self.name.title()

    def __repr__(self) -> str:
        return f'<CardSuit.{self.name}>'

    @property
    def emoji(self) -> str:
        return {
            CardSuit.spades: '\u2660',
            CardSuit.hearts: '\u2665',
            CardSuit.diamonds: '\u2666',
            CardSuit.clubs: '\u2663',
        }[self]


class CardRank(Enum):
    ace   = 1
    two   = 2
    three = 3
    four  = 4
    five  = 5
    six   = 6
    seven = 7
    eight = 8
    nine  = 9
    ten   = 10
    jack  = 11
    queen = 12
    king  = 13

    def __str__(self) -> str:
        match self:
            case CardRank.ace:
                return 'A'
            case CardRank.jack:
                return 'J'
            case CardRank.queen:
                return 'Q'
            case CardRank.king:
                return 'K'
            case _:
                return str(self.value)

    def __repr__(self) -> str:
        return f'<CardRank.{self.name}>'

    def __lt__(self, other: CardRank) -> bool:
        return self.value < other.value

    @property
    def poker_rank(self) -> int:
        return self.value if self is not CardRank.ace else 14

    @property
    def rank_value(self) -> int:
        match self:
            case CardRank.jack | CardRank.queen | CardRank.king:
                return 10
            case _:
                return self.value


class Card(NamedTuple):
    suit: CardSuit
    rank: CardRank

    def __str__(self) -> str:
        return f'{self.rank} of {self.suit}'

    def __repr__(self) -> str:
        return f'<Card: {self}>'

    @property
    def display(self) -> str:
        return f'{self.suit.emoji} {self.rank}'


class Deck(Generic[CardT]):
    def __init__(self, count: int = 1, *, cls: type[CardT] = Card) -> None:
        self.cards: list[CardT] = []
        self.reset(count, cls=cls)

    def reset(self, count: int = 1, *, cls: type[CardT] = Card) -> None:
        self.cards.clear()
        for _ in range(count):
            self.cards.extend(self.generate(cls=cls))

    def shuffle(self) -> None:
        random.shuffle(self.cards)

    @staticmethod
    def generate(*, cls: type[CardT] = Card) -> Iterable[CardT]:
        for suit in CardSuit:
            for rank in CardRank:
                yield cls(suit, rank)

    def draw(self) -> CardT:
        return self.cards.pop()

    def draw_many(self, n: int) -> list[CardT]:
        return [self.draw() for _ in range(n)]

    def __len__(self) -> int:
        return len(self.cards)

    def __iter__(self) -> Iterator[CardT]:
        return iter(self.cards)

    def __repr__(self) -> str:
        return f'<Deck of {len(self.cards)}>'


class BlackjackCard(Card):
    @property
    def value(self) -> int:
        return self.rank.rank_value

    def get_favorable_value(self, current: int) -> int:
        """In blackjack, aces can be either 1 or 11 depending on the current total."""
        if self.rank is CardRank.ace:
            return 1 if current + 11 > 21 else 11
        return self.value


class BlackjackHand(NamedTuple):
    cards: list[BlackjackCard]

    @property
    def total(self) -> int:
        return functools.reduce(lambda total, card: total + card.get_favorable_value(total), self.cards, 0)

    @property
    def natural(self) -> bool:
        return self.total == 21 and len(self.cards) == 2

    def __repr__(self) -> str:
        return f'<BlackjackHand worth {self.total}: {self.cards}>'

    @property
    def full_display(self) -> str:
        """Displays the hand with all cards visible."""
        return ' '.join(f'`{card.display}`' for card in self.cards)

    @property
    def hidden_display(self) -> str:
        """Displays the hand with only the first card visible."""
        return ' '.join(f'`{card.display}`' if i == 0 else '`?`' for i, card in enumerate(self.cards))


class Blackjack(UserView):
    """Implements a game of Blackjack."""

    def __init__(self, ctx: Context, *, bet: int, record: UserRecord) -> None:
        super().__init__(ctx.author, timeout=60)

        self.ctx = ctx
        self.record = record
        self.bet = bet
        self._embed = discord.Embed(color=Colors.secondary, timestamp=ctx.now).set_author(
            name=f'{ctx.author}: Blackjack Game', icon_url=ctx.author.avatar,
        )

        # setup blackjack
        self.deck: Deck[BlackjackCard] = Deck(2, cls=BlackjackCard)  # blackjack is usually played with 1-8 decks
        self.deck.shuffle()
        # draw cards
        self.player: BlackjackHand = BlackjackHand(self.deck.draw_many(2))
        self.dealer: BlackjackHand = BlackjackHand(self.deck.draw_many(2))
        self.doubled_down: bool = False

        if record.wallet < bet * 2:
            self.double_down.disabled = True

    def make_embed(self) -> discord.Embed:
        embed = self._embed.copy()
        embed.clear_fields()

        embed.description = f'**Bet:** {Emojis.coin} {self.bet:,}'
        if self.doubled_down:
            embed.description += '\n**Doubled down:** Your bet has been doubled.'

        embed.add_field(name=f'{self.ctx.author}: **{self.player.total}**', value=self.player.full_display)
        if self.is_finished():
            embed.add_field(name=f'Dealer: **{self.dealer.total}**', value=self.dealer.full_display)
            embed.set_footer(text=f'Next card: {self.deck.cards[-1].display if self.deck.cards else "None"}')
        else:
            embed.add_field(name=f'Dealer', value=self.dealer.hidden_display)

        return embed

    async def handle_lose(self, message: str) -> discord.Embed:
        self.stop()
        embed = self.make_embed()
        embed.colour = Colors.error
        await self.record.add(wallet=-self.bet)
        await _handle_quests(self.ctx, self.record, bet=self.bet)

        embed.add_field(
            name=f"**{message}**",
            value=dedent(f'''
                You lost {Emojis.coin} **{self.bet:,} coins**.
                You now have {Emojis.coin} **{self.record.wallet:,}**.
            '''),
            inline=False,
        )
        return embed

    async def lose(self, interaction: TypedInteraction, message: str) -> None:
        embed = await self.handle_lose(message)
        await interaction.response.edit_message(embed=embed, view=None)

    async def handle_win(self, message: str) -> discord.Embed:
        self.stop()
        multiplier = random.uniform(0.7, 1.0)
        adjustment, adjusted_text = Casino.adjust_multiplier(self.record, modification=0.6)
        multiplier += adjustment
        profit = round(self.bet * multiplier)
        await self.record.add(wallet=profit)
        await _handle_quests(self.ctx, self.record, bet=self.bet, profit=profit)

        embed = self.make_embed()
        embed.colour = Colors.success
        embed.add_field(
            name=f"**{message}**",
            value=dedent(f"""
                You won {Emojis.coin} **{profit:,} coins**.
                Multiplier: **{multiplier:.1%}**{adjusted_text}
                You now have {Emojis.coin} **{self.record.wallet:,}**.
            """),
            inline=False,
        )
        return embed

    async def win(self, interaction: TypedInteraction, message: str) -> None:
        embed = await self.handle_win(message)
        await interaction.response.edit_message(embed=embed, view=None)

    async def handle_tie(self, message: str) -> discord.Embed:
        self.stop()
        embed = self.make_embed()
        embed.colour = Colors.warning
        embed.add_field(
            name=f"**{message}**",
            value=dedent(f"""
                You neither won nor lost any coins.
                You still have {Emojis.coin} **{self.record.wallet:,}**.
            """),
            inline=False,
        )
        await _handle_quests(self.ctx, self.record, bet=self.bet)
        return embed

    async def tie(self, interaction: TypedInteraction, message: str) -> None:
        embed = await self.handle_tie(message)
        await interaction.response.edit_message(embed=embed, view=None)

    async def handle_hit(self, interaction: TypedInteraction) -> None:
        self.player.cards.append(self.deck.draw())
        if self.player.total > 21:
            return await self.lose(interaction, 'Bust! You went over 21!')

        # Automatically stand if the player has 21
        if self.player.total == 21:
            await self.handle_stand(interaction)

    @discord.ui.button(label='Hit', style=discord.ButtonStyle.success)
    async def hit(self, interaction: TypedInteraction, _: discord.ui.Button) -> None:
        await self.handle_hit(interaction)
        if self.is_finished():
            return
        await interaction.response.edit_message(embed=self.make_embed())

    async def handle_stand(self, interaction: TypedInteraction) -> None:
        while self.dealer.total < 17:
            self.dealer.cards.append(self.deck.draw())

        if self.dealer.total > 21:
            return await self.win(interaction, 'Dealer bust!')

        if self.player.total > self.dealer.total:
            return await self.win(interaction, 'You beat the dealer!')
        elif self.player.total == self.dealer.total:
            return await self.tie(interaction, 'You tied with the dealer!')

        return await self.lose(interaction, 'Dealer beat you!')

    @discord.ui.button(label='Stand', style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: TypedInteraction, _: discord.ui.Button) -> None:
        await self.handle_stand(interaction)

    @discord.ui.button(label='Double Down', style=discord.ButtonStyle.primary)
    async def double_down(self, interaction: TypedInteraction, button: discord.ui.Button) -> None:
        self.bet *= 2
        self.doubled_down = True
        button.disabled = True

        await self.handle_hit(interaction)
        if self.is_finished():
            return

        await self.handle_stand(interaction)

    @discord.ui.button(label='Surrender', style=discord.ButtonStyle.danger)
    async def surrender(self, interaction: TypedInteraction, _: discord.ui.Button) -> None:
        await self.lose(interaction, 'You surrendered!')


class SlotsCell(Enum):
    coinhead = 0
    spinning_coin = 1
    seven = 2
    bell = 3
    diamond = 4
    clover = 5
    cherry = 6
    watermelon = 7
    grape = 8

    @property
    def emoji(self) -> str:
        return SLOTS_EMOJI_MAPPING[self]


SLOTS_EMOJI_MAPPING: Final[dict[SlotsCell, str]] = {
    SlotsCell.coinhead: Items.coinhead.emoji,
    SlotsCell.spinning_coin: Items.spinning_coin.emoji,
    SlotsCell.seven: '<:slots_7:1379667368174948402>',
    SlotsCell.bell: '<:slots_bell:1379667379918995536>',
    SlotsCell.diamond: '<:slots_diamond:1379667353217798214>',
    SlotsCell.clover: '<:slots_clover:1379667348499464252>',
    SlotsCell.cherry: '<:slots_cherry:1379667390291247125>',
    SlotsCell.watermelon: '<:slots_watermelon:1379667387464417363>',
    SlotsCell.grape: '<:slots_grape:1379667377658007614>',
}

SLOTS_SPINNING_EMOJIS: Final[list[str]] = [
    '<a:slots_spinning_1:1379667361551876158>',
    '<a:slots_spinning_2:1379667373937660088>',
    '<a:slots_spinning_3:1379667356594475188>',
    '<a:slots_spinning_4:1379667359077236747>',
    '<a:slots_spinning_5:1379667382028603473>',
    '<a:slots_spinning_6:1379667351019982909>',
    '<a:slots_spinning_7:1379667385392304229>',
    '<a:slots_spinning_8:1379667370980806677>',
]

SLOTS_TRIPLE_MULTIPLIERS: Final[dict[SlotsCell, int | float]] = {
    SlotsCell.coinhead: 10,
    SlotsCell.spinning_coin: 8,
    SlotsCell.seven: 7,
    SlotsCell.bell: 6,
    SlotsCell.diamond: 5,
    SlotsCell.clover: 4,
    SlotsCell.cherry: 3,
    SlotsCell.watermelon: 2,
    SlotsCell.grape: 2,
}

SLOTS_DOUBLE_MULTIPLIERS: Final[dict[SlotsCell, int | float]] = {
    SlotsCell.coinhead: 1.8,
    SlotsCell.spinning_coin: 1.5,
    SlotsCell.seven: 1.2,
    SlotsCell.bell: 1,
    SlotsCell.diamond: 1,
    SlotsCell.clover: 0.8,
    SlotsCell.cherry: 0.8,
    SlotsCell.watermelon: 0.6,
    SlotsCell.grape: 0.6,
}


class MinesGemButton(discord.ui.Button['MinesView']):
    def __init__(self, *, row: int) -> None:
        super().__init__(label='\u200b', row=row)

    def reveal(self) -> None:
        self.style = discord.ButtonStyle.success
        self.label = '\U0001f48e'  # Gem
        self.disabled = True

    async def callback(self, interaction: TypedInteraction) -> None:
        self.view.gems += 1
        self.reveal()
        self.view.cash_out.disabled = False

        if self.view.gems >= self.view.max_gems:
            await self.view.cash_out.callback(interaction)
        else:
            embed = self.view.base_embed
            embed.add_field(name='Return', value=self.view.return_expansion, inline=False)
            await interaction.response.edit_message(embed=embed, view=self.view)


class MinesSkullButton(discord.ui.Button['MinesView']):
    def __init__(self, *, row: int) -> None:
        super().__init__(label='\u200b', row=row)

    def reveal(self) -> None:
        self.style = discord.ButtonStyle.danger
        self.label = '\U0001f480'  # Skull
        self.disabled = True

    async def callback(self, interaction: TypedInteraction) -> None:
        self.view.lost = True
        self.view.reveal_all()

        embed = self.view.base_embed
        embed.description = ''
        embed.add_field(name='You hit a mine!', inline=False, value=(
            f'You lost {Emojis.coin} **{self.view.bet:,}**.\n'
            f'{Emojis.Expansion.single} You now have {Emojis.coin} **{self.view.record.wallet:,}**.'
        ))
        embed.colour = Colors.error

        await interaction.response.edit_message(embed=embed, view=self.view)
        await _handle_quests(self.view.ctx, self.view.record, bet=self.view.bet)
        self.view.stop()


class MinesView(UserView):
    """Implements a game of mines and gems."""

    HOUSE_EDGE = 0.02

    def __init__(self, ctx: Context, record: UserRecord, *, bet: int, size: int = 4, mines: int = 1) -> None:
        super().__init__(ctx.author, timeout=300)
        self.ctx: Context = ctx
        self.record: UserRecord = record
        self.bet: int = bet
        self.gems: int = 0
        self.mines: int = mines
        self.size: int = size
        self.lost: bool = False

        self.remove_item(self.cash_out)
        locations = random.sample(range(self.cells), mines)
        for idx in range(self.cells):
            row = idx // self.size
            self.add_item(MinesSkullButton(row=row) if idx in locations else MinesGemButton(row=row))

        self.cash_out.row = self.size
        self.cash_out.disabled = True
        self.add_item(self.cash_out)

    def reveal_all(self) -> None:
        for child in self.children:
            if isinstance(child, (MinesGemButton, MinesSkullButton)):
                child.reveal()
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(label='Cash Out', emoji='\U0001f4b8', style=discord.ButtonStyle.primary)
    async def cash_out(self, interaction: TypedInteraction, _button: discord.ui.Button['MinesView']) -> None:
        self.reveal_all()

        if self.lost:
            raise RuntimeError('should never have gotten here')

        await self.record.add(wallet=round(self.bet * self.multiplier))
        await _handle_quests(self.ctx, self.record, bet=self.bet, profit=self.prospective_profit)

        expansion = (
            self.return_expansion.replace(Emojis.Expansion.single, Emojis.Expansion.first)
            + f'\n{Emojis.Expansion.last} You now have {Emojis.coin} **{self.record.wallet:,}**.'
        )
        embed = self.base_embed.add_field(name='Profit', value=expansion, inline=False)
        embed.description = ''
        embed.colour = Colors.success
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()


    _GAME_INSTRUCTIONS = (
        'Click on cells to collect gems. If you hit a mine (denoted with a \U0001f480), you will lose your bet.'
    )

    @property
    def base_embed(self) -> discord.Embed:
        embed = discord.Embed(color=Colors.secondary, description=self._GAME_INSTRUCTIONS, timestamp=self.ctx.now)
        embed.add_field(name='Bet', value=f'{Emojis.coin} **{self.bet:,}**')
        s = 's' if self.mines != 1 else ''
        embed.add_field(
            name='Configuration',
            value=f'{self.size}x{self.size} grid, {self.mines} mine{s}'
        )

        embed.set_author(name=f'{self.ctx.author.name}: Mines', icon_url=self.ctx.author.display_avatar)
        return embed

    @property
    def prospective_profit(self) -> int:
        """Calculates the prospective profit based on the current number of gems."""
        return round(self.bet * (self.multiplier - 1))

    @property
    def return_expansion(self) -> str:
        profit_multiplier = self.multiplier - 1
        return (
            f'\U0001f48e {self.gems} {Emojis.arrow} **{self.multiplier:.02f}x** (+{profit_multiplier:.02%})\n'
            f'{Emojis.Expansion.single} {Emojis.coin} **+{self.prospective_profit:,}**'
        )

    @property
    def max_gems(self) -> int:
        return self.cells - self.mines

    @property
    def cells(self) -> int:
        return self.size * self.size

    @property
    def multiplier(self) -> float:
        edge = 1 - self.HOUSE_EDGE if self.cells else 1
        return edge * comb(self.cells, self.gems) / comb(self.max_gems, self.gems)


class MinesFlags(Flags):
    size: int = flag(short='s', default=4)
    mines: int = flag(short='m', default=1)


class PokerFlags(Flags):
    small_blind: int = flag(short='s', aliases=('sb', 'small-blind'), converter=CasinoBet(5, 5000))
    big_blind: int = flag(short='b', aliases=('bb', 'big-blind'), converter=CasinoBet(10, 10000))


async def _handle_quests(ctx: Context, record: UserRecord, *, bet: int, profit: int = 0, connection=None) -> None:
    quests = await record.quest_manager.wait()

    if quest := quests.get_active_quest(QuestTemplates.gamble_coins):
        await quest.add_progress(bet, connection=connection)
    if profit > 0:
        if quest := quests.get_active_quest(QuestTemplates.gamble_wins):
            await quest.add_progress(1, connection=connection)
        if quest := quests.get_active_quest(QuestTemplates.gamble_wins_specific_command):
            if ctx.command.qualified_name == quest.quest.extra:
                await quest.add_progress(1, connection=connection)

        if quest := quests.get_active_quest(QuestTemplates.gamble_profit):
            await quest.add_progress(profit, connection=connection)
        if quest := quests.get_active_quest(QuestTemplates.gamble_profit_specific_command):
            if ctx.command.qualified_name == quest.quest.extra:
                await quest.add_progress(profit, connection=connection)


class Casino(Cog):
    """Gamble off all of your coins at the casino!"""

    emoji = '\U0001f911'

    @staticmethod
    def _format_roll(roll: tuple[int, int] | list[int]) -> str:
        assert len(roll) == 2

        first, second = roll
        return f"{Emojis.dice[first]} {Emojis.dice[second]}"

    @staticmethod
    def adjust_multiplier(record: UserRecord, *, modification: float = 1.0) -> tuple[float, str]:
        expansion = Emojis.Expansion
        if expiry := record.alcohol_expiry:
            multiplier = 0.25 * modification
            return (
                multiplier,
                f'\n{expansion.first} applied +{multiplier:.0%} multiplier from {Items.alcohol.emoji} Alcohol'
                f'\n{expansion.last} expires {format_dt(expiry, "R")}'
            )
        return 0, ''

    @command(aliases={'diceroll', 'r', 'bet', 'gamble'}, hybrid=True)
    @app_commands.describe(
        bet='The amount of coins to bet. Must be between 200 and 500,000. Use "max" to bet as many coins as possible.',
    )
    @simple_cooldown(1, 25)
    @user_max_concurrency(1)
    @lock_transactions
    async def roll(self, ctx: Context, *, bet: CasinoBet()) -> Any:
        """You and I each will roll a pair of dice. The one with the higher sum wins!"""
        record = await ctx.db.get_user_record(ctx.author.id)

        yield f'{Emojis.loading} Rolling...', REPLY
        await asyncio.sleep(random.uniform(2, 4))

        async with ctx.db.acquire() as conn:
            await record.add_random_exp(10, 15, chance=0.5, ctx=ctx, connection=conn)
            await record.add_random_bank_space(10, 15, chance=0.5, connection=conn)

        their_dice = random.choices(range(1, 7), k=2)
        my_dice = random.randint(2, 6), random.randint(1, 6)

        their_sum = sum(their_dice)
        my_sum = sum(my_dice)

        embed = discord.Embed(timestamp=ctx.now)
        embed.add_field(name=f'Your Roll ({their_sum})', value=self._format_roll(their_dice), inline=True)
        embed.add_field(name=f'My Roll ({my_sum})', value=self._format_roll(my_dice), inline=True)

        if their_sum > my_sum:
            multiplier = random.uniform(0.55, 0.95)
            adjustment, adjusted_text = self.adjust_multiplier(record)
            multiplier += adjustment

            profit = round(bet * multiplier)
            await record.add(wallet=profit)
            await _handle_quests(ctx, record, bet=bet, profit=profit)

            embed.colour = Colors.success
            embed.set_author(name='Winner!', icon_url=ctx.author.display_avatar)

            embed.add_field(name=f'You won {Emojis.coin} **{profit:,}**!', inline=False, value=dedent(f"""
                Multiplier: **{multiplier:.1%}**{adjusted_text}
                You now have {Emojis.coin} **{record.wallet:,}**.
            """))

        elif their_sum == my_sum:
            embed.colour = Colors.warning
            embed.set_author(name='Tie!', icon_url=ctx.author.display_avatar)
            embed.add_field(name='**We tied!**', value='You get absolutely nothing, try again next time.', inline=False)

        else:
            await record.add(wallet=-bet)
            await _handle_quests(ctx, record, bet=bet)

            embed.colour = Colors.error
            embed.set_author(name='Loser!', icon_url=ctx.author.display_avatar)

            embed.add_field(name='**You lost!**', inline=False, value=dedent(f"""
                You lost {Emojis.coin} **{bet:,}**.
                You now have {Emojis.coin} **{record.wallet:,}**.
            """))

            embed.set_footer(text='Better luck next time!')

        yield '', embed, EDIT

    @command('slots', aliases={'slot', 'sl'}, hybrid=True)
    @app_commands.describe(
        bet='The amount of coins to bet. Must be between 200 and 500,000. Use "max" to bet as many coins as possible.',
    )
    @simple_cooldown(1, 30)
    @user_max_concurrency(1)
    async def slots(self, ctx: Context, *, bet: CasinoBet()) -> CommandResponse:
        """Spin the slot machine and earn coins for matching symbols!"""
        record = await ctx.db.get_user_record(ctx.author.id)
        await record.add(wallet=-bet)

        embed = discord.Embed(color=Colors.warning, timestamp=ctx.now)
        embed.set_author(name=f'{ctx.author.display_name}\'s Slot Machine', icon_url=ctx.author.display_avatar)
        embed.description = f'**\xbb** {" ".join(random.sample(SLOTS_SPINNING_EMOJIS, k=3))} **\xab**'
        yield embed, REPLY

        slots = [first, second, third] = random.choices(list(SlotsCell), k=3)
        if first is second is third:
            match first:
                case SlotsCell.coinhead: field = '**MEGA MEGA JACKPOT!!!**'
                case SlotsCell.spinning_coin: field = '**MEGA JACKPOT!!**'
                case SlotsCell.seven: field = '**JACKPOT!**'
                case _: field = '**Three in a row!**'

            multiplier = SLOTS_TRIPLE_MULTIPLIERS[first]
        elif first is second or first is third:
            field = 'Two in a row!'
            multiplier = SLOTS_DOUBLE_MULTIPLIERS[first]
        elif second is third:
            field = 'Two in a row!'
            multiplier = SLOTS_DOUBLE_MULTIPLIERS[second]
        else:
            field = 'Loser!'
            multiplier = 0

        embed.description = f'**\xbb** {" ".join(s.emoji for s in slots)} **\xab**'
        embed.colour = Colors.success if multiplier else Colors.error

        profit = 0
        if multiplier:
            adjustment, adjusted_text = self.adjust_multiplier(record)
            multiplier += adjustment
            profit = round(bet * multiplier)
            await record.add(wallet=profit + bet)

            embed.add_field(name=field, value=dedent(f"""
                You won {Emojis.coin} **{profit:,}**!
                Multiplier: **{multiplier:.1%}**{adjusted_text}
                You now have {Emojis.coin} **{record.wallet:,}**.
            """))
        else:
            embed.add_field(
                name=field,
                value=f'You lost {Emojis.coin} **{bet:,}**.\nYou now have {Emojis.coin} **{record.wallet:,}**.',
            )

        await _handle_quests(ctx, record, bet=bet, profit=profit)
        await asyncio.sleep(random.uniform(2.5, 4.0))
        yield embed, EDIT

    @command(aliases={'scratchticket', 'scratch-ticket', 'scratchoff', 'scr'}, hybrid=True)
    @app_commands.describe(
        bet='The amount of coins to bet. Must be between 500 and 500,000. Use "max" to bet as many coins as possible.',
    )
    @simple_cooldown(1, 25)
    @user_max_concurrency(1)
    async def scratch(self, ctx: Context, *, bet: CasinoBet(500)) -> Any:
        """Scratch a scratch-off ticket and hope for profit."""
        record = await ctx.db.get_user_record(ctx.author.id)
        await record.add(wallet=-bet)

        view = ScratchView(ctx, bet)
        yield view.embed, view, REPLY
        await view.wait()

    @command(aliases={'bj', '21'}, hybrid=True)
    @app_commands.describe(
        bet='The amount of coins to bet. Must be between 500 and 500,000. Use "max" to bet as many coins as possible.',
    )
    @simple_cooldown(1, 25)
    @user_max_concurrency(1)
    @lock_transactions
    async def blackjack(self, ctx: Context, *, bet: CasinoBet(500)) -> Any:
        """Bet your coins in a game of Blackjack."""
        record = await ctx.db.get_user_record(ctx.author.id)

        game = Blackjack(ctx, bet=bet, record=record)
        # Check for blackjack
        if game.player.natural:
            if game.dealer.natural:
                yield await game.handle_tie('Standoff'), REPLY
            else:
                yield await game.handle_win('Blackjack!'), REPLY
            return
        elif game.dealer.natural:
            yield await game.handle_lose('Dealer got blackjack!'), REPLY
            return

        yield game.make_embed(), game, REPLY
        await game.wait()

    @command(aliases={'skulls', 'mn'}, hybrid=True, with_app_command=False)
    @simple_cooldown(1, 25)
    @user_max_concurrency(1)
    async def mines(self, ctx: Context, bet: CasinoBet(100), *, flags: MinesFlags) -> Any:
        """Win big by collecting as many gems as possible without hitting any mines.

        By default, you will receive a 4x4 grid with one mine where you can win up to a ~15x multiplier, however
        you can use flags (documented below) to alter the size of the grid and amount of mines planted.

        Flags:
        - ``--size <2|3|4>``: The size of the grid (you will get a ``size x size`` grid). Defaults to `4` (as in ``4x4``)
        - ``--mines <N>``: Indicates that N mines will be planted. N must be less than ``size * size``.
        """
        if not 2 <= flags.size <= 4:
            yield 'Grid size must be either 2, 3, or 4', BAD_ARGUMENT
            return

        if flags.mines < 1:
            yield 'Mines must be greater than or equal to 1', BAD_ARGUMENT
            return

        if flags.mines >= flags.size * flags.size:
            yield (
                f'For a {flags.size}x{flags.size} grid, there must be less than {flags.size * flags.size} mines.',
                BAD_ARGUMENT,
            )
            return

        record = await ctx.db.get_user_record(ctx.author.id)
        await record.add(wallet=-bet)

        game = MinesView(ctx, record, bet=bet, size=flags.size, mines=flags.mines)
        yield game.base_embed, game, REPLY
        await game.wait()

    @mines.define_app_command()
    @app_commands.describe(
        bet='The amount of coins to bet. Must be between 100 and 500,000. Use "max" to bet as many coins as possible.',
        size='The size of the grid (you will get a NxN grid). Defaults to 4.',
        mines='The number of mines planted. Must be less than (size * size). Defaults to 1.',
    )
    async def mines_app_command(self, ctx: HybridContext, bet: str, size: int = 4, mines: int = 1) -> Any:
        flags: Any = DottedDict(size=size, mines=mines)
        bet = await CasinoBet(100)().convert(ctx, bet)
        await ctx.invoke(ctx.command, bet=bet, flags=flags)

    @command(aliases={'holdem', 'hold\'em', 'pok'}, hybrid=True, with_app_command=False)
    @simple_cooldown(1, 25)
    @max_concurrency(1, BucketType.channel)
    async def poker(self, ctx: Context, buy_in: CasinoBet(500), *, flags: PokerFlags) -> CommandResponse:
        """Play a game of Texas Hold'em Poker with your friends."""
        from app.features.poker import Poker

        if flags.small_blind:
            if not flags.big_blind:
                s, b = flags.small_blind, flags.small_blind * 2
            elif flags.small_blind >= flags.big_blind:
                yield 'The small blind must be less than the big blind.', BAD_ARGUMENT
                return
            else:
                s, b = flags.small_blind, flags.big_blind
        elif flags.big_blind:
            s, b = flags.big_blind // 2, flags.big_blind
        else:
            s, b = buy_in // 100, buy_in // 50

        record = await ctx.db.get_user_record(ctx.author.id)
        await record.add(wallet=-buy_in)
        game = Poker(ctx, buy_in=buy_in, small_blind=s, big_blind=b)
        yield game.pregame_embed, game, REPLY
        await game.wait()

    @poker.define_app_command()
    @app_commands.describe(
        buy_in='The amount of coins the players should start with. Must be between 500 and 500,000.',
        small_blind='The amount of coins the small blind should be. Must be between 10 and 5,000',
        big_blind='The amount of coins the big blind should be. Must be between 20 and 10,000',
    )
    async def poker_app_command(
        self,
        ctx: HybridContext,
        buy_in: str,
        small_blind: str | None = None,
        big_blind: str | None = None,
    ) -> Any:
        buy_in = await CasinoBet(500)().convert(ctx, buy_in)
        small_blind = await CasinoBet(5, 5000)().convert(ctx, small_blind) if small_blind else None
        big_blind = await CasinoBet(10, 10000)().convert(ctx, big_blind) if big_blind else None
        flags: Any = DottedDict(small_blind=small_blind, big_blind=big_blind)
        await ctx.invoke(ctx.command, buy_in=buy_in, flags=flags)


setup = Casino.simple_setup
