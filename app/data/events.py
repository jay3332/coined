from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from heapq import nlargest
from typing import Any, Final, NamedTuple, TypeAlias, TYPE_CHECKING

import discord

from app.data.enemies import Enemies, Enemy
from app.data.quests import QuestTemplates
from app.features.battles import AttackCommentaryEntry, PvEBattleView
from config import Emojis

if TYPE_CHECKING:
    from app.core import Context
    from app.util.types import AsyncCallable, TypedInteraction

    EventCallback: TypeAlias = AsyncCallable[['Events', Context, 'Event'], 'EventResults']


class EventRarity(Enum):
    common = 0
    uncommon = 1
    rare = 2
    epic = 3
    legendary = 4
    mythic = 5


EVENT_RARITY_WEIGHTS: Final[dict[EventRarity, int]] = {
    EventRarity.common: 1000,
    EventRarity.uncommon: 500,
    EventRarity.rare: 200,
    EventRarity.epic: 50,
    EventRarity.legendary: 10,
    EventRarity.mythic: 2,
}


class EventResults(NamedTuple):
    participants: set[int]  # user_ids
    winners: set[int]  # user_ids

    @property
    def losers(self) -> set[int]:
        return self.participants - self.winners


@dataclass
class Event:
    key: str
    name: str
    rarity: EventRarity
    _callback: EventCallback = None

    def callback(self, func: EventCallback) -> EventCallback:
        self._callback = func
        return func

    def __hash__(self) -> int:
        return hash(self.key)

    async def __call__(self, ctx: Context) -> EventResults:
        results = await self._callback(_EVENTS_INSTANCE, ctx, self)

        async with ctx.db.acquire() as conn:
            for user_id in results.participants:
                quests = await ctx.db.get_user_record(user_id, fetch=False).quest_manager.wait()

                if entry := quests.get_active_quest(QuestTemplates.event_participant):
                    await entry.add_progress(1, connection=conn)

                if user_id in results.winners:
                    if entry := quests.get_active_quest(QuestTemplates.event_winner):
                        await entry.add_progress(1, connection=conn)

        return results


class ViewBattleEarningsButton(discord.ui.Button):
    def __init__(
        self, mapping: dict[discord.Member, int], profits: dict[discord.Member, int], enemy: Enemy, **kwargs: Any,
    ) -> None:
        super().__init__(emoji='\U0001f4b0', label='View Earnings', **kwargs)
        self.mapping = mapping
        self.profits = profits
        self.enemy = enemy

    async def callback(self, interaction: TypedInteraction) -> Any:
        if hp := self.mapping.get(interaction.user):
            base_profit = hp * 4
            profit = self.profits.get(interaction.user, 0)

            multiplier_mention = interaction.client.tree.get_app_command('multiplier').mention
            multi_text = (
                f'\n{Emojis.Expansion.standalone} *Increased to {Emojis.coin} {profit:,} after applying multipliers ({multiplier_mention})*'
                if profit > base_profit else ''
            )

            return await interaction.response.send_message(
                f'{self.emoji} You received {Emojis.coin} **{base_profit:,}** for dealing **{hp:,} HP** to **{self.enemy.display}**.{multi_text}',
                ephemeral=True,
            )

        await interaction.response.send_message(
            f'You didn\'t deal any damage to {self.enemy.display}, so you got nothing.', ephemeral=True,
        )


class Events:
    """Collection of randomly spawning events."""

    @staticmethod
    async def _battle_event(
        ctx: Context,
        event: Event,
        *,
        description: str,
        opponent: Enemy,
        time_limit: int = 180,
    ) -> EventResults:
        view = PvEBattleView.public(
            ctx,
            opponent=opponent,
            level=2,
            title=f'{event.rarity.name.title()} Event: {event.name}',
            description=description,
            time_limit=time_limit,
        )
        original = await ctx.send(embeds=view.make_public_embeds(), view=view)
        view._original_message = original
        await view.wait()

        participants = set(m.id for m in view.players)
        if not view.won:
            await ctx.send(
                f'You guys stink, you weren\'t able to take down {opponent.display} in time. Better luck next time!',
                reference=original,
            )
            return EventResults(participants=participants, winners=set())

        damage_mapping = {
            player.user: hp for player, hp in view.damage_dealt.items()
            if isinstance(player.user, discord.Member)
        }
        top_damage = nlargest(5, damage_mapping.items(), key=lambda x: x[1])

        max_hp = view.opponent_player.max_hp
        top_damage = '\n'.join(
            f'{i}. {user.mention} dealt **{hp:,} HP** ({hp / max_hp:.1%})'
            for i, (user, hp) in enumerate(top_damage, start=1)
        )

        attacks = (
            (view.format_commentary_entry(c), c.damage) for c in view.commentary if isinstance(c, AttackCommentaryEntry)
        )
        best_attacks = '\n'.join(
            f'{i}. {text}' for i, (text, _) in enumerate(nlargest(3, attacks, key=lambda x: x[1] or 0), start=1)
        )
        profits = {}

        async with ctx.db.acquire() as conn:  # TODO: artifacts
            for user, hp in damage_mapping.items():
                record = view.records[user]
                profits[user] = await record.add_coins(hp * 4, ctx=ctx, connection=conn)

        finishing_view = discord.ui.View(timeout=600)
        finishing_view.add_item(ViewBattleEarningsButton(damage_mapping, profits, view.opponent))

        await ctx.send(
            f'## {opponent.display} has been defeated! \N{CROWN}\n'
            f'### Top damage dealers:\n{top_damage}\n### Best attacks:\n{best_attacks}',
            reference=original,
            view=finishing_view,
        )
        return EventResults(participants=participants, winners=participants)

    karen = Event(key='karen', name='Karen', rarity=EventRarity.common)

    @karen.callback
    async def karen_callback(self, ctx: Context, event: Event) -> EventResults:
        return await self._battle_event(
            ctx,
            event,
            description='A wild Karen has appeared! Join in the fight to take her down!',
            opponent=Enemies.karen,
        )


_EVENTS_INSTANCE: Final[Events] = Events()
