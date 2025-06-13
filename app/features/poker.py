from __future__ import annotations

from enum import Enum
from typing import Any, NamedTuple

import discord

from app.core import Context
from app.extensions.casino import Card, CardRank, CardSuit, Deck, _handle_quests
from app.util.converters import get_amount, NotEnough, NotAnInteger, PastMinimum
from app.util.types import TypedInteraction
from config import Colors, Emojis


class PokerHandType(Enum):
    high_card = 0
    pair = 1
    two_pair = 2
    three_of_a_kind = 3
    straight = 4
    flush = 5
    full_house = 6
    four_of_a_kind = 7
    straight_flush = 8
    royal_flush = 9

    @property
    def is_flush(self) -> bool:
        return self is PokerHandType.flush or self >= PokerHandType.straight_flush

    def __lt__(self, other: PokerHandType) -> bool:
        return self.value < other.value

    def __eq__(self, other: PokerHandType) -> bool:
        return self.value == other.value

    def __str__(self) -> str:
        return self.name.replace('_', ' ').title().replace('Of A', 'of a')


class PokerHand(NamedTuple):
    type: PokerHandType
    ranks: list[CardRank]  # only lists relevant ranks

    @property
    def rank(self) -> CardRank:
        return self.ranks[0]

    @property
    def kicker(self) -> CardRank:
        return self.ranks[2 if self.type is PokerHandType.two_pair else 1]

    def __lt__(self, other: PokerHand) -> bool:
        return self.cmp_key < other.cmp_key

    def __eq__(self, other: PokerHand) -> bool:
        return not self.cmp_key < other.cmp_key and not other.cmp_key < self.cmp_key

    def __gt__(self, other: PokerHand) -> bool:
        return self.cmp_key > other.cmp_key

    @property
    def cmp_key(self):  # -> impl PartialOrd
        return self.type, *(r.poker_rank for r in self.ranks)

    def __str__(self) -> str:
        return str(self.type)


def find_straight(all_cards: list[Card]) -> CardRank | None:
    """Finds a straight in a list of cards."""
    straight = [all_cards[0].rank]

    for card in all_cards[1:]:
        rank = card.rank
        if rank.poker_rank == straight[-1].poker_rank - 1:
            straight.append(rank)

        elif rank.poker_rank != straight[-1].poker_rank:
            straight = [rank]
            continue

        if len(straight) == 4 and straight[-1] is CardRank.two and all_cards[0] is CardRank.ace:  # ace to 5 straight
            straight.append(CardRank.ace)

        if len(straight) == 5:
            return straight[0]

    return None


class PokerState:
    """Represents the state of a poker player."""

    def __init__(self, user: discord.User, interaction: TypedInteraction | None, *, bet: int, hand: list[Card]) -> None:
        self.user: discord.User = user
        self.bet: int = bet  # initial bet
        self.invested: int = 0  # bet for this round
        self.folded: bool = False
        self.remaining: int = bet  # remaining chips (coins)
        self.hand: list[Card] = hand
        self.requested_leave: bool = False
        self.done: bool = False
        self.last_action: str = ''
        self.interaction: TypedInteraction | None = interaction
        self.message: discord.Message | None = None
        self.view: PokerEphemeralView | None = None
        self.show: bool = False

    def __repr__(self) -> str:
        return f'<PokerState: {self.user}>'

    def __eq__(self, other: PokerState) -> bool:
        return self.user == other.user

    def __hash__(self) -> int:
        return hash(self.user)

    @property
    def display_hand(self) -> str:
        joiner = '`\u2002`'
        return f'`{joiner.join(card.display for card in self.hand)}`'

    @property
    def all_in(self) -> bool:
        return self.remaining <= 0

    @property
    def can_play(self) -> bool:
        return not self.requested_leave and self.remaining > 0

    def reset(self, hand: list[Card]) -> None:
        """Resets the state for a new round."""
        self.invested = 0
        self.done = False
        self.folded = False
        self.show = False
        self.hand = hand
        self.last_action = ''

    def raise_bet(self, amount: int) -> None:
        """Raises the player's bet."""
        self.invested += amount
        self.remaining -= amount
        self.last_action = f'Raise {self.invested:,}'

    def fold(self) -> None:
        """Folds the player's hand."""
        self.folded = True
        self.done = True
        self.last_action = 'Fold'

    def calculate_hand(self, cards: list[Card]) -> PokerHand:
        """Calculates the best hand from the player's hand and the community cards."""
        all_cards = cards + self.hand
        all_cards.sort(reverse=True, key=lambda card: card.rank.poker_rank)

        # check for any flush
        flush_cards = None
        generator = (suit for suit in CardSuit if sum(card.suit == suit for card in all_cards) >= 5)
        if flush_suit := next(generator, None):
            flush_cards = [card.rank for card in all_cards if card.suit == flush_suit]

        # check for straight
        straight = find_straight(all_cards)
        if straight is not None:
            # straight/royal flush?
            if flush_cards is not None:
                if straight is CardRank.ace:
                    return PokerHand(PokerHandType.royal_flush, [straight])
                return PokerHand(PokerHandType.straight_flush, [straight])
            return PokerHand(PokerHandType.straight, [straight])

        # check for standard flush
        if flush_cards is not None:
            return PokerHand(PokerHandType.flush, flush_cards)

        ranks = [card.rank for card in all_cards]

        # check for two/three/four of a kind
        n_of_a_kind = []
        for span, type in (
            (3, PokerHandType.four_of_a_kind),
            (2, PokerHandType.three_of_a_kind),
            (1, PokerHandType.pair),
        ):
            for i in range(len(ranks) - span):
                if ranks[i] == ranks[i + span]:  # we can do this because the list is sorted
                    kicker = next(rank for rank in ranks if rank != ranks[i])
                    n_of_a_kind.append(PokerHand(type, [ranks[i], kicker]))

        # quads take precedence over everything else
        if n_of_a_kind and n_of_a_kind[0].type is PokerHandType.four_of_a_kind:
            return n_of_a_kind[0]

        three_of_a_kinds = [hand for hand in n_of_a_kind if hand.type is PokerHandType.three_of_a_kind]
        three_of_a_kind_ranks = set(hand.rank for hand in three_of_a_kinds)
        pairs = [
            hand for hand in n_of_a_kind if hand.type is PokerHandType.pair and hand.rank not in three_of_a_kind_ranks
        ]

        # check for full house
        if three_of_a_kinds and pairs:
            return PokerHand(PokerHandType.full_house, [three_of_a_kinds[0].rank])

        # check for three of a kind
        if three_of_a_kinds:
            return three_of_a_kinds[0]

        # check for two pair
        if len(pairs) >= 2:
            kicker = next(rank for rank in ranks if rank not in (pairs[0].rank, pairs[1].rank))
            return PokerHand(PokerHandType.two_pair, [pairs[0].rank, pairs[1].rank, kicker])

        # check for pair
        if pairs:
            return pairs[0]

        # high card
        return PokerHand(PokerHandType.high_card, ranks)


class CustomRaiseModal(discord.ui.Modal, title='Raise Bet'):
    bet = discord.ui.TextInput(label='Raise to how much?')

    def __init__(self, view: PokerEphemeralView) -> None:
        self.bet.label = f'Raise by how much? (You have {view.game.current_turn.remaining:,})'
        self.bet.placeholder = 'Enter a bet amount (e.g. 50, 1k, 1/3, or 20%)'
        self.view = view
        self.game = view.game
        super().__init__()

    async def on_submit(self, interaction: TypedInteraction) -> Any:
        try:
            raise_by = get_amount(
                total=self.game.current_turn.remaining,
                minimum=self.game.big_blind,
                maximum=self.game.current_turn.remaining,
                arg=self.bet.value,
            )
        except NotAnInteger:
            return await interaction.response.send_message('Invalid bet amount', ephemeral=True)
        except NotEnough:
            return await interaction.response.send_message(
                f'You have **{self.game.coin} {self.game.current_turn.remaining:,}** remaining, '
                'so you can only raise by that much',
                ephemeral=True,
            )
        except PastMinimum:
            return await interaction.response.send_message(
                f'Must raise by at least the big blind ({self.game.coin} {self.game.big_blind:,})',
                ephemeral=True,
            )

        dummy = RaiseButton(bet=self.game.bet + raise_by, label='')
        dummy._view = self.view
        await dummy.callback(interaction)


class RaiseButton(discord.ui.Button['PokerEphemeralView']):
    def __init__(self, bet: int, *, label: str) -> None:
        self.bet: int = bet
        super().__init__(style=discord.ButtonStyle.primary, label=label, row=0)

    async def callback(self, interaction: TypedInteraction) -> Any:
        if not await self.view.game.is_current_turn(interaction):
            return

        difference = self.bet - self.view.state.invested
        self.view.game.base_raise = self.bet - self.view.game.bet
        self.view.game.bet = self.bet
        state = self.view.game.current_turn
        state.raise_bet(difference)
        state.last_action = f"{'Raise' if state.remaining > 0 else 'All-in'} {self.bet:,}"
        for player in self.view.game.players:
            if not player.folded:
                player.done = False
        state.done = True
        self.view.game.next_turn()
        self.view.update()
        await interaction.response.edit_message(content=self.view.display, view=self.view)
        await self.view.update_message()


class PokerEphemeralView(discord.ui.View):
    def __init__(self, game: Poker, state: PokerState) -> None:
        super().__init__()
        self.game: Poker = game
        self.state: PokerState = state
        self.update()

    def update(self) -> None:
        self.game.update()
        self.update_buttons()

    def update_buttons(self) -> None:
        self.clear_items()

        call = self.game.bet - self.state.invested
        if call >= self.state.remaining:
            self.check_button.label = 'All-in'
        else:
            self.check_button.label = 'Check' if call == 0 else f'Call {call:,}'
            remaining = self.state.remaining
            current = self.game.bet
            self.add_item(RaiseButton(bet := current + self.game.base_raise, label=f'Raise to {bet:,}'))
            self.add_item(self.raise_button)
            self.add_item(RaiseButton(self.state.invested + remaining, label='All-in'))

        self.add_item(self.check_button)
        self.add_item(self.fold_button)

        for item in self.children:
            item.disabled = self.game.current_turn != self.state or self.state.folded or self.game.show

        if self.state.folded:
            self.add_item(self.show_button)
            self.show_button.disabled = self.state.show

    @property
    def display(self) -> str:
        state = self.state
        return (
            f'{state.calculate_hand(self.game.community_cards)}: **{state.display_hand}**'
            if self.game.community_cards else f'**{state.display_hand}**'
        )

    async def update_message(self) -> None:
        await self.game.message.edit(embed=self.game.game_embed, view=self.game)

    @discord.ui.button(label='Fold', style=discord.ButtonStyle.danger, row=1)
    async def fold_button(self, interaction: TypedInteraction, _button: discord.ui.Button) -> Any:
        if not await self.game.is_current_turn(interaction):
            return

        self.state.fold()
        self.game.next_turn()
        self.update()
        self.game._last_interactions[interaction.user] = interaction

        await interaction.response.edit_message(content=self.display, view=self)
        await self.update_message()

    @discord.ui.button(label='Check', style=discord.ButtonStyle.success, row=1)
    async def check_button(self, interaction: TypedInteraction, _button: discord.ui.Button) -> Any:
        if not await self.game.is_current_turn(interaction):
            return

        remaining = self.game.bet - self.state.invested
        if remaining > 0:
            self.state.raise_bet(remaining)
            self.state.last_action = 'Call'
        else:
            self.state.last_action = 'Check'
        self.state.done = True
        self.game.next_turn()
        self.update()
        self.game._last_interactions[interaction.user] = interaction

        await interaction.response.edit_message(content=self.display, view=self)
        await self.update_message()

    @discord.ui.button(label='Raise Custom', style=discord.ButtonStyle.primary, row=0)
    async def raise_button(self, interaction: TypedInteraction, _button: discord.ui.Button) -> Any:
        if not await self.game.is_current_turn(interaction):
            return

        self.game._last_interactions[interaction.user] = interaction
        await interaction.response.send_modal(CustomRaiseModal(self))

    @discord.ui.button(label='Show Hand', style=discord.ButtonStyle.secondary, row=1)
    async def show_button(self, interaction: TypedInteraction, button: discord.ui.Button) -> Any:
        if not await self.game.is_current_turn(interaction):
            return

        self.state.show = True
        button.disabled = True
        self.game._last_interactions[interaction.user] = interaction
        await interaction.response.edit_message(content=self.display, view=self)

        # If already showing, update the message
        if self.game.show:
            self.game.hands[self.state] = self.state.calculate_hand(self.game.community_cards)
            await self.update_message()


class Poker(discord.ui.View):
    def __init__(self, ctx: Context, *, buy_in: int, small_blind: int, big_blind: int) -> None:
        super().__init__(timeout=60)

        self.ctx: Context = ctx
        self.coin = (
            Emojis.coin
            if not ctx.interaction and ctx.guild and ctx.channel.permissions_for(ctx.guild.me).external_emojis
            or ctx.interaction and ctx.interaction.app_permissions.external_emojis
            else '\U0001fa99'
        )
        self.host: discord.User = ctx.author
        self.players: list[PokerState] = []
        self.community_cards: list[Card] = []
        self.queue: dict[discord.User, TypedInteraction | None] = {ctx.author: None}
        self._last_interactions: dict[discord.User, TypedInteraction] = {}
        self.deck: Deck = Deck()

        self.show: bool = False
        self.winners: list[PokerState] = []

        self.buy_in: int = buy_in
        self.small_blind: int = small_blind
        self.big_blind: int = big_blind
        self.dealer_idx: int = -1
        self.turn_idx: int = 0
        self.bet: int = 0
        self.pot: int = 0
        self.side_pot: int = 0
        self.no_side_pot: set[PokerState] = set()
        self.base_raise: int = self.big_blind
        self.update()

    @property
    def dealer(self) -> PokerState:
        return self.players[self.dealer_idx % len(self.players)]

    @property
    def small_blind_player(self) -> PokerState:
        return self.players[(self.dealer_idx + 1) % len(self.players)]

    @property
    def big_blind_player(self) -> PokerState:
        return self.players[(self.dealer_idx + 2) % len(self.players)]

    @property
    def previous_turn(self) -> PokerState:
        return self.players[(self.turn_idx - 1) % len(self.players)]

    @property
    def current_turn(self) -> PokerState:
        return self.players[self.turn_idx % len(self.players)]

    def next_game(self) -> None:
        """Starts a new game."""
        self.deck.reset()
        self.deck.shuffle()
        self.show = False
        self.winners = []

        self.community_cards = []
        self.players = [player for player in self.players if not player.requested_leave and player.remaining > 0]
        for player in self.players:
            player.reset(self.deck.draw_many(2))
            player.interaction = self._last_interactions.pop(player.user, player.interaction)
        self.players.extend(
            PokerState(user, itx, bet=self.buy_in, hand=self.deck.draw_many(2))
            for user, itx in self.queue.items()
        )
        self.queue.clear()

        self.pot = 0
        self.dealer_idx += 1
        self.no_side_pot.clear()
        if len(self.players) == 2:
            self.turn_idx = self.dealer_idx + 1
        else:
            self.turn_idx = self.dealer_idx + 3

        self.small_blind_player.raise_bet(self.small_blind)
        self.small_blind_player.last_action = f'Small Blind {self.small_blind:,}'

        self.big_blind_player.raise_bet(self.big_blind)
        self.big_blind_player.last_action = f'Big Blind {self.big_blind:,}'

        self.bet = self.big_blind
        self.base_raise = self.big_blind

    def show_hands(self) -> None:
        self.show = True
        self.hands = {
            player: player.calculate_hand(self.community_cards)
            for player in self.players if not player.folded or player.show
        } if self.community_cards else {}

        for player in self.players:
            if not player.folded:
                player.last_action = ''

    def _calculate_hands(self, predicate, *, pot: int) -> list[PokerState]:
        self.show_hands()
        base_filtered = (player for player in self.hands if not player.folded)
        best_hand = max(self.hands[hand].cmp_key for hand in base_filtered if predicate(hand))
        winners = [player for player, hand in self.hands.items() if best_hand == hand.cmp_key]
        for winner in winners:
            winner.remaining += pot // len(winners)

        return winners

    def calculate_hands(self) -> None:
        if not self.community_cards:
            self.show_hands()
            sole_winner = next(p for p in self.players if not p.folded)
            sole_winner.remaining += self.pot
            return

        self.winners = self._calculate_hands(lambda _: True, pot=self.pot)
        if self.side_pot:
            self._calculate_hands(lambda player: player not in self.no_side_pot, pot=self.side_pot)

    async def refresh_card_views(self) -> None:
        for state in self.players:
            if itx := state.interaction:
                method = state.message.edit if state.message else itx.edit_original_response
                state.view.update_buttons()
                await method(content=state.view.display, view=state.view)

    def draw_community_cards(self, count: int) -> None:
        self.community_cards.extend(self.deck.draw_many(count))

    def next_round(self) -> None:
        self.bet = 0
        for player in self.players:
            player.done = False
            player.last_action = ''
            self.pot += player.invested

            if player.remaining < 0:
                self.pot += player.remaining
                self.side_pot += abs(player.remaining)
                self.no_side_pot.add(player)
            player.invested = 0

        if len(self.players) == 1:
            self.winners = self.players
            self.winners[0].remaining += self.pot
            self.show_hands()
            return

        not_folded = [player for player in self.players if not player.folded]
        if len(not_folded) == 1:
            self.winners = not_folded
            self.winners[0].remaining += self.pot
            self.show_hands()
            return

        if sum(not p.folded and not p.all_in for p in self.players) <= 1:
            self.draw_community_cards(5 - len(self.community_cards))
            return self.calculate_hands()

        if len(self.community_cards) == 0:
            self.draw_community_cards(3)
        elif len(self.community_cards) in (3, 4):
            self.draw_community_cards(1)
        else:
            return self.calculate_hands()

        self.turn_idx = self.dealer_idx + 1
        while self.current_turn.folded:
            self.turn_idx += 1
        self.base_raise = self.big_blind

    def next_turn(self) -> None:
        """Advances the turn to the next player."""

        if all(player.done or player.all_in for player in self.players):
            self.next_round()
        else:
            self.turn_idx += 1
            while self.current_turn.folded:
                self.turn_idx += 1

        self.ctx.bot.loop.create_task(self.refresh_card_views())

    def __contains__(self, item: discord.User) -> bool:
        return any(player.user == item for player in self.players) or item in self.queue

    def get_state(self, user: discord.User) -> PokerState | None:
        return next((player for player in self.players if player.user == user), None)

    @property
    def pregame_embed(self) -> discord.Embed:
        embed = discord.Embed(color=Colors.primary, timestamp=self.ctx.now)
        embed.set_author(name=f'{self.host.display_name} wants to play Poker', icon_url=self.host.avatar)
        embed.set_footer(text=f'Game will start when {self.host.display_name} clicks the Start button.')
        embed.description = (
            f'Blind: {self.coin} {self.small_blind:,} SB / {self.big_blind:,} BB\n'
            f'Buy-in: {self.coin} **{self.buy_in:,}**\n-# Joining will cost you the buy-in amount.'
        )

        if self.queue:
            embed.add_field(
                name=f'Players ({len(self.queue)}/8)',
                value='\n'.join(f'- {user.mention}' for user in self.queue),
                inline=False,
            )

        return embed

    @property
    def game_embed(self) -> discord.Embed:
        embed = discord.Embed(color=Colors.secondary, timestamp=self.ctx.now)
        if not self.show:
            embed.set_author(
                name=f'{self.current_turn.user.display_name}\'s Turn',
                icon_url=self.current_turn.user.avatar,
            )
        elif len(self.winners) == 1:
            winner = self.winners[0]
            embed.set_author(name=f'{winner.user.display_name} wins this round!', icon_url=winner.user.avatar)
        else:
            embed.set_author(name='Split Pot')

        description = ''
        em_dash ='\u2014 '
        for i, player in enumerate(self.players):
            if self.show:
                name = player.user.display_name if player not in self.winners else f'**{player.user.display_name}** \U0001f3c6'
            else:
                name = player.user.display_name if self.turn_idx % len(self.players) != i else f'**{player.user.display_name}**'
                name += ' \U0001f518' if self.dealer_idx % len(self.players) == i else ''
            if player.folded:
                name = f'~~{name}~~'
            description += (
                f'{name}: {self.coin} **{max(0, player.remaining):,}** '
                f'{player.last_action and em_dash + player.last_action}'
            )
            if self.show and player in self.hands:
                folded = 'Folded ' if player.folded else ''
                description += f'\n-# {folded}{self.hands[player]}: {player.display_hand}'
            description += '\n'
        embed.description = description

        if self.community_cards:
            embed.description += f"## `{'` `'.join(card.display for card in self.community_cards)}`"

        # embed.add_field(
        #     name='Community Cards',
        #     value=f"### `{'` `'.join(card.display for card in self.community_cards)}`"
        #     if self.community_cards else 'Preflop',
        #     inline=False,
        # )
        embed.add_field(name='Bet', value=f'{self.coin} **{self.bet:,}**')
        embed.add_field(name='Pot', value=f'{self.coin} **{self.pot:,}**')
        if self.side_pot:
            embed.add_field(name='Side Pot', value=f'{self.coin} **{self.side_pot:,}**')
        embed.set_footer(
            text=f'{self.small_blind:,}/{self.big_blind:,} Stake \u2022 Hosted by {self.host.display_name}',
            icon_url=self.host.avatar,
        )
        return embed

    def update(self) -> None:
        self.clear_items()
        self.join_button.label = f'Join ({self.buy_in:,} Buy-in)'
        self.add_item(self.join_button)
        self.add_item(self.leave_button)
        self.leave_button.label = 'Leave / Cash out' if self.players else 'Leave'

        if not self.players:
            self.add_item(self.start_button)
            return
        elif self.show:
            self.add_item(self.next_game_button)
            return
        else:
            self.add_item(self.view_button)

    @discord.ui.button(label='Join', style=discord.ButtonStyle.success)
    async def join_button(self, interaction: TypedInteraction, _button: discord.ui.Button) -> Any:
        if interaction.user in self:
            return await interaction.response.send_message('You are already in the game.', ephemeral=True)

        if len(self.players) >= 8:
            return await interaction.response.send_message('The game is full.', ephemeral=True)

        record = await self.ctx.db.get_user_record(interaction.user.id)
        if record.wallet < self.buy_in:
            return await interaction.response.send_message(
                f'The buy-in for this poker game is {self.coin} **{self.buy_in:,}**, '
                f'but you only have {self.coin} **{record.wallet:,}**.',
                ephemeral=True,
            )

        await record.add(wallet=-self.buy_in)
        self.queue[interaction.user] = interaction
        message = (
            f'You paid the buy-in of {Emojis.coin}**{self.buy_in:,}** and joined the game.\n'
            f'{Emojis.Expansion.standalone} You now have {Emojis.coin} **{record.wallet:,}**'
        )
        if not self.players:
            await interaction.response.edit_message(embed=self.pregame_embed)
            await interaction.followup.send(message, emphemeral=True)
        else:
            await interaction.response.send_message(f'{message}\n-# You will play next round.', ephemeral=True)

    async def cash_out(self, state: PokerState, *, auto_join: bool = False) -> discord.Embed:
        record = self.ctx.db.get_user_record(state.user.id, fetch=False)
        net_remaining = state.remaining - self.buy_in if auto_join else state.remaining
        await record.add(wallet=net_remaining)

        profit = state.remaining - state.bet
        await _handle_quests(self.ctx, record, bet=state.bet, profit=profit)

        embed = discord.Embed(
            color=Colors.success if profit > 0 else Colors.error if profit < 0 else Colors.warning
        )
        embed.set_author(name=f'{state.user.display_name}: Cash out', icon_url=state.user.avatar)
        exp = Emojis.Expansion.standalone if self.coin == Emojis.coin else '\u2937'
        extra = (
            f'-# You also repaid the buy-in of {self.coin} **{self.buy_in:,}**\n'
            f'-# Leave the game to receive back the buy-in.\n'
        ) if auto_join else ''
        embed.add_field(
            name=f'Cashed out {self.coin} **{state.remaining:,}**',
            value=(
                f'You {"profited" if profit > 0 else "lost"} {self.coin} **{abs(profit):,}**.\n{extra}'
                f'{exp} You now have {self.coin} **{record.wallet:,}**'
            ) if profit else 'You broke even.'
        )
        return embed

    async def lobby(self, interaction: TypedInteraction) -> bool:
        if sum(p.can_play for p in self.players) > 1:
            return False

        self.queue = {player.user: player.interaction for player in self.players if player.can_play}
        # cash out to remaining player
        for player in self.players:
            if player.can_play:
                embed = await self.cash_out(player, auto_join=True)
                embed.set_footer(text='Automatically cashed out because all players left')
                if itx := player.interaction:
                    if player.message:
                        self.ctx.bot.loop.create_task(player.message.delete())

                    await itx.followup.send(embed=embed, ephemeral=True)
                break

        self.host = next(iter(self.queue.keys()))
        self.players.clear()
        self.update()
        if self.message:
            await self.message.delete()
        else:
            try:
                await interaction.delete_original_response()
            finally:
                pass

        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send(embed=self.pregame_embed, view=self)
        self.timeout = 60
        return True

    @discord.ui.button(label='Leave', style=discord.ButtonStyle.danger)
    async def leave_button(self, interaction: TypedInteraction, _button: discord.ui.Button) -> Any:
        if interaction.user in self.queue:
            self.queue.pop(interaction.user, None)
            # Return buy-in
            await self.ctx.db.get_user_record(interaction.user.id, fetch=False).add(wallet=self.buy_in)
            await interaction.response.edit_message(embed=self.pregame_embed)
            await interaction.followup.send(
                f'You left the game! You were refunded the buy-in of {Emojis.coin} **{self.buy_in}**.',
                ephemeral=True,
            )

        elif state := self.get_state(interaction.user):
            state.fold()
            if state == self.current_turn:
                self.next_turn()
                self.update()
            state.requested_leave = True

            embed = await self.cash_out(state)
            await interaction.response.edit_message(embed=self.game_embed, view=self)
            await interaction.followup.send(embed=embed, ephemeral=True)
            await self.lobby(interaction)
        else:
            return await interaction.response.send_message('You are not in the game.', ephemeral=True)

    async def is_current_turn(self, interaction: TypedInteraction) -> bool:
        if not self.get_state(interaction.user):
            await interaction.response.send_message('You are not in the game.', ephemeral=True)
            return False

        if self.current_turn.user != interaction.user:
            await interaction.response.send_message('It is not your turn.', ephemeral=True)
            return False

        return True

    @discord.ui.button(label='Start', style=discord.ButtonStyle.primary)
    async def start_button(self, interaction: TypedInteraction, _button: discord.ui.Button) -> Any:
        if interaction.user != self.host:
            return await interaction.response.send_message('Only the host can start the game.', ephemeral=True)

        if len(self.queue) < 2:
            return await interaction.response.send_message(
                'You can only start the game with two or more players.',
                ephemeral=True,
            )

        self.queue[self.host] = interaction
        self.next_game()
        self.update()
        self.timeout = None
        await interaction.response.edit_message(content='Starting...', embed=None, view=None)
        self.message = await interaction.followup.send(embed=self.game_embed, view=self)

        for state in self.players:
            if itx := state.interaction:
                state.view = PokerEphemeralView(self, state)
                state.message = await itx.followup.send(state.display_hand, view=state.view, ephemeral=True, wait=True)

    @discord.ui.button(label='View Cards', style=discord.ButtonStyle.secondary)
    async def view_button(self, interaction: TypedInteraction, _button: discord.ui.Button) -> Any:
        if state := self.get_state(interaction.user):
            state.interaction = interaction
            state.message = None
            self._last_interactions[interaction.user] = interaction

            view = state.view or PokerEphemeralView(self, state)
            view.update_buttons()
            return await interaction.response.send_message(view.display, view=view, ephemeral=True)
        await interaction.response.send_message('You are not in the game.', ephemeral=True)

    @discord.ui.button(label='Next Game', style=discord.ButtonStyle.primary)
    async def next_game_button(self, interaction: TypedInteraction, _button: discord.ui.Button) -> Any:
        if interaction.user != self.host:
            return await interaction.response.send_message('Only the host can use this button.', ephemeral=True)

        self._last_interactions[self.host] = interaction
        if await self.lobby(interaction):
            return

        self.next_game()
        self.update()
        await interaction.response.edit_message(content='Starting next game...', embed=None, view=None)
        self.message = await interaction.followup.send(embed=self.game_embed, view=self)

        for state in self.players:
            if itx := state.interaction:
                state.view = PokerEphemeralView(self, state)
                state.message = await itx.followup.send(state.display_hand, view=state.view, ephemeral=True, wait=True)
