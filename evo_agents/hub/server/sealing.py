"""Sealing of the values the hub keeps for runs: AES-256-GCM under EVO_HUB_SECRETS_KEY (``docs/credentials.md``).

A sealed value is the ciphertext with its 16-byte tag, the 12 random bytes of nonce drawn for it, and the key_id of
the key that sealed it: the first 8 hex digits of the key's SHA-256, which names the key without giving it away. The
associated data binds a value to where it belongs, ``secret:{owner_id}:{name}:{kind}`` for a member's secret,
``agent-secret:{profile_id}:{name}`` for an env secret an agent profile owns (never of the member form, so a sealed
value copied between the two kinds of row does not open) and ``lease:{id}`` for a token the hub leased, ``plugin-config:{plugin}:{layer}:{project}:{profile}:{field}`` for a
secret field of a plugin's config, and ``browser-identity:{id}:{version}`` for the data key of one version of a browser
identity's profile (``evo_agents.hub.server.browser_profiles``), and ``market-source:{id}`` for the GitHub token of a
market source (``evo_agents.hub.market_refresh``), so a sealed value copied into another row, or to
another version, does not open there. The key is 32 bytes in base64url, read by ``evo_agents.hub.config``; it never
reaches the database, a log line or an error. ``Sealer.seal`` and ``Sealer.open`` take text; ``seal_bytes`` and
``open_bytes`` take bytes, such as a data key, in the same form.

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


def agent_secret_aad(profile_id: int, name: str) -> bytes:
    """The associated data of an env secret an agent profile owns: it opens only as that profile's secret of this name,
    and never as a member's, whose form is ``secret:{owner_id}:{name}:{kind}``."""
    return f"agent-secret:{profile_id}:{name}".encode()


def agent_provider_aad(profile_id: int, name: str) -> bytes:
    """The associated data of the key of an agent's provider account: it opens neither as an env secret of the
    profile of the same name nor as a member's."""
    return f"agent-provider:{profile_id}:{name}".encode()


def lease_aad(lease_id: int) -> bytes:
    """The associated data of a leased token: it opens only as the value of this lease."""
    return f"lease:{lease_id}".encode()


def plugin_config_aad(plugin_id: int, layer: str, project_id: int | None, profile_id: int | None, name: str) -> bytes:
    """The associated data of a secret field of a plugin's config: it opens only as field ``name`` of that plugin's
    layer ``layer`` of that project and agent profile, at any version of the layer (a rollback copies it)."""
    return f"plugin-config:{plugin_id}:{layer}:{project_id or '-'}:{profile_id or '-'}:{name}".encode()


def browser_identity_aad(identity_id: int, version: int) -> bytes:
    """The associated data of the data key of version ``version`` of a browser identity's profile: it opens only for
    that identity at that version, so a sealed key copied to another identity or version does not open."""
    return f"browser-identity:{identity_id}:{version}".encode()


def market_source_aad(source_id: int) -> bytes:
    """The associated data of the GitHub token of market source ``source_id``: it opens only as that source's."""
    return f"market-source:{source_id}".encode()


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
        return self.seal_bytes(value.encode(), aad)

    def seal_bytes(self, data: bytes, aad: bytes) -> Sealed:
        """``data`` sealed for ``aad``, in the form ``seal`` gives text."""
        nonce = os.urandom(NONCE_BYTES)
        return Sealed(self._aead.encrypt(nonce, data, aad), nonce, self.key_id)

    def open(self, sealed: Sealed, aad: bytes) -> str:
        """The value ``sealed`` holds; ``Unsealable`` when this key did not seal it for ``aad``."""
        try:
            return self.open_bytes(sealed, aad).decode()
        except UnicodeDecodeError:  # bytes sealed as bytes, never as text
            raise _altered(sealed) from None

    def seal_stored(self, value: str, aad: bytes) -> dict:
        """``value`` sealed for ``aad`` as a JSONB column keeps it: ``{ciphertext, nonce, key_id}``, base64."""
        sealed = self.seal(value, aad)
        return {
            "ciphertext": base64.b64encode(sealed.ciphertext).decode(),
            "nonce": base64.b64encode(sealed.nonce).decode(),
            "key_id": sealed.key_id,
        }

    def open_stored(self, stored: dict, aad: bytes) -> str:
        """The value ``seal_stored`` gave ``stored`` for ``aad``; ``Unsealable`` otherwise."""
        try:
            sealed = Sealed(base64.b64decode(stored["ciphertext"]), base64.b64decode(stored["nonce"]), stored["key_id"])
        except (KeyError, TypeError, ValueError):
            raise Unsealable("a sealed value that is not {ciphertext, nonce, key_id}") from None
        return self.open(sealed, aad)

    def open_bytes(self, sealed: Sealed, aad: bytes) -> bytes:
        """The bytes ``sealed`` holds; ``Unsealable`` when this key did not seal them for ``aad``."""
        if sealed.key_id != self.key_id:
            raise Unsealable(f"sealed under key {sealed.key_id}, and the hub's key is {self.key_id}")
        try:
            return self._aead.decrypt(bytes(sealed.nonce), bytes(sealed.ciphertext), aad)
        except (InvalidTag, ValueError):  # ValueError: a nonce of a length AES-GCM does not take
            raise _altered(sealed) from None


def _altered(sealed: Sealed) -> Unsealable:
    return Unsealable(
        f"a value sealed under key {sealed.key_id} does not open here: it was sealed for another place or altered"
    )
