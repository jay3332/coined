from __future__ import annotations

import random
from bisect import bisect_left
from typing import NamedTuple, TYPE_CHECKING

from PIL import Image, ImageDraw

from app.data.items import Item, Items
from app.util.common import executor_function, weighted_choices

if TYPE_CHECKING:
    from app.features.digging import RGB


class Layer(NamedTuple):
    depth: int
    items: dict[Item, float]
    dirt: Item
    dirt_color: RGB
    grain_color: RGB | list[RGB] | dict[RGB, float]
    grain_density: int = 7

    def __hash__(self) -> int:
        return hash(self.dirt.key)

    @executor_function
    def generate_dirt_sample(self, cell_width: int, grain_width: int) -> Image.Image:
        """Generate a list of dirt samples for the digging session."""
        image = Image.new('RGBA', (cell_width, cell_width), self.dirt_color)
        draw = ImageDraw.Draw(image)

        grain_color = self.grain_color
        if isinstance(grain_color, tuple):
            grain_color = [grain_color]  # type: ignore
        if isinstance(grain_color, list):
            grain_color = {color: 1 for color in grain_color}

        xy = []
        tolerance = grain_width * 3
        while len(xy) < self.grain_density:
            x = random.randint(grain_width, cell_width - grain_width * 2)
            y = random.randint(grain_width, cell_width - grain_width * 2)
            if any(abs(x - px) < tolerance and abs(y - py) < tolerance for px, py in xy):
                continue
            xy.append((x, y))

        for (x, y), color in zip(xy, weighted_choices(grain_color, k=self.grain_density)):
            draw.rectangle((x, y, x + grain_width, y + grain_width), fill=color)

        return image


class UnlockRequirements(NamedTuple):
    level: int
    prestige: int = 0
    price: int = 0


class Biome(NamedTuple):
    key: str
    name: str
    description: str
    unlock_requirements: UnlockRequirements
    entry_price: int  # Price to enter the biome to dig, charged every time!
    backdrop_path: str
    layers: list[Layer]
    ore_hp_multiplier: float = 1.0

    def get_layer(self, y: int) -> Layer:
        """Returns the layer for the given GRID y coordinate."""
        if y < 1:
            return self.layers[0]
        return self.layers[bisect_left(self.layers, y, key=lambda layer: layer.depth) - 1]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Biome):
            return False
        return self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)


class Biomes:
    backyard = Biome(
        key='backyard',
        name='Backyard',
        description='A familiar place, which you can return to every time',
        unlock_requirements=UnlockRequirements(level=0),
        entry_price=0,
        backdrop_path='assets/digging/backdrops/backyard.png',
        layers=[
            Layer(
                depth=0,
                items={
                    None: 2,
                    # shovel-based:
                    Items.worm: 0.25,
                    Items.gummy_worm: 0.08,
                    Items.earthworm: 0.03,
                    Items.hook_worm: 0.0075,
                    Items.poly_worm: 0.0025,
                    Items.ancient_relic: 0.00005,  # 0.005%
                    # pickaxe-based:
                    Items.iron: 0.5,
                    Items.copper: 0.17,
                    Items.silver: 0.075,
                    Items.gold: 0.015,
                    Items.obsidian: 0.005,
                    Items.emerald: 0.0015,
                    Items.diamond: 0.0003,
                },
                dirt=Items.dirt,
                dirt_color=(139, 93, 43),
                grain_color=(88, 53, 16),
            ),
            Layer(
                depth=20,
                items={
                    None: 1.8,
                    # shovel-based:
                    Items.worm: 0.3,
                    Items.gummy_worm: 0.2,
                    Items.earthworm: 0.07,
                    Items.hook_worm: 0.02,
                    Items.poly_worm: 0.007,
                    Items.ancient_relic: 0.0001,  # 0.01%
                    # pickaxe-based:
                    Items.iron: 0.5,
                    Items.copper: 0.25,
                    Items.silver: 0.1,
                    Items.gold: 0.03,
                    Items.obsidian: 0.0075,
                    Items.emerald: 0.003,
                    Items.diamond: 0.00075,
                },
                dirt=Items.clay,
                dirt_color=(149, 124, 107),
                grain_color=(115, 91, 75),
            ),
            Layer(
                depth=40,
                items={
                    None: 1.5,
                    # shovel-based:
                    Items.worm: 0.4,
                    Items.gummy_worm: 0.3,
                    Items.earthworm: 0.15,
                    Items.hook_worm: 0.05,
                    Items.poly_worm: 0.02,
                    Items.ancient_relic: 0.0004,  # 0.04%
                    # pickaxe-based:
                    Items.iron: 0.5,
                    Items.copper: 0.3,
                    Items.silver: 0.15,
                    Items.gold: 0.05,
                    Items.obsidian: 0.015,
                    Items.emerald: 0.0075,
                    Items.diamond: 0.002,
                },
                dirt=Items.gravel,
                dirt_color=(149, 124, 107),
                grain_color=[(88, 38, 38), (62, 59, 59), (173, 182, 184)],
            ),
            Layer(
                depth=60,
                items={
                    None: 1.2,
                    # shovel-based:
                    Items.worm: 0.3,
                    Items.gummy_worm: 0.3,
                    Items.earthworm: 0.25,
                    Items.hook_worm: 0.08,
                    Items.poly_worm: 0.04,
                    Items.ancient_relic: 0.001,  # 0.1%
                    # pickaxe-based:
                    Items.iron: 0.5,
                    Items.copper: 0.4,
                    Items.silver: 0.25,
                    Items.gold: 0.15,
                    Items.obsidian: 0.03,
                    Items.emerald: 0.015,
                    Items.diamond: 0.005,
                },
                dirt=Items.limestone,
                dirt_color=(229, 205, 177),
                grain_color=[(194, 166, 126), (208, 179, 143)],
            ),
            Layer(
                depth=80,
                items={
                    None: 1.0,
                    # shovel-based:
                    Items.worm: 0.3,
                    Items.gummy_worm: 0.3,
                    Items.earthworm: 0.3,
                    Items.hook_worm: 0.10,
                    Items.poly_worm: 0.06,
                    Items.ancient_relic: 0.003,  # 0.3%
                    # pickaxe-based:
                    Items.iron: 0.5,
                    Items.copper: 0.5,
                    Items.silver: 0.4,
                    Items.gold: 0.3,
                    Items.obsidian: 0.1,
                    Items.emerald: 0.04,
                    Items.diamond: 0.02,
                },
                dirt=Items.granite,
                dirt_color=(161, 142, 126),
                grain_color=(114, 98, 84),
                grain_density=10,
            ),
            Layer(
                depth=100,
                items={
                    None: 0.8,
                    # shovel-based:
                    Items.worm: 0.3,
                    Items.gummy_worm: 0.3,
                    Items.earthworm: 0.3,
                    Items.hook_worm: 0.2,
                    Items.poly_worm: 0.1,
                    Items.ancient_relic: 0.006,  # 0.6%
                    # pickaxe-based:
                    Items.iron: 0.5,
                    Items.copper: 0.5,
                    Items.silver: 0.4,
                    Items.gold: 0.4,
                    Items.obsidian: 0.4,
                    Items.emerald: 0.1,
                    Items.diamond: 0.05,
                },
                dirt=Items.magma,
                dirt_color=(102, 65, 47),
                grain_color=(255, 95, 35),
                grain_density=12,
            ),
        ]
    )

    desert = Biome(
        key='desert',
        name='Desert',
        description='Hot and dry but full of treasures',
        unlock_requirements=UnlockRequirements(level=10, price=500_000),
        entry_price=1_000,
        backdrop_path='assets/digging/backdrops/desert.png',
        layers=[
            Layer(
                depth=0,
                items={
                    None: 1.2,
                    # shovel-based:
                    Items.dust_mite: 0.25,
                    Items.cactus_worm: 0.08,
                    Items.cricket: 0.03,
                    Items.beetle: 0.01,
                    Items.ancient_relic: 0.005,
                    Items.fossil: 0.005,
                    # pickaxe-based:
                    Items.iron: 0.5,
                    Items.copper: 0.5,
                    Items.silver: 0.4,
                    Items.gold: 0.3,
                    Items.obsidian: 0.1,
                    Items.emerald: 0.04,
                    Items.ruby: 0.04,
                    Items.diamond: 0.02,
                },
                dirt=Items.sand,
                dirt_color=(241, 193, 107),
                grain_color=(231, 172, 71),
            ),
            Layer(
                depth=20,
                items={
                    None: 1.0,
                    # shovel-based:
                    Items.dust_mite: 0.3,
                    Items.cactus_worm: 0.15,
                    Items.cricket: 0.07,
                    Items.beetle: 0.03,
                    Items.fossil: 0.02,
                    Items.ancient_relic: 0.01,
                    # pickaxe-based:
                    Items.iron: 0.5,
                    Items.copper: 0.5,
                    Items.silver: 0.4,
                    Items.gold: 0.4,
                    Items.obsidian: 0.2,
                    Items.emerald: 0.1,
                    Items.ruby: 0.1,
                    Items.diamond: 0.05,
                },
                dirt=Items.sand_clay,
                dirt_color=(219, 155, 43),
                grain_color=(141, 96, 18),
                grain_density=9,
            ),
            Layer(
                depth=40,
                items={
                    None: 0.8,
                    # shovel-based:
                    Items.dust_mite: 0.4,
                    Items.cactus_worm: 0.3,
                    Items.cricket: 0.15,
                    Items.beetle: 0.05,
                    Items.fossil: 0.03,
                    Items.ancient_relic: 0.02,
                    # pickaxe-based:
                    Items.iron: 0.5,
                    Items.copper: 0.5,
                    Items.silver: 0.5,
                    Items.gold: 0.5,
                    Items.obsidian: 0.3,
                    Items.emerald: 0.2,
                    Items.ruby: 0.2,
                    Items.diamond: 0.1,
                },
                dirt=Items.sandstone,
                dirt_color=(252, 172, 0),
                grain_color={(255, 143, 0): 3, (166, 93, 0): 2},
                grain_density=8,
            ),
            Layer(
                depth=60,
                items={
                    None: 0.6,
                    # shovel-based:
                    Items.dust_mite: 0.5,
                    Items.cactus_worm: 0.4,
                    Items.cricket: 0.25,
                    Items.beetle: 0.1,
                    Items.fossil: 0.05,
                    Items.ancient_relic: 0.03,
                    # pickaxe-based:
                    Items.iron: 0.5,
                    Items.copper: 0.5,
                    Items.silver: 0.5,
                    Items.gold: 0.6,
                    Items.obsidian: 0.4,
                    Items.emerald: 0.3,
                    Items.ruby: 0.3,
                    Items.diamond: 0.15,
                },
                dirt=Items.fossil_rock,
                dirt_color=(215, 188, 142),
                grain_color=[(137, 120, 91), (228, 232, 173)],
                grain_density=10,
            ),
            Layer(
                depth=80,
                items={
                    None: 0.4,
                    # shovel-based:
                    Items.dust_mite: 0.5,
                    Items.cactus_worm: 0.5,
                    Items.cricket: 0.4,
                    Items.beetle: 0.2,
                    Items.fossil: 0.1,
                    Items.ancient_relic: 0.05,
                    # pickaxe-based:
                    Items.iron: 0.5,
                    Items.copper: 0.5,
                    Items.silver: 0.6,
                    Items.gold: 0.7,
                    Items.obsidian: 0.5,
                    Items.emerald: 0.4,
                    Items.ruby: 0.4,
                    Items.diamond: 0.2,
                },
                dirt=Items.quartzite,
                dirt_color=(243, 222, 186),
                grain_color=(255, 255, 255),
                grain_density=8,
            ),
            Layer(
                depth=100,
                items={
                    None: 0.2,
                    # shovel-based:
                    Items.dust_mite: 0.5,
                    Items.cactus_worm: 0.5,
                    Items.cricket: 0.5,
                    Items.beetle: 0.3,
                    Items.fossil: 0.2,
                    Items.ancient_relic: 0.1,
                    # pickaxe-based:
                    Items.iron: 0.5,
                    Items.copper: 0.5,
                    Items.silver: 0.7,
                    Items.gold: 0.8,
                    Items.obsidian: 0.6,
                    Items.emerald: 0.5,
                    Items.ruby: 0.5,
                    Items.diamond: 0.3,
                },
                dirt=Items.sunstone,
                dirt_color=(242, 108, 37),
                grain_color={(255, 200, 0): 2, (255, 252, 93): 2, (175, 82, 9): 1},
                grain_density=10,
            ),
        ],
        ore_hp_multiplier=3,
    )
