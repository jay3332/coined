from __future__ import annotations

from collections import defaultdict
from enum import Enum
from functools import total_ordering
from typing import Callable, NamedTuple

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


class Pet(NamedTuple):
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

    @property
    def display(self) -> str:
        return f'{self.emoji} {self.name}'

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
        emoji='<:dog:1134641292245205123>',
        rarity=PetRarity.common,
        description="A descendant of the wolf and a man's best friend.",
        energy_per_minute=0.05,
        max_energy=100,
        benefit=lambda level: (
            f'- +{1 + level * 0.3:g}% coins from begging\n'
            f'- +{1 + level * 0.4:g}% chance to find items while searching'
        ),
    )

    cat = Pet(
        name='Cat',
        key='cat',
        emoji='<:cat:1134641341092089948>',
        rarity=PetRarity.common,
        description='A small, domesticated, carnivorous mammal.',
        energy_per_minute=0.05,
        max_energy=100,
        benefit=lambda level: (
            f'- +{1 + level * 0.2:g}% weight on finding rarer items when fishing\n'
            f'- +{0.8 + level * 0.4:g}% global XP multiplier'
        ),
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
        )
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
        )
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
        )
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
        )
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
        )
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
        )
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
        )
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
