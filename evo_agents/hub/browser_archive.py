"""The archive of a browser identity's profile (plan agent-browser, criterion a4), as the worker packs it and unpacks
it, and as the hub checks it before it seals it (``evo_agents.hub.server.browser_profiles``).

An archive is a gzipped tar of a Chromium profile directory, packed once Chromium has exited (``pack``): regular
files and directories only, with neither the caches Chromium rebuilds (CACHES, at any depth) nor what names the
process that ran (the Singleton* files, DevToolsActivePort). Every member is owned by no one (uid and gid 0, no user
or group name), so an archive names nothing of the machine it was packed on.

``check`` is what the hub takes: a gzipped tar of at most MAX_MEMBERS members, regular files and directories only, a
relative name without ``..`` for each, and at most MAX_UNPACKED_BYTES of them all, counted as it decompresses, so an
archive that inflates without end is refused early. ``unpack`` checks the same as it writes into an empty directory,
then removes any Singleton* or DevToolsActivePort file, which would make Chromium take the profile for one in use or
read a port of another process. Standard library only: the worker and the hub both import it.
"""

from __future__ import annotations

import gzip
import io
import os
import tarfile
import zlib
from pathlib import Path, PurePosixPath

CACHES = frozenset(
    {
        "Cache",
        "Code Cache",
        "GPUCache",
        "GPUPersistentCache",
        "GraphiteDawnCache",
        "DawnGraphiteCache",
        "DawnWebGPUCache",
        "ShaderCache",
        "GrShaderCache",
        "component_crx_cache",
        "Crashpad",
        "Crash Reports",
        "BrowserMetrics",
    }
)
LEFT_BY_A_PROCESS = ("Singleton", "DevToolsActivePort")  # names starting so are never packed and go at unpack
MAX_MEMBERS = 50_000
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
FILE_MODE = 0o600
DIR_MODE = 0o700
CHUNK = 1024 * 1024
HEADER_BYTES = 2048  # a member's tar and PAX headers and padding, at most, for a name Chromium gives a file


class ProfileArchiveError(ValueError):
    """An archive that is not a profile as ``pack`` makes one: not a gzipped tar, a member of another kind or name, or
    more than the limits."""


def _skipped(name: str) -> bool:
    return name in CACHES or name.startswith(LEFT_BY_A_PROCESS)


def _owned_by_none(info: tarfile.TarInfo, mode: int) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = mode
    return info


def pack(directory: Path) -> bytes:
    """The archive of the profile in ``directory``, without its caches; symbolic links and other special files are
    left out. Run it once Chromium has exited, so no file is half written."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for root, dirs, files in os.walk(directory):
            here = Path(root)
            dirs[:] = sorted(name for name in dirs if not _skipped(name) and not (here / name).is_symlink())
            for name in dirs:
                path = here / name
                info = archive.gettarinfo(str(path), path.relative_to(directory).as_posix())
                archive.addfile(_owned_by_none(info, DIR_MODE))
            for name in sorted(files):
                path = here / name
                if _skipped(name) or path.is_symlink() or not path.is_file():
                    continue
                info = archive.gettarinfo(str(path), path.relative_to(directory).as_posix())
                with path.open("rb") as handle:
                    archive.addfile(_owned_by_none(info, FILE_MODE), handle)
    return buffer.getvalue()


class _Bounded(io.RawIOBase):
    """The decompressed bytes of an archive, refused past ``limit``: a member's header says its size, the stream says
    what it is."""

    def __init__(self, data: bytes, limit: int):
        self.inner = gzip.GzipFile(fileobj=io.BytesIO(data))
        self.limit = limit
        self.read_bytes = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        try:
            data = self.inner.read(len(buffer))
        except (OSError, EOFError, zlib.error) as exc:
            raise ProfileArchiveError(f"the profile is not a gzipped tar: {exc}") from None
        self.read_bytes += len(data)
        if self.read_bytes > self.limit:
            raise ProfileArchiveError(f"the profile unpacks to more than {self.limit // (1024 * 1024)} MiB")
        buffer[: len(data)] = data
        return len(data)


def _members(archive: bytes, limit: int):
    """Each member of ``archive`` with the stream it is read from, checked as ``check`` says."""
    headers = MAX_MEMBERS * HEADER_BYTES  # what the tar itself takes besides its members, at most
    stream = tarfile.open(fileobj=io.BufferedReader(_Bounded(archive, limit + headers)), mode="r|")
    try:
        total = count = 0
        for member in stream:
            count += 1
            if count > MAX_MEMBERS:
                raise ProfileArchiveError(f"the profile has more than {MAX_MEMBERS} files")
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or member.name.startswith("/"):
                raise ProfileArchiveError("the profile names a file outside itself")
            if not (member.isfile() or member.isdir()):
                raise ProfileArchiveError("the profile holds a link or a special file")
            total += member.size
            if total > limit:
                raise ProfileArchiveError(f"the profile unpacks to more than {limit // (1024 * 1024)} MiB")
            yield member, stream
    except tarfile.TarError as exc:
        raise ProfileArchiveError(f"the profile is not a gzipped tar: {exc}") from None
    finally:
        stream.close()


def check(archive: bytes, *, limit: int = MAX_UNPACKED_BYTES) -> int:
    """The number of members of ``archive``; ``ProfileArchiveError`` when it is not a profile as ``pack`` makes one.
    CPU-bound: run it in a thread."""
    return sum(1 for _ in _members(archive, limit))


def unpack(archive: bytes, directory: Path, *, limit: int = MAX_UNPACKED_BYTES) -> None:
    """Write the profile ``archive`` holds into ``directory``, empty or absent, checking it as it goes, then remove
    what a process left in it. ``ProfileArchiveError`` for an archive ``check`` refuses; what was written stays for
    the caller to remove."""
    directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    for member, stream in _members(archive, limit):
        path = directory.joinpath(*PurePosixPath(member.name).parts)
        if member.isdir():
            path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            continue
        path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        source = stream.extractfile(member)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags, FILE_MODE), "wb") as target:
            while chunk := source.read(CHUNK):
                target.write(chunk)
    for left in directory.iterdir():
        if left.name.startswith(LEFT_BY_A_PROCESS) and (left.is_symlink() or left.is_file()):
            left.unlink()
