"""The checks of a secret's name, day and value, and the reading of a value that is never printed: what ``hub
secret``, ``hub account``, ``hub agent account``, ``hub plugin config`` and ``hub decision`` share. Standard library
only, like the rest of the client."""

from __future__ import annotations

import getpass
import re
import sys
from datetime import UTC, datetime

from evo_agents.hub.credentials import FORGE_KIND, MAX_SECRET_BYTES, SECRET_NAME

DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class Refused(Exception):
    """The arguments or the value cannot make a secret; ``str`` says why, never with the value."""


def check_name(name: str) -> None:
    if not SECRET_NAME.fullmatch(name):
        raise Refused(
            f"{name!r} is not a secret's name: 1 to 64 of a-z, 0-9, '.', '_' and '-', starting with a letter or digit"
        )


def expires_at(day: str) -> str:
    """``YYYY-MM-DD`` as the moment the secret ends: 00:00 UTC of that day, in ISO 8601."""
    try:
        moment = datetime.strptime(day, "%Y-%m-%d") if DAY.fullmatch(day) else None
    except ValueError:
        moment = None
    if moment is None:
        raise Refused(f"--expires takes a day as YYYY-MM-DD, such as 2027-01-31, not {day!r}")
    return moment.replace(tzinfo=UTC).isoformat()


NO_VALUE = (
    "no value: pipe it on stdin, such as `pbpaste | evo-agents hub secret set NAME ...`, or run the command in a "
    "terminal to be asked for it"
)


def read_value(name: str, stdin=None, prompt: str | None = None, missing: str = NO_VALUE) -> str:
    """The secret's value: stdin when it is not a terminal, one final line break dropped; else asked for without
    echo, with ``prompt``. Refused with ``missing`` when nothing came."""
    stream = sys.stdin if stdin is None else stdin
    if stream is not None and stream.isatty():
        try:
            value = getpass.getpass(prompt or f"Value of secret {name} (not shown): ")
        except EOFError:
            value = ""
    else:
        value = stream.read() if stream is not None else ""
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    if not value:
        raise Refused(missing)
    return value


def check_value(kind: str, value: str) -> None:
    """What the hub would refuse in the value, said without it."""
    if len(value.encode()) > MAX_SECRET_BYTES:
        raise Refused(f"the value is {len(value.encode())} bytes; a secret's value is at most {MAX_SECRET_BYTES}")
    if "\x00" in value:
        raise Refused("the value holds a NUL character, which a secret's value may not")
    if kind == "git" and ("\n" in value or "\r" in value):
        raise Refused("the value of a secret of kind git is one line: git's credential protocol has no other")
    if kind == FORGE_KIND and ("\n" in value or "\r" in value):
        raise Refused("the value of a secret of kind forge is one line: an HTTP header holds it")
