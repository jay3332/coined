from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from functools import total_ordering
from typing import Callable

from app.util.common import BaseCurve, ExponentialCurve, walk_collection
from config import Emojis


@total_ordering
class PetRarity(Enum):
    common = 0
    uncommon = 1
    rare = 2
    epic = 3
    legendary = 4
    mythic = 5
    special = 6

    @property
    def emoji(self) -> str:
        return getattr(Emojis.Rarity, self.name)

    @property
    def singular(self) -> str:
        if self in (PetRarity.uncommon, PetRarity.epic):
            return 'an'
        return 'a'

    def __lt__(self, other: PetRarity) -> bool:
        if not isinstance(other, PetRarity):
            return NotImplemented
        return self.value < other.value

    def __eq__(self, other: PetRarity) -> bool:
        if not isinstance(other, PetRarity):
            return NotImplemented
        return self.value == other.value

    def __hash__(self) -> int:
        return hash(self.value)


def generate_pet_weights(*, none: float = 0.0, **rarity_weights: float) -> dict[Pet, float]:
    groups = defaultdict(list)
    for pet in walk_collection(Pets, Pet):
        if pet.rarity.name not in rarity_weights:
            continue
        groups[pet.rarity].append(pet)

    weights = {
        pet: rarity_weights[rarity.name] / len(pets)
        for rarity, pets in groups.items()
        for pet in pets
    }
    weights[None] = none
    return weights


@dataclass
class Pet:
    name: str
    key: str
    emoji: str
    rarity: PetRarity
    description: str
    energy_per_minute: float
    max_energy: int
    benefit: Callable[[int], str]  # Passive
    abilities: Callable[[int], str] | None = None  # Active
    # Leveling
    leveling_curve: BaseCurve = ExponentialCurve(50, 1.15, precision=10)
    max_level: int = 200
    jumbo_emoji: list[str] = field(default_factory=list)  # large emoji made up of 4 emojis
    # Grammar
    singular: str = None
    plural: str = None

    def __post_init__(self) -> None:
        if not self.singular:
            self.singular = 'an' if self.name.lower().startswith(tuple('aeiou')) else 'a'
        if not self.plural:
            self.plural = self.name + 's'

    @property
    def display(self) -> str:
        return f'{self.emoji} {self.name}'

    def jumbo_display(self, line_1: str, line_2: str) -> str:
        """Returns a formatted string for the jumbo display."""
        if not self.jumbo_emoji:
            return f'{self.emoji} {line_1}\n{Emojis.space} {line_2}'

        a, b, c, d = self.jumbo_emoji
        return f'{a + b}  {line_1}\n{c + d}  {line_2}'

    def full_abilities(self, level: int) -> str:
        if self.abilities is None:
            return self.benefit(level)
        return f'{self.benefit(level)}\n{self.abilities(level)}'

    def __hash__(self) -> int:
        return hash(self.key)

    def __eq__(self, other) -> bool:
        if not isinstance(other, self.__class__):
            return False
        return self.key == other.key


class Pets:
    dog = Pet(
        name='Dog',
        key='dog',
        emoji='<:dog:1379662817308971105>',
        rarity=PetRarity.common,
        description="A descendant of the wolf and a man's best friend.",
        energy_per_minute=0.05,
        max_energy=100,
        benefit=lambda level: (
            f'- +{1 + level * 0.3:g}% coins from begging\n'
            f'- +{1 + level * 0.4:g}% chance to find items while searching'
        ),
        jumbo_emoji=[
            '<:dog_1:1379687630807109763>',
            '<:dog_2:1379687631973257267>',
            '<:dog_3:1379687633235480606>',
            '<:dog_4:1379687635118718986>',
        ],
    )

    cat = Pet(
        name='Cat',
        key='cat',
        emoji='<:cat:1379662815299768361>',
        rarity=PetRarity.common,
        description='A small, domesticated, carnivorous mammal.',
        energy_per_minute=0.05,
        max_energy=100,
        benefit=lambda level: (
            f'- +{1 + level * 0.2:g}% weight on finding rarer items when fishing\n'
            f'- +{0.8 + level * 0.4:g}% global XP multiplier'
        ),
        jumbo_emoji=[
            '<:cat_1:1379687617678807100>',
            '<:cat_2:1379687618731708431>',
            '<:cat_3:1379687620291985440>',
            '<:cat_4:1379687621495881871>',
        ],
    )

    bird = Pet(
        name='Bird',
        key='bird',
        emoji='\U0001f426',
        rarity=PetRarity.common,
        description='Birb. These can fly, in case you were clueless.',
        energy_per_minute=0.05,
        max_energy=100,
        benefit=lambda level: (
            f'- +{1 + level * 0.4:g}% global coin multiplier'
        ),
        jumbo_emoji=[
            '<:bird_1:1379687604072616006>',
            '<:bird_2:1379687605817315348>',
            '<:bird_3:1379687606903771177>',
            '<:bird_4:1379687608220782624>',
        ],
    )

    bunny = Pet(
        name='Bunny',
        key='bunny',
        emoji='\U0001f430',
        rarity=PetRarity.common,
        description='A mammal with long ears that hops around.',
        energy_per_minute=0.05,
        max_energy=100,
        benefit=lambda level: (
            f'- +{1 + level * 0.5:g}% global XP multiplier'
        ),
        jumbo_emoji=[
            '<:bunny_1:1379687610749947965>',
            '<:bunny_2:1379687611693531187>',
            '<:bunny_3:1379687612788510783>',
            '<:bunny_4:1379687614558244939>',
        ],
        plural='Bunnies',
    )

    hamster = Pet(
        name='Hamster',
        key='hamster',
        emoji='\U0001f439',
        rarity=PetRarity.common,
        description='A small rodent that is often kept as a pet.',
        energy_per_minute=0.12,
        max_energy=100,
        benefit=lambda level: (
            f'- +{2 + level * 0.2:g}% more coins from digging\n'
            f'- +{0.5 + level * 0.1:g}% money back when buying items'
        ),
        jumbo_emoji=[
            '<:hamster_1:1379687652332277771>',
            '<:hamster_2:1379687653447962644>',
            '<:hamster_3:1379687656212004894>',
            '<:hamster_4:1379687657285750794>',
        ],
    )

    mouse = Pet(
        name='Mouse',
        key='mouse',
        emoji='\U0001f42d',
        rarity=PetRarity.common,
        description='A small rodent that likes cheese.',
        energy_per_minute=0.05,
        max_energy=100,
        benefit=lambda level: (
            f'- +{5 + level * 0.5:g}% XP multiplier increase from eating cheese\n'
            f'- +{1 + level * 0.4:g}% chance to find items while searching'
        ),
        jumbo_emoji=[
            '<:mouse_1:1379687668044271657>',
            '<:mouse_2:1379687669319204915>',
            '<:mouse_3:1379687671416229918>',
            '<:mouse_4:1379687673433952349>',
        ],
        plural='Mice',
    )

    duck = Pet(
        name='Duck',
        key='duck',
        emoji='\U0001f986',
        rarity=PetRarity.uncommon,
        description='Waddle waddle and then they go quack',
        energy_per_minute=0.1,
        max_energy=200,
        benefit=lambda level: (
            f'- +{2 + level * 0.5:g}% profit from working\n'
            f'- +{1 + level * 0.25:g}% chance to get rarer crates when claiming hourly crates\n'
            f'- +{1 + level * 0.3:g}% global XP multiplier'
        ),
        jumbo_emoji=[
            '<:duck_1:1379687638063124540>',
            '<:duck_2:1379687639715676260>',
            '<:duck_3:1379687640843943968>',
            '<:duck_4:1379687642290978936>',
        ],
    )

    bee = Pet(
        name='Bee',
        key='bee',
        emoji='\U0001f41d',
        rarity=PetRarity.uncommon,
        description='A flying insect that pollinates flowers and makes honey.',
        energy_per_minute=0.1,
        max_energy=300,
        benefit=lambda level: (
            f'- +{1 + level * 0.4:g}% faster harvesting crops\n'
            f'- {2 + level * 0.25:g}% chance to sting someone when they try robbing you'
        ),
        abilities=lambda level: (
            f'- Produce honey (1 per hour) with `.honey` ({Emojis.bolt} 60)'
        ),
        jumbo_emoji=[
            '<:bee_1:1379687597332369489>',
            '<:bee_2:1379687598909554780>',
            '<:bee_3:1379687600092217426>',
            '<:bee_4:1379687601539387402>',
        ],
    )

    tortoise = Pet(
        name='Tortoise',
        key='tortoise',
        emoji='\U0001f422',
        rarity=PetRarity.uncommon,
        description='Slow and steady wins the race head ahh',
        energy_per_minute=0.03,
        max_energy=500,
        benefit=lambda level: (
            f'- +{1 + level * 0.5:g}% chance to find rarer items when fishing\n'
            f'- +{2 + level * 0.5:g}% Global XP multiplier'
        ),
        jumbo_emoji=[
            '<:tortoise_1:1379687694262730813>',
            '<:tortoise_2:1379687695667826788>',
            '<:tortoise_3:1379687696984969276>',
            '<:tortoise_4:1379687697819500595>',
        ],
    )

    weasel = Pet(
        name='Weasel',
        key='weasel',
        emoji='<:weasel:1376726983836438588>',
        rarity=PetRarity.uncommon,
        description='Small and slippery, the weasel can sneak through tough spots.',
        energy_per_minute=0.1,
        max_energy=300,
        benefit=lambda level: (
            f'- +{2 + level * 0.5:g}% coins from search and crime\n'
            f'- -{1 + level * 0.5:g}% chance to get caught when committing crimes\n'
            f'- +{1 + level * 0.5:g}% global coin multiplier'
        ),
        jumbo_emoji=[
            '<:weasel_1:1379687700902449174>',
            '<:weasel_2:1379687702110408805>',
            '<:weasel_3:1379687703557312512>',
            '<:weasel_4:1379687704668930118>',
        ],
    )

    cow = Pet(
        name='Cow',
        key='cow',
        emoji='\U0001f42e',
        rarity=PetRarity.rare,
        description='A large mammal used for producing milk (and steak of course).',
        energy_per_minute=0.12,
        max_energy=500,
        benefit=lambda level: (
            f'- +{2 + level * 0.5:g}% more coins from beg, search, and crime\n'
            f'- +{2 + level * 0.6:g}% global XP multiplier'
        ),
        abilities=lambda level: (
            f'- Produce milk (1 per hour) with `.milk` ({Emojis.bolt} 100)'
        ),
        jumbo_emoji=[
            '<:cow_1:1379687624012337214>',
            '<:cow_2:1379687625266298884>',
            '<:cow_3:1379687626449354753>',
            '<:cow_4:1379687628223418480>',
        ],
    )

    panda = Pet(
        name='Panda',
        key='panda',
        emoji='\U0001f43c',
        rarity=PetRarity.rare,
        description='Celebrated for their unique black-and-white appearance, bamboo shoots make up most of their diet.',
        energy_per_minute=0.1,
        max_energy=400,
        benefit=lambda level: (
            f'- +{2 + level}% global coin multiplier\n'
            f'- +{2 + level * 0.5:g}% chance to find rarer wood when chopping trees'
        ),
        jumbo_emoji=[
            '<:panda_1:1379687676776546354>',
            '<:panda_2:1379687677905076284>',
            '<:panda_3:1379687680601886752>',
            '<:panda_4:1379687682523005038>',
        ],
    )

    armadillo = Pet(
        name='Armadillo',
        key='armadillo',
        emoji='<:armadillo:1376727000873566228>',
        rarity=PetRarity.rare,
        description='Boasts a tough and sturdy shell, making it a formidable defender.',
        energy_per_minute=0.1,
        max_energy=300,
        benefit=lambda level: (
            f'- +{1 + level} stamina when digging and diving\n'
            f'- +{2 + level * 0.5:g}% chance to find rarer items when searching\n'
            f'- +{2 + level * 0.6:g}% global XP multiplier'
        ),
        jumbo_emoji=[
            '<:armadillo_1:1379687589551800401>',
            '<:armadillo_2:1379687591183388753>',
            '<:armadillo_3:1379687592689139842>',
            '<:armadillo_4:1379687593817669672>',
        ],
    )

    fox = Pet(
        name='Fox',
        key='fox',
        emoji='\U0001f98a',
        rarity=PetRarity.epic,
        description='A small to medium-sized omnivorous mammal.',
        energy_per_minute=0.1,
        max_energy=350,
        benefit=lambda level: (
            f'- +{5 + level:g}% global coin multiplier\n'
            f'- +{2 + level:g}% global bank space multiplier\n'
            f'- +{2 + level * 0.5:g}% chance to find rarer items when searching'
        ),
        abilities=lambda level: (
            f'- Produce berries (1 per hour) with `.berries` ({Emojis.bolt} 200)'
        ),
        jumbo_emoji=[
            '<:fox_1:1379687644786589737>',
            '<:fox_2:1379687646267445281>',
            '<:fox_3:1379687647856824341>',
            '<:fox_4:1379687649002131457>',
        ],
        plural='Foxes',
    )

    jaguar = Pet(
        name='Jaguar',
        key='jaguar',
        emoji='<:jaguar:1376727015591510067>',
        rarity=PetRarity.epic,
        description='A fierce jungle predator that strikes swiftly and efficiently.',
        energy_per_minute=0.2,
        max_energy=500,
        benefit=lambda level: (
            f'- +{5 + level:g}% global coin multiplier\n'
            f'- +{3 + level:g}% chance to find rarer wood when chopping trees\n'
            f'- +{1 + level * 0.5:g}% weight on catching rarer pets when hunting\n'
            f'- +{5 + level:g}% more HP dealt during digging, diving, and combat'
        ),
        jumbo_emoji=[
            '<:jaguar_1:1379687659982815314>',
            '<:jaguar_2:1379687661756874873>',
            '<:jaguar_3:1379687663258304740>',
            '<:jaguar_4:1379687665254924380>',
        ],
    )

    tiger = Pet(
        name='Tiger',
        key='tiger',
        emoji='<:tiger:1376727023820472330>',
        rarity=PetRarity.legendary,
        description='Majestic and powerful, the tiger is a symbol of dominance and strength.',
        energy_per_minute=0.5,
        max_energy=800,
        benefit=lambda level: (
            f'- +{8 + level * 1.5:g}% global coin multiplier\n'
            f'- +{10 + level * 2:g}% more HP dealt during digging, diving, and combat\n'
            f'- +{2 + level:g}% weight on catching rarer pets when hunting\n'
            f'- +{2 + level * 2} stamina when digging and diving'
        ),
        jumbo_emoji=[
            '<:tiger_1:1379687686268518403>',
            '<:tiger_2:1379687688004702308>',
            '<:tiger_3:1379687689397473372>',
            '<:tiger_4:1379687690404102185>',
        ],
    )


DEFAULT_PET_WEIGHTS: dict[Pet, float] = generate_pet_weights(
    none=20,
    common=68,
    uncommon=7,
    rare=4,
    epic=0.8,
    legendary=0.18,
    mythic=0.02,
)
