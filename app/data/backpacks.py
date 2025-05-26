from __future__ import annotations

from typing import NamedTuple


class Backpack(NamedTuple):
    name: str
    key: str
    emoji: str
    description: str
    capacity: int  # storage units
    price: int
    min_prestige: int = 0

    @property
    def display(self) -> str:
        return f'{self.emoji} {self.name}'


class Backpacks:
    standard_backpack = Backpack(
        name='Standard Backpack',
        key='standard_backpack',
        emoji='<:standard_backpack:1375671517039558706>',
        description='A standard backpack, good for most things.',
        capacity=50,
        price=0,
    )

    suitcase = Backpack(
        name='Suitcase',
        key='suitcase',
        emoji='<:suitcase:1375671685914562560>',
        description='The standard glossy blue suitcase',
        capacity=100,
        price=100_000,
    )

    duffel_bag = Backpack(
        name='Duffel Bag',
        key='duffel_bag',
        emoji='<:duffel_bag:1375674850248757388>',
        description='A reinforced duffel bag made designed for large capacities.',
        capacity=200,
        price=500_000,
    )
