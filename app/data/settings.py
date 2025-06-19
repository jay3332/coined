from __future__ import annotations

from typing import NamedTuple, TYPE_CHECKING

from config import Emojis

if TYPE_CHECKING:
    from app.core import Context


class Setting(NamedTuple):
    key: str
    name: str
    description: str

    async def set(self, ctx: Context, value: bool) -> None:
        record = await ctx.db.get_user_record(ctx.author.id)
        await record.update(**{self.key: value})

        new = f'{Emojis.enabled} Enabled' if value else f'{Emojis.disabled} Disabled'
        await ctx.send(f'Setting **{self.name}** set to **{new}**.', reference=ctx.message, ephemeral=True)
        await ctx.thumbs()


class Settings:
    dm_notifications = Setting(
        key='dm_notifications',
        name='DM Notifications',
        description='When enabled, I will direct message you whenever you receive a notification.',
    )

    anonymous = Setting(
        key='anonymous_mode',
        name='Anonymous Mode',
        description=(
            'Your username will not be shown in global leaderboards (it will be replaced with *Anonymous User*).'
        ),
    )

    hide_partnerships = Setting(
        key='hide_partnerships',
        name='Hide Partnerships',
        description='When enabled, you will no longer see server partnerships/advertisements.'
    )

    hide_tips = Setting(
        key='hide_tips',
        name='Hide Tips',
        description='When enabled, you will no longer see occasional tips in the bot\'s messages.'
    )
