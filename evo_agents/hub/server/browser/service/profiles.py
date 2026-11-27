"""The profiles of browser identities: sealed per version, kept two at a time, opened only for the version they are.

A profile reaches the hub as an archive of a Chromium profile without its caches, which holds the cookies of the
identity's sites in what is in effect plain text (Chromium on Linux encrypts them under a fixed key). The hub never
puts it in the bucket as it is: ``seal_profile`` draws a fresh 32-byte data key for every version, encrypts the
archive under it with AES-256-GCM (associated data ``browser-profile:{id}:{version}``), and seals the data key under
EVO_HUB_SECRETS_KEY with ``sealing.browser_identity_aad``, so a sealed key copied to another identity or version does
not open there, nor a blob moved to another row. The blob is FORMAT, the 12-byte nonce, then the ciphertext and its
tag; its kind is browser-profile, its limit ``KIND_LIMITS[KIND]``.

``save_profile`` puts the blob, then in one transaction holding the blob lock exclusively (``blob_gc``) checks that
the identity is live, still at the version the caller started from and, for a run, still held by it; it makes the
new version current, keeps the one it replaces as the previous one, records for deletion the one before that, and
writes the audit row. A save that loses records its own blob for deletion instead and raises ``ProfileConflict``.
``retire_profiles`` drops both versions of an identity being deleted, keys first. Nothing here logs a key, an archive
or a domain.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.blobs import KIND_LIMITS, BlobStore
from evo_agents.hub.server import audit
from evo_agents.hub.server.blobs import fetch_bytes, put_bytes
from evo_agents.hub.server.browser.models.profiles import (
    FORMAT,
    KIND,
    MAX_ARCHIVE_BYTES,
    OVERHEAD,
    VERSION_COLUMNS,
    ProfileConflict,
    ProfileTooLarge,
    SealedProfile,
)
from evo_agents.hub.server.browser.repository import profiles as repository
from evo_agents.hub.server.credentials.models.sealing import (
    KEY_BYTES,
    NONCE_BYTES,
    Sealed,
    Sealer,
    Unsealable,
    browser_identity_aad,
)

log = logging.getLogger(__name__)


def blob_aad(identity_id: int, version: int) -> bytes:
    """The associated data of the archive inside a sealed profile: it opens only as that identity's version."""
    return f"browser-profile:{identity_id}:{version}".encode()


def seal_profile(sealer: Sealer, identity_id: int, version: int, archive: bytes) -> SealedProfile:
    """``archive`` encrypted under a fresh data key as version ``version`` of identity ``identity_id``, and the data
    key sealed by ``sealer``. ``ProfileTooLarge`` over MAX_ARCHIVE_BYTES. CPU-bound: run it in a thread."""
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ProfileTooLarge(f"a browser profile is at most {MAX_ARCHIVE_BYTES} bytes, not {len(archive)}")
    data_key = os.urandom(KEY_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    blob = FORMAT + nonce + AESGCM(data_key).encrypt(nonce, archive, blob_aad(identity_id, version))
    key = sealer.seal_bytes(data_key, browser_identity_aad(identity_id, version))
    return SealedProfile(hashlib.sha256(blob).hexdigest(), len(blob), blob, key)


def open_profile_blob(sealer: Sealer, identity_id: int, version: int, blob: bytes, key: Sealed) -> bytes:
    """The archive ``blob`` holds, as version ``version`` of identity ``identity_id``; ``Unsealable`` when the key or
    the blob belongs elsewhere or was altered."""
    data_key = sealer.open_bytes(key, browser_identity_aad(identity_id, version))
    if len(blob) < OVERHEAD or not blob.startswith(FORMAT):
        raise Unsealable(f"version {version} of browser identity {identity_id} is not a sealed profile")
    nonce, ciphertext = blob[len(FORMAT) : len(FORMAT) + NONCE_BYTES], blob[len(FORMAT) + NONCE_BYTES :]
    try:
        return AESGCM(data_key).decrypt(nonce, ciphertext, blob_aad(identity_id, version))
    except InvalidTag:
        raise Unsealable(f"version {version} of browser identity {identity_id} was altered") from None


def _columns(prefix: str, profile: SealedProfile | None = None) -> dict:
    if profile is None:
        return {f"{prefix}{name}": None for name in VERSION_COLUMNS}
    key = profile.key
    values = (profile.sha256, profile.size, key.ciphertext, key.nonce, key.key_id)
    return {f"{prefix}{name}": value for name, value in zip(VERSION_COLUMNS, values, strict=True)}


def _refusal(row, base_version: int, run_id: int | None, unheld: bool = False) -> str | None:
    if row is None or row.deleted_at is not None:
        return "the browser identity is deleted"
    if row.version != base_version:
        return f"the browser identity is at version {row.version} now, not {base_version}"
    if run_id is not None and row.held_by_run != run_id:
        return f"run {run_id} does not hold the browser identity"
    if unheld and row.held_by_run is not None:
        return f"run {row.held_by_run} holds the browser identity"
    return None


async def save_profile(
    engine,
    store: BlobStore,
    sealer: Sealer,
    *,
    identity_id: int,
    base_version: int,
    archive: bytes,
    chromium_version: str | None,
    actor_id: int | None,
    token_id: int | None,
    run_id: int | None = None,
    login_worker_id: int | None = None,
    unheld: bool = False,
) -> int:
    """Seal ``archive`` as the version after ``base_version`` of identity ``identity_id`` and make it current; the
    new version. ``ProfileTooLarge``, or ``ProfileConflict`` when the identity is deleted, at another version, or not
    held by ``run_id`` when one is given; the version it has stays. A login session (``login_worker_id``, the worker it ran
    on) also sets the identity active and on that worker. ``unheld``: a save outside any run (a login on the worker's own
    machine), refused while a run holds the identity."""
    version = base_version + 1
    profile = await asyncio.to_thread(seal_profile, sealer, identity_id, version, archive)
    await asyncio.to_thread(put_bytes, store, profile.sha256, profile.blob)
    async with engine.begin() as conn:
        row = await repository.locked(conn, identity_id)
        refusal = _refusal(row, base_version, run_id, unheld)
        if refusal is not None:
            await repository.forget(conn, (profile.sha256, profile.size))
        else:
            assert row is not None  # _refusal refuses a missing row
            saved = {"version": version, "chromium_version": chromium_version, **_columns("", profile)}
            stamps = {"last_saved_at": func.now(), "last_saved_by": actor_id, "updated_at": func.now()}
            if login_worker_id is not None:
                stamps |= {"status": "active", "worker_id": login_worker_id}
            await repository.write(conn, identity_id, {**repository.shifted(VERSION_COLUMNS), **saved, **stamps})
            await repository.forget(conn, (row.previous_sha256, row.previous_size))
            target = f"{row.project} browser-identity:{identity_id} version={version}"
            # in the name of ``actor_id`` (a run's dispatcher) with ``token_id`` (the worker's): no Principal's
            await audit.record(
                conn,
                actor_id=actor_id,
                token_id=token_id,
                action=audit.BROWSER_IDENTITY_SAVE,
                target=target,
                project_id=row.project_id,
            )
    if refusal is not None:
        raise ProfileConflict(refusal)
    log.info(
        "browser profile saved",
        extra={"browser_identity": identity_id, "version": version, "bytes": profile.size},
    )
    return version


def _sealed(row, prefix: str) -> Sealed:
    return Sealed(bytes(row[f"{prefix}sealed_key"]), bytes(row[f"{prefix}key_nonce"]), row[f"{prefix}key_id"])


async def open_profile(engine, store: BlobStore, sealer: Sealer, identity_id: int, *, previous: bool = False):
    """(version, archive) of the current version of identity ``identity_id``, or of the one before it with
    ``previous``; None when the identity is deleted or has no such version. ``Unsealable`` when it does not open,
    ``ProfileConflict`` when its blob is missing from the bucket."""
    async with engine.connect() as conn:
        row = await repository.current(conn, identity_id)
    prefix = "previous_" if previous else ""
    if row is None or row["deleted_at"] is not None or row[f"{prefix}sha256"] is None:
        return None
    version = row["version"] - 1 if previous else row["version"]
    blob = await asyncio.to_thread(fetch_bytes, store, row[f"{prefix}sha256"], KIND_LIMITS[KIND])
    if blob is None:
        raise ProfileConflict(f"version {version} of browser identity {identity_id} is missing from the blob store")
    archive = await asyncio.to_thread(open_profile_blob, sealer, identity_id, version, blob, _sealed(row, prefix))
    return version, archive


async def retire_profiles(conn: AsyncConnection, row) -> None:
    """Mark identity ``row`` (as ``locked`` read it, in this transaction) deleted: its data keys go from the row, and
    both blobs are recorded for deletion."""
    gone = {"deleted_at": func.now(), "updated_at": func.now(), **_columns(""), **_columns("previous_")}
    await repository.write(conn, row.id, gone)
    await repository.forget(conn, (row.sha256, row.size), (row.previous_sha256, row.previous_size))
