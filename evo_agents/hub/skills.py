"""Skill bundles: a skill directory as a deterministic tar.gz, and the checks every bundle passes before the hub
records it as a version or ``hub skills sync`` writes it into a runtime's skills directory.

A bundle holds the regular files of one skill directory, by their paths relative to it, sorted, each with mtime 0,
uid and gid 0, no owner names and mode 0644, or 0755 when any execute bit is set; directories are implied by the
paths, so an empty one is not kept. The tar is in PAX format and compressed by gzip at level 9 with no file name and
mtime 0 in its header, so the same directory gives the same bytes and the same SHA-256 every time (on one zlib
release: another one may compress differently, which changes nothing here because the hub hashes what it receives).
Left out, wherever they are: ``__pycache__`` and ``.learned``, ``*.pyc``, ``.DS_Store``, ``.skillfish.json`` and
``.evo-hub.json``, which runtimes, continuous-learning, skillfish and ``hub skills sync`` itself write next to a
skill. A symbolic link or any other file that is not regular stops the packing: a bundle never holds one.

``read_bundle`` checks a bundle whole before anything is written: a gzip-compressed tar that decompresses to at most
MAX_UNPACKED bytes, at most MAX_FILES entries, only regular files and directories, every path relative and made of
parts that are neither empty, ``.`` nor ``..``, without backslashes or control characters, unique even ignoring case,
no file where another entry needs a directory, no name packing leaves out, and SKILL.md at the top whose frontmatter
gives the skill's name and description. ``write_tree`` then writes the files into a directory the caller just
created, never following a link. The server runs the same checks on every version it records.

Standard library and PyYAML only: the client uses it on a core install.
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

import yaml

from evo_agents.hub.memory import FRONTMATTER

KIND = "skill-bundle"  # the blob kind of a bundle in the blob store
MiB = 1024 * 1024
MAX_BUNDLE = 10 * MiB  # the hub's KIND_LIMITS["skill-bundle"]: checked before any upload URL is issued
MAX_UNPACKED = 128 * MiB  # bytes of all files of one bundle together
MAX_FILES = 10_000
MAX_PATH = 1024  # bytes of UTF-8 in one path
MAX_PART = 255  # bytes in one part of a path, as file systems allow
MAX_DESCRIPTION = 4096  # characters of the frontmatter description
SKILL_FILE = "SKILL.md"
MARKER = ".evo-hub.json"  # what `hub skills sync` writes into every skill it manages
SKIPPED_NAMES = frozenset({"__pycache__", ".learned", ".DS_Store", ".skillfish.json", MARKER})
SKIPPED_SUFFIXES = (".pyc",)
RESERVED_NAMES = frozenset({"synced", "learned"})  # directories other tools keep among a runtime's skills
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
FILE_MODE = 0o644
EXEC_MODE = 0o755
DIR_MODE = 0o755
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FIELD = re.compile(r"([A-Za-z0-9_-]+):(?:[ \t]+(.*))?")


class BundleError(ValueError):
    """A directory that cannot be packed, or bytes that are not a bundle this client or hub accepts."""


def name_problem(name) -> str | None:
    """Why ``name`` cannot name a skill, or None. A name is also a directory name in every runtime."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        return (
            f"a skill name is 1 to 100 letters, digits, '.', '_' or '-', starting with a letter or digit, not {name!r}"
        )
    if name.lower() in RESERVED_NAMES:
        return f"{name!r} is a directory other tools keep among the skills of a runtime, never a skill"
    return None


def skipped(name: str) -> bool:
    """Whether packing leaves out a file or directory called ``name``."""
    return name in SKIPPED_NAMES or name.endswith(SKIPPED_SUFFIXES)


def path_problem(path: str) -> str | None:
    """Why ``path`` cannot be the path of a file in a bundle, or None."""
    if not isinstance(path, str) or not path:
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
        if skipped(part):
            return f"{part!r}, which a bundle never holds"
    return None


@dataclass(frozen=True)
class Entry:
    """One file of a skill: its path in the bundle, whether it is executable, its bytes."""

    path: str
    executable: bool
    data: bytes

    @property
    def mode(self) -> int:
        return EXEC_MODE if self.executable else FILE_MODE


@dataclass(frozen=True)
class Contents:
    """What a bundle holds, checked: its files in path order, and the name and description of its SKILL.md."""

    name: str
    description: str
    files: tuple[Entry, ...]

    @property
    def tree(self) -> str:
        """The digest ``tree_digest`` gives the directory these files are written into."""
        return _digest((e.path, f"F {int(e.executable)} {hashlib.sha256(e.data).hexdigest()}") for e in self.files)


@dataclass(frozen=True)
class Bundle:
    """A packed skill directory."""

    contents: Contents
    data: bytes  # the tar.gz
    sha256: str

    @property
    def name(self) -> str:
        return self.contents.name

    @property
    def size(self) -> int:
        return len(self.data)


def _digest(entries: Iterable[tuple[str, str]]) -> str:
    """SHA-256 over (path, what is there) pairs in path order."""
    digest = hashlib.sha256()
    for path, what in sorted(entries):
        digest.update(f"{path}\0{what}\n".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def skill_frontmatter(text: str) -> dict:
    """The frontmatter of a SKILL.md. YAML when it parses; otherwise, as the runtimes read it, every top-level
    ``key: value`` line, an indented line continuing the value before it. Descriptions with an unquoted ``: `` are
    common and no YAML. Empty when there is no frontmatter."""
    found = FRONTMATTER.match(text)
    if found is None or found.group(1) is None:
        return {}
    try:
        data = yaml.safe_load(found.group(1))
    except yaml.YAMLError:
        data = None
    if isinstance(data, dict):
        return data
    fields: dict[str, str] = {}
    key = None
    for line in found.group(1).splitlines():
        field = _FIELD.fullmatch(line.rstrip())
        if field:
            key = field.group(1)
            fields[key] = (field.group(2) or "").strip()
        elif key is not None and line[:1] in (" ", "\t") and line.strip():
            fields[key] = f"{fields[key]} {line.strip()}".strip()
    for key, value in fields.items():
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            fields[key] = value[1:-1]
    return fields


def _skill_metadata(data: bytes, expected: str | None) -> tuple[str, str]:
    """The name and description of SKILL.md ``data``; BundleError when it does not name a skill (``expected``)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise BundleError(f"{SKILL_FILE} is not UTF-8 text") from None
    meta = skill_frontmatter(text)
    name, description = meta.get("name"), meta.get("description", "")
    if not isinstance(name, str) or not name.strip():
        raise BundleError(f"{SKILL_FILE} has no frontmatter name: it opens with ---, name: <skill>, description: ...")
    name = name.strip()
    problem = name_problem(name)
    if problem:
        raise BundleError(f"{SKILL_FILE} names the skill {name!r}: {problem}")
    if expected is not None and name != expected:
        raise BundleError(f"{SKILL_FILE} names the skill {name!r}, not {expected!r}")
    if not isinstance(description, str):
        raise BundleError(f"the description in {SKILL_FILE} is not text")
    description = description.strip()
    if len(description) > MAX_DESCRIPTION:
        raise BundleError(f"the description in {SKILL_FILE} is over {MAX_DESCRIPTION} characters")
    return name, description


def _check_paths(files: list[str], directories: Iterable[str] = ()) -> None:
    """BundleError when two entries have the same path, even ignoring case (one file on macOS and Windows), or an
    entry is inside a file."""
    seen: dict[str, str] = {}
    paths = [*files, *directories]
    for path in paths:
        if path.lower() in seen:
            raise BundleError(f"the bundle holds {seen[path.lower()]!r} and {path!r}, one path on macOS and Windows")
        seen[path.lower()] = path
    held = {path.lower() for path in files}
    for path in paths:
        for parent in PurePosixPath(path.lower()).parents:
            if str(parent) in held:
                raise BundleError(f"{path!r} is inside {str(parent)!r}, which is a file")


# Packing


def _walk(root: Path, base: str = "") -> Iterable[tuple[str, os.DirEntry]]:
    """Every entry under ``root`` that packing does not leave out, as (path in the bundle, entry), never following a
    link; directories are walked into, not returned."""
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


def pack(directory: str | os.PathLike) -> Bundle:
    """The bundle of skill directory ``directory``, whose name is the frontmatter name of its SKILL.md and must be
    the directory's own. BundleError naming what to fix when it cannot be one."""
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
        if entry.is_symlink():
            raise BundleError(f"{where} is a symbolic link; a bundle holds regular files only, copy what it points at")
        if not entry.is_file(follow_symlinks=False):
            raise BundleError(f"{where} is not a regular file; a bundle holds regular files only")
        problem = path_problem(path)
        if problem:
            raise BundleError(f"{where}: {problem}")
        if len(entries) >= MAX_FILES:
            raise BundleError(f"{root} holds over {MAX_FILES} files")
        try:
            fd = os.open(where, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                data = handle.read(MAX_UNPACKED - total + 1)
        except OSError as exc:
            raise BundleError(f"cannot read {where}: {exc.strerror or exc}") from None
        total += len(data)
        if total > MAX_UNPACKED:
            raise BundleError(f"the files of {root} are over {MAX_UNPACKED // MiB} MiB together")
        entries.append(Entry(path, bool(stat.S_IMODE(info.st_mode) & 0o111), data))
    entries.sort(key=lambda entry: entry.path)
    _check_paths([entry.path for entry in entries])
    top = next((entry for entry in entries if entry.path == SKILL_FILE), None)
    if top is None:
        raise BundleError(f"{root} has no {SKILL_FILE}: a skill directory holds one at its top")
    name, description = _skill_metadata(top.data, None)
    if name != root.name:
        raise BundleError(
            f"{root / SKILL_FILE} names the skill {name!r} but the directory is {root.name!r}: a skill is synced into "
            "a directory of its name, so rename one of them"
        )
    contents = Contents(name, description, tuple(entries))
    data = _compress(contents.files)
    if len(data) > MAX_BUNDLE:
        raise BundleError(
            f"the bundle of {root} is {len(data)} bytes, over the {MAX_BUNDLE // MiB} MiB a skill bundle may have; "
            "nothing was sent"
        )
    return Bundle(contents, data, hashlib.sha256(data).hexdigest())


def _compress(files: Iterable[Entry]) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT, encoding="utf-8") as tar:
            for entry in files:
                info = tarfile.TarInfo(entry.path)  # mtime 0, uid and gid 0, no owner names
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


TAR_OVERHEAD = 16 * MiB  # headers and padding read on top of the files themselves


def read_bundle(data: bytes, name: str | None = None) -> Contents:
    """The files of bundle ``data``, checked whole (see the module docstring); with ``name``, its SKILL.md must name
    that skill. BundleError saying what is wrong otherwise."""
    if len(data) > MAX_BUNDLE:
        raise BundleError(f"the bundle is {len(data)} bytes, over {MAX_BUNDLE // MiB} MiB")
    files: list[Entry] = []
    directories: list[str] = []
    total = 0
    try:
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
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    raise BundleError(f"{path!r} in the bundle is not a regular file or a directory")
                total += member.size
                if total > MAX_UNPACKED:
                    raise BundleError(f"the files of the bundle are over {MAX_UNPACKED // MiB} MiB together")
                handle = tar.extractfile(member)
                content = handle.read() if handle is not None else b""
                if len(content) != member.size:
                    raise BundleError(f"{path!r} in the bundle is cut short")
                files.append(Entry(path, bool(member.mode & 0o111), content))
    except BundleError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error, UnicodeError) as exc:
        raise BundleError(f"not a gzip-compressed tar ({type(exc).__name__}: {exc})") from None
    _check_paths([entry.path for entry in files], directories)
    top = next((entry for entry in files if entry.path == SKILL_FILE), None)
    if top is None:
        raise BundleError(f"the bundle has no {SKILL_FILE} at its top")
    skill, description = _skill_metadata(top.data, name)
    return Contents(skill, description, tuple(sorted(files, key=lambda entry: entry.path)))


# Directories on disk


def tree_digest(directory: str | os.PathLike) -> str:
    """A digest of what a skill directory holds, as packing sees it: the path, execute bit and SHA-256 of every file,
    and what any link or other entry is, the names packing leaves out aside. Equal to ``Contents.tree`` for the
    directory ``write_tree`` wrote."""
    found = []
    for path, entry in _walk(Path(directory)):
        if entry.is_symlink():
            found.append((path, f"L {os.readlink(entry.path)}"))
        elif entry.is_file(follow_symlinks=False):
            digest = hashlib.sha256()
            with open(entry.path, "rb") as handle:
                for chunk in iter(lambda: handle.read(MiB), b""):  # noqa: B023 - consumed right here
                    digest.update(chunk)
            executable = bool(stat.S_IMODE(entry.stat(follow_symlinks=False).st_mode) & 0o111)
            found.append((path, f"F {int(executable)} {digest.hexdigest()}"))
        else:
            found.append((path, "O"))
    return _digest(found)


def write_tree(contents: Contents, directory: Path) -> None:
    """Write the files of ``contents`` into ``directory``, an empty directory the caller just created. Every
    directory on the way is made here and checked to be one; a file is created only where nothing is, and a link is
    never followed."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    os.chmod(directory, DIR_MODE)
    made = {directory}
    for entry in contents.files:
        target = directory.joinpath(*entry.path.split("/"))
        for parent in reversed(target.relative_to(directory).parents[:-1]):
            folder = directory / parent
            if folder in made:
                continue
            try:
                os.mkdir(folder, DIR_MODE)
            except FileExistsError:
                pass
            if not stat.S_ISDIR(os.lstat(folder).st_mode):
                raise BundleError(f"{folder} is not a directory")
            os.chmod(folder, DIR_MODE)
            made.add(folder)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, entry.mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(entry.data)
            os.fchmod(handle.fileno(), entry.mode)  # the umask applied to os.open's mode
