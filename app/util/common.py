from __future__ import annotations

import asyncio
import datetime
import math
import random
import re
from bisect import bisect
from difflib import SequenceMatcher
from functools import wraps
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable, Iterator,
    Mapping,
    MutableMapping,
    Optional,
    ParamSpec,
    Self,
    TYPE_CHECKING,
    Type,
    TypeVar,
    overload,
)

import discord
from discord.ext.commands import Converter

from config import Emojis, support_server

if TYPE_CHECKING:
    from app.core import Context

    Q = TypeVar('Q')
    T = TypeVar('T')
    Ty = TypeVar('Ty', bound=type)
    P = ParamSpec('P')
    R = TypeVar('R')
    K = TypeVar('K')
    V = TypeVar('V')

__all__ = (
    'sentinel',
    'CubicCurve',
    'ExponentialCurve',
    'converter',
)

EMOJI_REGEX: re.Pattern[str] = re.compile(r'<(a)?:([a-zA-Z0-9_]{2,32}):([0-9]{17,25})>')
PLURALIZE_REGEX: re.Pattern[str] = re.compile(r'(?P<quantity>-?[0-9.,]+) (?P<thing>[a-zA-Z]+)\((?P<plural>i?e?s)\)')


# This exists for type checkers
class ConstantT:
    pass


def _create_sentinel_callback(v: V) -> Callable[[ConstantT], V]:
    def wrapper(_self: ConstantT) -> V:
        return v

    return wrapper


def sentinel(name: str, **dunders) -> ConstantT:
    attrs = {f'__{k}__': _create_sentinel_callback(v) for k, v in dunders.items()}
    return type(name, (ConstantT,), attrs)()


def converter(f: Callable[[Context, str], T]) -> Type[Converter | T]:
    class Wrapper(Converter):
        async def convert(self, ctx: Context, argument: str) -> T:
            return await f(ctx, argument)

    return Wrapper


def ordinal(number: int) -> str:
    """Convert a number to its ordinal representation."""
    if number % 100 // 10 != 1:
        if number % 10 == 1:
            return f"{number}st"

        if number % 10 == 2:
            return f"{number}nd"

        if number % 10 == 3:
            return f"{number}rd"

    return f"{number}th"


class BaseCurve:
    def __init__(
        self,
        *,
        precision: int = 50,
        memoize_to: int = 500,
    ) -> None:
        self.precision = precision
        self.memoize_to = memoize_to

        # _sums[N] = total EXP needed to reach level N
        self._sums: list[int] = [self(0)] * (self.memoize_to + 1)

        for n in range(1, self.memoize_to + 1):
            self._sums[n] = self._sums[n - 1] + self(n)

        # Binary search worst-case: ceil(log_2 N)
        # Linear search average-case: (K + 1) / 2
        # Set up (K + 1) // 2 = ceil(log_2 N) => K = 2 * ceil(log_2 N) - 1
        k = 2 * math.ceil(math.log2(self.memoize_to)) - 1
        self._total_exp_linear_threshold: int = self._sums[k]

    def unrounded(self, x: float) -> float:
        raise NotImplementedError

    def requirement_for(self, level: int, /) -> int:
        return math.ceil(self.unrounded(level) / self.precision) * self.precision

    def __call__(self, level: int) -> int:
        return self.requirement_for(level)

    def total_exp_for(self, level: int, /) -> int:
        if level < 0:
            return 0
        if level < self.memoize_to:
            return self._sums[level]

        # If we are above the memoize threshold, we need to calculate it
        for n in range(self.memoize_to, level + 1):
            self._sums.append(self._sums[n - 1] + self(n))
        return self._sums[level]

    def _compute_level_linear(self, exp: int, *, start: int = 0) -> tuple[int, int, int]:
        level = start
        while self.total_exp_for(level) <= exp:
            level += 1

        exp -= self.total_exp_for(level - 1)
        requirement = self.requirement_for(level)
        return level, exp, requirement

    def compute_level(self, exp: int) -> tuple[int, int, int]:
        # Note: computing with a binary search assumes curve is ALWAYS increasing
        if exp < self._total_exp_linear_threshold:
            return self._compute_level_linear(exp)
        if exp > self._sums[-1]:
            return self._compute_level_linear(exp, start=len(self._sums) - 1)

        level = bisect(self._sums, exp)
        exp -= self.total_exp_for(level - 1)
        requirement = self.requirement_for(level)
        return level, exp, requirement


class CubicCurve(BaseCurve):
    """Models a cubic curve, f(x) = ax^3 + bx^2 + cx."""

    def __init__(
        self,
        a: float = 0,
        b: float = 0,
        c: float = 0,
        *,
        threshold: float | None = None,
        **kwargs,
    ) -> None:
        self.a: float = a
        self.b: float = b
        self.c: float = c
        self.threshold: float = threshold
        super().__init__(**kwargs)

    @property
    def solve_threshold(self) -> float:
        return (self.a + self.b + self.c) / 2 if self.threshold is None else self.threshold

    def unrounded(self, x: float) -> float:
        return max(self.solve_threshold, self.a * x ** 3 + self.b * x ** 2 + self.c * x)

    @classmethod
    def default(cls) -> Self:
        return cls(0.25, 11.75, 88)


class ExponentialCurve(BaseCurve):
    """Models an exponential curve, f(x) = (initial)(ratio)^x."""

    def __init__(self, initial: float, ratio: float, **kwargs) -> None:
        self.initial: float = initial
        self.ratio: float = ratio
        super().__init__(**kwargs)

    def unrounded(self, x: float) -> float:
        return self.initial * self.ratio ** x


def image_url_from_emoji(emoji: str | discord.PartialEmoji) -> str:
    if isinstance(emoji, discord.PartialEmoji):
        return emoji.url

    if match := EMOJI_REGEX.match(emoji):
        animated, _, id = match.groups()
        extension = 'gif' if animated else 'png'
        return f'https://cdn.discordapp.com/emojis/{id}.{extension}?v=1'
    else:
        code = '-'.join(format(ord(c), 'x') for c in emoji if c != '\ufe0f')
        return f'https://twitter.github.io/twemoji/v/latest/72x72/{code}.png'


def walk_collection(
    collection: Ty,
    cls: Type[Q],
    *,
    method: Callable[[Ty], Iterable[str]] = dir,
) -> Iterator[Q]:
    for attr in method(collection):
        if attr.startswith('_'):
            continue

        obj = getattr(collection, attr)
        if not isinstance(obj, cls):
            continue

        yield obj


def get_by_key(collection: type, key: str) -> Any:
    for attr in dir(collection):
        if attr.startswith('_'):
            continue

        obj = getattr(collection, attr)
        if hasattr(obj, 'key') and obj.key == key:
            return obj


def query_collection_many(
    collection: type,
    cls: Type[Q],
    query: str,
    *,
    get_key: Callable[[Q], str] = lambda item: item.key,
    get_name: Callable[[Q], str] = lambda item: item.name,
    prioritizer: Callable[[Q], int] = lambda _: 0,  # higher priority = more favorable
) -> Iterable[Q]:
    query = query.lower()
    queued = []

    for obj in walk_collection(collection, cls):
        name = get_name(obj).lower()

        if query in (name, get_key(obj)):
            return [obj]

        if len(query) >= 3 and query in name or query in get_key(obj):
            queued.append(obj)

        matcher = SequenceMatcher(None, query, name)
        if matcher.ratio() > .85 and all(digit not in query for digit in '0123456789'):
            queued.append(obj)

    if queued:
        return sorted(queued, key=lambda item: (-prioritizer(item), len(get_key(item))))
    return []


def query_collection(
    collection: type,
    cls: Type[Q],
    query: str,
    *,
    get_key: Callable[[Q], str] = lambda item: item.key,
    get_name: Callable[[Q], str] = lambda item: item.name,
    prioritizer: Callable[[Q], int] = lambda _: 0,  # higher priority = more favorable
) -> Optional[Q]:
    results = query_collection_many(collection, cls, query, get_key=get_key, get_name=get_name, prioritizer=prioritizer)
    return next(iter(results), None)


def cutoff(string: str, /, max_length: int = 64, *, exact: bool = False) -> str:
    """Cuts-off a string at a certain length, and if it has been cutoff, append "..." to it."""
    if len(string) <= max_length:
        return string

    offset = 0 if not exact else 3
    return string[:max_length - offset] + '...'


def pluralize(text: str, /) -> str:
    """Automatically finds words that need to be pluralized in a string and pluralizes it."""
    def callback(match):
        quantity = abs(float((q := match.group('quantity')).replace(',', '')))
        return f'{q} ' + match.group('thing') + (('', match.group('plural'))[quantity != 1])

    return PLURALIZE_REGEX.sub(callback, text)


def humanize_list(li: list[Any], *, joiner: str = 'and') -> str:
    """Takes a list and returns it joined."""
    if len(li) <= 2:
        return f' {joiner} '.join(li)

    return ", ".join(li[:-1]) + f", {joiner} {li[-1]}"


def humanize_small_duration(seconds: float, /) -> str:
    """Turns a very small duration into a human-readable string."""
    units = ('ms', 'μs', 'ns', 'ps')

    for i, unit in enumerate(units, start=1):
        boundary = 10 ** 3 * i

        if seconds > 1 / boundary:
            m = seconds * boundary
            m = round(m, 2) if m >= 10 else round(m, 3)

            return f"{m} {unit}"

    return "<1 ps"


def humanize_duration(seconds: int | float | datetime.timedelta, depth: int = 3):
    """Formats a duration (in seconds) into one that is human-readable."""
    if isinstance(seconds, datetime.timedelta):
        seconds = seconds.total_seconds()
    if seconds < 1:
        return '<1 second'

    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    mo, d = divmod(d, 30)
    y, mo = divmod(mo, 12)

    if y > 100:
        return ">100 years"

    y, mo, d, h, m, s = [int(entity) for entity in (y, mo, d, h, m, s)]
    items = (y, 'year'), (mo, 'month'), (d, 'day'), (h, 'hour'), (m, 'minute'), (s, 'second')

    as_list = [f"{quantity} {unit}{'s' if quantity != 1 else ''}" for quantity, unit in items if quantity > 0]
    return humanize_list(as_list[:depth])


def insert_random_u200b(text: str, /) -> str:
    """Inserts random zero-width space characters into a string, usually to make them copy-paste proof."""
    return ''.join(c + random.randint(0, 4) * '\u200b' for c in text)


def executor_function(func: Callable[P, R]) -> Callable[P, Awaitable[R]]:
    """Runs the decorated function in an executor"""
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Awaitable[R]:
        return asyncio.to_thread(func, *args, **kwargs)

    return wrapper  # type: ignore


def expansion_list(entries: Iterable[str]) -> str:
    """Formats a list into expansion format."""
    entries = list(entries)
    emojis = Emojis.Expansion

    if len(entries) == 1:
        first, *lines = entries[0].splitlines()
        result = f'{emojis.single} {first}'

        if lines:
            result += '\n' + '\n'.join(f'{Emojis.space} {line}' for line in lines)

        return result

    result = []

    for i, entry in enumerate(entries):
        first, *lines = entry.splitlines()

        if i + 1 == len(entries):
            result.append(f'{emojis.last} {first}')
            result.extend(f'{Emojis.space} {line}' for line in lines)
            continue

        emoji = emojis.first if i == 0 else emojis.mid

        result.append(f'{emoji} {first}')
        result.extend(f'{emojis.ext} {line}' for line in lines)

    return '\n'.join(result)


def progress_bar(ratio: float, *, length: int = 8, u200b: bool = True, provider: type = Emojis.ProgressBars) -> str:
    # noinspection PyTypeChecker
    ratio = min(1, max(0, ratio))

    result = ''
    span = 1 / length

    # Pre-calculate spans
    quarter_span = span / 4
    half_span = span / 2
    high_span = 3 * quarter_span

    for i in range(length):
        lower = i / length

        if ratio <= lower:
            key = 'empty'
        elif ratio <= lower + quarter_span:
            key = 'low'
        elif ratio <= lower + half_span:
            key = 'mid'
        elif ratio <= lower + high_span:
            key = 'high'
        else:
            key = 'full'

        if i == 0:
            start = 'left'
        elif i == length - 1:
            start = 'right'
        else:
            start = 'mid'

        result += getattr(provider, f'{start}_{key}')

    if u200b:
        return result + "\u200b"

    return result


@overload
def pick(d: Mapping[str, V], /, *keys: str, **transform_keys: V) -> dict[str, V]:
    ...


@overload
def pick(d: Mapping[K, V], /, *keys: K) -> dict[K, V]:
    ...


def pick(d: Mapping[K, V], /, *keys: K, **transform_keys: V) -> dict[K, V]:
    """Picks keys from a dictionary and returns them in a new dictionary."""
    if transform_keys:
        return {transform_keys.get(k, k): v for k, v in d.items() if k in keys or k in transform_keys}
    return {k: v for k, v in d.items() if k in keys}


COMMAND_SUBSTITUTION = re.compile(r'\{/([^}]+)}')


def format_line(ctx: Context, line: str) -> str:
    """Formats an external line."""
    line = line.format(support_server=support_server)

    def sub(match: re.Match) -> str:
        cmd = ctx.bot.tree.get_app_command(query := match.group(1))
        if cmd is None:
            return f'/{query}'
        return cmd.mention

    return COMMAND_SUBSTITUTION.sub(sub, line)


def weighted_choices(choices: Mapping[T, int | float], /, k: int) -> list[T]:
    """Returns a random choice from a dictionary of choices with weights."""
    return random.choices(
        list(choices.keys()),
        weights=list(choices.values()),
        k=k,
    )  # type: ignore[no-any-return, no-any-unpack]  # We know the return type is T


def weighted_choice(choices: Mapping[T, int | float], /) -> T:
    """Returns a random choice from a dictionary of choices with weights."""
    return weighted_choices(choices, k=1)[0]  # type: ignore[no-any-return, no-any-unpack]  # We know the return type is T


def adjust_weight(weights: MutableMapping[T, int | float], /, item: T, *, k: float) -> float:
    """Adjusts the weight of an item in a weighted choice dictionary by factor `k`.
    Returns the true factor `r` by which the weight was multiplied.

    For example, if P_old(a_n) = M, then P_new(a_n) = kM.
    """
    # Let a_n be the sequence of keys
    # Let w_n be the weight of the item at key ``item``
    # Let W be the sum of all weights
    #
    # Then we want an r s.t. P(a_n) = kw_n / W = rw_n / ( W + (r-1)w_n )
    # Thus, r = k(w_n - W) / (kw_n - W)

    w_n = weights[item]
    W = sum(weights.values())

    if not (0 <= k * w_n <= W):
        raise ValueError('Invalid weight adjustment: 0 <= kP(a_n) <= 1')

    r = k * (w_n - W) / (k * w_n - W)
    weights[item] *= r
    return r
