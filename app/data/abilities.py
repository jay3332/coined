from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, NamedTuple, TypeAlias, TYPE_CHECKING

from app.core.helpers import MISSING
from app.util.common import BaseCurve, ExponentialCurve, get_by_key

if TYPE_CHECKING:
    from app.data.enemies import Enemy
    from app.data.pets import Pet
    from app.features.battles import BattleContext
    from app.util.types import AsyncCallable

    AbilityCallback: TypeAlias = AsyncCallable[['Abilities', BattleContext], Any]


class RefType(Enum):
    enemy = 0
    pet = 1


class Ref(NamedTuple):
    key: str
    type: RefType


class AbilityType(Enum):
    attack = 'Attack'
    defense = 'Defense'
    healing = 'Healing'
    buff = 'Buff'


@dataclass
class Ability:
    key: str
    name: str
    type: AbilityType
    description: str
    effect: str
    emoji: str
    skins: dict[str, str] = field(default_factory=dict)
    stamina: int = 0
    curve: BaseCurve = ExponentialCurve(100, 1.22, precision=10)
    exclusive_to: list[Ref] | None = None
    _callback: AbilityCallback = MISSING

    @property
    def display(self) -> str:
        return self.display_for(None)

    def display_for(self, enemy: Enemy | str | None, /) -> str:
        return f'{self.emoji_for(enemy)} {self.name}'

    def emoji_for(self, enemy: Enemy | str | None, /) -> str:
        return self.skins.get(enemy if isinstance(enemy, str) else enemy.key, self.emoji) if enemy else self.emoji

    @property
    def exclusive_to_resolved(self) -> list[Enemy | Pet] | None:
        from app.data.enemies import Enemies
        from app.data.pets import Pets

        if self.exclusive_to is None:
            return None

        return [
            get_by_key(Enemies, ref.key) if ref.type is RefType.enemy else get_by_key(Pets, ref.key)
            for ref in self.exclusive_to
        ]

    def callback(self, func: AbilityCallback) -> AbilityCallback:
        self._callback = func
        return func

    async def dispatch(self, ctx: BattleContext) -> Any:
        ctx.player.hp -= ctx.player.poison_damage
        return await self._callback(_ABILITIES_INST, ctx)

    def __hash__(self) -> int:
        return hash(self.key)


class Abilities:
    """A collection of all abilities."""

    punch = Ability(
        key='punch',
        name='Punch',
        type=AbilityType.attack,
        description='Punches the opponent somewhere in the body.',
        effect='Deals damage to the opponent.',
        emoji='\U0001f44a',
        stamina=1,
    )

    @punch.callback
    async def callback(self, ctx: BattleContext) -> Any:
        if random.random() < 0.4 * ctx.player.accuracy:
            reason = random.choice((
                'misses!',
                f'{ctx.target.user} dodges it',
                f'{ctx.target.user} blocks it',
            ))
            return ctx.add_attack_commentary(
                text=f'{ctx.player.user} tries to **punch** {ctx.target.user}, but {reason}',
            )

        base = random.uniform(8, 16) * ctx.level ** 1.2
        area = random.choice((
            'face',
            'chest',
            'stomach',
            'arm',
            'leg',
        ))
        if critical := random.random() < 0.1:
            base *= 3

        damage = ctx.deal_attack(round(base))
        ctx.add_attack_commentary(
            damage=damage,
            text=(
                f'{ctx.player.user} **punches** {ctx.target.user} with a mighty blow and deals **{damage} HP!**'
                if critical
                else f'{ctx.player.user} **punches** {ctx.target.user} in the {area} and deals **{damage} HP**'
            ),
            critical=critical,
        )

    kick = Ability(
        key='kick',
        name='Kick',
        type=AbilityType.attack,
        description='Deals a mighty kick the opponent.',
        effect='Deals damage to the opponent.',
        emoji='\U0001f45f',
        stamina=1,
    )

    @kick.callback
    async def callback(self, ctx: BattleContext) -> Any:
        if random.random() < 0.4 * ctx.player.accuracy:
            reason = random.choice((
                'misses!',
                f'{ctx.target.user} dodges it',
                f'{ctx.target.user} blocks it',
            ))
            return ctx.add_attack_commentary(
                text=f'{ctx.player.user} tries to **kick** {ctx.target.user}, but {reason}',
            )

        base = random.uniform(10, 14) * ctx.level ** 1.2
        area = random.choice((
            'stomach',
            'arm',
            'leg',
        ))
        if critical := random.random() < 0.1:
            area = '**groin**'
            base *= 3

        damage = ctx.deal_attack(round(base))
        ctx.add_attack_commentary(
            damage=damage,
            text=f'{ctx.player.user} **kicks** {ctx.target.user} in the {area} and deals **{damage} HP**',
            critical=critical,
        )

    herb = Ability(
        key='herb',
        name='Herb',
        type=AbilityType.healing,
        description='Uses a herb to replenish HP.',
        effect='Heals the player by a small amount of HP.',
        emoji='\U0001f33f',
        stamina=1,
    )

    @herb.callback
    async def callback(self, ctx: BattleContext) -> Any:
        hp = round(random.uniform(8, 14) * ctx.level ** 1.2)
        hp = ctx.player.heal(hp)
        ctx.player.tick_offensive(AbilityType.healing)

        if not hp:
            return ctx.add_heal_commentary(
                text=f'{ctx.player.user} uses a **herb**, but they are already at max HP', heal=0,
            )
        ctx.add_heal_commentary(text=f'{ctx.player.user} uses a **herb** and replenishes **{hp} HP**', heal=hp)

    speak_to_the_manager = Ability(
        key='speak_to_the_manager',
        name='Speak to the Manager',
        type=AbilityType.attack,
        description='Karen demands to speak to the manager, distracting the player and lowering their accuracy.',
        effect='Applies a 50% accuracy debuff for the next 2 attacks from the player.',
        emoji='<:karen:1379664191492853935>',
        exclusive_to=[Ref('karen', RefType.enemy)],
    )

    @speak_to_the_manager.callback
    async def callback(self, ctx: BattleContext) -> Any:
        ctx.target.accuracy_stack.append(0.75, 2, types={AbilityType.attack})
        ctx.add_buff_commentary(
            player=ctx.player,
            text=f'**Karen** demands to speak to the manager, distracting {ctx.target.user}.',
            buff=f'**Accuracy Debuff:** -25% for 2 attacks',
        )

    insult = Ability(
        key='insult',
        name='Insult',
        type=AbilityType.attack,
        description='Insults the opponent, lowering their motivation and lowering their attack.',
        effect='Applies a 25% attack debuff for the next 2 attacks from the opponent.',
        emoji='\U0001f595',
    )

    @insult.callback
    async def callback(self, ctx: BattleContext) -> Any:
        ctx.target.attack_stack.append(0.75, 2, types={AbilityType.attack})
        ctx.player.tick_offensive(AbilityType.attack)
        ctx.add_buff_commentary(
            player=ctx.player,
            text=f'{ctx.player.user} **insults** {ctx.target.user}, lowering their motivation.',
            buff=f'**Attack Debuff:** -25% for 2 attacks',
        )

    block = Ability(
        key='block',
        name='Block',
        type=AbilityType.defense,
        description='Blocks the next attack from the opponent, reducing the damage taken.',
        effect='Reduces the damage taken from the next attack.',
        emoji='\U0001f6e1',
    )

    _BLOCK_BUFF_CURVE_BASE = 1 / 1.025

    @block.callback
    async def callback(self, ctx: BattleContext) -> Any:
        buff = 0.45 * self._BLOCK_BUFF_CURVE_BASE ** (ctx.level - 1)
        ctx.player.defense_stack.append(buff, 1, types={AbilityType.attack})
        ctx.player.tick_offensive(AbilityType.defense)
        ctx.add_buff_commentary(
            player=ctx.player,
            text=f'{ctx.player.user} **blocks** the next attack from {ctx.target.user}.',
            buff=f'**Next attack taken:** -{1 - buff:.1%} damage',
        )

    handcuffs = Ability(
        key='handcuffs',
        name='Handcuffs',
        type=AbilityType.attack,
        description='Handcuffs the opponent, preventing them from attacking for the next turn.',
        effect='Prevents the opponent from attacking for the next turn.',
        emoji='<:handcuffs:1379664189391634512>',
    )

    @handcuffs.callback
    async def callback(self, ctx: BattleContext) -> Any:
        ctx.target.attack_stack.append(0, 1)
        ctx.add_buff_commentary(
            player=ctx.player,
            text=f'{ctx.player.user} **handcuffs** {ctx.target.user}.',
            buff='**Next move:** ATK suppressed to zero',
        )

    taser = Ability(
        key='taser',
        name='Taser',
        type=AbilityType.attack,
        description='Tases the opponent, dealing damage and lowering their defense.',
        effect='Deals a small amount of damage and applies a 25% defense debuff for the next 2 moves from the opponent.',
        emoji='<:taser:1379664204344328222>',
    )

    @taser.callback
    async def callback(self, ctx: BattleContext) -> Any:
        damage = ctx.deal_attack(round(random.uniform(4, 9) * ctx.level ** 1.2))
        ctx.target.defense_stack.append(1.25, 2, types={AbilityType.attack})

        ctx.add_attack_commentary(
            damage=damage,
            text=f'{ctx.player.user} **tases** {ctx.target.user} and deals **{damage} HP**',
            buff=f'**Defense Debuff:** -25% for 2 attacks',
        )

    swim = Ability(
        key='swim',
        name='Swim',
        type=AbilityType.defense,
        description='Swims around in the water, making them harder for the opponent to hit.',
        effect='Adds a 25% defense buff against the next attack from the opponent.',
        emoji='\U0001f3ca',
        skins=dict(shark='<:shark_swim:1152430176236474379>'),
    )

    @swim.callback
    async def callback(self, ctx: BattleContext) -> Any:
        ctx.player.defense_stack.append(buff := 0.75, 1, types={AbilityType.attack})
        ctx.player.tick_offensive(AbilityType.defense)
        ctx.add_buff_commentary(
            player=ctx.player,
            text=f'{ctx.player.user} **swims** around in the water, making them hard to find.',
            buff=f'**Next attack taken:** -{1 - buff:.1%} damage',
        )

    sonic_wave = Ability(
        key='sonic_wave',
        name='Sonic Wave',
        type=AbilityType.attack,
        description='The dolphin emits a powerful high-pitched sonic wave, disorienting the player.',
        effect='Applies a 15% accuracy debuff for the next 3 attacks from the opponent.',
        emoji='<:sonic_wave:1379664200946942056>',
        exclusive_to=[Ref('dolphin', RefType.enemy)],
    )

    @sonic_wave.callback
    async def callback(self, ctx: BattleContext) -> Any:
        ctx.target.accuracy_stack.append(0.85, 3, types={AbilityType.attack})
        ctx.player.tick_offensive(AbilityType.attack)
        ctx.add_buff_commentary(
            player=ctx.player,
            text=f'{ctx.player.user} emits a high-pitched **sonic wave** at {ctx.target.user}, disorienting them.',
            buff=f'**Accuracy Debuff:** -15% for 3 attacks',
        )

    blowhole = Ability(
        key='blowhole',
        name='Blowhole',
        type=AbilityType.attack,
        description='The dolphin uses its blowhole to spray water at the opponent, dealing damage.',
        effect='Deals a small amount of damage to the opponent.',
        emoji='<:blowhole:1379664181032128613>',
        exclusive_to=[Ref('dolphin', RefType.enemy)],
    )

    @blowhole.callback
    async def callback(self, ctx: BattleContext) -> Any:
        damage = ctx.deal_attack(round(random.uniform(3, 7) * ctx.level ** 1.2))
        ctx.add_attack_commentary(
            damage=damage,
            text=f'{ctx.player.user} uses their **blowhole** to spray water at {ctx.target.user} and deals **{damage} HP**',
        )

    shark_bite = Ability(
        key='shark_bite',
        name='Shark Bite',
        type=AbilityType.attack,
        description='The shark lunges forward and bites the opponent; jaws snapping shut with immense force.',
        effect='Deals a large amount of damage and slowly removes a small amount of HP per turn for the next 3 turns due to bleeding.',
        emoji='<:shark_bite:1379664198568513668>',
        exclusive_to=[Ref('shark', RefType.enemy)],
    )

    @shark_bite.callback
    async def callback(self, ctx: BattleContext) -> Any:
        if random.random() < 0.3:
            ctx.add_attack_commentary(
                text=f'{ctx.player.user} tries **biting** {ctx.target.user}, but misses!',
            )

        damage = ctx.deal_attack(round(random.uniform(5, 10) * ctx.level ** 1.1))
        ctx.target.poison_stack.append(damage // 3, 3)

        ctx.add_attack_commentary(
            damage=damage,
            text=f'{ctx.player.user} **bites** {ctx.target.user}; jaws snapping shut with immense force, dealing **{damage} HP**',
            buff=f'**You\'re Bleeding!** -3 HP per turn for 3 turns',
        )

    serrated_fins = Ability(
        key='serrated_fins',
        name='Serrated Fins',
        type=AbilityType.attack,
        description='The shark\'s fins become razor-sharp, dealing damage and lowering the opponent\'s defense.',
        effect='Deals a small amount of damage and applies a 25% defense debuff for the next 2 moves from the opponent.',
        emoji='<:serrated_fins:1379664196647649442>',
        exclusive_to=[Ref('shark', RefType.enemy)],
    )

    @serrated_fins.callback
    async def callback(self, ctx: BattleContext) -> Any:
        damage = ctx.deal_attack(round(random.uniform(3, 6) * ctx.level ** 1.1))
        ctx.target.defense_stack.append(1.25, 2, types={AbilityType.attack})

        ctx.add_attack_commentary(
            damage=damage,
            text=f'{ctx.player.user} **slashes** {ctx.target.user} with its **serrated fins**, dealing **{damage} HP**',
            buff=f'**Defense Debuff:** -25% for 2 attacks',
        )

    mighty_splash = Ability(
        key='mighty_splash',
        name='Mighty Splash',
        type=AbilityType.attack,
        description='The whale leaps out of the water and crashes down, causing a massive splash that stuns the opponent.',
        effect='Deals a medium amount of damage and applies a 25% damage debuff for the next attack from the opponent.',
        emoji='<:mighty_splash:1379664193476886610>',
        exclusive_to=[Ref('whale', RefType.enemy)],
    )

    @mighty_splash.callback
    async def callback(self, ctx: BattleContext) -> Any:
        damage = ctx.deal_attack(round(random.uniform(4, 8) * ctx.level ** 1.1))
        ctx.target.attack_stack.append(0.75, 1, types={AbilityType.attack})

        ctx.add_attack_commentary(
            damage=damage,
            text=f'{ctx.player.user} leaps out of the water and crashes down, causing a **mighty splash** dealing **{damage} HP**',
            buff=f'**Damage Debuff:** -25% for 1 attack',
        )

    echolocation = Ability(
        key='echolocation',
        name='Echolocation',
        type=AbilityType.defense,
        description='The whale uses echolocation to find the opponent, making them easier to hit.',
        effect='Increases whale attack by 25% for its next 2 attacks.',
        emoji='<:echolocation:1379664183041327156>',
        exclusive_to=[Ref('whale', RefType.enemy)],
    )

    @echolocation.callback
    async def callback(self, ctx: BattleContext) -> Any:
        ctx.player.attack_stack.append(1.25, 2, types={AbilityType.attack})
        ctx.player.tick_offensive(AbilityType.defense)
        ctx.add_buff_commentary(
            player=ctx.target,
            text=f'{ctx.player.user} uses **echolocation** to find {ctx.target.user}, making them easier to hit.',
            buff=f'**Attack Buff:** +50% for 2 attacks',
        )

    tidal_surge = Ability(
        key='tidal_surge',
        name='Tidal Surge',
        type=AbilityType.attack,
        description='The whale summons a tidal wave to crash down on the opponent, dealing damage and stunning them.',
        effect='Deals a large amount of damage and applies a 50% accuracy debuff for the next attack from the opponent.',
        emoji='<:tidal_surge:1379664206567182347>',
        exclusive_to=[Ref('vibe_fish', RefType.enemy)],
    )

    @tidal_surge.callback
    async def callback(self, ctx: BattleContext) -> Any:
        if random.random() < 0.3 * ctx.player.accuracy:
            return ctx.add_attack_commentary(
                text=f'{ctx.player.user} tries to create a **tidal surge** to crash down on {ctx.target.user}, but fails!',
            )

        ctx.target.accuracy_stack.append(0.5, 1, types={AbilityType.attack})
        damage = ctx.deal_attack(round(random.uniform(7, 12) * ctx.level ** 1.1))

        ctx.add_attack_commentary(
            damage=damage,
            text=f'{ctx.player.user} summons a **tidal surge** to crash down on {ctx.target.user}, dealing **{damage} HP**',
            buff=f'**Accuracy Debuff:** -50% for 1 attack',
        )

    electric_whirlpool = Ability(
        key='electric_whirlpool',
        name='Electric Whirlpool',
        type=AbilityType.attack,
        description='Summons a whirlpool and electrifies it, causing damage over time.',
        effect='Slowly removes a medium amount of HP per turn for the next 3 turns due to electrocution.',
        emoji='<:electric_whirlpool:1379664185520295997>',
        exclusive_to=[Ref('vibe_fish', RefType.enemy)],  # TODO: and eel
    )

    @electric_whirlpool.callback
    async def callback(self, ctx: BattleContext) -> Any:
        if random.random() < 0.3 * ctx.player.accuracy:
            return ctx.add_attack_commentary(
                text=f'{ctx.player.user} tries to summon an **electric whirlpool** around {ctx.target.user}, but fails!',
            )

        ctx.target.poison_stack.append(amount := round(random.uniform(4, 6) * ctx.level ** 1.1), 3)
        ctx.player.tick_offensive(AbilityType.attack)
        ctx.add_buff_commentary(
            player=ctx.target,
            text=f'{ctx.player.user} summons an **electric whirlpool** around {ctx.target.user}',
            buff=f'**You\'re Electrocuted!** -{amount:,} HP per turn for 3 turns',
        )

    vibe_blast = Ability(
        key='vibe_blast',
        name='Vibe Blast',
        type=AbilityType.attack,
        description=(
            'The vibe fish charges up its vibe and eventually releases it in a powerful blast, '
            'dealing massive damage and stunning the opponent.'
        ),
        effect='Deals a massive amount of damage and suppresses the next attack from the opponent.',
        emoji='<:vibe_blast:1379664208966324374>',
        exclusive_to=[Ref('vibe_fish', RefType.enemy)],
    )

    @vibe_blast.callback
    async def callback(self, ctx: BattleContext) -> Any:
        charge = ctx.metadata.setdefault('charge', 0)
        if charge < 3:
            ctx.metadata['charge'] += 1
            return ctx.add_attack_commentary(text=f'{ctx.player.user} charges up its **vibe blast** ({charge + 1}/3)')

        ctx.metadata['charge'] = 0
        damage = ctx.deal_attack(round(random.uniform(20, 30) * ctx.level ** 1.1))
        ctx.target.attack_stack.append(0, 1, types={AbilityType.attack})
        ctx.add_attack_commentary(
            damage=damage,
            text=f'{ctx.player.user} releases its **vibe blast** and {ctx.target.user} is hit with immense force, dealing **{damage} HP**!',
            buff=f'**Next move:** ATK suppressed to zero',
        )


_ABILITIES_INST = Abilities()
