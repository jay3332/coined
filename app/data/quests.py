from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, NamedTuple, TypeAlias, TYPE_CHECKING

from app.data.items import Item, ItemType, Items, ItemRarity, Reward
from app.data.pets import Pets, Pet, PetRarity
from app.util.common import CubicCurve, get_by_key, walk_collection, weighted_choice
from config import Emojis

if TYPE_CHECKING:
    from app.database import UserRecord
    from app.util.types import AsyncCallable

    GenerateCallback: TypeAlias = AsyncCallable[['QuestTemplates', 'QuestTemplate', 'QuestSlot', UserRecord], 'Quest']
    SetupCallback: TypeAlias = AsyncCallable[['QuestTemplates', 'Quest'], None]
    GetTitleCallback: TypeAlias = Callable[['QuestTemplates', 'Quest'], str]
    GetMaxProgressCallback: TypeAlias = Callable[['QuestTemplates', 'Quest'], int]
    GetTicketsCallback: TypeAlias = Callable[['QuestTemplates', 'Quest'], int]


class QuestSlot(Enum):
    vote = 1
    recurring_easy = 2  # 12h
    recurring_mid = 3   # 48h
    recurring_hard = 4  # 7d
    daily_1 = 5
    daily_2 = 6

    @property
    def is_recurring(self) -> bool:
        """Check if this quest type is a recurring quest."""
        return self in (QuestSlot.recurring_easy, QuestSlot.recurring_mid, QuestSlot.recurring_hard)

    @property
    def is_daily(self) -> bool:
        """Check if this quest type is a daily quest."""
        return self in (QuestSlot.daily_1, QuestSlot.daily_2)

    def get_random_category(self, *, exclude: set[QuestCategory] = None) -> QuestCategory:
        """Get the category of this quest type."""
        if self is QuestSlot.vote:
            return QuestCategory.vote

        weights = _WEIGHTS[self]
        if exclude is not None:
            weights = {k: v for k, v in weights.items() if k not in exclude} or weights

        return weighted_choice(weights)


class QuestCategory(Enum):
    vote = 0  # Vote on Top.gg once or many times
    command = 1  # Use a command (or some category) once or many times
    event = 2  # Participate in or win a chat event once or many times
    coins = 3  # Accumulate a certain amount of coins in PROFIT (gifting or selling does not count)
    # items = 4  # Accumulate or sell a certain amount of items (gifting does not count)
    hunt = 5  # Catch a certain pet once or many times (note the use of the /hunt command would be type 1)
    feed = 6  # Feed N coins worth of items to pets, feed N energy, or feed a specific item to pets (easy only)
    fish = 7  # Catch a certain fish once or many times (note the use of the /fish command would be type 1)
    dig = 8  # Dig to a certain depth or use a certain amount of stamina when digging (note the use of the /dig command would be type 1)
    work = 9  # Work SUCCESSFULLY a certain amount of times or get a raise (note the use of the /work command would be type 1)
    casino = 10  # Gamble a certain amount of coins, win a certain number of times, or win a certain amount of coins (note the use of the gambling commands in general would be type 1)
    farm = 11  # Harvest a certain amount of crops, water N crops, or plant N seeds
    rob = 12  # Rob a certain amount of coins from other users, or rob successfully N times (note the use of the /rob command would be type 1)


_STANDARD = {category: 2 for category in QuestCategory} | {
    QuestCategory.vote: 0,
    QuestCategory.farm: 1,
    QuestCategory.rob: 1,
}
_WEIGHTS: dict[QuestSlot, dict[QuestCategory, int]] = {
    QuestSlot.vote: {category: 0 for category in QuestCategory} | {QuestCategory.vote: 1},
    QuestSlot.recurring_easy: _STANDARD | {
        QuestCategory.event: 1,
        QuestCategory.command: 3,
    },
    QuestSlot.recurring_mid: _STANDARD | {
        QuestCategory.event: 1,
        QuestCategory.command: 1,
    },
    QuestSlot.recurring_hard: _STANDARD | {
        QuestCategory.command: 1,
        QuestCategory.event: 0,
    },
    QuestSlot.daily_1: _STANDARD | {
        QuestCategory.command: 0,
        QuestCategory.event: 0,
    },
    QuestSlot.daily_2: _STANDARD | {
        QuestCategory.command: 0,
        QuestCategory.event: 0,
    },
}


def dummy(*args, **kwargs):
    raise NotImplementedError(f'{args} {kwargs}')


async def noop(*_args, **_kwargs):
    pass


@dataclass
class QuestTemplate:
    key: str
    category: QuestCategory

    _generate: GenerateCallback = dummy
    _setup: SetupCallback = noop
    get_title: GetTitleCallback = dummy
    get_max_progress: GetMaxProgressCallback = lambda _, quest: quest.arg
    get_tickets: GetTicketsCallback = dummy

    def __hash__(self) -> int:
        return hash(self.key)

    async def generate(self, type: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest based on the template."""
        return await self._generate(_INST, self, type, record)

    async def setup(self, quest: Quest) -> None:
        """Setup the quest, this is called after the quest is generated."""
        await self._setup(_INST, quest)

    def to_generate(self, func: GenerateCallback) -> GenerateCallback:
        """Set the generate function for this quest."""
        self._generate = func
        return func

    def to_setup(self, func: SetupCallback) -> SetupCallback:
        """Set the setup function for this quest."""
        self._setup = func
        return func

    def to_get_title(self, func: GetTitleCallback) -> GetTitleCallback:
        """Set the get_title function for this quest."""
        self.get_title = func
        return func

    def to_get_max_progress(self, func: GetMaxProgressCallback) -> GetMaxProgressCallback:
        """Set the get_max_progress function for this quest."""
        self.get_max_progress = func
        return func

    def to_get_tickets(self, func: GetTicketsCallback) -> GetTicketsCallback:
        """Set the get_tickets function for this quest."""
        self.get_tickets = func
        return func


class Quest(NamedTuple):
    record: UserRecord
    template: QuestTemplate
    slot: QuestSlot
    arg: int
    extra: str | None = None

    @property
    def title(self) -> str:
        return self.template.get_title(_INST, self)

    @property
    def max_progress(self) -> int:
        return self.template.get_max_progress(_INST, self)

    @property
    def tickets(self) -> int:
        return self.template.get_tickets(_INST, self)


class QuestTemplates:
    vote = QuestTemplate(key='vote', category=QuestCategory.vote)

    @vote.to_generate
    async def generate_vote_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        assert slot is QuestSlot.vote
        arg = 1
        quests = record.quest_manager

        if sum(q.quest.slot is QuestSlot.vote for q in quests.cached) >= 3:
            arg = weighted_choice({1: 100, 2: 20, 3: 5, 4: 2, 5: 1})

        return Quest(record, template, slot, arg)

    @vote.to_get_title
    def get_vote_title(self, quest: Quest) -> str:
        emoji = '<:upvote:1379979028118638662>'
        top_gg = f'[Top.gg](https://top.gg/bot/{quest.record.db.bot.user.id}/vote)'
        if quest.arg == 1:
            return f'{emoji} Vote for Coined on {top_gg}'

        return f'{emoji} Vote for Coined on {top_gg} {quest.arg} times'

    @vote.to_get_tickets
    def get_vote_tickets(self, quest: Quest) -> int:
        return 10 * quest.arg

    all_commands = QuestTemplate(key='all_commands', category=QuestCategory.command)
    currency_commands = QuestTemplate(key='currency_commands', category=QuestCategory.command)

    @staticmethod
    def _command_count(type: QuestSlot) -> int:
        match type:
            case QuestSlot.recurring_easy:
                return random.randint(2, 6) * 20
            case QuestSlot.recurring_mid:
                return random.randint(10, 20) * 20
            case QuestSlot.recurring_hard:
                return random.randint(20, 50) * 50
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                return random.randint(5, 10) * 20

    @all_commands.to_generate
    @currency_commands.to_generate
    async def generate_command_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        arg = self._command_count(slot)
        return Quest(record, template, slot, arg)

    @all_commands.to_get_title
    def get_command_title(self, quest: Quest) -> str:
        return f'{Emojis.coined} Use {quest.arg:,} commands'

    @currency_commands.to_get_title
    def get_currency_command_title(self, quest: Quest) -> str:
        return f'{Emojis.coined} Use {quest.arg:,} currency commands'

    @all_commands.to_get_tickets
    @currency_commands.to_get_tickets
    def get_command_tickets(self, quest: Quest) -> int:
        return quest.arg // 20

    gamble_commands = QuestTemplate(key='gamble_commands', category=QuestCategory.command)

    @gamble_commands.to_generate
    async def generate_gamble_command_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires using gambling commands."""
        arg = self._command_count(slot) // 4
        return Quest(record, template, slot, arg)

    @gamble_commands.to_get_title
    def get_gamble_command_title(self, quest: Quest) -> str:
        return f'\U0001f3b2 Gamble {quest.arg:,} times'

    @gamble_commands.to_get_tickets
    def get_gamble_command_tickets(self, quest: Quest) -> int:
        return quest.arg // 8

    specific_command = QuestTemplate(key='specific_command', category=QuestCategory.command)

    @specific_command.to_generate
    async def generate_specific_command_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires using a specific command."""
        command = weighted_choice({
            'hunt': 20,
            'fish': 20,
            'dig': 20,
            'work': 20,
            'slots': 10,
            'blackjack': 10,
        })
        arg = self._command_count(slot) // 5
        if command in ('dig', 'work'):
            arg //= 2
        return Quest(record, template, slot, arg, command)

    @specific_command.to_get_title
    def get_specific_command_title(self, quest: Quest) -> str:
        from app.extensions.casino import SLOTS_EMOJI_MAPPING, SlotsCell

        arg = quest.arg
        return dict(
            hunt=f'{Items.net.emoji} Hunt {arg:,} times',
            fish=f'{Items.fish.emoji} Go fishing {arg:,} times',
            dig=f'{Items.shovel.emoji} Dig {arg:,} times',
            work=f'\U0001f4bc Work {arg:,} times',
            slots=f'{SLOTS_EMOJI_MAPPING[SlotsCell.seven]} Play slots {arg:,} times',
            blackjack=f'\U0001f0cf Play blackjack {arg:,} times',
        ).get(
            quest.extra,
            f'{Emojis.coined} Use the `{quest.extra}` command {arg:,} times',
        )

    @specific_command.to_get_tickets
    def get_specific_command_tickets(self, quest: Quest) -> int:
        """Get the number of tickets for a specific command quest."""
        if quest.extra in ('dig', 'work'):
            return quest.arg // 5
        return quest.arg // 10

    event_participant = QuestTemplate(key='event_participant', category=QuestCategory.event)

    @event_participant.to_generate
    async def generate_event_participant_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires participating in an event."""
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(2, 4) * 2
            case QuestSlot.recurring_mid:
                arg = random.randint(2, 4) * 5
            case QuestSlot.recurring_hard:
                arg = random.randint(6, 10) * 5
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(4, 7)
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @event_participant.to_get_title
    def get_event_participant_title(self, quest: Quest) -> str:
        return f'\U0001f389 Participate in {quest.arg:,} chat events'

    @event_participant.to_get_tickets
    def get_event_participant_tickets(self, quest: Quest) -> int:
        """Get the number of tickets for an event participant quest."""
        return quest.arg * 3 // 2

    event_winner = QuestTemplate(key='event_winner', category=QuestCategory.event)

    @event_winner.to_generate
    async def generate_event_winner_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires winning an event."""
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(2, 4)
            case QuestSlot.recurring_mid:
                arg = random.randint(6, 10)
            case QuestSlot.recurring_hard:
                arg = random.randint(8, 12) * 2
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(3, 5)
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @event_winner.to_get_title
    def get_event_winner_title(self, quest: Quest) -> str:
        return f'\U0001f389 Win {quest.arg:,} chat events'

    @event_winner.to_get_tickets
    def get_event_winner_tickets(self, quest: Quest) -> int:
        """Get the number of tickets for an event winner quest."""
        return quest.arg * 2

    earn_coins = QuestTemplate(key='earn_coins', category=QuestCategory.coins)

    @earn_coins.to_generate
    async def generate_earn_coins_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires earning coins."""
        multiplier = record.coin_multiplier
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(10, 25) * 1000
            case QuestSlot.recurring_mid:
                arg = random.randint(40, 80) * 1000
            case QuestSlot.recurring_hard:
                arg = random.randint(100, 200) * 1000
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(20, 40) * 1000
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, round(arg * multiplier))

    @earn_coins.to_get_title
    def get_earn_coins_title(self, quest: Quest) -> str:
        return f'{Emojis.coin} Earn {quest.arg:,} coins in profit'

    @earn_coins.to_get_tickets
    def get_earn_coins_tickets(self, quest: Quest) -> int:
        """Get the number of tickets for an earn coins quest."""
        return quest.arg // 2500

    sell_items = QuestTemplate(key='sell_items', category=QuestCategory.coins)

    @sell_items.to_generate
    async def generate_sell_items_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires selling items."""
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(5, 10) * 1000
            case QuestSlot.recurring_mid:
                arg = random.randint(25, 40) * 1000
            case QuestSlot.recurring_hard:
                arg = random.randint(75, 150) * 1000
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(10, 20) * 1000
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @sell_items.to_get_title
    def get_sell_items_title(self, quest: Quest) -> str:
        return f'{Emojis.coin} Sell {quest.arg:,} coins worth of items'

    @sell_items.to_get_tickets
    def get_sell_items_tickets(self, quest: Quest) -> int:
        """Get the number of tickets for a sell items quest."""
        return quest.arg // 1250

    # accumulate_items = QuestTemplate(key='accumulate_items', category=QuestCategory.items)
    # obtain_specific_item = QuestTemplate(key='obtain_specific_item', category=QuestCategory.items)
    # obtain_items_of_rarity = QuestTemplate(key='obtain_items_of_rarity', category=QuestCategory.items)

    catch_pets = QuestTemplate(key='catch_pets', category=QuestCategory.hunt)

    @catch_pets.to_generate
    async def generate_catch_pets_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires catching pets."""
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(2, 4) * 5
            case QuestSlot.recurring_mid:
                arg = random.randint(6, 10) * 5
            case QuestSlot.recurring_hard:
                arg = random.randint(10, 20) * 10
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(3, 5) * 5
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @catch_pets.to_get_title
    def get_catch_pets_title(self, quest: Quest) -> str:
        return f'{Items.net.emoji} Catch {quest.arg:,} pets'

    @catch_pets.to_get_tickets
    def get_catch_pets_tickets(self, quest: Quest) -> int:
        return quest.arg // 5 * 2

    catch_specific_pet = QuestTemplate(key='catch_specific_pet', category=QuestCategory.hunt)

    @catch_specific_pet.to_generate
    async def generate_catch_specific_pet_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        arg = 1
        match slot:
            case QuestSlot.recurring_easy:
                rarity = PetRarity.common
            case QuestSlot.recurring_mid:
                rarity = PetRarity.uncommon
            case QuestSlot.recurring_hard:
                rarity = weighted_choice({PetRarity.rare: 3, PetRarity.epic: 1})
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                rarity = PetRarity.common
                arg = 2
            case _:
                raise ValueError(f'Invalid quest type: {slot}')

        pet = random.choice([pet for pet in walk_collection(Pets, Pet) if pet.rarity is rarity])
        return Quest(record, template, slot, arg, pet.key)

    @catch_specific_pet.to_get_title
    def get_catch_specific_pet_title(self, quest: Quest) -> str:
        pet = get_by_key(Pets, quest.extra)
        if quest.arg > 1:
            return f'{pet.emoji} Catch {quest.arg:,} {pet.plural}'
        return f'{pet.emoji} Catch {pet.singular} {pet.name}'

    @catch_specific_pet.to_get_tickets
    def get_catch_specific_pet_tickets(self, quest: Quest) -> int:
        pet = get_by_key(Pets, quest.extra)
        match pet.rarity:
            case PetRarity.common:
                return quest.arg * 5
            case PetRarity.uncommon:
                return quest.arg * 15
            case PetRarity.rare:
                return quest.arg * 30
            case PetRarity.epic:
                return quest.arg * 100

    catch_pets_of_rarity = QuestTemplate(key='catch_pets_of_rarity', category=QuestCategory.hunt)

    @catch_pets_of_rarity.to_generate
    async def generate_catch_pets_of_rarity_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires catching pets of a specific rarity."""
        match slot:
            case QuestSlot.recurring_easy:
                choices = {
                    PetRarity.common: random.randint(2, 4) * 5,
                    PetRarity.uncommon: random.randint(2, 4),
                }
            case QuestSlot.recurring_mid:
                choices = {
                    PetRarity.common: random.randint(5, 9) * 5,
                    PetRarity.uncommon: random.randint(6, 10),
                    PetRarity.rare: random.randint(2, 4),
                }
            case QuestSlot.recurring_hard:
                choices = {
                    PetRarity.common: random.randint(8, 16) * 5,
                    PetRarity.uncommon: random.randint(8, 15) * 2,
                    PetRarity.rare: random.randint(6, 10),
                    PetRarity.epic: random.randint(2, 3),
                    PetRarity.legendary: 1,
                }
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                choices = {
                    PetRarity.common: random.randint(3, 5) * 4,
                    PetRarity.uncommon: random.randint(3, 5),
                }
            case _:
                raise ValueError(f'Invalid quest type: {slot}')

        rarity = random.choice(list(choices.keys()))
        arg = choices[rarity]

        return Quest(record, template, slot, arg, rarity.name)

    @catch_pets_of_rarity.to_get_title
    def get_catch_pets_of_rarity_title(self, quest: Quest) -> str:
        rarity = PetRarity[quest.extra]
        if quest.arg > 1:
            return f'{rarity.emoji} Catch {quest.arg} {rarity.name} pets'
        return f'{rarity.emoji} Catch {rarity.singular} {rarity.name} pet'

    @catch_pets_of_rarity.to_get_tickets
    def get_catch_pets_of_rarity_tickets(self, quest: Quest) -> int:
        rarity = PetRarity[quest.extra]
        match rarity:
            case PetRarity.common:
                return quest.arg // 5 * 3
            case PetRarity.uncommon:
                return quest.arg * 3
            case PetRarity.rare:
                return quest.arg * 10
            case PetRarity.epic:
                return quest.arg * 20
            case PetRarity.legendary:
                return quest.arg * 100

    feed_worth_coins = QuestTemplate(key='feed_worth_coins', category=QuestCategory.feed)

    @staticmethod
    def _feed_coins_worth(slot: QuestSlot) -> int:
        match slot:
            case QuestSlot.recurring_easy:
                return random.randint(4, 8) * 500
            case QuestSlot.recurring_mid:
                return random.randint(25, 50) * 500
            case QuestSlot.recurring_hard:
                return random.randint(80, 120) * 500
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                return random.randint(8, 15) * 500
            case _:
                raise ValueError(f'Invalid quest type: {slot}')

    @feed_worth_coins.to_generate
    async def generate_feed_worth_coins_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        arg = self._feed_coins_worth(slot)
        return Quest(record, template, slot, arg)

    @feed_worth_coins.to_get_title
    def get_feed_worth_coins_title(self, quest: Quest) -> str:
        return f'{Items.carrot.emoji} Feed {quest.arg:,} coins worth of items to your pets'

    @feed_worth_coins.to_get_tickets
    def get_feed_worth_coins_tickets(self, quest: Quest) -> int:
        return quest.arg // 500

    feed_energy = QuestTemplate(key='feed_energy', category=QuestCategory.feed)

    @feed_energy.to_generate
    async def generate_feed_energy_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(3, 10) * 50
            case QuestSlot.recurring_mid:
                arg = random.randint(25, 50) * 50
            case QuestSlot.recurring_hard:
                arg = random.randint(80, 120) * 50
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(5, 12) * 50
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @feed_energy.to_get_title
    def get_feed_energy_title(self, quest: Quest) -> str:
        return f'{Emojis.max_bolt} Feed {quest.arg:,} energy to your pets'

    @feed_energy.to_get_tickets
    def get_feed_energy_tickets(self, quest: Quest) -> int:
        return quest.arg // 50

    feed_specific_item = QuestTemplate(key='feed_specific_item', category=QuestCategory.feed)

    @feed_specific_item.to_generate
    async def generate_feed_specific_item_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        item = random.choice([
            item for item in walk_collection(Items, Item) if item.energy and item.rarity <= ItemRarity.rare
        ])
        coins = self._feed_coins_worth(slot)
        arg = max(1, coins // item.sell)

        return Quest(record, template, slot, arg, item.key)

    @feed_specific_item.to_get_title
    def get_feed_specific_item_title(self, quest: Quest) -> str:
        item = get_by_key(Items, quest.extra)
        return f'{item.emoji} Feed {item.quantify(quest.arg)} to your pets'

    @feed_specific_item.to_get_tickets
    def get_feed_specific_item_tickets(self, quest: Quest) -> int:
        item = get_by_key(Items, quest.extra)
        return max(1, quest.arg * item.sell // 500)

    catch_fish = QuestTemplate(key='catch_fish', category=QuestCategory.fish)

    @catch_fish.to_generate
    async def generate_catch_fish_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires catching fish."""
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(4, 8) * 5
            case QuestSlot.recurring_mid:
                arg = random.randint(6, 20) * 10
            case QuestSlot.recurring_hard:
                arg = random.randint(40, 80) * 10
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(4, 8) * 10
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @catch_fish.to_get_title
    def get_catch_fish_title(self, quest: Quest) -> str:
        return f'{Items.fishing_pole.emoji} Catch {quest.arg:,} fish'

    @catch_fish.to_get_tickets
    def get_catch_fish_tickets(self, quest: Quest) -> int:
        return quest.arg // 5 * 7

    catch_specific_fish = QuestTemplate(key='catch_specific_fish', category=QuestCategory.fish)

    @catch_specific_fish.to_generate
    async def generate_catch_specific_fish_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        match slot:
            case QuestSlot.recurring_easy:
                choices = {
                    ItemRarity.common: random.randint(2, 4) * 10,
                    ItemRarity.uncommon: random.randint(2, 4) * 3,
                }
            case QuestSlot.recurring_mid:
                choices = {
                    ItemRarity.common: random.randint(5, 9) * 15,
                    ItemRarity.uncommon: random.randint(6, 10) * 3,
                    ItemRarity.rare: random.randint(2, 4) * 3,
                }
            case QuestSlot.recurring_hard:
                choices = {
                    ItemRarity.common: random.randint(8, 16) * 15,
                    ItemRarity.uncommon: random.randint(8, 15) * 6,
                    ItemRarity.rare: random.randint(6, 10) * 3,
                    ItemRarity.epic: random.randint(2, 3) * 3,
                    ItemRarity.legendary: random.randint(1, 2),
                }
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                choices = {
                    ItemRarity.common: random.randint(3, 5) * 10,
                    ItemRarity.uncommon: random.randint(3, 5) * 2,
                }
            case _:
                raise ValueError(f'Invalid quest type: {slot}')

        rarity = random.choice(list(choices.keys()))
        arg = choices[rarity]

        item = random.choice([
            item for item in walk_collection(Items, Item)
            if item.rarity is rarity and item.type is ItemType.fish
        ])
        return Quest(record, template, slot, arg, item.key)

    @catch_specific_fish.to_get_title
    def get_catch_specific_fish_title(self, quest: Quest) -> str:
        item = get_by_key(Items, quest.extra)
        return f'{item.emoji} Catch {item.quantify(quest.arg)}'

    @catch_specific_fish.to_get_tickets
    def get_catch_specific_fish_tickets(self, quest: Quest) -> int:
        item = get_by_key(Items, quest.extra)
        match item.rarity:
            case ItemRarity.common:
                return quest.arg // 4
            case ItemRarity.uncommon:
                return quest.arg
            case ItemRarity.rare:
                return quest.arg * 3
            case ItemRarity.epic:
                return quest.arg * 10
            case ItemRarity.legendary:
                return quest.arg * 75

    dig_to_depth = QuestTemplate(key='dig_to_depth', category=QuestCategory.dig)

    @dig_to_depth.to_generate
    async def generate_dig_to_depth_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires digging to a specific depth."""
        match slot:  # TODO adjust based on biome
            case QuestSlot.recurring_easy:
                arg = random.randint(40 // 5, 60 // 5) * 5
            case QuestSlot.recurring_mid:
                arg = random.randint(80 // 5, 110 // 5) * 5
            case QuestSlot.recurring_hard:
                arg = random.randint(125 // 5, 140 // 5) * 5
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(60 // 5, 80 // 5) * 5
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @dig_to_depth.to_get_title
    def get_dig_to_depth_title(self, quest: Quest) -> str:
        return f'{Items.shovel.emoji} Dig {quest.arg:,} meters deep'

    @dig_to_depth.to_get_tickets
    def get_dig_to_depth_tickets(self, quest: Quest) -> int:
        match quest.slot:
            case QuestSlot.recurring_easy:
                return quest.arg // 10
            case QuestSlot.recurring_mid:
                return quest.arg // 7 * 2
            case QuestSlot.recurring_hard:
                return quest.arg // 5 * 4
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                return quest.arg // 7
            case _:
                raise ValueError(f'Invalid quest type: {quest.slot}')

    dig_coins = QuestTemplate(key='dig_coins', category=QuestCategory.dig)

    @dig_coins.to_generate
    async def generate_dig_coins_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires digging coins."""
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(3, 8) * 500
            case QuestSlot.recurring_mid:
                arg = random.randint(6, 20) * 1000
            case QuestSlot.recurring_hard:
                arg = random.randint(30, 60) * 1000
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(5, 12) * 1000
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @dig_coins.to_get_title
    def get_dig_coins_title(self, quest: Quest) -> str:
        return f'{Emojis.coin} Collect {quest.arg:,} coins while digging'

    @dig_coins.to_get_tickets
    def get_dig_coins_tickets(self, quest: Quest) -> int:
        return quest.arg // 500

    dig_items = QuestTemplate(key='dig_items', category=QuestCategory.dig)  # NON DIRT ITEMS

    @dig_items.to_generate
    async def generate_dig_items_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(2, 4) * 5
            case QuestSlot.recurring_mid:
                arg = random.randint(4, 8) * 10
            case QuestSlot.recurring_hard:
                arg = random.randint(10, 20) * 10
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(4, 8) * 5
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @dig_items.to_get_title
    def get_dig_items_title(self, quest: Quest) -> str:
        return f'{Items.shovel.emoji} Find {quest.arg:,} non-dirt items while digging'

    @dig_items.to_get_tickets
    def get_dig_items_tickets(self, quest: Quest) -> int:
        return quest.arg // 5 * 3

    dig_ores = QuestTemplate(key='dig_ores', category=QuestCategory.dig)

    @dig_ores.to_generate
    async def generate_dig_ores_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(2, 4) * 3
            case QuestSlot.recurring_mid:
                arg = random.randint(4, 8) * 8
            case QuestSlot.recurring_hard:
                arg = random.randint(10, 20) * 8
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(4, 8) * 3
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @dig_ores.to_get_title
    def get_dig_ores_title(self, quest: Quest) -> str:
        return f'{Items.pickaxe.emoji} Mine {quest.arg:,} ores while digging'

    @dig_ores.to_get_tickets
    def get_dig_ores_tickets(self, quest: Quest) -> int:
        return quest.arg

    dig_single_item = QuestTemplate(key='dig_single_item', category=QuestCategory.dig)

    dig_stamina = QuestTemplate(key='dig_stamina', category=QuestCategory.dig)

    @staticmethod
    def _dig_stamina_arg(slot: QuestSlot) -> int:
        match slot:
            case QuestSlot.recurring_easy:
                return random.randint(8, 15) * 10
            case QuestSlot.recurring_mid:
                return random.randint(25, 55) * 10
            case QuestSlot.recurring_hard:
                return random.randint(80, 150) * 10
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                return random.randint(15, 25) * 10
            case _:
                raise ValueError(f'Invalid quest type: {slot}')

    @dig_stamina.to_generate
    async def generate_dig_stamina_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires digging stamina."""
        arg = self._dig_stamina_arg(slot)
        return Quest(record, template, slot, arg)

    @dig_stamina.to_get_title
    def get_dig_stamina_title(self, quest: Quest) -> str:
        return f'{Emojis.bolt} Use {quest.arg:,} stamina while digging'

    @dig_stamina.to_get_tickets
    def get_dig_stamina_tickets(self, quest: Quest) -> int:
        """Get the number of tickets for a dig stamina quest."""
        return quest.arg // 10

    dig_hp = QuestTemplate(key='dig_hp', category=QuestCategory.dig)
    
    @dig_hp.to_generate
    async def generate_dig_hp_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires digging HP."""
        arg = self._dig_stamina_arg(slot) * 3
        return Quest(record, template, slot, arg)

    @dig_hp.to_get_title
    def get_dig_hp_title(self, quest: Quest) -> str:
        return f'{Items.shovel.emoji} Deal {quest.arg:,} HP while digging'

    @dig_hp.to_get_tickets
    def get_dig_hp_tickets(self, quest: Quest) -> int:
        return quest.arg // 30

    work_successes = QuestTemplate(key='work_successes', category=QuestCategory.work)

    @work_successes.to_generate
    async def generate_work_successes_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(2, 4) * 2
            case QuestSlot.recurring_mid:
                arg = random.randint(6, 10) * 2
            case QuestSlot.recurring_hard:
                arg = random.randint(10, 15) * 4
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(3, 6) * 2
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @work_successes.to_get_title
    def get_work_successes_title(self, quest: Quest) -> str:
        return f'\U0001f4bc Work successfully {quest.arg:,} times'

    @work_successes.to_get_tickets
    def get_work_successes_tickets(self, quest: Quest) -> int:
        """Get the number of tickets for a work successes quest."""
        return quest.arg * 2

    work_raises = QuestTemplate(key='work_raises', category=QuestCategory.work)

    @work_raises.to_generate
    async def generate_work_raises_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires getting work raises."""
        match slot:
            case QuestSlot.recurring_easy:
                arg = 1
            case QuestSlot.recurring_mid:
                arg = random.randint(2, 3)
            case QuestSlot.recurring_hard:
                arg = random.randint(5, 7)
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = 1
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @work_raises.to_get_title
    def get_work_raises_title(self, quest: Quest) -> str:
        if quest.arg == 1:
            return '\U0001f4bc Get promoted at work'
        return f'\U0001f4bc Get promoted {quest.arg:,} times at work'

    @work_raises.to_get_tickets
    def get_work_raises_tickets(self, quest: Quest) -> int:
        return quest.arg * 10

    gamble_coins = QuestTemplate(key='gamble_coins', category=QuestCategory.casino)

    @staticmethod
    def _gamble_coins_arg(slot: QuestSlot) -> int:
        match slot:
            case QuestSlot.recurring_easy:
                return random.randint(10, 25) * 1000
            case QuestSlot.recurring_mid:
                return random.randint(30, 60) * 1000
            case QuestSlot.recurring_hard:
                return random.randint(10, 30) * 10_000
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                return random.randint(25, 40) * 1000
            case _:
                raise ValueError(f'Invalid quest type: {slot}')

    @gamble_coins.to_generate
    async def generate_gamble_coins_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires gambling coins."""
        arg = self._gamble_coins_arg(slot)
        return Quest(record, template, slot, arg)

    @gamble_coins.to_get_title
    def get_gamble_coins_title(self, quest: Quest) -> str:
        return f'\U0001f3b2 Bet {quest.arg:,} coins at the casino'

    @gamble_coins.to_get_tickets
    def get_gamble_coins_tickets(self, quest: Quest) -> int:
        return quest.arg // 2000

    gamble_wins = QuestTemplate(key='gamble_wins', category=QuestCategory.casino)

    @gamble_wins.to_generate
    async def generate_gamble_wins_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires winning at the casino."""
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(5, 10)
            case QuestSlot.recurring_mid:
                arg = random.randint(25, 35)
            case QuestSlot.recurring_hard:
                arg = random.randint(100, 200)
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(15, 25)
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @gamble_wins.to_get_title
    def get_gamble_wins_title(self, quest: Quest) -> str:
        return f'\U0001f3b2 Win {quest.arg:,} games at the casino'

    @gamble_wins.to_get_tickets
    def get_gamble_wins_tickets(self, quest: Quest) -> int:
        return quest.arg * 2 // 3

    gamble_wins_specific_command = QuestTemplate(key='gamble_wins_specific_command', category=QuestCategory.casino)

    @gamble_wins_specific_command.to_generate
    async def generate_gamble_wins_specific_command_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        cmd = weighted_choice(dict(blackjack=20, mines=20, poker=15, slots=10))
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(5, 10)
            case QuestSlot.recurring_mid:
                arg = random.randint(20, 30)
            case QuestSlot.recurring_hard:
                arg = random.randint(50, 100)
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(15, 25)
            case _:
                raise ValueError(f'Invalid quest type: {slot}')

        if cmd == 'poker':
            arg //= 5

        return Quest(record, template, slot, arg, cmd)

    @gamble_wins_specific_command.to_get_title
    def get_gamble_wins_specific_command_title(self, quest: Quest) -> str:
        mapping = dict(
            blackjack=f'Win {quest.arg:,} games of blackjack',
            mines=f'Win {quest.arg:,} games of mines',
            poker=f'Win {quest.arg:,} rounds of poker',
            slots=f'Win {quest.arg:,} rounds of slots',
        )
        return f'\U0001f3b2 {mapping[quest.extra]}'

    @gamble_wins_specific_command.to_get_tickets
    def get_gamble_wins_specific_command_tickets(self, quest: Quest) -> int:
        """Get the number of tickets for a gamble wins specific command quest."""
        base = quest.arg * 3 // 2
        if quest.extra == 'poker':
            base *= 5
        return base

    gamble_profit = QuestTemplate(key='gamble_profit', category=QuestCategory.casino)

    @gamble_profit.to_generate
    async def generate_gamble_profit_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        arg = self._gamble_coins_arg(slot) * 2 // 3
        return Quest(record, template, slot, arg)

    @gamble_profit.to_get_title
    def get_gamble_profit_title(self, quest: Quest) -> str:
        return f'{Emojis.coin} Profit {quest.arg:,} coins from gambling'

    @gamble_profit.to_get_tickets
    def get_gamble_profit_tickets(self, quest: Quest) -> int:
        """Get the number of tickets for a gamble profit quest."""
        return quest.arg * 3 // 4000

    gamble_profit_specific_command = QuestTemplate(key='gamble_profit_specific_command', category=QuestCategory.casino)

    @gamble_profit_specific_command.to_generate
    async def generate_gamble_profit_specific_command_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        cmd = weighted_choice(dict(blackjack=20, mines=20, poker=15, slots=10))
        arg = self._gamble_coins_arg(slot) * 4 // 5
        return Quest(record, template, slot, arg, cmd)

    @gamble_profit_specific_command.to_get_title
    def get_gamble_profit_specific_command_title(self, quest: Quest) -> str:
        mapping = dict(
            blackjack=f'Profit {quest.arg:,} coins from playing blackjack',
            mines=f'Profit {quest.arg:,} coins from playing mines',
            poker=f'Profit {quest.arg:,} coins from playing poker',
            slots=f'Profit {quest.arg:,} coins from playing slots',
        )
        return f'{Emojis.coin} {mapping[quest.extra]}'

    @gamble_profit_specific_command.to_get_tickets
    def get_gamble_profit_specific_command_tickets(self, quest: Quest) -> int:
        return quest.arg * 5 // 8000

    harvest_crops = QuestTemplate(key='harvest_crops', category=QuestCategory.farm)

    @staticmethod
    async def _get_harvest_arg(slot: QuestSlot, record: UserRecord) -> tuple[int, int]:
        crops = await record.crop_manager.wait()
        count = max(sum(crop.crop is not None for crop in crops.cached.values()), 5)
        match slot:
            case QuestSlot.recurring_easy:
                return random.randint(2, 4) * count, count
            case QuestSlot.recurring_mid:
                return random.randint(15, 25) * count, count
            case QuestSlot.recurring_hard:
                return random.randint(50, 100) * count, count
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                return random.randint(5, 10) * count, count
            case _:
                raise ValueError(f'Invalid quest type: {slot}')

    @harvest_crops.to_generate
    async def generate_harvest_crops_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires harvesting crops."""
        arg, count = await self._get_harvest_arg(slot, record)
        return Quest(record, template, slot, arg, str(count))

    @harvest_crops.to_get_title
    def get_harvest_crops_title(self, quest: Quest) -> str:
        return f'{Items.corn.emoji} Harvest {quest.arg:,} crops'

    @harvest_crops.to_get_tickets
    def get_harvest_crops_tickets(self, quest: Quest) -> int:
        return quest.arg // int(quest.extra) * 3 // 2

    water_crops = QuestTemplate(key='water_crops', category=QuestCategory.farm)
    
    @water_crops.to_generate
    async def generate_water_crops_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        arg, count = await self._get_harvest_arg(slot, record)
        return Quest(record, template, slot, arg // 2, str(count))

    @water_crops.to_get_title
    def get_water_crops_title(self, quest: Quest) -> str:
        return f'{Items.watering_can.emoji} Water {quest.arg:,} crops'

    @water_crops.to_get_tickets
    def get_water_crops_tickets(self, quest: Quest) -> int:
        return quest.arg // int(quest.extra) * 3

    plant_crops = QuestTemplate(key='plant_crops', category=QuestCategory.farm)

    @plant_crops.to_generate
    async def generate_plant_crops_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(5, 10) * 2
            case QuestSlot.recurring_mid:
                arg = random.randint(4, 10) * 5
            case QuestSlot.recurring_hard:
                arg = random.randint(10, 20) * 10
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(4, 8) * 5
            case _:
                raise ValueError(f'Invalid quest type: {slot}')

        return Quest(record, template, slot, arg)

    @plant_crops.to_get_title
    def get_plant_crops_title(self, quest: Quest) -> str:
        return f'{Items.corn.emoji} Plant {quest.arg:,} crops'

    @plant_crops.to_get_tickets
    def get_plant_crops_tickets(self, quest: Quest) -> int:
        return quest.arg // 3

    rob_coins = QuestTemplate(key='rob_coins', category=QuestCategory.rob)

    @rob_coins.to_generate
    async def generate_rob_coins_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires robbing coins."""
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(4, 10) * 500
            case QuestSlot.recurring_mid:
                arg = random.randint(10, 25) * 1000
            case QuestSlot.recurring_hard:
                arg = random.randint(70, 150) * 1000
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(10, 20) * 1000
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @rob_coins.to_get_title
    def get_rob_coins_title(self, quest: Quest) -> str:
        return f'{Items.padlock.emoji} Rob {quest.arg:,} coins from other players'

    @rob_coins.to_get_tickets
    def get_rob_coins_tickets(self, quest: Quest) -> int:
        return quest.arg // 750

    rob_successes = QuestTemplate(key='rob_successes', category=QuestCategory.rob)

    @rob_successes.to_generate
    async def generate_rob_successes_quest(self, template: QuestTemplate, slot: QuestSlot, record: UserRecord) -> Quest:
        """Generate a quest that requires successful robberies."""
        match slot:
            case QuestSlot.recurring_easy:
                arg = random.randint(1, 3)
            case QuestSlot.recurring_mid:
                arg = random.randint(6, 12)
            case QuestSlot.recurring_hard:
                arg = random.randint(20, 30)
            case QuestSlot.daily_1 | QuestSlot.daily_2:
                arg = random.randint(2, 4)
            case _:
                raise ValueError(f'Invalid quest type: {slot}')
        return Quest(record, template, slot, arg)

    @rob_successes.to_get_title
    def get_rob_successes_title(self, quest: Quest) -> str:
        if quest.arg == 1:
            return f'{Items.padlock.emoji} Successfully rob a player'
        return f'{Items.padlock.emoji} Successfully rob players {quest.arg:,} times'

    @rob_successes.to_get_tickets
    def get_rob_successes_tickets(self, quest: Quest) -> int:
        return quest.arg * 3

    @classmethod
    def walk_templates(cls, category: QuestCategory) -> Iterable[QuestTemplate]:
        """Walk through all templates of a specific quest category."""
        for template in cls.__dict__.values():
            if isinstance(template, QuestTemplate) and template.category == category:
                yield template


_INST = QuestTemplates()

QUEST_PASS_CURVE = CubicCurve(0.015, 0.095, 4.89, 10, precision=5)
QUEST_PASS_REWARDS: list[Reward] = [
    Reward(coins=10_000),  # 1
    Reward(items={Items.uncommon_crate: 1}),
    Reward(coins=10_000),
    Reward(items={Items.rare_crate: 1}),
    Reward(coins=15_000),  # 5
    Reward(items={Items.epic_crate: 1}),
    Reward(coins=20_000),
    Reward(items={Items.rare_crate: 1}),
    Reward(coins=25_000),
    Reward(items={Items.legendary_crate: 1}),  # 10
    Reward(coins=30_000),
    Reward(items={Items.epic_crate: 1}),
    Reward(coins=35_000),
    Reward(items={Items.uncommon_crate: 1}),
    Reward(coins=40_000),  # 15
    Reward(items={Items.legendary_crate: 1}),
    Reward(coins=50_000),
    Reward(items={Items.epic_crate: 1}),
    Reward(coins=60_000),
    Reward(items={Items.mythic_crate: 1}),  # 20
    Reward(coins=70_000),
    Reward(items={Items.legendary_crate: 1}),
    Reward(coins=80_000),
    Reward(items={Items.mythic_crate: 1}),
    Reward(coins=90_000),  # 25
    Reward(items={Items.legendary_crate: 1}),
    Reward(coins=100_000),
    Reward(items={Items.mythic_crate: 1}),
    Reward(coins=150_000),
    Reward(items={Items.plasma_shovel: 1}),  # 30
]


def reward_for_achieving_tier(tier: int, /) -> Reward:
    if 1 <= tier <= len(QUEST_PASS_REWARDS):
        return QUEST_PASS_REWARDS[tier - 1]
    return Reward()
