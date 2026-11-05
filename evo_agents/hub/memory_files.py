"""The memory files of Claude Code: what names and bodies a memory may have, the frontmatter that gives its type, the
pointer lines of the index MEMORY.md, conflict copies, and reading a file without following a symlink or waiting on a
FIFO. ``evo_agents.hub.memory`` syncs them with the hub, whose server applies the same checks to every write.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

import yaml

from evo_agents.hub.client import host_name, write_atomic

TYPES = ("user", "feedback", "project", "reference")
SHARED_TYPES = frozenset({"project", "reference"})  # in a project, its members see these; their owner the others
PRIVATE_TYPE = "user"  # what a file without a readable type counts as: only its owner sees it
INDEX = "MEMORY.md"
MAX_BODY = 256 * 1024  # bytes of UTF-8 in one memory file
MAX_NAME = 255  # bytes in a file name, as file systems allow
FILE_MODE = 0o600

CONFLICT_COPY = re.compile(r"\.conflict-[A-Za-z0-9-]+\.md\Z")
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def name_problem(name) -> str | None:
    """Why ``name`` cannot name a memory file, or None. Shared with the server, which applies it to every write."""
    if not isinstance(name, str) or len(name) <= 3 or not name.endswith(".md"):
        return "a memory name is a file name ending in .md"
    try:
        size = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        return "a memory name must be valid Unicode text"
    if size > MAX_NAME:
        return f"a memory name is at most {MAX_NAME} bytes"
    if name.startswith("."):
        return "a memory name does not start with a dot"
    if "/" in name or "\\" in name or _CONTROL.search(name):
        return "a memory name holds no slash, backslash or control character"
    if name.lower() == INDEX.lower():
        return f"{INDEX} is the index of a memory directory, never a memory"
    return None


def body_problem(text) -> str | None:
    """Why ``text`` cannot be the content of a memory, or None."""
    if not isinstance(text, str):
        return "a memory is text"
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return "a memory must be valid Unicode text"
    if size > MAX_BODY:
        return f"a memory is at most {MAX_BODY // 1024} KiB of UTF-8"
    if "\x00" in text:
        return "a memory holds no NUL character"
    return None


def frontmatter(text: str) -> dict:
    """The YAML block a memory file opens with; empty when it has none or it cannot be read."""
    found = FRONTMATTER.match(text)
    if found is None or found.group(1) is None:
        return {}
    try:
        data = yaml.safe_load(found.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def memory_type(text: str) -> str:
    """metadata.type of the frontmatter, else type, else ``user``: a memory nobody typed stays its owner's."""
    data = frontmatter(text)
    metadata = data.get("metadata")
    for value in (metadata.get("type") if isinstance(metadata, dict) else None, data.get("type")):
        if isinstance(value, str) and value.strip().lower() in TYPES:
            return value.strip().lower()
    return PRIVATE_TYPE


def _one_line(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, (str, int, float)) else ""


def pointer_line(name: str, text: str) -> str:
    """The MEMORY.md line pointing at memory file ``name``, as Claude Code writes them."""
    data = frontmatter(text)
    title = (_one_line(data.get("name")) or name[:-3]).replace("[", "(").replace("]", ")")
    target = f"<{name}>" if re.search(r"[\s()<>]", name) else name
    description = _one_line(data.get("description"))
    return f"- [{title}]({target})" + (f" — {description}" if description else "")


def points_at(index: str, name: str) -> bool:
    """Whether MEMORY.md text ``index`` holds a link to memory file ``name``, in any spelling Claude Code writes."""
    targets = {name, f"./{name}", f"<{name}>", f"<./{name}>", quote(name), f"./{quote(name)}"}
    return any(f"]({target})" in index for target in targets)


def add_pointers(directory: Path, files: Mapping[str, str]) -> list[str]:
    """Append to ``directory``/MEMORY.md a pointer line for each of ``files`` (name to text) it does not point at
    yet, creating it if needed. Every line already there stays as it is. The names a line was added for."""
    index = directory / INDEX
    try:
        raw = index.read_bytes()
    except FileNotFoundError:
        raw = None
    held = (raw or b"").decode("utf-8", "replace")
    missing = [name for name in sorted(files) if not points_at(held, name)]
    if not missing:
        return []
    data = raw or b""
    if data and not data.endswith(b"\n"):
        data += b"\n"
    data += "".join(pointer_line(name, files[name]) + "\n" for name in missing).encode("utf-8")
    write_atomic(index, data, file_mode(index))
    return missing


def file_mode(path: Path) -> int:
    """The permission bits of ``path``, without following a symlink; FILE_MODE when nothing is there."""
    try:
        return stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return FILE_MODE


def host_tag(host: str | None = None) -> str:
    """This machine's name as a conflict copy carries it: letters, digits and ``-`` only."""
    return re.sub(r"[^A-Za-z0-9]+", "-", host or host_name()).strip("-")[:40] or "host"


def conflict_path(path: Path, host: str) -> Path:
    """``<name>.conflict-<host>.md`` next to ``path``, numbered when that is taken."""
    stem = path.name[:-3]
    number = 1
    while True:
        suffix = f".conflict-{host}" + (f"-{number}" if number > 1 else "") + ".md"
        while len((stem + suffix).encode("utf-8")) > MAX_NAME:
            stem = stem[:-1]
        candidate = path.with_name(stem + suffix)
        if not os.path.lexists(candidate):
            return candidate
        number += 1


class LeftAlone(Exception):
    """A file the sync does not read or replace; ``str`` says why."""


def read_regular(path: Path, limit: int = MAX_BODY) -> bytes | None:
    """The bytes of regular file ``path``, at most ``limit`` + 1 of them; None when nothing is there. A symlink is
    never followed and a FIFO never waited on."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and os.path.islink(path):
        raise LeftAlone("a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LeftAlone("a symlink" if os.path.islink(path) else exc.strerror or str(exc)) from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise LeftAlone("not a regular file")
        return handle.read(limit + 1)


def read_memory(path: Path) -> bytes | None:
    """``read_regular`` for a memory file, which is at most MAX_BODY bytes: a larger one is never cut short."""
    data = read_regular(path)
    if data is not None and len(data) > MAX_BODY:
        raise LeftAlone(f"larger than {MAX_BODY // 1024} KiB")
    return data
