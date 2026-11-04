"""Reading an uploaded archive (.zip, .tar, .tar.gz, .tgz) entry by entry, safely and in a fixed order: what the hub's
extraction of an upload (``evo_agents.hub.kg_upload_extract``) turns into the items of its source.

``walk`` yields each regular file as an ``Entry``: its path inside the archive, its size, its modification time and a
way to read its bytes, one entry at a time. The archive is never unpacked to disk: a zip is read through its central
directory, a tar as one stream, and only a nested archive is spooled to a temporary file, to be read the same way.

Every entry that is not taken is counted by reason in a ``Counter`` the caller passes:

- ``unsafe_path``: an absolute path, a drive letter, a ``..`` part or a control character; refused per entry, the
  rest of the archive still read;
- ``symlink``, ``link`` (a hard link of a tar) and ``device`` (a character or block device, a FIFO, a socket);
- ``encrypted``: a zip entry with its encryption flag;
- ``ratio``: a zip entry past MIN_RATIO_BYTES whose size is more than ``max_ratio`` times its compressed size, a zip
  bomb, never decompressed;
- ``too_many_entries`` past ``max_entries`` files, ``total_size`` once the files taken would pass ``max_total`` bytes;
- ``depth``: an archive nested deeper than ``max_depth``; ``corrupt``: an entry or nested archive that cannot be read;
- ``duplicate``: a second entry with the path of one taken, the first kept.

Names: a zip entry without the UTF-8 flag is read as cp437, as the format says, unless its bytes are UTF-8, as many
tools write without setting the flag (a Vietnamese name among them); every name is put in Unicode NFC. Paths of a
nested archive's entries start with the nested archive's own path: ``reports/2026.zip/q1/summary.md``.
"""

from __future__ import annotations

import calendar
import re
import shutil
import stat
import tarfile
import tempfile
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from evo_agents.kg.ids import epoch_to_iso

GiB = 1024**3
MAX_TOTAL = 100 * GiB  # bytes of the files taken from one upload, nested archives' included
MAX_ENTRIES = 200_000  # files taken from one upload
MAX_RATIO = 100.0  # size over compressed size of one zip entry
MIN_RATIO_BYTES = 1024 * 1024  # an entry smaller than this is read whatever its ratio: it cannot fill a disk
MAX_DEPTH = 2  # archives inside the upload opened, one inside the other
MAX_NOTED = 200  # paths of entries not taken that a walk notes
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")
UTF8_FLAG = 0x800
ENCRYPTED_FLAG = 0x1
SPOOL_CHUNK = 1024 * 1024
DRIVE = re.compile(r"^[A-Za-z]:")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Limits:
    """What one upload may hold (see the module)."""

    max_total: int = MAX_TOTAL
    max_entries: int = MAX_ENTRIES
    max_ratio: float = MAX_RATIO
    max_depth: int = MAX_DEPTH


@dataclass
class _Walk:
    """What the walk of one upload has taken so far, across its nested archives."""

    limits: Limits
    skipped: Counter
    spool: Path
    noted: list | None = None
    entries: int = 0
    total: int = 0
    seen: set[str] = field(default_factory=set)

    def skip(self, reason: str, path: str) -> None:
        self.skipped[reason] += 1
        if self.noted is not None and len(self.noted) < MAX_NOTED:
            self.noted.append({"path": path, "reason": reason})


@dataclass(frozen=True)
class Entry:
    """A regular file of an archive: its path, size and modification time (UTC, ISO), and its bytes through
    ``open``. A tar is one stream: read an entry before taking the next."""

    path: str
    size: int
    mtime: str
    _opener: object

    def open(self) -> BinaryIO:
        return self._opener()

    def read(self, limit: int) -> bytes | None:
        """The entry's bytes, None when it holds more than ``limit``."""
        if self.size > limit:
            return None
        with self.open() as handle:
            data = handle.read(limit + 1)
        return None if len(data) > limit else data


def archive_kind(name: str) -> str | None:
    """The suffix of the archive format ``name`` has (one of ARCHIVE_SUFFIXES), None for another file."""
    lower = name.lower()
    return next((suffix for suffix in ARCHIVE_SUFFIXES if lower.endswith(suffix)), None)


def safe_path(name: str) -> str | None:
    """``name`` as a relative POSIX path inside the archive, in NFC; None when it is absolute, has a drive letter, a
    ``..`` part or a control character. ``.`` parts, empty parts and a trailing slash go."""
    name = unicodedata.normalize("NFC", name.replace("\\", "/"))
    if name.startswith("/") or DRIVE.match(name) or CONTROL.search(name):
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def zip_name(info: zipfile.ZipInfo) -> str:
    """The name of a zip entry: zipfile reads one without the UTF-8 flag as cp437; when those bytes are UTF-8, they
    were meant as UTF-8."""
    if info.flag_bits & UTF8_FLAG:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _zip_time(info: zipfile.ZipInfo) -> str:
    try:
        return epoch_to_iso(calendar.timegm((*info.date_time, 0, 0, 0)))
    except (OverflowError, ValueError):
        return epoch_to_iso(0)


def _take(walk: _Walk, path: str, size: int) -> bool:
    """Whether the walk takes a file of ``path`` and ``size``; counts the reason when not."""
    if path in walk.seen:
        walk.skip("duplicate", path)
        return False
    if walk.entries >= walk.limits.max_entries:
        walk.skip("too_many_entries", path)
        return False
    if walk.total + size > walk.limits.max_total:
        walk.skip("total_size", path)
        return False
    walk.seen.add(path)
    walk.entries += 1
    walk.total += size
    return True


def _zip_problem(info: zipfile.ZipInfo, limits: Limits) -> str | None:
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return "device"
    if info.flag_bits & ENCRYPTED_FLAG:
        return "encrypted"
    if info.file_size > MIN_RATIO_BYTES and info.file_size > limits.max_ratio * max(info.compress_size, 1):
        return "ratio"
    return None


def _tar_problem(member: tarfile.TarInfo) -> str | None:
    if member.issym():
        return "symlink"
    if member.islnk():
        return "link"
    if member.ischr() or member.isblk() or member.isfifo():
        return "device"
    if not member.isfile():
        return "device"
    return None


def _nested(walk: _Walk, entry: Entry, depth: int) -> Iterator[Entry]:
    """The entries of the archive ``entry`` is, spooled to a temporary file; ``depth``: its own depth."""
    if depth > walk.limits.max_depth:
        walk.skip("depth", entry.path)
        return
    if not _take(walk, entry.path, 0):
        return
    walk.entries -= 1  # an archive opened counts by its files, not itself
    # closed before it is read again by name: Windows cannot open a NamedTemporaryFile a second time while it is open
    handle, name = tempfile.mkstemp(dir=walk.spool, suffix=archive_kind(entry.path))
    spooled = Path(name)
    try:
        try:
            with open(handle, "wb") as out, entry.open() as source:
                shutil.copyfileobj(source, out, SPOOL_CHUNK)
        except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError):
            walk.skip("corrupt", entry.path)
            return
        yield from _archive(walk, spooled, entry.path, depth)
    finally:
        spooled.unlink(missing_ok=True)


def _zip_entries(walk: _Walk, path: Path, prefix: str) -> Iterator[tuple[Entry, bool]]:
    with zipfile.ZipFile(path) as archive:
        for info in sorted(archive.infolist(), key=lambda found: found.header_offset):
            if info.is_dir():
                continue
            raw = zip_name(info)
            name = safe_path(raw)
            problem = "unsafe_path" if name is None else _zip_problem(info, walk.limits)
            if problem:
                walk.skip(problem, prefix + (name or raw))
                continue
            entry = Entry(prefix + name, info.file_size, _zip_time(info), lambda info=info: archive.open(info))
            yield entry, archive_kind(name) is not None


def _tar_entries(walk: _Walk, path: Path, prefix: str) -> Iterator[tuple[Entry, bool]]:
    with tarfile.open(path, "r|*") as archive:
        for member in archive:
            if member.isdir():
                continue
            name = safe_path(member.name)
            problem = "unsafe_path" if name is None else _tar_problem(member)
            if problem:
                walk.skip(problem, prefix + (name or member.name))
                continue
            mtime = epoch_to_iso(max(0, int(member.mtime))) if member.mtime else epoch_to_iso(0)
            entry = Entry(prefix + name, member.size, mtime, lambda member=member: archive.extractfile(member))
            yield entry, archive_kind(name) is not None


def _archive(walk: _Walk, path: Path, inside: str, depth: int) -> Iterator[Entry]:
    prefix = f"{inside}/" if inside else ""
    reader = _zip_entries if zipfile.is_zipfile(path) else _tar_entries
    try:
        for entry, nested in reader(walk, path, prefix):
            if nested:
                yield from _nested(walk, entry, depth + 1)
            elif _take(walk, entry.path, entry.size):
                yield entry
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError, RuntimeError):
        walk.skip("corrupt", inside or "(archive)")


def walk(
    path: Path,
    skipped: Counter,
    *,
    limits: Limits | None = None,
    spool: Path | None = None,
    noted: list | None = None,
) -> Iterator[Entry]:
    """Each regular file of the archive at ``path``, in the order the archive holds them, nested archives opened in
    place (see the module); ``skipped`` counts what was not taken, by reason, and ``noted``, when given, gets the
    first MAX_NOTED of them as ``{"path", "reason"}``. ``spool``: where a nested archive is spooled, the system's
    temporary directory by default."""
    state = _Walk(limits or Limits(), skipped, Path(spool or tempfile.gettempdir()), noted)
    yield from _archive(state, path, "", 0)


__all__ = ["ARCHIVE_SUFFIXES", "MAX_NOTED", "Entry", "Limits", "archive_kind", "safe_path", "walk", "zip_name"]
