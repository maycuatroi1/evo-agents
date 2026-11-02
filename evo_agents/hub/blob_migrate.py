"""``evo-agents hub storage``: check a blob store, and copy every blob of one store to another.

A store is described by the EVO_HUB_S3_* variables the hub reads (``evo_agents.hub.config``): those of the process
environment, or those of an environment file, ``NAME=value`` lines as compose and Dokploy write them, so a second
store's key pair never goes on a command line, which ``ps`` shows.

``check`` does what the hub does with a store, on one object of random bytes: HeadBucket, a presigned PUT from this
machine, the copy to a sealed key and the read that hashes it, the copy to its blob key, a presigned GET, a ranged
read, a listing, and the deletes. The object is left nowhere: its bytes are random, so no row of any hub refers to
it. ``check --create-bucket`` creates the bucket first when it is missing.

``migrate`` copies the objects under ``blobs/sha256/``, the hub's every durable object, from a source store to a
target store. Each is streamed through SHA-256 into a temporary file and written to the target only when its hash
is its name, so a damaged object is reported and never copied. A blob the target holds already, with the same size,
is left as it is (with ``verify``, read back and hashed first, and copied again when its hash is wrong), so the
command can run again until nothing is left to copy. Uploads in progress (``uploads/``) are not copied: they last
minutes, and a client whose commit fails uploads again.

Moving a running hub to a new store: migrate while the hub still uses the old one, point the hub's EVO_HUB_S3_*
variables at the new one and redeploy, then migrate again for the blobs written in between (docs/hub-deployment.md,
section Moving the blob store).
"""

from __future__ import annotations

import hashlib
import secrets
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.hub.blobs import (
    BlobStore,
    BlobStoreUnavailable,
    Upload,
    blob_key,
    new_upload_id,
    upload_key,
)
from evo_agents.hub.config import S3_VARIABLES, ConfigError, load_blob_store

DEFAULT_PARALLEL = 8
MAX_PARALLEL = 64
CHECK_BYTES = 64 * 1024
CHECK_TIMEOUT = 30.0  # seconds for the presigned PUT and GET of ``check``
PROGRESS_EVERY = 10.0  # seconds between two calls of ``migrate``'s progress


def read_env_file(path: Path) -> dict[str, str]:
    """The variables of an environment file: ``NAME=value`` lines, ``export`` before the name or not, the value bare
    or in matching quotes; blank lines and lines starting with ``#`` skipped. A line of another shape is a ValueError
    naming its number, never its text, which may hold a secret."""
    found: dict[str, str] = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.removeprefix("export ").partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name.isidentifier():
            raise ValueError(f"{path}: line {number} is not NAME=value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[name] = value
    return found


def store_from_env(env: Mapping[str, str], *, concurrency: int = DEFAULT_PARALLEL) -> BlobStore:
    """The store ``env``'s EVO_HUB_S3_* variables describe; ConfigError when they describe none or not all of one."""
    settings = load_blob_store(env)
    store = BlobStore.from_settings(settings, concurrency=concurrency)
    if store is None:
        raise ConfigError(S3_VARIABLES[0], f"no blob store: set {', '.join(S3_VARIABLES)}")
    return store


# check


@dataclass
class Step:
    name: str
    ok: bool
    millis: int
    detail: str = ""


def check(store: BlobStore, *, create_bucket: bool = False) -> list[Step]:
    """The steps of one object's round trip through ``store`` (module docstring), each ok or not; the first step that
    fails ends the check, after the cleanup."""
    steps: list[Step] = []
    data = secrets.token_bytes(CHECK_BYTES)
    upload = Upload(new_upload_id(), hashlib.sha256(data).hexdigest(), len(data), "check")

    def step(name: str, func: Callable[[], str | None]) -> bool:
        started = time.monotonic()
        try:
            problem = func()
        except (BlobStoreUnavailable, OSError, ValueError) as exc:
            problem = str(exc) or type(exc).__name__
        steps.append(Step(name, problem is None, round((time.monotonic() - started) * 1000), problem or ""))
        return problem is None

    def bucket() -> str | None:
        if create_bucket and store.ensure_bucket():
            steps.append(Step("CreateBucket", True, 0, f"created {store.bucket}"))
        store.check()
        return None

    def put() -> str | None:
        request = urllib.request.Request(
            store.presign_put(upload.upload_id, upload.size),
            data=data,
            method="PUT",
            headers={"Content-Type": "application/octet-stream"},
        )
        with urllib.request.urlopen(request, timeout=CHECK_TIMEOUT) as response:
            return None if 200 <= response.status < 300 else f"HTTP {response.status}"

    def seal() -> str | None:
        return store.seal(upload).problem

    def publish() -> str | None:
        store.publish(upload)
        return None if store.has_blob(upload.sha256) else "the blob is not there after the copy"

    def get() -> str | None:
        with urllib.request.urlopen(store.presign_get(upload.sha256, filename="check.bin"), timeout=CHECK_TIMEOUT) as r:
            body = r.read()
            disposition = r.headers.get("Content-Disposition") or ""
        if body != data:
            return f"{len(body)} bytes came back, not the {len(data)} written"
        if "check.bin" not in disposition:
            return "the store ignored the download name (ResponseContentDisposition)"
        return None

    def ranged() -> str | None:
        opened = store.open_range(upload.sha256, 10, 19)
        if opened is None:
            return "the blob is missing"
        chunks, count = opened
        body = b"".join(chunks)
        return None if body == data[10:20] and count == 10 else "the store did not answer the range asked for"

    def listed() -> str | None:
        keys = [key for key, _ in store.list_keys(upload_key(upload.upload_id))]
        return None if upload_key(upload.upload_id) in keys else "the upload is missing from the listing"

    try:
        for name, func in (
            ("HeadBucket", bucket),
            ("presigned PUT", put),
            ("seal (CopyObject, GetObject)", seal),
            ("publish (CopyObject)", publish),
            ("presigned GET", get),
            ("ranged GetObject", ranged),
            ("ListObjectsV2", listed),
        ):
            if not step(name, func):
                break
    finally:

        def cleanup() -> str | None:
            store.discard([upload.upload_id])
            store.delete([blob_key(upload.sha256)])
            return None if not store.has_blob(upload.sha256) else "the blob is still there after the delete"

        if steps and steps[0].ok:
            step("delete" + (" (DeleteObjects)" if store.batch_delete else " (DeleteObject)"), cleanup)
    return steps


# migrate


@dataclass
class Report:
    listed: int = 0  # blobs in the source
    copied: int = 0  # written to the target by this run (to be written, on a dry run)
    present: int = 0  # in the target already, with the same size (and hash, with verify)
    copied_bytes: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (sha256, what went wrong)

    def as_dict(self) -> dict:
        return {
            "listed": self.listed,
            "copied": self.copied,
            "present": self.present,
            "copied_bytes": self.copied_bytes,
            "failed": len(self.failed),
        }


def migrate(
    source: BlobStore,
    target: BlobStore,
    *,
    scratch: Path | None = None,
    parallel: int = DEFAULT_PARALLEL,
    dry_run: bool = False,
    verify: bool = False,
    progress: Callable[[Report], None] | None = None,
    blobs: Iterable[tuple[str, int]] | None = None,
) -> Report:
    """Copy every blob of ``source`` that ``target`` lacks (module docstring), ``parallel`` at a time, through
    temporary files in ``scratch``; what happened to each. ``blobs`` (sha256, size) replaces the source's listing.
    ``progress`` is called with the report so far, once every PROGRESS_EVERY seconds at most, from any thread."""
    if not 1 <= parallel <= MAX_PARALLEL:
        raise ValueError(f"parallel is between 1 and {MAX_PARALLEL}, not {parallel}")
    if (source.internal_endpoint, source.bucket) == (target.internal_endpoint, target.bucket):
        raise ValueError("the source and the target are the same bucket")
    report = Report()
    lock = threading.Lock()
    last = [time.monotonic()]

    def tell() -> None:
        if progress is not None and time.monotonic() - last[0] >= PROGRESS_EVERY:
            last[0] = time.monotonic()
            progress(report)

    def one(item: tuple[str, int]) -> None:
        sha256, size = item
        outcome, problem = _copy(source, target, sha256, size, scratch, dry_run, verify)
        with lock:
            if problem is not None:
                report.failed.append((sha256, problem))
            elif outcome == "present":
                report.present += 1
            else:
                report.copied += 1
                report.copied_bytes += size
            tell()

    items = source.blobs() if blobs is None else blobs
    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="blob-migrate") as pool:
        pending = []
        for item in items:
            report.listed += 1
            pending.append(pool.submit(one, item))
            if len(pending) >= parallel * 4:  # a listing of millions never waits in memory
                pending.pop(0).result()
        for future in pending:
            future.result()
    return report


def _copy(
    source: BlobStore, target: BlobStore, sha256: str, size: int, scratch: Path | None, dry_run: bool, verify: bool
) -> tuple[str, str | None]:
    """("present" or "copied", None), or ("", what went wrong)."""
    key = blob_key(sha256)
    try:
        held = target.size(key)
        if held == size:
            if not verify:
                return "present", None
            hashed = target.hash(key, size)
            if hashed is not None and hashed == (sha256, size):
                return "present", None
        if dry_run:
            return "copied", None
        with tempfile.NamedTemporaryFile(dir=scratch, prefix="blob-") as sink:
            fetched = source.fetch(key, sink, size)
            if fetched is None:
                return "", "gone from the source"
            digest, read = fetched
            if read != size:
                return "", f"the source holds {read} bytes, its listing said {size}"
            if digest != sha256:
                return "", "the source's bytes do not have the SHA-256 the blob is named after"
            sink.flush()
            target.put_file(sha256, Path(sink.name), replace=True)
        return "copied", None
    except BlobStoreUnavailable as exc:
        return "", str(exc)


def describe(store: BlobStore) -> str:
    """Where a store is, for the command's output: no key, no secret."""
    return f"{store.endpoint}/{store.bucket} (region {store.region})"
