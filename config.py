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
    coin = '<:c:896432147152400394>'
    coined = '<:coined:1376426825168850974>'
    loading = '<a:l:825862907626913842>'
    space = '<:s:940748421701185637>'
    arrow = '<:a:831333449562062908>'
    refresh = '<:r:1374213217806712882>'

    orb = '<:o:1144043541564244079>'
    hp = '<:h:1142106667601903858>'
    bolt = '<:b:1137588508228321413>'
    max_bolt = '<:z:1138247288259616840>'

    enabled = '<:e:939549340458954762>'
    disabled = '<:d:939549360570662952>'
    neutral = '<:n:838593591965384734>'

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
        '<:d1:935227885671837747>',
        '<:d2:935227904068034560>',
        '<:d3:935227914163724398>',
        '<:d4:935227927665188995>',
        '<:d5:935227982426038292>',
        '<:d6:935228010062282752>',
    )

    prestige = (
        '',  # Index 0 is nothing
        '<:prestige1:722846087622164560>',
        '<:prestige2:722846800175956080>',
        '<:prestige3:722847524427137125>',
        '<:prestige4:722848108501008436>',
        '<:prestige5:722849078794387547>',
        '<:prestige6:722850899692880013>',
        '<:prestige7:722853221755781200>',
        '<:prestige8:722853234888146944>',
        '<:prestige9:722853244606480505>',
        '<:prestige10:722853256048541696>',
        '<:prestige11:722961247305334874>',
        '<:prestige12:722961494660218900>',
        '<:prestige13:722966966603612201>',
        '<:prestige14:722966985700409405>',
        '<:prestige15:722966989307641876>',
        '<:prestige16:723205927188299787>',
        '<:prestige17:723205942635659325>',
        '<:prestige18:723205964270141440>',
        '<:prestige19:723205981927899266>',
        '<:prestige20:723205998625423371>',
    )

    @classmethod
    def get_prestige_emoji(cls, prestige: int, *, trailing_ws: bool = False) -> str:
        base = cls.prestige[prestige] if prestige < len(cls.prestige) else cls.prestige[-1]
        return base and f'{base} ' if trailing_ws else base

    class ProgressBars:
        left_empty = '<:p:937082616333602836>'
        left_low = '<:p:937082634046173194>'
        left_mid = '<:p:937082669068595300>'
        left_high = '<:p:937082728376045598>'
        left_full = '<:p:937082777927561297>'

        mid_empty = '<:p:937082833107828786>'
        mid_low = '<:p:937082868226752552>'
        mid_mid = '<:p:937082902880083988>'
        mid_high = '<:p:937082944655351860>'
        mid_full = '<:p:937082993057595473>'

        right_empty = '<:p:937083054340595803>'
        right_low = '<:p:937083097969754193>'
        right_mid = '<:p:937083245173026887>'
        right_high = '<:p:937083276827439164>'
        right_full = '<:p:937083328648056862>'


    class RedProgressBars:
        left_empty = '<:p:1154201648479076444>'
        left_low = '<:p:1154201650915966996>'
        left_mid = '<:p:1154201652480446564>'
        left_high = '<:p:1154201654531457034>'
        left_full = '<:p:1154201656637014036>'
        
        mid_empty = '<:p:1154201658927087626>'
        mid_low = '<:p:1154201660428656701>'
        mid_mid = '<:p:1154201663129792615>'
        mid_high = '<:p:1154201664979484765>'
        mid_full = '<:p:1154201668779511848>'
        
        right_empty = '<:p:1154201670738260008>'
        right_low = '<:p:1154201672751534112>'
        right_mid = '<:p:1154201675440066560>'
        right_high = '<:p:1154201678774554625>'
        right_full = '<:p:1154201680611647569>'

    
    class GreenProgressBars:
        left_empty = '<:p:1154208083317358602>'
        left_low = '<:p:1154208085238370368>'
        left_mid = '<:p:1154208087255822466>'
        left_high = '<:p:1154208089290047550>'
        left_full = '<:p:1154208091190079559>'
        
        mid_empty = '<:p:1154208093245284372>'
        mid_low = '<:p:1154208094692323390>'
        mid_mid = '<:p:1154208098492350464>'
        mid_high = '<:p:1154208100484661309>'
        mid_full = '<:p:1154208102938312786>'
        
        right_empty = '<:p:1154208104964169748>'
        right_low = '<:p:1154208106864181278>'
        right_mid = '<:p:1154208108684529735>'
        right_high = '<:p:1154208110207053925>'
        right_full = '<:p:1154208112564240434>'


    class Expansion:
        first = '<:x:968651020097945811>'
        mid = '<:x:968652421721120828>'
        last = '<:x:968652421700124723>'
        ext = '<:x:968653920106872842>'
        single = standalone = '<:x:968652421377167371>'


    class Rarity:
        common = '<:c:1374619910440751236>'
        uncommon = '<:u:1374619962022432768>'
        rare = '<:r:1374620340550111304>'
        epic = '<:e:1374620370044452965>'
        legendary = '<:l:1374620392194572428>'
        mythic = '<:m:1374620428093358111>'
        unknown = unobtainable = '<:n:1374620473693966337>'
        special = '<:s:1374620450931605547>'
