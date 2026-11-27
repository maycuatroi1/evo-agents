"""How the hub keeps the profile of a browser identity (``service.profiles``): the kind and layout of a sealed
profile, the largest archive it seals, the columns of a version, what a save refuses with, and one version sealed."""

from __future__ import annotations

from dataclasses import dataclass

from evo_agents.hub.blobs import KIND_LIMITS
from evo_agents.hub.server.credentials.models.sealing import NONCE_BYTES, TAG_BYTES, Sealed

KIND = "browser-profile"
FORMAT = b"\x01"  # the first byte of a sealed profile: this layout, AES-256-GCM under the version's data key
OVERHEAD = len(FORMAT) + NONCE_BYTES + TAG_BYTES
MAX_ARCHIVE_BYTES = KIND_LIMITS[KIND] - OVERHEAD  # so the sealed blob stays within its kind's limit
VERSION_COLUMNS = ("sha256", "size", "sealed_key", "key_nonce", "key_id")


class ProfileTooLarge(ValueError):
    """An archive over MAX_ARCHIVE_BYTES; nothing was stored."""


class ProfileConflict(Exception):
    """A save that lost: the identity was deleted, saved by someone else meanwhile, or is no longer the run's."""


@dataclass(frozen=True)
class SealedProfile:
    """One version of a profile as the hub keeps it: the blob for the bucket and its data key, sealed."""

    sha256: str
    size: int
    blob: bytes
    key: Sealed
