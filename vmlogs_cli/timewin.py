"""Окно времени запроса: длительность назад от «сейчас» или RFC3339 → RFC3339 UTC.

VictoriaLogs не понимает start=5m как «5 минут назад», поэтому окно считаем сами.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .errors import CliError

_DURATION = re.compile(r"^(\d+)([smhdw])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_FRACTION = re.compile(r"\.(\d+)")
_DATE_SEP = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]")
_ZULU = re.compile(r"[Zz]$")  # только завершающий: «Z» в середине — это ошибка, а не UTC


def _fmt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_point(value: str, now: datetime) -> str:
    """`15m`, `2h`, `1d`, `1w` — столько назад от now; иначе RFC3339 с часовым поясом."""
    match = _DURATION.match(value)
    if match:
        try:
            return _fmt(now - timedelta(**{_UNITS[match.group(2)]: int(match.group(1))}))
        except OverflowError:
            raise CliError(f"длительность «{value}» слишком большая") from None
    # Python 3.9 понимает в дробной части секунд только 3 или 6 цифр, VictoriaLogs пишет 9.
    text = _FRACTION.sub(
        lambda f: "." + f.group(1)[:6].ljust(6, "0"), _ZULU.sub("+00:00", value), count=1
    )
    try:
        # fromisoformat принимает любой символ между датой и временем, в том числе «Z».
        if not _DATE_SEP.match(text):
            raise ValueError(text)
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise CliError(
            f"не понял время «{value}»: нужна длительность (30m, 2h, 1d) или RFC3339"
        ) from None
    if moment.tzinfo is None:
        raise CliError(f"у времени «{value}» нет часового пояса: добавь Z или смещение (+03:00)")
    return _fmt(moment)


def _pad(point: str) -> str:
    """Результат parse_point (всегда с Z) в вид, понятный fromisoformat на Python 3.9."""
    return point[:-1] + "+00:00"


def window(
    since: str, start: Optional[str], end: Optional[str], now: datetime
) -> tuple[str, Optional[str]]:
    """(start, end) для API: --start перебивает --since, end — только если задан."""
    first = parse_point(start or since, now)
    last = parse_point(end, now) if end else None
    # Строки RFC3339 в UTC с одинаковой длиной сравнимы как строки, но доли секунды бывают разной длины.
    if last and datetime.fromisoformat(_pad(first)) > datetime.fromisoformat(_pad(last)):
        raise CliError(f"начало окна позже конца: {first} > {last}")
    return first, last
