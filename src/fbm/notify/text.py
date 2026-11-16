"""Formatting for chat messages: percentages, owner-local clock times, day labels, and fitting a size budget."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NO_REPLY = "NO_REPLY"  # the exact line an alert prints when there is nothing new


def zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def pct(p: float) -> str:
    return f"{round(float(p) * 100):d}%"


def clock(moment: datetime, tz: tzinfo) -> str:
    """'7:05 PM CST' in the given zone (no platform-specific strftime flags)."""
    local = moment.astimezone(tz)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'} {local.tzname() or ''}".strip()


def until(moment: datetime, now: datetime) -> str:
    minutes = max(0, round((moment - now).total_seconds() / 60))
    return f"in {minutes} min" if minutes < 60 else f"in {minutes // 60} h {minutes % 60:02d} min"


def day_label(day: date) -> str:
    return day.strftime("%a %b ") + str(day.day)


_MARKUP = str.maketrans({"*": "", "`": "'", "[": "(", "]": ")"})


def clip(text: object, chars: int) -> str:
    """Quoted data (ESPN notes, engine reasons, errors) as one plain line of at most `chars`: Markdown control
    characters are neutralized so quoted text never formats or links."""
    s = " ".join(str(text or "").translate(_MARKUP).split())
    return s if len(s) <= chars else s[: chars - 1].rstrip() + "…"


def fit(head: Sequence[str], sections: Sequence[Sequence[str]], limit: int, more: str) -> str:
    """Head lines, then whole sections in priority order while they fit in `limit` characters; a final `more` line
    (with {n} = sections left out) replaces whatever doesn't fit."""
    text = "\n".join(head)
    dropped = 0
    for section in sections:
        block = "\n".join(section)
        if not block:
            continue
        if len(text) + 2 + len(block) <= limit - len(more) - 2:
            text += "\n\n" + block
        else:
            dropped += 1
    if dropped:
        text += "\n\n" + more.format(n=dropped)
    return text[:limit]
