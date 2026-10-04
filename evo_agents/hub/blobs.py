"""The blob store: content-addressed objects in a private bucket on Cloudflare R2, reached through its S3 API.

Keys in the bucket:

- ``blobs/sha256/<sha256>``: a blob. Only the hub writes there, and only bytes whose SHA-256 it computed itself.
  Every project holding the blob shares the object; the ``blobs`` table says which projects hold it.
- ``uploads/<upload_id>``: where a client PUTs one upload, through a presigned URL that works for UPLOAD_TTL and
  signs the declared Content-Length.
- ``uploads/<upload_id>.sealed``: the hub's own copy of an upload, made when it is committed. The hub hashes the
  sealed copy and copies that one to the blob key, so a client that PUTs again while its URL still works cannot
  slip other bytes in between the check and the copy.

R2 keeps no SHA-256 of a whole object (only CRC64NVME), so the hub reads every upload back to hash it. Whatever is
under ``uploads/`` after STALE_AFTER is removed by the worker's cleanup job.

Transient failures (connection errors, timeouts, throttling, 5xx) are retried by botocore in its standard mode, with
exponential backoff and jitter; reading an object's body is retried here. What still fails raises
BlobStoreUnavailable, which the routes answer with 503. The health check has a client of its own that does not retry
and gives up after HEALTH_TIMEOUT. A presigned URL is a bearer credential until it expires: it goes to the caller
and never into a log line, and the log filter masks SigV4 signatures and key ids anyway.

Every call blocks; the server runs them with ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from evo_agents.hub.config import HubConfig

log = logging.getLogger(__name__)

REGION = "auto"  # what Cloudflare documents for R2; the signature carries it, R2 ignores it
BLOB_PREFIX = "blobs/sha256/"
UPLOAD_PREFIX = "uploads/"
SEALED_SUFFIX = ".sealed"
UPLOAD_TTL = timedelta(minutes=15)  # how long a presigned PUT works
GET_TTL = timedelta(minutes=5)  # how long a presigned GET works unless the caller asks for less
MAX_GET_TTL = timedelta(minutes=15)
ATTACHMENT_NAME = re.compile(r"[A-Za-z0-9._-]{1,200}")  # what a presigned GET may name its download
STALE_AFTER = timedelta(hours=24)  # an upload not committed by then is removed, object and row
MiB = 1024 * 1024
KIND_LIMITS = {
    "skill-bundle": 10 * MiB,  # a skill's tar.gz; skill_versions.size has the same bound
    "kg-log": 256 * MiB,  # the jsonl.gz log of one connector run
    "kg-blob": 64 * MiB,  # a source file a run log refers to
}
SHA256 = re.compile(r"[0-9a-f]{64}")
CHUNK = 1 * MiB
ATTEMPTS = 4  # per S3 call, the first one included
READ_ATTEMPTS = 3  # per streamed read of an object's body
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 60.0
HEALTH_TIMEOUT = 3.0
PARALLEL = 16  # S3 calls in flight for one commit
DELETE_BATCH = 1000  # the most keys DeleteObjects takes


class BlobStoreUnavailable(RuntimeError):
    """The blob store did not answer, or refused the hub's request, after the retries."""


@dataclass(frozen=True)
class Upload:
    upload_id: str
    sha256: str
    size: int
    kind: str


@dataclass(frozen=True)
class Verdict:
    upload: Upload
    problem: str | None  # None when the sealed copy has the declared size and SHA-256


def blob_key(sha256: str) -> str:
    if not SHA256.fullmatch(sha256):
        raise ValueError(f"not a hex SHA-256: {sha256!r}")
    return BLOB_PREFIX + sha256


def upload_key(upload_id: str) -> str:
    return UPLOAD_PREFIX + str(uuid.UUID(upload_id))


def sealed_key(upload_id: str) -> str:
    return upload_key(upload_id) + SEALED_SUFFIX


def new_upload_id() -> str:
    return str(uuid.uuid4())


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(
            exc.response.get("Error", {}).get("Code") or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        )
    return type(exc).__name__


def _missing(exc: ClientError) -> bool:
    """A 404 about the object; a missing bucket is a configuration problem, not a missing object."""
    code = exc.response.get("Error", {}).get("Code")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("NoSuchKey", "NotFound", "404") or (status == 404 and code != "NoSuchBucket")


def _unavailable(operation: str, exc: Exception) -> BlobStoreUnavailable:
    # The code only: botocore's messages name the endpoint and the key, never a credential, but stay out anyway.
    code = _error_code(exc)
    log.warning("blob store call failed", extra={"operation": operation, "error": code})
    return BlobStoreUnavailable(f"the blob store did not complete {operation} ({code})")


class BlobStore:
    """One bucket on an S3 API. Thread-safe: boto3 clients are."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        *,
        attempts: int = ATTEMPTS,
        connect_timeout: float = CONNECT_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
    ):
        self.endpoint = endpoint
        self.bucket = bucket
        session = boto3.session.Session()
        common = {
            "signature_version": "s3v4",
            "s3": {"addressing_style": "path"},
            # R2 does not take every checksum boto3 >= 1.36 sends by default; Cloudflare documents this setting.
            "request_checksum_calculation": "when_required",
            "response_checksum_validation": "when_required",
        }

        def client(**config):
            return session.client(
                "s3",
                endpoint_url=endpoint,
                region_name=REGION,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                config=Config(**common, **config),
            )

        self._s3 = client(
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={"mode": "standard", "total_max_attempts": attempts},
            max_pool_connections=PARALLEL * 2,
        )
        self._probe = client(
            connect_timeout=HEALTH_TIMEOUT,
            read_timeout=HEALTH_TIMEOUT,
            retries={"mode": "standard", "total_max_attempts": 1},
        )

    def __repr__(self) -> str:  # the key pair stays out of tracebacks and debug output
        return f"BlobStore(endpoint={self.endpoint!r}, bucket={self.bucket!r})"

    def close(self) -> None:
        """Close the clients' connection pools."""
        self._s3.close()
        self._probe.close()

    @classmethod
    def from_config(cls, config: HubConfig) -> BlobStore | None:
        """The store EVO_HUB_S3_* configure, or None when they are not set."""
        if config.blob_store_missing():
            return None
        return cls(config.s3_endpoint, config.s3_bucket, config.s3_access_key_id, config.s3_secret_access_key)

    def _call(self, operation: str, method: str, **params):
        """One S3 call with botocore's retries; None when the object it names does not exist."""
        try:
            return getattr(self._s3, method)(Bucket=self.bucket, **params)
        except ClientError as exc:
            if _missing(exc):
                return None
            raise _unavailable(operation, exc) from None
        except ParamValidationError:
            raise  # a bug in the hub, not an outage
        except BotoCoreError as exc:
            raise _unavailable(operation, exc) from None

    # Health

    def check(self) -> None:
        """HeadBucket, once, within HEALTH_TIMEOUT; BlobStoreUnavailable when the bucket does not answer."""
        try:
            self._probe.head_bucket(Bucket=self.bucket)
        except (ClientError, BotoCoreError) as exc:
            raise _unavailable("HeadBucket", exc) from None

    async def status(self) -> str:
        """``ok`` or ``unavailable``, for /v1/health."""
        try:
            await asyncio.wait_for(asyncio.to_thread(self.check), HEALTH_TIMEOUT + 1)
        except (BlobStoreUnavailable, TimeoutError, asyncio.TimeoutError):
            return "unavailable"
        return "ok"

    # Presigned URLs: computed locally, no call to the store

    def presign_put(self, upload_id: str, size: int) -> str:
        """A PUT of exactly ``size`` bytes to the upload's key, working for UPLOAD_TTL."""
        params = {"Bucket": self.bucket, "Key": upload_key(upload_id), "ContentLength": size}
        return self._s3.generate_presigned_url("put_object", Params=params, ExpiresIn=int(UPLOAD_TTL.total_seconds()))

    def presign_get(self, sha256: str, expires: timedelta = GET_TTL, *, filename: str | None = None) -> str:
        """A GET of blob ``sha256``, working for ``expires`` (at most MAX_GET_TTL). The caller must have checked that
        the requester may see what refers to the blob: the URL itself checks nothing. With ``filename`` (letters,
        digits and ``._-`` only), the store answers as an attachment of that name, so a browser saves the file."""
        if not timedelta(seconds=1) <= expires <= MAX_GET_TTL:
            raise ValueError(f"a presigned GET works between 1 second and {MAX_GET_TTL}, not {expires}")
        params = {"Bucket": self.bucket, "Key": blob_key(sha256)}
        if filename is not None:
            if not ATTACHMENT_NAME.fullmatch(filename):
                raise ValueError(f"an attachment name is 1 to 200 of [A-Za-z0-9._-], not {filename!r}")
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return self._s3.generate_presigned_url("get_object", Params=params, ExpiresIn=int(expires.total_seconds()))

    # Objects

    def size(self, key: str) -> int | None:
        """The size of the object at ``key``, None when there is none."""
        head = self._call("HeadObject", "head_object", Key=key)
        return None if head is None else int(head["ContentLength"])

    def has_blob(self, sha256: str) -> bool:
        return self.size(blob_key(sha256)) is not None

    def hash(self, key: str, limit: int) -> tuple[str, int] | None:
        """The SHA-256 and size of the object at ``key``, reading at most ``limit`` + 1 bytes; None when there is
        none. A body cut off on the way is read again from the start."""
        for attempt in range(READ_ATTEMPTS):
            response = self._call("GetObject", "get_object", Key=key)
            if response is None:
                return None
            body = response["Body"]
            digest = hashlib.sha256()
            read = 0
            try:
                for chunk in body.iter_chunks(CHUNK):
                    read += len(chunk)
                    if read > limit:  # the size is wrong already; the rest does not change that
                        break
                    digest.update(chunk)
                return digest.hexdigest(), read
            except (BotoCoreError, OSError) as exc:
                if attempt == READ_ATTEMPTS - 1:
                    raise _unavailable("GetObject", exc) from None
                log.info("reading a blob was cut off; reading it again", extra={"error": _error_code(exc)})
                time.sleep(0.5 * 2**attempt)
            finally:
                body.close()
        raise AssertionError("unreachable")

    def fetch(self, key: str, sink: BinaryIO, limit: int) -> tuple[str, int] | None:
        """Write the object at ``key`` into ``sink`` (a binary file open for writing, from its start), reading at most
        ``limit`` + 1 bytes and writing at most ``limit``; its SHA-256 and size, None when there is none. A body cut off
        on the way is written again from the start, ``sink`` emptied first. The caller compares both with what it
        expects before trusting what ``sink`` holds: a size over ``limit`` means the object is larger. For a caller
        that has checked the requester may see what refers to the object."""
        for attempt in range(READ_ATTEMPTS):
            response = self._call("GetObject", "get_object", Key=key)
            if response is None:
                return None
            body = response["Body"]
            digest = hashlib.sha256()
            read = 0
            try:
                sink.seek(0)
                sink.truncate()
                for chunk in body.iter_chunks(CHUNK):
                    read += len(chunk)
                    if read > limit:  # the size is wrong already; the rest does not change that
                        break
                    digest.update(chunk)
                    sink.write(chunk)
                return digest.hexdigest(), read
            # Not every OSError: one from writing to ``sink`` (a full disk) is the caller's, not the blob store's.
            except (BotoCoreError, ConnectionError, TimeoutError) as exc:
                if attempt == READ_ATTEMPTS - 1:
                    raise _unavailable("GetObject", exc) from None
                log.info("reading a blob was cut off; reading it again", extra={"error": _error_code(exc)})
                time.sleep(0.5 * 2**attempt)
            finally:
                body.close()
        raise AssertionError("unreachable")

    def put_file(self, sha256: str, path: Path) -> bool:
        """Store the file at ``path`` as blob ``sha256``, which the caller computed from these very bytes, unless the
        blob is there already. True when this call wrote it."""
        key = blob_key(sha256)
        if self.size(key) is not None:
            return False
        size = path.stat().st_size
        with open(path, "rb") as handle:
            self._call("PutObject", "put_object", Key=key, Body=handle, ContentLength=size)
        if self.size(key) != size:
            raise BlobStoreUnavailable(f"blob {sha256} does not have its {size} bytes after it was written")
        return True

    def copy(self, source: str, target: str) -> bool:
        """Copy ``source`` to ``target`` inside the bucket; False when ``source`` does not exist."""
        source_ref = {"Bucket": self.bucket, "Key": source}
        return self._call("CopyObject", "copy_object", Key=target, CopySource=source_ref) is not None

    def delete(self, keys: Iterable[str]) -> None:
        """Delete ``keys``; a key that does not exist is not an error."""
        keys = sorted(set(keys))
        for start in range(0, len(keys), DELETE_BATCH):
            batch = [{"Key": key} for key in keys[start : start + DELETE_BATCH]]
            result = self._call("DeleteObjects", "delete_objects", Delete={"Objects": batch, "Quiet": True})
            errors = (result or {}).get("Errors") or []
            if errors:
                codes = sorted({str(error.get("Code")) for error in errors})
                raise _unavailable("DeleteObjects", RuntimeError(f"{len(errors)} keys not deleted: {', '.join(codes)}"))

    # Uploads

    def _each(self, func: Callable, items: Sequence) -> list:
        """``func`` over ``items``, PARALLEL at a time, in order; the first BlobStoreUnavailable is raised."""
        if len(items) <= 1:
            return [func(item) for item in items]
        with ThreadPoolExecutor(max_workers=min(PARALLEL, len(items)), thread_name_prefix="blob-store") as pool:
            return list(pool.map(func, items))

    def seal(self, upload: Upload) -> Verdict:
        """Copy the upload to its sealed key and check the sealed copy against what was declared."""
        staged, sealed = upload_key(upload.upload_id), sealed_key(upload.upload_id)
        found = self.size(staged)
        if found is None:
            return Verdict(upload, "nothing was uploaded")
        if found != upload.size:
            return Verdict(upload, f"{found} bytes were uploaded, {upload.size} were declared")
        if not self.copy(staged, sealed):
            return Verdict(upload, "nothing was uploaded")
        hashed = self.hash(sealed, upload.size)
        if hashed is None:
            return Verdict(upload, "nothing was uploaded")
        digest, read = hashed
        if read != upload.size:
            return Verdict(upload, f"{read} bytes were uploaded, {upload.size} were declared")
        if digest != upload.sha256:
            return Verdict(upload, "the bytes uploaded do not have the declared SHA-256")
        return Verdict(upload, None)

    def seal_all(self, uploads: Sequence[Upload]) -> list[Verdict]:
        return self._each(self.seal, uploads)

    def publish(self, upload: Upload) -> bool:
        """Copy a sealed upload that passed ``seal`` to its blob key, unless a blob is there already (it has the
        same bytes: only verified bytes are ever written there). True when this call wrote the blob."""
        key = blob_key(upload.sha256)
        if self.size(key) is not None:
            return False
        if not self.copy(sealed_key(upload.upload_id), key):
            raise BlobStoreUnavailable(f"the sealed copy of upload {upload.upload_id} disappeared before publishing")
        return True

    def publish_all(self, uploads: Sequence[Upload]) -> list[bool]:
        return self._each(self.publish, uploads)

    def discard(self, upload_ids: Iterable[str]) -> None:
        """Delete the uploaded and the sealed object of each upload."""
        self.delete(key for upload_id in upload_ids for key in (upload_key(upload_id), sealed_key(upload_id)))

    def remove_stale_uploads(self, cutoff: datetime) -> int:
        """Delete every object under ``uploads/`` last written before ``cutoff``; how many there were."""
        stale = []
        try:
            for page in self._s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=UPLOAD_PREFIX):
                stale += [item["Key"] for item in page.get("Contents", []) if item["LastModified"] < cutoff]
        except (ClientError, BotoCoreError) as exc:
            raise _unavailable("ListObjectsV2", exc) from None
        self.delete(stale)
        return len(stale)
