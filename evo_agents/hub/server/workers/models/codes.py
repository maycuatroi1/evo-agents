"""Pairing codes and the joins they make: the form of a code (Crockford base32, CODE_LENGTH characters written
XXXX-XXXX, the first SELECTOR_LENGTH kept in clear), how long a pairing lasts and how many a member holds, the wrong
tries that lock one, the refused joins an address may make, and the texts of the refusals."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Literal

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O or U
_READ_AS = str.maketrans({"O": "0", "I": "1", "L": "1"})  # what Crockford base32 reads the look-alikes as
CODE_LENGTH = 8
SELECTOR_LENGTH = 4  # the start of a code, kept in clear so a wrong try finds the pairing it is against
PAIRING_TTL = timedelta(minutes=10)
PAIRING_KEPT = timedelta(days=1)  # an unused pairing is deleted once its expiry is this far behind
MAX_LIVE_PAIRINGS = 5  # per member: neither used nor expired, locked ones included
MAX_WRONG_TRIES = 5
PAIRING_PURPOSE = "pairing-code"  # keeps the HMAC of a code apart from every other HMAC of the session secret
JOIN_REFUSALS = 10  # refused joins from one address within JOIN_WINDOW, before the next join gets 429
JOIN_WINDOW = 600.0  # seconds
MAX_JOIN_CLIENTS = 10_000  # addresses RefusalLimit tracks at once; past that it forgets the least recently refused
DRAWS = 20  # codes drawn before giving up on a selector no unused pairing holds
REFUSED_CODE = (
    "the pairing code is wrong, expired, used already or locked after 5 wrong tries: create a new one on the web"
)
NO_SECRET = "pairing codes need EVO_HUB_SESSION_SECRET, which this hub does not have: ask its operator to set it"


def new_code() -> str:
    """A pairing code in its stored form: CODE_LENGTH characters of Crockford base32, without the hyphen."""
    return "".join(secrets.choice(CROCKFORD) for _ in range(CODE_LENGTH))


def normal_code(text: str) -> str | None:
    """The code ``text`` names, read as Crockford base32 does: any case, hyphens and spaces left out, O read as 0 and
    I and L as 1. None when that is not CODE_LENGTH characters of the alphabet."""
    code = "".join(text.split()).replace("-", "").upper().translate(_READ_AS)
    if len(code) != CODE_LENGTH or any(char not in CROCKFORD for char in code):
        return None
    return code


def shown_code(code: str) -> str:
    return f"{code[:SELECTOR_LENGTH]}-{code[SELECTOR_LENGTH:]}"


def pairing_status(used: bool, attempts: int, expired: bool) -> Literal["joined", "locked", "expired", "waiting"]:
    """What a pairing is now: joined, locked after MAX_WRONG_TRIES wrong tries, expired or waiting."""
    if used:
        return "joined"
    if attempts >= MAX_WRONG_TRIES:
        return "locked"
    return "expired" if expired else "waiting"
