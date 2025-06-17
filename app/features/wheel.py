from __future__ import annotations

import random
from asyncio import sleep
from datetime import timedelta
from math import cos, degrees, pi, sin
from io import BytesIO
from typing import ClassVar, TypeVar, TYPE_CHECKING

import discord
from PIL import Image
from aiohttp import ClientSession
from discord import ui
from discord.utils import format_dt, utcnow

from app.data.items import Item, Items, Reward
from app.util.common import executor_function, image_url_from_emoji
from app.util.types import TypedInteraction
from app.util.views import UserLayoutView
from config import Emojis

if TYPE_CHECKING:
    from app.core import Context
    from app.database import UserRecord

    T = TypeVar('T')

WHEEL_RESET_INTERVAL: timedelta = timedelta(hours=6)
COIN_REWARDS: dict[int, int] = {
    500: 100,
    1000: 100,
    5000: 70,
    10000: 40,
    15000: 20,
    20000: 5,
    50000: 2,
    100000: 1,
}
CRATE_REWARDS: dict[Item, int] = {
    Items.common_crate: 100,
    Items.uncommon_crate: 80,
    Items.rare_crate: 50,
    Items.epic_crate: 20,
    Items.legendary_crate: 5,
    Items.mythic_crate: 1,
}
ITEM_REWARDS: dict[Item, int | float] = {
    Items.key: 100,
    Items.banknote: 100,
    Items.dynamite: 100,
    Items.cheese: 70,
    Items.fishing_pole: 40,
    Items.jar_of_honey: 40,
    Items.shovel: 40,
    Items.pickaxe: 40,
    Items.net: 40,
    Items.golden_net: 5,
    Items.golden_shovel: 5,
    Items.golden_fishing_pole: 5,
    Items.diamond_fishing_pole: 1,
    Items.diamond_pickaxe: 1,
    Items.diamond_shovel: 1,
    Items.spinning_coin: 1,
    Items.plasma_shovel: 0.1,
    Items.meth: 0.01,
}


class Wheel:
    WHEEL_IMAGE: ClassVar[Image.Image] = Image.open('assets/wheel.png').convert('RGBA')
    WHEEL_POINTER: ClassVar[Image.Image] = Image.open('assets/wheel_ptr.png').convert('RGBA')
    WHEEL_POINTER_POSITION: ClassVar[tuple[int, int]] = 185, 0
    ASSET_WIDTH: ClassVar[int] = 53

    dt: ClassVar[float] = 0.10
    initial_angular_velocity: ClassVar[float] = -12.0  # rad s^-1
    angular_acceleration: ClassVar[float] = 2.0  # rad s^-2

    def __init__(self) -> None:
        self.theta_0: float = 0.0
        self.spins: int = 0

    def theta(self, t: float) -> float:
        if t == 0 or t < self.stall_duration:
            return self.theta_0 + self.initial_angular_velocity * t
        t -= self.stall_duration
        return self.true_theta_0 + self.initial_angular_velocity * t + 0.5 * self.angular_acceleration * t * t

    def angular_velocity(self, t: float) -> float:
        if t == 0 or t < self.stall_duration:
            return self.initial_angular_velocity
        t -= self.stall_duration
        return self.initial_angular_velocity + self.angular_acceleration * t

    @property
    def true_theta_0(self) -> float:
        return self.theta_0 + self.initial_angular_velocity * self.stall_duration

    @property
    def total_t(self) -> float:
        return self.stall_duration - self.initial_angular_velocity / self.angular_acceleration

    @property
    def theta_final(self) -> float:
        return self.true_theta_0 - self.initial_angular_velocity ** 2 / self.angular_acceleration / 2.0

    @property
    def choice(self) -> int:
        effective = pi / 2 - self.theta_final
        return int(effective / (pi / 4) % 8)

    @executor_function
    def paste_asset(self, fp: BytesIO, *, angle: float) -> None:
        paste_radius = round(self.WHEEL_IMAGE.width / 2 * 0.65)
        x = round(paste_radius * cos(angle) - self.ASSET_WIDTH / 2) + self.WHEEL_IMAGE.width // 2
        y = round(paste_radius * sin(-angle) - self.ASSET_WIDTH / 2) + self.WHEEL_IMAGE.height // 2

        asset = Image.open(fp).convert('RGBA')
        asset = asset.resize((self.ASSET_WIDTH, self.ASSET_WIDTH), Image.Resampling.NEAREST)
        asset = asset.rotate(degrees(angle - pi / 2), expand=False, resample=Image.NEAREST)
        self.base_image.paste(asset, (x, y), asset)
        asset.close()

    async def prepare(self, rewards: list[Reward], session: ClientSession) -> None:
        centers = (k * pi / 8 for k in range(1, 16, 2))
        self.base_image: Image.Image = self.WHEEL_IMAGE.copy()

        for angle, reward in zip(centers, rewards):
            async with session.get(image_url_from_emoji(reward.principal_emoji, static=True)) as response:
                response.raise_for_status()
                await self.paste_asset(BytesIO(await response.read()), angle=angle)

    def spin(self) -> None:
        period = self.initial_angular_velocity / pi / 2.0
        self.stall_duration: float = random.uniform(0.0, period)
        self.spins += 1

    def render_frame(self, t: float) -> Image.Image:
        frame = self.base_image.copy().rotate(degrees(self.theta(t)), resample=Image.NEAREST, expand=False)
        pointer = self.WHEEL_POINTER.copy()
        frame.paste(pointer, self.WHEEL_POINTER_POSITION, pointer)
        return frame

    @executor_function
    def render_preview(self, t: float = 0.0) -> discord.File:
        with self.render_frame(t) as preview:
            preview.save(buffer := BytesIO(), format='png')
            buffer.seek(0)
            return discord.File(buffer, filename='wheel_preview.png')

    @executor_function
    def render(self) -> discord.File:
        frames = []
        durations = []

        t = 0.0
        while t < self.total_t:
            frames.append(self.render_frame(t))
            durations.append(int(self.dt * 100))
            t += self.dt

        frames.append(self.render_frame(self.total_t))
        durations.append(30_000)  # 30 seconds

        frames[0].save(
            buffer := BytesIO(),
            format='GIF',
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            disposal=2,
            loop=1,
        )
        buffer.seek(0)

        for frame in frames:
            frame.close()

        return discord.File(buffer, filename='wheel.gif')


class WheelActionRow(ui.ActionRow['WheelView']):
    def __init__(self, parent: WheelContainer) -> None:
        super().__init__()
        self.parent: WheelContainer = parent
        self.wheel: Wheel = parent.wheel
        self.price_to_spin: int = 0

    def update(self) -> None:
        self.clear_items()
        total_spins = self.view.record.wheel_spins_this_cycle
        if total_spins and self.parent.has_free_vote_spin:
            self.add_item(self.vote_spin)
            return

        self.spin.emoji = Emojis.coin
        match total_spins:
            case 0:
                self.price_to_spin = 0
                self.spin.emoji = None
                self.spin.label = 'Spin!'
            case 1:
                self.price_to_spin = 5_000
            case 2:
                self.price_to_spin = 50_000
            case 3:
                self.price_to_spin = 500_000
            case 4:
                self.price_to_spin = 5_000_000
            case _:
                self.spin.disabled = True
                self.spin.emoji = None
                self.spin.label = 'Max spins reached'

        if self.price_to_spin and not self.spin.disabled:
            self.spin.label = f'Spin for {self.price_to_spin:,} coins'
        self.add_item(self.spin)

    def set_disabled(self, disabled: bool) -> None:
        for btn in self.children:
            if isinstance(btn, ui.Button):
                btn.disabled = disabled
        self.parent.refresh_button.disabled = disabled

    async def _spin(self, interaction: TypedInteraction, after: callable) -> None:
        await interaction.response.defer()
        self.wheel.spin()
        self.set_disabled(True)

        self.parent.filename = 'attachment://wheel.gif'
        await interaction.edit_original_response(
            view=self.view,
            attachments=[await self.wheel.render()],
            allowed_mentions=discord.AllowedMentions.none(),
        )

        reward = self.view.rewards[self.wheel.choice]
        self.view.total_reward += reward
        self.set_disabled(False)

        async with self.view.record.db.acquire() as conn:
            await reward.apply(self.view.record, connection=conn)
            await after(self.view.record)
        await self.parent.update()

        await sleep(self.wheel.total_t + 0.1)
        self.parent.filename = 'attachment://wheel_preview.png'
        await interaction.edit_original_response(
            view=self.view,
            attachments=[await self.wheel.render_preview(self.wheel.total_t)],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ui.button(label='Spin!', style=discord.ButtonStyle.primary)
    async def spin(self, interaction: TypedInteraction, _btn: ui.Button):
        price = self.price_to_spin
        if self.view.record.wallet < price:
            return await interaction.response.send_message(
                f'You need {Emojis.coin} **{price:,}** to spin the wheel, but you only have'
                f' {Emojis.coin} **{self.view.record.wallet:,}**.',
                ephemeral=True,
            )

        await self.view.record.add(wallet=-price)
        await self._spin(interaction, lambda record: record.add(wheel_spins_this_cycle=1))
        if price:
            await interaction.followup.send(
                f'Spent {Emojis.coin} **{price:,}** to spin the wheel.\n'
                f'{Emojis.Expansion.standalone} You now have {Emojis.coin} **{self.view.record.wallet:,}**',
                ephemeral=True,
            )

    @ui.button(label='Spin again (Thanks for voting!)', style=discord.ButtonStyle.primary)
    async def vote_spin(self, interaction: TypedInteraction, _btn: ui.Button):
        await self._spin(interaction, lambda record: record.update(redeemed_vote_wheel_spin=True))


class RefreshButton(ui.Button['WheelView']):
    def __init__(self, parent: WheelContainer) -> None:
        super().__init__(emoji=Emojis.refresh, style=discord.ButtonStyle.secondary)
        self.parent: WheelContainer = parent

    async def callback(self, interaction: TypedInteraction) -> None:
        await self.parent.update()
        await interaction.response.edit_message(view=self.view)


class WheelContainer(ui.Container['WheelView']):
    COLORS: ClassVar[list[int]] = [
        0xe6b58f,
        0x8fa0e6,
        0x8fe6d9,
        0xe6dd8f,
        0xc88fe6,
        0x8eb8e6,
        0xa8e68f,
        0xe68f8f,
    ]

    def __init__(self, wheel: Wheel) -> None:
        super().__init__()
        self.wheel: Wheel = wheel
        self.refresh_button = RefreshButton(self)
        self.action_row = WheelActionRow(self)
        self.filename: str = 'attachment://wheel_preview.png'
        self.add_item(self.action_row)  # just to make it aware of the view

    @property
    def has_free_vote_spin(self) -> bool:
        if not self.view.record.last_dbl_vote:
            return False
        elapsed_since_vote = utcnow() - self.view.record.last_dbl_vote
        if elapsed_since_vote > timedelta(hours=12):
            return False
        if self.view.record.redeemed_vote_wheel_spin and elapsed_since_vote < timedelta(hours=12):
            return False
        return True

    @property
    def can_vote_for_spin(self) -> bool:
        if not self.view.record.last_dbl_vote:
            return True
        elapsed_since_vote = utcnow() - self.view.record.last_dbl_vote
        if elapsed_since_vote > timedelta(hours=12):
            return True
        return False

    async def update(self) -> None:
        self.clear_items()
        self.accent_colour = self.COLORS[self.wheel.choice] if self.wheel.spins else None
        if discord.utils.utcnow() >= self.view.record.wheel_resets_at:
            expiry = self.view.record.wheel_resets_at + WHEEL_RESET_INTERVAL
            while discord.utils.utcnow() >= expiry:
                expiry += WHEEL_RESET_INTERVAL
            await self.view.record.update(wheel_resets_at=expiry)
            await self.view.reroll()

        self.add_item(ui.Section(
            f'## {self.view.ctx.author.display_name}\'s Wheel\n'
            f'-# Resets {format_dt(self.view.record.wheel_resets_at, "R")}',
            accessory=self.refresh_button,
        ))
        self.add_item(ui.Separator(spacing=discord.SeparatorSize.large))

        self.add_item(ui.MediaGallery(discord.MediaGalleryItem(media=self.filename)))
        if self.wheel.spins:
            self.add_item(ui.TextDisplay(f'### You spun {self.view.rewards[self.wheel.choice].short}!'))
        self.action_row.update()
        self.add_item(self.action_row)
        if self.action_row.price_to_spin and self.can_vote_for_spin:
            self.add_item(ui.ActionRow().add_item(ui.Button(
                label='Vote for Coined and get a free spin!',
                style=discord.ButtonStyle.link,
                url=f'https://top.gg/bot/{self.view.ctx.bot.user.id}/vote',
            )))

        if self.view.total_reward:
            self.add_item(ui.Separator(spacing=discord.SeparatorSize.large))
            self.add_item(ui.TextDisplay(f'### You have received:\n{self.view.total_reward}'))


def weighted_sample(k: int, src: dict[T, int | float]) -> list[T]:
    src = src.copy()
    choices = []
    for _ in range(k):
        r = random.uniform(0, sum(src.values()))
        cumulative_weight = 0.0
        for item, weight in src.items():
            cumulative_weight += weight
            if r < cumulative_weight:
                choices.append(item)
                src.pop(item)
                break

    return choices


class WheelView(UserLayoutView):
    def __init__(self, ctx: Context, record: UserRecord) -> None:
        super().__init__(ctx.author, timeout=300)
        self.ctx: Context = ctx
        self.record: UserRecord = record
        self.wheel: Wheel = Wheel()
        self.total_reward: Reward = Reward()
        self.roll_rewards()

        self.container = WheelContainer(self.wheel)
        self.add_item(self.container)

    def roll_rewards(self) -> None:
        self.rewards: list[Reward] = []
        multiplier = self.record.coin_multiplier_in_ctx(self.ctx)
        self.rewards.extend(Reward(coins=round(coins * multiplier)) for coins in weighted_sample(2, COIN_REWARDS))
        self.rewards.extend(Reward(items={crate: 1}) for crate in weighted_sample(2, CRATE_REWARDS))
        self.rewards.extend(Reward(items={item: 1}) for item in weighted_sample(4, ITEM_REWARDS))
        random.shuffle(self.rewards)

    async def reroll(self) -> None:
        self.roll_rewards()
        await self.wheel.prepare(self.rewards, self.ctx.bot.session)

    async def prepare(self) -> None:
        await self.wheel.prepare(self.rewards, self.ctx.bot.session)
        if self.record.wheel_resets_at is None:
            await self.record.update(wheel_resets_at=self.ctx.now + WHEEL_RESET_INTERVAL)
        elif self.record.wheel_resets_at < self.ctx.now:
            expiry = self.record.wheel_resets_at + WHEEL_RESET_INTERVAL
            while discord.utils.utcnow() >= expiry:
                expiry += WHEEL_RESET_INTERVAL
            await self.record.update(
                wheel_resets_at=expiry,
                wheel_spins_this_cycle=0,
                redeemed_vote_wheel_spin=False,
            )

        await self.container.update()
