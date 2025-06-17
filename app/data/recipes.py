from typing import NamedTuple

from app.data.items import Item, Items


class Recipe(NamedTuple):
    key: str
    name: str
    emoji: str
    description: str

    price: int
    ingredients: dict[Item, int]
    result: dict[Item, int]

    def __hash__(self) -> int:
        return hash(self.key)


class Recipes:
    durable_fishing_pole = Recipe(
        key="durable_fishing_pole",
        name="Durable Fishing Pole",
        description=Items.durable_fishing_pole.description,
        emoji=Items.durable_fishing_pole.emoji,
        price=10_000,
        ingredients={
            Items.fishing_pole: 3,
            Items.iron: 3,
        },
        result={
            Items.durable_fishing_pole: 1,
        },
    )

    golden_fishing_pole = Recipe(
        key="golden_fishing_pole",
        name="Golden Fishing Pole",
        description=Items.golden_fishing_pole.description,
        emoji=Items.golden_fishing_pole.emoji,
        price=60_000,
        ingredients={
            Items.fishing_pole: 5,
            Items.gold: 3,
        },
        result={
            Items.golden_fishing_pole: 1,
        },
    )

    diamond_fishing_pole = Recipe(
        key="diamond_fishing_pole",
        name="Diamond Fishing Pole",
        description=Items.diamond_fishing_pole.description,
        emoji=Items.diamond_fishing_pole.emoji,
        price=750_000,
        ingredients={
            Items.fishing_pole: 10,
            Items.diamond: 3,
        },
        result={
            Items.diamond_fishing_pole: 1,
        },
    )

    durable_shovel = Recipe(
        key="durable_shovel",
        name="Durable Shovel",
        description=Items.durable_shovel.description,
        emoji=Items.durable_shovel.emoji,
        price=10_000,
        ingredients={
            Items.shovel: 3,
            Items.iron: 3,
        },
        result={
            Items.durable_shovel: 1,
        },
    )

    golden_shovel = Recipe(
        key="golden_shovel",
        name="Golden Shovel",
        description=Items.golden_shovel.description,
        emoji=Items.golden_shovel.emoji,
        price=80_000,
        ingredients={
            Items.shovel: 5,
            Items.gold: 3,
        },
        result={
            Items.golden_shovel: 1,
        },
    )

    diamond_shovel = Recipe(
        key="diamond_shovel",
        name="Diamond Shovel",
        description=Items.diamond_shovel.description,
        emoji=Items.diamond_shovel.emoji,
        price=800_000,
        ingredients={
            Items.shovel: 10,
            Items.diamond: 3,
        },
        result={
            Items.diamond_shovel: 1,
        },
    )

    durable_pickaxe = Recipe(
        key="durable_pickaxe",
        name="Durable Pickaxe",
        description=Items.durable_pickaxe.description,
        emoji=Items.durable_pickaxe.emoji,
        price=10_000,
        ingredients={
            Items.pickaxe: 3,
            Items.iron: 3,
        },
        result={
            Items.durable_pickaxe: 1,
        },
    )

    diamond_pickaxe = Recipe(
        key="diamond_pickaxe",
        name="Diamond Pickaxe",
        description=Items.diamond_pickaxe.description,
        emoji=Items.diamond_pickaxe.emoji,
        price=100_000,
        ingredients={
            Items.pickaxe: 3,
            Items.diamond: 3,
        },
        result={
            Items.diamond_pickaxe: 1,
        },
    )

    golden_net = Recipe(
        key="golden_net",
        name="Golden Net",
        description=Items.golden_net.description,
        emoji=Items.golden_net.emoji,
        price=80_000,
        ingredients={
            Items.net: 5,
            Items.gold: 3,
        },
        result={
            Items.golden_net: 1,
        },
    )

    diamond_net = Recipe(
        key="diamond_net",
        name="Diamond Net",
        description=Items.diamond_net.description,
        emoji=Items.diamond_net.emoji,
        price=800_000,
        ingredients={
            Items.net: 10,
            Items.diamond: 3,
        },
        result={
            Items.diamond_net: 1,
        },
    )

    fish_bait = Recipe(
        key="fish_bait",
        name="Fish Bait",
        description=Items.fish_bait.description,
        emoji=Items.fish_bait.emoji,
        price=50,
        ingredients={
            Items.worm: 3,
        },
        result={
            Items.fish_bait: 1,
        },
    )

    stick = Recipe(
        key="stick",
        name="Stick",
        description=Items.stick.description,
        emoji=Items.stick.emoji,
        price=10,
        ingredients={
            Items.wood: 2,
        },
        result={
            Items.stick: 1,
        },
    )

    sheet_of_paper = Recipe(
        key="sheet_of_paper",
        name="Sheet of Paper",
        description=Items.sheet_of_paper.description,
        emoji=Items.sheet_of_paper.emoji,
        price=5000,
        ingredients={
            Items.wood: 2,
            Items.banknote: 1,
        },
        result={
            Items.sheet_of_paper: 1,
        },
    )

    cigarette = Recipe(
        key="cigarette",
        name="Cigarette",
        description=Items.cigarette.description,
        emoji=Items.cigarette.emoji,
        price=7000,
        ingredients={
            Items.tobacco: 2,
            Items.cotton_ball: 2,
            Items.sheet_of_paper: 1,
        },
        result={
            Items.cigarette: 1,
        },
    )

    flour = Recipe(
        key="flour",
        name="Flour",
        description=Items.flour.description,
        emoji=Items.flour.emoji,
        price=150,
        ingredients={
            Items.wheat: 2,
        },
        result={
            Items.flour: 1,
        },
    )

    bread = Recipe(
        key="bread",
        name="Bread",
        description=Items.bread.description,
        emoji=Items.bread.emoji,
        price=200,
        ingredients={
            Items.flour: 2,
            Items.glass_of_water: 1,
        },
        result={
            Items.bread: 1,
        },
    )

    glass_of_water = Recipe(
        key="glass_of_water",
        name="Glass of Water",
        description=Items.glass_of_water.description,
        emoji=Items.glass_of_water.emoji,
        price=150,
        ingredients={
            Items.watering_can: 1,
            Items.cup: 1,
        },
        result={
            Items.glass_of_water: 1,
        },
    )

    cheese = Recipe(
        key="cheese",
        name="Cheese",
        description=Items.cheese.description,
        emoji=Items.cheese.emoji,
        price=1500,
        ingredients={
            Items.milk: 8,
        },
        result={
            Items.cheese: 1,
        },
    )
