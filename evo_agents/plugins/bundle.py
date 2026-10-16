"""Plugin bundles: a plugin directory as a deterministic tar.gz, and the checks a bundle passes before anyone trusts
what it says.

A bundle holds the regular files of one plugin directory by their paths relative to it, sorted, each with mtime 0,
uid and gid 0, no owner names and mode 0644, or 0755 when any execute bit is set, in a PAX tar compressed by gzip at
level 9 with no file name and mtime 0, so the same directory gives the same bytes and SHA-256 every time. Its manifest,
``evo-plugin.json``, is at the top. The parts a manifest's contributions name (a backend, web pages, agent packs) are
directories beside it, which the extension points read.

``pack`` refuses a symbolic link or any file that is not regular, and leaves out ``.git``, ``__pycache__``,
``.DS_Store`` and ``*.pyc`` wherever they are. ``read_bundle`` checks a bundle whole before anything is written or
trusted: a gzip-compressed tar of at most MAX_BUNDLE bytes that decompresses to at most MAX_UNPACKED, at most
MAX_FILES entries, only regular files and directories, every path relative and made of parts that are neither empty,
``.`` nor ``..``, without backslashes or control characters, unique even ignoring case, no file where another entry
needs a directory, and a manifest that ``evo_agents.plugins.manifest`` reads. The signature is never inside the bundle:
it signs the bundle's bytes and travels beside them (``evo_agents.plugins.signing``).

Standard library only.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import stat
import tarfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from evo_agents.plugins.manifest import MANIFEST_FILE, Manifest, ManifestError, read_manifest

KIND = "plugin-bundle"  # the blob kind of a bundle in the hub's blob store
MiB = 1024 * 1024
MAX_BUNDLE = 32 * MiB  # bytes of the tar.gz; the hub's KIND_LIMITS["plugin-bundle"]
MAX_UNPACKED = 128 * MiB  # bytes of all files together
MAX_FILES = 10_000
MAX_PATH = 1024  # bytes of UTF-8 in one path
MAX_PART = 255  # bytes in one part of a path
SKIPPED_NAMES = frozenset({".git", "__pycache__", ".DS_Store"})
SKIPPED_SUFFIXES = (".pyc",)
FILE_MODE = 0o644
EXEC_MODE = 0o755
TAR_OVERHEAD = 16 * MiB  # headers and padding read on top of the files themselves
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class BundleError(ValueError):
    """Bytes that are not a plugin bundle, or a directory that cannot be packed into one."""


@dataclass(frozen=True)
class Entry:
    """One file of a bundle: its path, whether it is executable, its bytes."""

    path: str
    executable: bool
    data: bytes

    @property
    def mode(self) -> int:
        return EXEC_MODE if self.executable else FILE_MODE


@dataclass(frozen=True)
class Contents:
    """What a bundle holds, checked: its manifest and its files in path order."""

    manifest: Manifest
    files: tuple[Entry, ...]


@dataclass(frozen=True)
class Bundle:
    """A packed plugin: its contents, the tar.gz and its SHA-256."""

    contents: Contents
    data: bytes
    sha256: str

    @property
    def manifest(self) -> Manifest:
        return self.contents.manifest


def skipped(name: str) -> bool:
    return name in SKIPPED_NAMES or name.endswith(SKIPPED_SUFFIXES)


def path_problem(path: str) -> str | None:
    """Why ``path`` cannot be a path in a bundle, or None."""
    if not path:
        return "an empty path"
    try:
        size = len(path.encode("utf-8"))
    except UnicodeEncodeError:
        return "a path that is not valid Unicode"
    if size > MAX_PATH:
        return f"a path over {MAX_PATH} bytes"
    if path.startswith("/") or "\\" in path or _CONTROL.search(path):
        return "an absolute path, a backslash or a control character"
    for part in path.split("/"):
        if part in ("", ".", ".."):
            return "an empty, '.' or '..' part"
        if len(part.encode("utf-8")) > MAX_PART:
            return f"a part over {MAX_PART} bytes"
    return None


def _check_paths(files: list[str], directories: Iterable[str] = ()) -> None:
    """BundleError when two entries share a path ignoring case, or an entry is inside a file."""
    seen: dict[str, str] = {}
    paths = [*files, *directories]
    for path in paths:
        if path.lower() in seen:
            raise BundleError(f"the bundle holds {seen[path.lower()]!r} and {path!r}, one path ignoring case")
        seen[path.lower()] = path
    held = {path.lower() for path in files}
    for path in paths:
        for parent in PurePosixPath(path.lower()).parents:
            if str(parent) in held:
                raise BundleError(f"{path!r} is inside {str(parent)!r}, which is a file")


def _manifest_of(files: Iterable[Entry]) -> Manifest:
    top = next((entry for entry in files if entry.path == MANIFEST_FILE), None)
    if top is None:
        raise BundleError(f"the bundle has no {MANIFEST_FILE} at its top")
    try:
        return read_manifest(top.data)
    except ManifestError as exc:
        raise BundleError(f"{MANIFEST_FILE} is not a plugin manifest: {exc}") from None


# Packing


def _walk(root: Path, base: str = "") -> Iterable[tuple[str, os.DirEntry]]:
    with os.scandir(root) as found:
        entries = sorted(found, key=lambda entry: entry.name)
    for entry in entries:
        if skipped(entry.name):
            continue
        path = f"{base}{entry.name}"
        if entry.is_dir(follow_symlinks=False):
            yield from _walk(Path(entry.path), f"{path}/")
        else:
            yield path, entry


def _read_file(where: Path, left: int) -> tuple[bytes, bool]:
    """At most ``left`` + 1 bytes of the regular file ``where``, and whether it is executable."""
    fd = os.open(where, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        data = handle.read(left + 1)
    executable = bool(stat.S_IMODE(info.st_mode) & 0o111) if os.name != "nt" else data.startswith(b"#!")
    return data, executable


def pack(directory: str | os.PathLike) -> Bundle:
    """The bundle of plugin directory ``directory``; BundleError naming what to fix when it cannot be one."""
    root = Path(os.path.abspath(Path(directory).expanduser()))
    if not root.is_dir():
        raise BundleError(f"{root} is not a directory")
    entries: list[Entry] = []
    total = 0
    try:
        walked = list(_walk(root))
    except OSError as exc:
        raise BundleError(f"cannot read {root}: {exc.strerror or exc}") from None
    for path, entry in walked:
        where = root / path
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            raise BundleError(f"{where} is not a regular file; a bundle holds regular files only")
        problem = path_problem(path)
        if problem:
            raise BundleError(f"{where}: {problem}")
        if len(entries) >= MAX_FILES:
            raise BundleError(f"{root} holds over {MAX_FILES} files")
        try:
            data, executable = _read_file(where, MAX_UNPACKED - total)
        except OSError as exc:
            raise BundleError(f"cannot read {where}: {exc.strerror or exc}") from None
        total += len(data)
        if total > MAX_UNPACKED:
            raise BundleError(f"the files of {root} are over {MAX_UNPACKED // MiB} MiB together")
        entries.append(Entry(path, executable, data))
    entries.sort(key=lambda entry: entry.path)
    _check_paths([entry.path for entry in entries])
    contents = Contents(_manifest_of(entries), tuple(entries))
    data = compress(contents.files)
    if len(data) > MAX_BUNDLE:
        raise BundleError(f"the bundle of {root} is {len(data)} bytes, over the {MAX_BUNDLE // MiB} MiB it may have")
    return Bundle(contents, data, hashlib.sha256(data).hexdigest())


def compress(files: Iterable[Entry]) -> bytes:
    """The deterministic tar.gz of ``files``, in the order given."""
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT, encoding="utf-8") as tar:
            for entry in files:
                info = tarfile.TarInfo(entry.path)
                info.size = len(entry.data)
                info.mode = entry.mode
                tar.addfile(info, io.BytesIO(entry.data))
    return buffer.getvalue()


# Reading


class _Bounded(io.RawIOBase):
    """Reads ``source`` and raises BundleError once more than ``limit`` bytes came out of it."""

    def __init__(self, source, limit: int):
        self.source, self.left = source, limit

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self.source.read(min(len(buffer), self.left + 1))
        self.left -= len(data)
        if self.left < 0:
            raise BundleError(f"the bundle decompresses to over {MAX_UNPACKED // MiB} MiB")
        buffer[: len(data)] = data
        return len(data)


def _member(tar: tarfile.TarFile, member: tarfile.TarInfo, path: str) -> Entry:
    if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
        raise BundleError(f"{path!r} in the bundle is not a regular file or a directory")
    handle = tar.extractfile(member)
    content = handle.read() if handle is not None else b""
    if len(content) != member.size:
        raise BundleError(f"{path!r} in the bundle is cut short")
    return Entry(path, bool(member.mode & 0o111), content)


def _entries(data: bytes) -> tuple[list[Entry], list[str]]:
    files: list[Entry] = []
    directories: list[str] = []
    total = 0
    stream = _Bounded(gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb"), MAX_UNPACKED + TAR_OVERHEAD)
    with tarfile.open(fileobj=io.BufferedReader(stream), mode="r|", encoding="utf-8") as tar:
        for member in tar:
            if len(files) + len(directories) >= MAX_FILES:
                raise BundleError(f"the bundle holds over {MAX_FILES} entries")
            path = member.name.rstrip("/") if member.isdir() else member.name
            problem = path_problem(path)
            if problem:
                raise BundleError(f"the bundle holds {path!r}: {problem}")
            if member.isdir():
                directories.append(path)
                continue
            total += member.size
            if total > MAX_UNPACKED:
                raise BundleError(f"the files of the bundle are over {MAX_UNPACKED // MiB} MiB together")
            files.append(_member(tar, member, path))
    return files, directories


def read_bundle(data: bytes) -> Contents:
    """The manifest and files of bundle ``data``, checked whole; BundleError saying what is wrong otherwise."""
    if len(data) > MAX_BUNDLE:
        raise BundleError(f"the bundle is {len(data)} bytes, over {MAX_BUNDLE // MiB} MiB")
    try:
        files, directories = _entries(data)
    except BundleError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error, UnicodeError) as exc:
        raise BundleError(f"not a gzip-compressed tar ({type(exc).__name__}: {exc})") from None
    _check_paths([entry.path for entry in files], directories)
    files.sort(key=lambda entry: entry.path)
    return Contents(_manifest_of(files), tuple(files))
