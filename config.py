from os import getenv as env
from platform import system
from typing import Collection

import discord
from discord import AllowedMentions
from dotenv import load_dotenv

load_dotenv()

__all__ = (
    'beta',
    'name',
    'version',
    'description',
    'owner',
    'default_prefix',
    'allowed_mentions',
    'Colors',
    'DatabaseConfig',
    'Emojis',
    'token',
)

beta: bool = system() != 'Linux'

name: str = 'Coined'
version: str = '0.0.0'
description: str = (
    'Have fun with your friends with Coined, a carefully crafted, feature-packed, and open-source economy bot.'
)

# An ID or list of IDs
owner: Collection[int] | int = 414556245178056706
default_prefix: Collection[str] | str = '.' + '.' * beta
token: str = env('DISCORD_TOKEN' if not beta else 'DISCORD_STAGING_TOKEN')

default_permissions: int = 414531833025
support_server = 'https://discord.gg/BjzrQZjFwk'  # caif
# support_server = 'https://discord.gg/bpnedYgFVd'  # unnamed bot testing
website = 'https://coined.jay3332.tech'

ipc_secret = env('IPC_SECRET')
dbl_token: str = env('DBL_TOKEN')
dbl_secret: str = env('DBL_SECRET')
cdn_authorization: str = env('CDN_AUTHORIZATION')
stripe_api_key: str = env('STRIPE_API_KEY')

allowed_mentions: AllowedMentions = AllowedMentions.none()
allowed_mentions.users = True

multiplier_guilds: set[int] = {
    635944376761057282,  # CAIF
    893991611262976091,  # Unnamed bot testing
}

backups_channel: int = 1138551276062396469
guilds_channel: int = 1138551294907387974
votes_channel: int = 1139280620216930524
errors_channel: int = 1145421294481969222


class _RandomColor:
    def __get__(self, *_) -> int:
        return discord.Color.random().value


class OAuth:
    client_id: int = 753017377922482248
    client_secret: str = env('OAUTH_CLIENT_SECRET')


class StripeSKUs:
    coined_silver: str = 'price_1RXwmCGPHyZ4PQsxs66ixR0Q'
    coined_gold: str = 'price_1RXwqbGPHyZ4PQsxnFxY6nV7'
    coined_premium: str = 'price_1RXx07GPHyZ4PQsxWCRoNGye'


class DiscordSKUs:
    coined_silver: int = 1381750673452044401
    coined_gold: int = 1381754776512893028
    coined_premium: int = 1381757101260406908

    @classmethod
    def to_product_key(cls, sku_id: int) -> str:
        if sku_id == cls.coined_silver:
            return 'coined_silver'
        elif sku_id == cls.coined_gold:
            return 'coined_gold'
        elif sku_id == cls.coined_premium:
            return 'coined_premium'
        raise ValueError(f'Unknown SKU ID: {sku_id}')


class Colors:
    primary: int = _RandomColor()  # 0x6199f2
    secondary: int = 0x6199f2
    success: int = 0x17ff70
    warning: int = 0xfcba03
    error: int = 0xff1759


class DatabaseConfig:
    name: str = 'dank_ripoff_remastered'
    user: str | None = None if beta else 'postgres'
    host: str | None = 'localhost'
    port: int | None = None
    password: str | None = None if beta else env('DATABASE_PASSWORD')


class Emojis:
    coin = '<:c:1379658457413845003>'
    coined = '<:coined:1376426825168850974>'
    loading = '<a:l:1379658669125406750>'
    space = '<:s:1379658651458994310>'
    arrow = '<:a:1379658452774949008>'
    refresh = '<:r:1374213217806712882>'
    topgg_upvote = '<:upvote:1379979028118638662>'

    orb = '<:o:1379658492519907450>'
    quest_pass = '\U0001f396\ufe0f'  # TODO
    ticket = '\U0001f39f\ufe0f'      # TODO
    hp = '<:h:1379658483363741706>'
    bolt = '<:b:1379658454406398054>'
    max_bolt = '<:z:1379658666265022565>'

    enabled = '<:e:1379658481329508503>'
    disabled = '<:d:1379658477986775110>'
    neutral = '<:n:1379658491207352371>'

    class Subscriptions:
        coined_silver: str = '<:coined_silver:1382090510503641219>'
        coined_gold: str = '<:coined_gold:1382090529344458955>'
        coined_premium: str = '<:coined_premium:1382090497404965065>'

    class Arrows:
        left: str = ''
        right: str = ''
        up: str = ''
        down: str = ''

        # Pagination
        previous: str = '\u25c0'
        forward: str = '\u25b6'
        first: str = '\u23ea'
        last: str = '\u23e9'

    dice = (
        ...,  # Index 0 is nothing
        '<:d1:1379658462044356700>',
        '<:d2:1379658464179126304>',
        '<:d3:1379658467052093440>',
        '<:d4:1379658470164267089>',
        '<:d5:1379658472320405718>',
        '<:d6:1379658474811691078>',
    )

    prestige = (
        '',  # Index 0 is nothing
        '<:prestige1:1379658626330792018>',
        '<:prestige2:1379658629602344981>',
        '<:prestige3:1379658633549316116>',
        '<:prestige4:1379658635411591271>',
        '<:prestige5:1379658636825067601>',
        '<:prestige6:1379658640256143380>',
        '<:prestige7:1379658641916821640>',
        '<:prestige8:1379658643921698887>',
        '<:prestige9:1379658646811836436>',
        '<:prestige10:1379658602113011764>',
        '<:prestige11:1379658605715918869>',
        '<:prestige12:1379658607049707582>',
        '<:prestige13:1379658609205706759>',
        '<:prestige14:1379658612112363540>',
        '<:prestige15:1379658613928493066>',
        '<:prestige16:1379658615555752028>',
        '<:prestige17:1379658619070582815>',
        '<:prestige18:1379658621214003261>',
        '<:prestige19:1379658623105372242>',
        '<:prestige20:1379658628235268116>',
    )

    @classmethod
    def get_prestige_emoji(cls, prestige: int, *, trailing_ws: bool = False) -> str:
        base = cls.prestige[prestige] if prestige < len(cls.prestige) else cls.prestige[-1]
        return base and f'{base} ' if trailing_ws else base

    class ProgressBars:
        left_empty = '<:p:1379658568084488202>'
        left_low = '<:p:1379658570987208815>'
        left_mid = '<:p:1379658572719198240>'
        left_high = '<:p:1379658574522880073>'
        left_full = '<:p:1379658577874124891>'

        mid_empty = '<:p:1379658579438473308>'
        mid_low = '<:p:1379658581585956924>'
        mid_mid = '<:p:1379658584388014131>'
        mid_high = '<:p:1379658586208079895>'
        mid_full = '<:p:1379658588112556133>'

        right_empty = '<:p:1379658591572721705>'
        right_low = '<:p:1379658593845907527>'
        right_mid = '<:p:1379658595427291196>'
        right_high = '<:p:1379658598216503409>'
        right_full = '<:p:1379658600313524266>'

    class RedProgressBars:
        left_empty = '<:p:1379658493837050008>'
        left_low = '<:p:1379658497888751768>'
        left_mid = '<:p:1379658499713400858>'
        left_high = '<:p:1379658502242308126>'
        left_full = '<:p:1379658504830320762>'

        mid_empty = '<:p:1379658506893791295>'
        mid_low = '<:p:1379658508781355112>'
        mid_mid = '<:p:1379658512694644826>'
        mid_high = '<:p:1379658514569367716>'
        mid_full = '<:p:1379658516263997563>'

        right_empty = '<:p:1379658519426498662>'
        right_low = '<:p:1379658520915480597>'
        right_mid = '<:p:1379658523339784317>'
        right_high = '<:p:1379658526930112615>'
        right_full = '<:p:1379658528754634753>'

    class GreenProgressBars:
        left_empty = '<:p:1379658530478620844>'
        left_low = '<:p:1379658535079645224>'
        left_mid = '<:p:1379658536853962763>'
        left_high = '<:p:1379658539840045136>'
        left_full = '<:p:1379658543245824036>'

        mid_empty = '<:p:1379658545888231424>'
        mid_low = '<:p:1379658547478003833>'
        mid_mid = '<:p:1379658550204174418>'
        mid_high = '<:p:1379658552439865396>'
        mid_full = '<:p:1379658554461524050>'

        right_empty = '<:p:1379658556659335189>'
        right_low = '<:p:1379658558294986844>'
        right_mid = '<:p:1379658560551649371>'
        right_high = '<:p:1379658563592392775>'
        right_full = '<:p:1379658566306365450>'

    class Expansion:
        first = '<:x:1379658653661003788>'
        mid = '<:x:1379658660950839407>'
        last = '<:x:1379658659084243035>'
        ext = '<:x:1379658663643451473>'
        single = standalone = '<:x:1379658657309917295>'

    class Rarity:
        common = '<:common:1374619910440751236>'
        uncommon = '<:uncommon:1374619962022432768>'
        rare = '<:rare:1374620340550111304>'
        epic = '<:epic:1374620370044452965>'
        legendary = '<:legendary:1374620392194572428>'
        mythic = '<:mythic:1374620428093358111>'
        unknown = unobtainable = '<:unknown:1374620473693966337>'
        special = '<:special:1374620450931605547>'
