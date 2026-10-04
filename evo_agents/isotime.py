"""ISO 8601 timestamps read the same way on every Python the package supports.

``datetime.fromisoformat`` takes a trailing ``Z`` and a fraction of a second with any number of digits only from
Python 3.11; 3.10 rejects ``2026-10-04T15:59:41.219905Z``, which is how the hub writes every timestamp.
Standard library only.
"""

from __future__ import annotations

import re
from datetime import datetime

_FRACTION = re.compile(r"(?P<time>\d{2}:\d{2}:\d{2})\.(?P<digits>\d+)")


def parse_iso(text: str) -> datetime:
    """``datetime.fromisoformat(text)`` as Python 3.11 reads it: a trailing ``Z`` is UTC and the fraction of a
    second may have any number of digits (cut to microseconds). A naive value stays naive. ValueError otherwise."""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = _FRACTION.sub(lambda m: f"{m['time']}.{m['digits'][:6]:0<6}", text, count=1)
    return datetime.fromisoformat(text)
