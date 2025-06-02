from __future__ import annotations

import functools
import re
from datetime import timedelta
from typing import Callable, ClassVar, Final, Literal, NamedTuple, Type, TYPE_CHECKING

import discord
from discord.ext.commands import BadArgument, Converter, MemberConverter, MemberNotFound

from app.data.items import CropMetadata, Item, ItemType, Items, HarvestMetadata
from app.data.recipes import Recipe, Recipes
from app.data.settings import Setting, Settings
from app.data.skills import Skill, Skills
from app.util.common import converter, query_collection, walk_collection
from config import Emojis

if TYPE_CHECKING:
    from app.core import Context

    better_bool: Type[bool]
    query_item: Type[Item]
    query_skill: Type[Skill]
    query_setting: Type[Setting]
    query_recipe: Type[Recipe]

    IntervalConverter: Type[timedelta]

RELATIVE_TIME_REGEX: Final[re.Pattern[str]] = re.compile(
    r"(?:(?P<years>[0-9]{1,2})\s*(?:years?|yrs?|y)[, ]*)?"
    r"(?:(?P<months>[0-9]{1,2})\s*(?:months?|mo)[, ]*)?"
    r"(?:(?P<weeks>[0-9]{1,4})\s*(?:weeks?|wks?|w)[, ]*)?"
    r"(?:(?P<days>[0-9]{1,5})\s*(?:days?|d)[, ]*)?"
    r"(?:(?P<hours>[0-9]{1,5})\s*(?:hours?|hrs?|h)[, ]*)?"
    r"(?:(?P<minutes>[0-9]{1,5})\s*(?:minutes?|mins?|m)[, ]*)?"
    r"(?:(?P<seconds>[0-9]{1,5})\s*(?:seconds?|secs?|s))?",
)


class IntervalConverter(Converter[timedelta]):
    """Return a timedelta representing the interval of time specified.

    This can also be used as a way to parse relative time.
    """

    INTERVALS: Final[ClassVar[dict[str, int]]] = {
        'seconds': 1,
        'minutes': 60,
        'hours': 3600,
        'days': 86400,
        'weeks': 604800,
        'months': 86400 * 30,  # TODO: make month calculation more accurate. Right now it's constant to 30 days.
        'years': 86400 * 365,  # TODO: year calculation could also be adjusted to account for leap years.
    }

    @classmethod
    def _convert(cls, argument: str) -> tuple[timedelta, re.Match[str]]:
        """Convert the argument to a timedelta."""
        match = RELATIVE_TIME_REGEX.search(argument)
        if match is None:
            raise BadArgument(
                'Invalid time or time interval given. Try something such as "5 days" next time.',
            )

        groups = match.groupdict(default=0)
        seconds = sum(
            cls.INTERVALS[key] * float(value)
            for key, value in groups.items()
        )

        if seconds <= 0:
            raise BadArgument('Invalid time or time interval given.')

        return timedelta(seconds=seconds), match

    async def convert(self, ctx: Context, argument: str) -> timedelta:
        return self._convert(argument)[0]


def get_number(argument: str) -> int:
    argument = argument.lower().replace(",", "").replace("+", "").strip()
    if argument == "":
        raise ValueError()

    match argument[-1]:
        case 'k':
            argument = float(argument.rstrip("k")) * 1_000
        case 'm':
            argument = float(argument.rstrip("m")) * 1_000_000
        case 'b':
            argument = float(argument.rstrip("b")) * 1_000_000_000
        case _:
            if re.match(r"\de\d+", argument):
                num, exp = argument.split("e")
                num, exp = float(num), int(exp)
                argument = float(f"{num}e{exp}") if exp < 24 else 1e24

    return round(float(argument))


@converter
async def RichInteger(_, argument: str) -> int:
    return get_number(argument)


class NotAnInteger(Exception):
    pass


class ZeroQuantity(NotAnInteger):
    pass


class NotEnough(Exception):
    def __init__(self, amount: int) -> None:
        self.amount = amount


class PastMinimum(Exception):
    pass


class IncompatibleItemType(BadArgument):
    pass


class BadItemArgument(BadArgument):
    pass


def get_amount(total: float, minimum: int, maximum: int, arg: str) -> int:
    """Gets an amount of coins given an argument.

    Supports all/max, half, n/n fractions, and percentages. (and actual numbers, of course.)
    """
    arg = arg.lower().strip()

    if arg in ("all", "max", "a", "m"):
        amount = round(total)

    elif arg in ("half", "h"):
        amount = round(total / 2)

    elif arg.endswith("%"):
        percent = arg.rstrip('%')
        try:
            percent = float(percent) / 100
        except (TypeError, ValueError):
            raise NotAnInteger()
        else:
            amount = round(total * percent)

    elif re.match(r"[0-9.]+/[0-9.]+", arg):
        try:
            num, de = arg.split("/")
            num, de = float(num), float(de)
        except (ValueError, TypeError):
            raise NotAnInteger()
        else:
            if de == 0:
                raise ZeroDivisionError()

            amount = round(total * (num / de))
    else:
        try:
            amount = get_number(arg)
        except (ValueError, ZeroDivisionError, TimeoutError, IndexError, KeyError):
            raise NotAnInteger()

    if amount > total:
        raise NotEnough(amount)

    if minimum >= 0 and amount <= 0:
        if total == 0:
            raise ZeroQuantity()
        raise NotAnInteger()

    if minimum <= amount <= maximum:
        return amount

    elif amount > maximum:
        return maximum

    raise PastMinimum()


@converter
async def CaseInsensitiveMemberConverter(ctx: Context, argument: str) -> discord.Member:
    # This may not scale too well.
    try:
        return await MemberConverter().convert(ctx, argument)
    except MemberNotFound:
        argument = argument.lower()

        def check(member: discord.Member):
            return (
                member.name.lower() == argument
                or member.global_name and member.global_name.lower() == argument
                or member.display_name.lower() == argument
                or str(member).lower() == argument  # legacy discriminators support
            )

        if found := discord.utils.find(check, ctx.guild.members):
            return found

        raise MemberNotFound(argument)


class ItemAndQuantity(NamedTuple):
    item: Item
    quantity: int = 1


BUY = 'buy'
SELL = 'sell'
USE = 'use'
DROP = 'drop'
BYPASS = 'spawn'


def _unambiguous_query_item(query: str, /, **kwargs) -> Item | None:
    if query.lower() in ('all', 'max', 'a', 'm'):
        return None

    return try_query_item(query, **kwargs)


def parse_quantity_and_item(argument: str, **kwargs) -> tuple[Item | None, str]:
    item: Item | None = None
    quantity = '1'

    if result := _unambiguous_query_item(argument, **kwargs):
        item = result

    elif len(split := argument.split()) > 1:
        result, quantity = ' '.join(split[:-1]), split[-1]

        if result := _unambiguous_query_item(result, **kwargs):
            item = result

        if not item:
            result, quantity = ' '.join(split[1:]), split[0]
            if result := _unambiguous_query_item(result, **kwargs):
                item = result

    return item, quantity


async def transform_item_and_quantity(
    ctx: Context, method: Literal[0, 1, 2], item: Item, quantity: str,
) -> ItemAndQuantity:
    plural = item.get_display_name(plural=True)
    if method == BUY and not item.buyable:
        raise IncompatibleItemType(f'{plural} are currently not buyable.')

    if method == SELL and not item.sellable:
        raise IncompatibleItemType(f'{plural} are not sellable.')

    if method == USE and not item.usable:
        raise IncompatibleItemType(f'{plural} are not usable.')

    if method == DROP and not item.giftable:
        raise IncompatibleItemType(f'{plural} are not giftable.')

    record = await ctx.db.get_user_record(ctx.author.id)
    minimum = 1

    if method == BUY:
        maximum = record.wallet // item.price
    elif method == BYPASS:
        maximum = 2 ** 63  # bigint max
        minimum = -maximum
    else:
        inventory = await record.inventory_manager.wait()
        maximum = inventory.cached.quantity_of(item)

    try:
        quantity = get_amount(maximum, minimum, maximum, quantity)
    except PastMinimum:
        raise BadItemArgument(f'You must {method} at least one of that item.')
    except ZeroQuantity:
        raise BadItemArgument(
            "You can't afford that, pooron" if method == BUY else f'You don\'t have any {plural}.'
        )
    except NotAnInteger:
        raise BadItemArgument(
            f'Invalid quantity {quantity} - either what you specified yields 0, is negative, or it is not an integer.',
        )
    except NotEnough as exc:
        quantity = exc.amount
        raise BadItemArgument(
            f'Insufficient funds, you need {Emojis.coin} **{item.price * quantity:,}** to buy '
            f'{item.get_sentence_chunk(quantity)}, but you only have {Emojis.coin} **{record.wallet:,}**.'
            if method == BUY
            else f'You don\'t have that many, you only own {item.get_sentence_chunk(maximum)}.'
        )
    except ZeroDivisionError:
        raise BadItemArgument('Very funny, division by 0.')

    return ItemAndQuantity(item, quantity)


def ItemAndQuantityConverter(method: Literal[0, 1, 2]) -> Type[Converter | ItemAndQuantity]:
    class Wrapper(Converter):
        async def convert(self, ctx: Context, argument: str) -> ItemAndQuantity:
            kwargs = {'prioritizer': lambda it: it.buyable} if method == BUY else {}
            item, quantity = parse_quantity_and_item(argument, **kwargs)

            if not item:
                raise BadArgument(f'Item "{argument}" not found.')

            return await transform_item_and_quantity(ctx, method, item, quantity)

    return Wrapper


try_query_item = functools.partial(query_collection, Items, Item)


def query_item(query: str, /, *, prioritizer: Callable[[Item], int] = lambda _: 0) -> Item:
    if match := try_query_item(query, prioritizer=prioritizer):
        return match

    raise BadArgument(f"I couldn't find a item named {query!r}.")


def query_repairable_item(query: str, /) -> Item:
    item = query_item(query, prioritizer=lambda i: i.durability is not None)
    if item.durability is None:
        raise BadArgument(f'{item.get_display_name(plural=True)} are not repairable.')

    return item


def query_crop(query: str, /) -> Item:
    try:
        crop = query_item(query.lower().removesuffix(' crop') + ' crop')
    except BadArgument:
        crop = query_item(query)

    if crop is None:
        raise BadArgument(f"I couldn't find a crop named {query!r}.")

    if crop.type is not ItemType.crop:
        if crop.type is ItemType.harvest and crop.metadata is not None:
            crop: Item[HarvestMetadata]
            crop: Item[CropMetadata] = crop.metadata.get_source_crop()
        else:
            crop = query_item(crop.name + ' crop')

    if not isinstance(crop, Item) or crop.type is not ItemType.crop:
        raise BadArgument(f'{crop.name} is not a crop.')

    return crop


def query_skill(query: str, /) -> Skill:
    if match := query_collection(Skills, Skill, query):
        return match

    raise BadArgument(f"I couldn't find a skill named {query!r}.")


def query_setting(query: str, /) -> Setting:
    if match := query_collection(Settings, Setting, query):
        return match

    raise BadArgument(f"I couldn't find a setting named {query!r}.")


def query_recipe(query: str, /) -> Recipe:
    if match := query_collection(Recipes, Recipe, query):
        return match

    raise BadArgument(f"I couldn't find a craftable item/recipe named {query!r}.")


def better_bool(arg: str, /) -> bool:
    arg = arg.lower()

    if arg in {'true', 'yes', 'y', 'on', 'enable', 'enabled', 't', '1'}:
        return True

    if arg in {'false', 'no', 'n', 'off', 'disable', 'disabled', 'f', '0'}:
        return False

    raise BadArgument(f'Invalid boolean value {arg!r}')


@converter
async def RecipeConverter(ctx: Context, argument: str) -> Recipe:
    to_raise = BadArgument(
        f'Invalid craft entities given. Format your recipe by separating them with commas, e.g. `{ctx.clean_prefix}craft 3 wood, 2 iron`'
    )

    entities = map(str.strip, argument.rstrip(',').split(','))
    try:
        entities = {item: int(raw_quantity) for item, raw_quantity in map(parse_quantity_and_item, entities)}
    except ValueError:
        raise to_raise

    if None in entities:
        raise to_raise

    recipe = discord.utils.get(walk_collection(Recipes, Recipe), ingredients=entities)
    if not recipe:
        raise BadArgument(
            'Could not craft anything from that. Note that you can only craft/discover one item at once using this command. '
            f'You can craft already discovered recipes in bulk using the `{ctx.clean_prefix}recipes` command.'
        )

    return recipe


WITHDRAW = 0
DEPOSIT = 1


def BankTransaction(method: Literal[0, 1]) -> Type[Converter | int]:
    class Wrapper(Converter, int):
        async def convert(self, ctx: Context, arg: str) -> int:
            record = await ctx.db.get_user_record(ctx.author.id)
            _all = getattr(record, 'wallet' if method == DEPOSIT else 'bank')

            maximum = record.max_bank - record.bank if method == DEPOSIT else record.bank

            if maximum <= 0:
                raise BadArgument(
                    "You have no more space in your bank. Consider expanding it."
                    if method == DEPOSIT
                    else "You don't have any coins to withdraw."
                )

            verb = 'withdraw' if method == WITHDRAW else 'deposit'
            try:
                return get_amount(_all, 0, maximum, arg)
            except ZeroQuantity:
                raise BadArgument(f"You have nothing to {verb}.")
            except NotAnInteger:
                raise BadArgument(f"{verb.capitalize()} amount must be a positive integer.")
            except NotEnough:
                raise BadArgument(
                    "You don't have that many coins in your wallet, get better."
                    if method == DEPOSIT
                    else "You can't withdraw more than what you actually have."
                )
            except ZeroDivisionError:
                raise BadArgument("Very funny. Division by 0.")

    return Wrapper


def Investment(minimum: int = 500, maximum: int = 50000000) -> Type[Converter | int]:
    class Wrapper(Converter, int):
        async def convert(self, ctx: Context, arg: str) -> int:
            record = await ctx.db.get_user_record(ctx.author.id)
            _all = record.wallet

            try:
                return get_amount(_all, minimum, maximum, arg)
            except ZeroQuantity:
                raise BadArgument("You don't have any coins in your wallet to invest, maybe get some?")
            except NotAnInteger:
                raise BadArgument("Investment amount must be a positive integer.")
            except NotEnough:
                raise BadArgument(
                    f"You don't have that many coins. You only have {Emojis.coin} **{record.wallet:,}** in your wallet."
                )
            except PastMinimum:
                raise BadArgument(f"The minimum investment is {Emojis.coin} **{minimum:,}**.")
            except ZeroDivisionError:
                raise BadArgument("very funny, division by zero.")

    return Wrapper


@converter
async def DropAmount(ctx: Context, arg: str) -> int:
    record = await ctx.db.get_user_record(ctx.author.id)
    _all = record.wallet

    try:
        return get_amount(_all, 1, _all, arg)
    except ZeroQuantity:
        raise BadArgument("You don't have any coins in your wallet, pooron")
    except NotAnInteger:
        raise BadArgument("Entity must be a positive integer or a valid item with an optional quantity.")
    except NotEnough:
        raise BadArgument(
            f"You don't have that many coins. You only have {Emojis.coin} **{record.wallet:,}** in your wallet."
        )
    except PastMinimum:
        raise BadArgument("Amount must be positive.")
    except ZeroDivisionError:
        raise BadArgument("very funny, division by zero.")


def CasinoBet(minimum: int = 200, maximum: int = 500000) -> Type[Converter | int]:
    class Wrapper(Converter, int):
        async def convert(self, ctx: Context, argument: str):
            record = await ctx.db.get_user_record(ctx.author.id)

            try:
                return get_amount(record.wallet, minimum, maximum, argument)
            except ZeroQuantity:
                raise BadArgument("You dont have any coins in your wallet to bet, pooron")
            except NotAnInteger:
                raise BadArgument("Bet amount must be a positive integer.")
            except NotEnough:
                raise BadArgument(
                    f"You don't have that many coins. You only have {Emojis.coin} **{record.wallet:,}** in your wallet."
                )
            except PastMinimum:
                raise BadArgument(
                    f"The minimum bet for `{ctx.command.qualified_name}` is {Emojis.coin} **{minimum:,}**.",
                )
            except ZeroDivisionError:
                raise BadArgument("very funny, division by zero.")

    return Wrapper
