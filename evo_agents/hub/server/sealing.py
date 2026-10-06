"""Sealing of the values the hub keeps for runs: AES-256-GCM under EVO_HUB_SECRETS_KEY (``docs/credentials.md``).

A sealed value is the ciphertext with its 16-byte tag, the 12 random bytes of nonce drawn for it, and the key_id of
the key that sealed it: the first 8 hex digits of the key's SHA-256, which names the key without giving it away. The
associated data binds a value to where it belongs, ``secret:{owner_id}:{name}:{kind}`` for a member's secret and
``lease:{id}`` for a token the hub leased, so a sealed value copied into another row does not open there. The key
is 32 bytes in base64url, read by ``evo_agents.hub.config``; it never reaches the database, a log line or an error.

Without the key the hub keeps no secrets: ``Sealer.from_config`` answers None, a route that writes a secret answers
503 and a run asking for its leases gets none, with ``HubConfig.credentials_missing`` as the reason.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:
    from evo_agents.hub.config import HubConfig

KEY_BYTES = 32  # AES-256
NONCE_BYTES = 12  # drawn anew for every value sealed
TAG_BYTES = 16  # at the end of the ciphertext
KEY_ID_CHARS = 8


class Unsealable(Exception):
    """A sealed value that does not open: another key sealed it, it belongs to another place, or it was altered."""


@dataclass(frozen=True)
class Sealed:
    """A value as the database keeps it: secrets.sealed or credential_leases.sealed_value, nonce, key_id."""

    ciphertext: bytes
    nonce: bytes
    key_id: str


def key_id(key: bytes) -> str:
    """The name of ``key`` that a sealed value records: the first 8 hex digits of its SHA-256."""
    return hashlib.sha256(key).hexdigest()[:KEY_ID_CHARS]


def new_key() -> str:
    """A fresh key as EVO_HUB_SECRETS_KEY takes it: 32 random bytes in base64url, without padding."""
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).rstrip(b"=").decode()


def secret_aad(owner_id: int, name: str, kind: str) -> bytes:
    """The associated data of a member's secret: its value opens only as this owner's secret of this name and kind."""
    return f"secret:{owner_id}:{name}:{kind}".encode()


def lease_aad(lease_id: int) -> bytes:
    """The associated data of a leased token: it opens only as the value of this lease."""
    return f"lease:{lease_id}".encode()


class Sealer:
    """Seals and opens values under one key; ``repr`` shows its key_id, never the key."""

    def __init__(self, key: bytes):
        if len(key) != KEY_BYTES:
            raise ValueError(f"a sealing key is {KEY_BYTES} bytes, not {len(key)}")
        self._aead = AESGCM(key)
        self.key_id = key_id(key)

    @classmethod
    def from_config(cls, config: HubConfig) -> Sealer | None:
        """The hub's sealer, or None when EVO_HUB_SECRETS_KEY is not set."""
        return cls(config.secrets_key) if config.secrets_key else None

    def __repr__(self) -> str:
        return f"Sealer(key_id={self.key_id!r})"

    def seal(self, value: str, aad: bytes) -> Sealed:
        nonce = os.urandom(NONCE_BYTES)
        return Sealed(self._aead.encrypt(nonce, value.encode(), aad), nonce, self.key_id)

    def open(self, sealed: Sealed, aad: bytes) -> str:
        """The value ``sealed`` holds; ``Unsealable`` when this key did not seal it for ``aad``."""
        if sealed.key_id != self.key_id:
            raise Unsealable(f"sealed under key {sealed.key_id}, and the hub's key is {self.key_id}")
        try:
            return self._aead.decrypt(bytes(sealed.nonce), bytes(sealed.ciphertext), aad).decode()
        except (InvalidTag, ValueError):  # ValueError: a nonce of a length AES-GCM does not take
            raise Unsealable(
                f"a value sealed under key {sealed.key_id} does not open here: it was sealed for another place "
                "or altered"
            ) from None
