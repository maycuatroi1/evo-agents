"""The licence of a paid plugin (plan plugin-market, criterion a7): ``evo-license/1``, an Ed25519 signature of its
seller over what it grants, which a hub checks offline with the key its market's index lists for that seller.

A licence grants plugin ``plugin`` of publisher ``publisher`` to the hub whose stable id is ``hub`` (``hub_identity``)
from day ``from`` up to day ``until``, the first day it no longer covers; both are dates in UTC. ``order`` is the
seller's order it was issued for, ``license`` its own id, ``seller`` the seller's id in the market's index and ``key``
the id of the seller's key that signed it (``evo_agents.plugins.signing.key_id``).

The token is ``base64url(payload) + "." + base64url(signature)`` without padding. The payload is the licence as JSON,
keys sorted, no spaces, with ``v`` 1; the signature is Ed25519 over ``b"evo-license/1\\n" + payload``, so a signature
of another kind of message never reads as a licence. ``read_license`` refuses a token that does not read, whose key
the index does not list for its seller, whose signature is wrong, or that grants another hub or plugin.

Standard library only, besides the ``cryptography`` package the signatures need, imported where they are made or
checked.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from calendar import monthrange
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from evo_agents.plugins.signing import SignatureUnavailable, _public_key, key_id

LICENSE_FORMAT = "evo-license/1"
VERSION = 1
GRACE_DAYS = 7  # after ``until`` a hub keeps the plugin on, and the seller serves its bundle, this many days
REMIND_DAYS = 7  # a hub tells its admins this many days before ``until``
MAX_TOKEN_CHARS = 4096
LICENSE_ID = re.compile(r"lic_[0-9a-f]{16}")
FIELDS = ("from", "hub", "issued", "key", "license", "order", "plugin", "publisher", "seller", "until", "v")
_PREFIX = LICENSE_FORMAT.encode() + b"\n"
_SIGNATURE_BYTES = 64


class LicenseError(ValueError):
    """A licence the hub refuses; ``reason`` is malformed, unknown_key, invalid or not_ours."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class License:
    license: str
    plugin: str
    publisher: str
    seller: str
    hub: str
    order: str
    key: str
    valid_from: date
    valid_until: date
    issued: str  # when the seller signed it, ISO 8601 in UTC

    def state(self, today: date) -> str:
        """``pending`` before it starts, ``active``, ``grace`` for GRACE_DAYS after it ends, then ``lapsed``."""
        if today < self.valid_from:
            return "pending"
        if today < self.valid_until:
            return "active"
        if today < self.valid_until + timedelta(days=GRACE_DAYS):
            return "grace"
        return "lapsed"

    def payload(self) -> dict:
        found = asdict(self)
        found["from"] = found.pop("valid_from").isoformat()
        found["until"] = found.pop("valid_until").isoformat()
        return {**found, "v": VERSION}


def add_months(day: date, months: int) -> date:
    """``day`` moved ``months`` calendar months on, on the same day of the month or the month's last."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise LicenseError("malformed", "a licence is two parts in base64url separated by a dot") from None


def _message(payload: bytes) -> bytes:
    return _PREFIX + payload


def canonical(fields: Mapping) -> bytes:
    return json.dumps(dict(fields), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_license(granted: License, private_key: bytes) -> str:
    """The token of ``granted``, signed with ``private_key``, the 32-byte seed of the seller's key."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        raise SignatureUnavailable("signing a licence needs the cryptography package, which is not installed") from None
    payload = canonical(granted.payload())
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(_message(payload))
    return f"{_b64(payload)}.{_b64(signature)}"


def new_key() -> tuple[bytes, str]:
    """A new Ed25519 key of a seller: its 32-byte seed, and its public key in base64 as a market's index lists it."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return seed, base64.b64encode(public).decode()


def public_key_of(private_key: bytes) -> str:
    """The public key of seed ``private_key``, in base64."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    public = Ed25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(public).decode()


def seller_keys(seller: Mapping) -> dict[str, bytes]:
    """key id -> public key of each key a market's index lists for ``seller`` (``sellers[]`` of plugin-market-v1)."""
    found = {}
    for key in seller.get("keys") or ():
        try:
            raw = _public_key(key["public_key"])
        except (KeyError, ValueError):
            continue
        if key_id(raw) == key.get("key_id"):
            found[key["key_id"]] = raw
    return found


def _fields(payload: bytes) -> License:
    try:
        data = json.loads(payload)
    except ValueError:
        raise LicenseError("malformed", "a licence's payload is JSON") from None
    if not isinstance(data, dict) or sorted(data) != sorted(FIELDS) or data.get("v") != VERSION:
        raise LicenseError("malformed", f"a licence of {LICENSE_FORMAT} has exactly the keys {', '.join(FIELDS)}")
    if not all(isinstance(data[name], str) for name in FIELDS if name != "v"):
        raise LicenseError("malformed", "the fields of a licence are text")
    try:
        valid_from, valid_until = date.fromisoformat(data["from"]), date.fromisoformat(data["until"])
    except ValueError:
        raise LicenseError("malformed", "a licence's from and until are dates, YYYY-MM-DD") from None
    if valid_until <= valid_from or not LICENSE_ID.fullmatch(data["license"]):
        raise LicenseError("malformed", "a licence ends after it starts and has an id lic_ and 16 hex digits")
    return License(
        license=data["license"],
        plugin=data["plugin"],
        publisher=data["publisher"],
        seller=data["seller"],
        hub=data["hub"],
        order=data["order"],
        key=data["key"],
        valid_from=valid_from,
        valid_until=valid_until,
        issued=data["issued"],
    )


def read_license(token: str, keys: Mapping[str, bytes], *, hub: str | None = None, plugin: str | None = None):
    """The licence ``token`` grants, checked against ``keys`` (``seller_keys``), and granting hub ``hub`` and plugin
    ``plugin`` when given; LicenseError otherwise, SignatureUnavailable without the cryptography package."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or token.count(".") != 1:
        raise LicenseError("malformed", f"a licence of {LICENSE_FORMAT} is PAYLOAD.SIGNATURE in base64url")
    encoded, signed = token.split(".")
    payload, signature = _unb64(encoded), _unb64(signed)
    granted = _fields(payload)
    if canonical(granted.payload()) != payload:
        raise LicenseError("malformed", "a licence's payload is its JSON with sorted keys and no spaces")
    public = keys.get(granted.key)
    if public is None:
        raise LicenseError(
            "unknown_key", f"licence {granted.license} is signed with key {granted.key}, which the market does not list"
        )
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise SignatureUnavailable(
            "checking a licence needs the cryptography package, which is not installed"
        ) from None
    if len(signature) != _SIGNATURE_BYTES:
        raise LicenseError("malformed", f"a licence's signature is {_SIGNATURE_BYTES} bytes")
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, _message(payload))
    except InvalidSignature:
        raise LicenseError("invalid", f"the signature of licence {granted.license} is wrong") from None
    if hub is not None and granted.hub != hub:
        raise LicenseError("not_ours", f"licence {granted.license} grants hub {granted.hub}, not this hub ({hub})")
    if plugin is not None and granted.plugin != plugin:
        raise LicenseError("not_ours", f"licence {granted.license} grants plugin {granted.plugin}, not {plugin}")
    return granted
