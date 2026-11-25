"""Time parsing and formatting helpers for single-day timetables."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Union

Number = Union[int, float, str]


def parse_clock(value: Number) -> int:
    """Parse HH:MM[:SS], ISO timestamps, or seconds since midnight.

    Timetables sometimes use 24:xx or 25:xx for after-midnight service;
    those are accepted as ordinary numbers of seconds.
    """
    if isinstance(value, (int, float)):
        return int(value)
    if value is None:
        raise ValueError("time is required")
    text = str(value).strip()
    if not text:
        raise ValueError("time is required")
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return int(float(text))

    # ISO date-time: take the clock portion.  Trailing Z is accepted.
    if "T" in text or "t" in text:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.hour * 3600 + dt.minute * 60 + dt.second
        except ValueError:
            time_part = re.split(r"[Tt]", text, 1)[1]
            text = time_part

    match = re.fullmatch(r"(\d{1,3}):(\d{2})(?::(\d{2})(?:\.\d+)?)?", text)
    if not match:
        raise ValueError(f"unsupported time format: {value!r}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    if minute >= 60 or second >= 60:
        raise ValueError(f"invalid clock time: {value!r}")
    return hour * 3600 + minute * 60 + second


def format_clock(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    sign = "-" if seconds < 0 else ""
    seconds = abs(int(seconds))
    hour, rem = divmod(seconds, 3600)
    minute, second = divmod(rem, 60)
    return f"{sign}{hour:02d}:{minute:02d}:{second:02d}"
