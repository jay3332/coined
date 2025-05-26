from __future__ import annotations

import re

import discord
from discord.utils import oauth_url

from typing import Any, cast, NamedTuple, TYPE_CHECKING

from app.util.common import format_line
from app.util.views import StaticCommandButton, UserView
from config import Colors, Emojis, default_permissions, support_server, website

if TYPE_CHECKING:
    from app.core import Context
    from app.util.types import TypedInteraction

__all__ = ('GuideView', 'GUIDE_PAGES', 'GUIDE_SELECT_OPTIONS')


class GuidePageSelect(discord.ui.Select['GuideView']):
    def __init__(self, current: str | None = None) -> None:
        super().__init__(placeholder='Select a page...', min_values=1, max_values=1, row=0)

        for label, (page, description, pages) in GUIDE_SELECT_OPTIONS.items():
            self.add_option(
                label=label, value=page, description=description, emoji=GUIDE_PAGES[page].emoji,
                default=current in pages,
            )

    async def callback(self, interaction: TypedInteraction) -> None:
        self.view.update_page(self.values[0])
        await interaction.response.edit_message(embed=self.view.render(), view=self.view)


class PageRedirectButton(discord.ui.Button['GuideView']):
    def __init__(self, label: str, redirect: PageRedirect, row: int | None = None) -> None:
        super().__init__(label=label, style=redirect.style, emoji=redirect.emoji, row=row)
        self.source = redirect.page

    async def callback(self, interaction: TypedInteraction) -> None:
        self.view.update_page(self.source)
        await interaction.response.edit_message(embed=self.view.render(), view=self.view)


class AllCommandsButton(discord.ui.Button['GuideView']):
    def __init__(self) -> None:
        super().__init__(label='View All Commands', style=discord.ButtonStyle.primary, row=2)

    async def callback(self, interaction: TypedInteraction) -> None:
        self.view.ctx.interaction = interaction
        await self.view.ctx.send_help()


class BackButton(discord.ui.Button['GuideView']):
    def __init__(self) -> None:
        super().__init__(label='Back', style=discord.ButtonStyle.danger, row=1)

    async def callback(self, interaction: TypedInteraction) -> None:
        self.view._history.pop()
        self.view.update_page(self.view._history[-1], back=True)
        await interaction.response.edit_message(embed=self.view.render(), view=self.view)


class HelpRedirectButton(discord.ui.Button['GuideView']):
    def __init__(self, redirect: HelpRedirect, **kwargs: Any) -> None:
        super().__init__(emoji=redirect.emoji, **kwargs)
        self.entity = redirect.entity

    async def callback(self, interaction: TypedInteraction) -> None:
        self.view.ctx.interaction = interaction
        await self.view.ctx.send_help(self.entity)


class GuideView(UserView):
    BACKSLASH_SUBSTITUTION = re.compile(r'\\.', re.MULTILINE)

    source: str
    lines: list[str]
    page: GuidePage

    def __init__(self, ctx: Context, *, page: str = None) -> None:
        super().__init__(ctx.author, timeout=240.0)

        self.ctx: Context = ctx
        self._embed_color = Colors.primary
        self._history: list[str] = []
        self.update_page(page or 'index')

    @classmethod
    def walk_markdown(cls, ctx: Context, lines: list[str], page: GuidePage, embed: discord.Embed) -> None:
        index = 0
        current = []
        current_field = None

        while index < len(lines):
            line = lines[index]
            line = line.rstrip()
            line = format_line(ctx, line)
            while line.endswith('\\'):
                index += 1
                line = line[:-1] + format_line(ctx, lines[index].rstrip())

            if line.startswith('##'):
                if current:
                    if current_field is not None:
                        embed.add_field(name=current_field, value='\n'.join(current), inline=False)
                    elif current:
                        embed.description = '\n'.join(current)
                    current = []

                current_field = line[2:].lstrip()
            elif line.startswith('#'):
                fmt = '{0} {1}' if page.emoji and page.show_emoji_in_title else '{1}'
                embed.title = fmt.format(page.emoji, line[1:].lstrip())
            else:
                current.append(line)

            index += 1

        if current_field is not None:
            embed.add_field(name=current_field, value='\n'.join(current), inline=False)
        elif current:
            embed.description = '\n'.join(current)

    def update_page(self, source: str, *, back: bool = False) -> None:
        self.source = source
        if not back:
            self._history.append(source)
        self.lines = cast('Miscellaneous', self.ctx.bot.get_cog('Miscellaneous')).get_guide_source_lines(source)
        self.page = GUIDE_PAGES[source]
        self.update()

    def update(self) -> None:
        self.clear_items()
        self.add_item(GuidePageSelect(self.source))

        if len(self._history) > 1:
            self.add_item(BackButton())
        blacklist = self._history[-1] if self._history else None

        for label, redirect in self.page.redirects.items():
            if isinstance(redirect, PageRedirect):
                if blacklist and redirect.page == blacklist:  # don't repeat the back button
                    continue
                self.add_item(PageRedirectButton(label, redirect, row=2 if self.source == 'index' else None))
            elif isinstance(redirect, HelpRedirect):
                self.add_item(HelpRedirectButton(
                    redirect,
                    label=label,
                    style=discord.ButtonStyle.primary,
                    row=2 if self.source == 'index' else None,
                ))
            else:
                self.add_item(StaticCommandButton(
                    command=self.ctx.bot.get_command(redirect.command),
                    command_args=redirect.command_args,
                    command_kwargs=redirect.command_kwargs,
                    label=label,
                    emoji=redirect.emoji,
                    style=discord.ButtonStyle.primary,
                ))

        if self.source == 'index':
            self.add_item(discord.ui.Button(
                label='Add Coined to your server!',
                url=oauth_url(self.ctx.bot.user.id, permissions=discord.Permissions(default_permissions)),
                row=1,
            ))
            self.add_item(discord.ui.Button(label='Support Server', url=support_server, row=1))
            self.add_item(discord.ui.Button(label='Website', url=website, row=1))
            self.add_item(AllCommandsButton())

    def render(self) -> discord.Embed:
        embed = discord.Embed(color=self._embed_color, timestamp=self.ctx.now)
        embed.set_author(name='Coined Guide', icon_url=self.ctx.author.display_avatar)

        if self.page.thumbnail:
            embed.set_thumbnail(url=self.page.thumbnail)
        if self.page.image_url:
            embed.set_image(url=self.page.image_url)

        self.walk_markdown(self.ctx, self.lines, self.page, embed)
        return embed


class PageRedirect(NamedTuple):
    page: str
    style: discord.ButtonStyle = discord.ButtonStyle.secondary
    emoji: str | None = None


class CommandRedirect(NamedTuple):
    command: str
    command_args: list[str] | None = None
    command_kwargs: dict[str, Any] | None = None
    emoji: str | None = None


class HelpRedirect(NamedTuple):
    entity: str
    emoji: str | None = None


class GuidePage(NamedTuple):
    emoji: str | None = None
    show_emoji_in_title: bool = True
    thumbnail: str | None = None
    image_url: str | None = None
    redirects: dict[str, PageRedirect | CommandRedirect] = {}


GUIDE_PAGES: dict[str, GuidePage] = {
    'index': GuidePage(
        emoji='<:coined_yahoo:1140378821288263761>',
        show_emoji_in_title=False,
        thumbnail='https://cdn.discordapp.com/avatars/753017377922482248/1b5ac72b4541c1eb26f2c92076ce5687.png',
        redirects={'Getting Started': PageRedirect('getting-started')},
    ),
    'getting-started': GuidePage(
        emoji='\U0001f44b',
        redirects={
            'Earning Coins': PageRedirect('earning'),
            'Stats That Matter': PageRedirect('stats-that-matter'),
        },
    ),
    'earning': GuidePage(
        emoji='\U0001f4b0',
        redirects={
            'Next Page': PageRedirect('earning-2', style=discord.ButtonStyle.success),
            'Work and Jobs': PageRedirect('work'),
            'Digging & Mining': PageRedirect('digging'),
        },
    ),
    'earning-2': GuidePage(
        emoji='\U0001f4b0',
        redirects={
            'Diving': PageRedirect('diving'),
            'Browse Casino Commands': HelpRedirect('casino'),
        },
    ),
    'digging': GuidePage(
        emoji='<:shovel:1376356818258759751>',
        redirects={
            'Next Page': PageRedirect('digging-2', style=discord.ButtonStyle.success),
            'More ways to profit': PageRedirect('earning'),
            'Begin Digging': CommandRedirect('dig'),
        }
    ),
    'digging-2': GuidePage(
        emoji='<:pickaxe:1376356818258759751>',
        redirects={
            'Crafting Items': PageRedirect('craft'),
            'Pets': PageRedirect('pets'),
            'More ways to profit': PageRedirect('earning'),
            'Begin Digging': CommandRedirect('dig'),
        }
    ),
    'levels': GuidePage(
        emoji='\u2728',
        redirects={
            'View my Level': CommandRedirect('level'),
            'Prestige': PageRedirect('prestige'),
        },
    ),
    'work': GuidePage(
        emoji='\U0001f4bc',
        redirects={
            'Begin Working': CommandRedirect('work'),
        },
    ),
    'diving': GuidePage(
        emoji='\U0001f30a',
        redirects={
            'View more ways to profit': PageRedirect('earning'),
            'Begin Diving': CommandRedirect('dive'),
        }
    ),
    'stats-that-matter': GuidePage(
        emoji='\U0001f4c8',
        redirects={
            'Levels and XP': PageRedirect('levels'),
            'Items': PageRedirect('items'),
            'Skills': PageRedirect('skills'),
            'Farming': PageRedirect('farming'),
            'Pets': PageRedirect('pets'),
        }
    ),
    'skills': GuidePage(emoji='\U0001f52e'),
    'pets': GuidePage(
        emoji='<:dog:1134641292245205123>',
        redirects={
            'How do I hunt?': PageRedirect('hunt'),
            'Equipping Pets': PageRedirect('equip'),
            'Feeding Pets': PageRedirect('feed'),
        },
    ),
    'hunt': GuidePage(
        emoji='<:net:1137070560753496104>',
        image_url='https://cdn.discordapp.com/attachments/1145117706509631548/1147735395136716831/ezgif.com-optimize-3.gif',
        redirects={
            'What are Pets?': PageRedirect('pets'),
            'Equipping Pets': PageRedirect('equip'),
            'Feeding Pets': PageRedirect('feed'),
            'Begin Hunting': CommandRedirect('hunt'),
        },
    ),
    'farming': GuidePage(
        emoji='\U0001f9d1\u200d\U0001f33e',
        redirects={
            'Pets': PageRedirect('pets'),
            'Crafting': PageRedirect('craft'),
            'View your Farm': CommandRedirect('farm'),
        }
    ),
    'craft': GuidePage(
        emoji='\u2692\ufe0f',
        redirects={
            'Items': PageRedirect('items'),
            'Earning Coins': PageRedirect('earning'),
            'Begin Crafting': CommandRedirect('craft'),
        },
    ),
    'items': GuidePage(
        emoji='\U0001f392',
        redirects={
            'Your Inventory': PageRedirect('inventory'),
            'Take a peek at the shop': CommandRedirect('shop'),
            'Crafting': PageRedirect('craft'),
        },
    ),
    'inventory': GuidePage(
        emoji='\U0001f4e6',
        redirects={
            'View your Inventory': CommandRedirect('inventory'),
        },
    ),
    'equip': GuidePage(),
    'feed': GuidePage(
        emoji='<:carrot:941096334365175839>',
        redirects={
            'Begin Feeding': CommandRedirect('feed'),
            'Farming': PageRedirect('farming'),
        }
    ),
    'prestige': GuidePage(
        emoji=Emojis.prestige[1],
        redirects={
            'View your Prestige Requirements': CommandRedirect('prestige'),
        },
    ),
}

GUIDE_SELECT_OPTIONS: dict[str, tuple[str, str, set[str]]] = {
    'Coined Overview': ('index', 'An overview of Coined, which is the guide homepage.', {'index'}),
    'Getting Started': ('getting-started', 'Learn how to get started with Coined.', {'getting-started'}),
    'Earning Coins': ('earning', 'Learn how to earn coins.', {'earning', 'earning-2', 'stats-that-matter'}),
    'Digging & Mining': ('digging', 'Learn about digging and mining.', {'digging', 'digging-2'}),
    'Levels and XP': ('levels', 'Learn more about how levels and experience work.', {'levels'}),
    'Items': ('items', 'Learn more about items and inventory.', {'items', 'inventory', 'craft'}),
    'Farming': ('farming', 'Learn more about crops and the farming system.', {'farming'}),
    'Skills': ('skills', 'Learn more about skills and training them.', {'skills'}),
    'Pets': ('pets', 'Learn more about the pets system.', {'pets', 'hunt', 'equip', 'feed'}),
    'Work and Jobs': ('work', 'Learn more about working and jobs.', {'work'}),
    'Diving': ('diving', 'Learn how to dive wisely to optimize profit.', {'diving'}),
    'Prestige': ('prestige', 'Learn why and how users prestige.', {'prestige'}),
}
