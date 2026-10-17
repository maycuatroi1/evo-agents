"""The requirements file of an isolated plugin's backend, read before pip sees it (docs/plugins.md).

The plugin host installs a backend's requirements from wheels the bundle carries, and from nothing else: no index,
no URL, no local directory, no source distribution. A requirements file pip accepts can do much more than name a
wheel: ``-r`` and ``-c`` read other files (``-r /proc/1/environ`` would put the host's environment in pip's error),
``-e``, a path or a URL point at a directory whose build backend pip runs, ``--index-url`` and ``--find-links`` reach
the network. So the host never hands pip the plugin's file. It reads it here, keeps only lines of the one form below,
refuses the file at the first other line, and gives pip a copy it writes itself (``normalized``).

Each requirement is one logical line, ``name==version`` and at least one ``--hash=sha256:<64 hex>``; a backslash at
the end of a line continues it, and ``#`` at the start of a line or after a space starts a comment, as ``pip-compile
--generate-hashes`` and ``uv pip compile --generate-hashes`` write them. The wheels sit in ``wheels/`` next to the
file (``WHEELS``), and pip runs with ``--no-index --find-links <wheels> --only-binary=:all: --require-hashes``, so it
installs exactly those files or fails.

Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

WHEELS = "wheels"  # the directory of wheels next to the requirements file
MAX_REQUIREMENTS = 500
REQUIREMENT = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?)==(?P<version>[A-Za-z0-9.+!_-]{1,64})"
)
HASH = re.compile(r"--hash=sha256:(?P<digest>[0-9a-fA-F]{64})")
SHOWN = 80  # characters of a refused line in the error


class RequirementsError(ValueError):
    """A requirements file the host refuses: the message names the line and the one form it accepts."""


@dataclass(frozen=True)
class Requirement:
    name: str
    version: str
    hashes: tuple[str, ...]

    def line(self) -> str:
        return f"{self.name}=={self.version} " + " ".join(f"--hash=sha256:{digest}" for digest in self.hashes)


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """The lines of ``text`` with continuations joined and comments dropped, each with its first line number."""
    out: list[tuple[int, str]] = []
    pending, start = "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"(^|\s)#.*$", "", raw)
        if not pending:
            start = number
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        joined = (pending + line).strip()
        pending = ""
        if joined:
            out.append((start, joined))
    if pending.strip():
        out.append((start, pending.strip()))
    return out


def _requirement(number: int, line: str) -> Requirement:
    tokens = line.split()
    head = REQUIREMENT.fullmatch(tokens[0])
    hashes = [HASH.fullmatch(token) for token in tokens[1:]]
    if head is None or not hashes or not all(hashes):
        shown = line if len(line) <= SHOWN else line[:SHOWN] + "..."
        raise RequirementsError(
            f"line {number}: the plugin host installs only lines `name==version --hash=sha256:<64 hex>`, from the "
            f"wheels in {WHEELS}/ next to the file; not {shown!r}"
        )
    return Requirement(head["name"], head["version"], tuple(match["digest"].lower() for match in hashes))


def parse_requirements(text: str) -> list[Requirement]:
    """The requirements of ``text``; RequirementsError at the first line of any other form."""
    lines = _logical_lines(text)
    if len(lines) > MAX_REQUIREMENTS:
        raise RequirementsError(f"{len(lines)} requirements, over the {MAX_REQUIREMENTS} the plugin host installs")
    return [_requirement(number, line) for number, line in lines]


def normalized(requirements: list[Requirement]) -> str:
    """The file pip reads: one validated line per requirement, nothing else."""
    return "".join(requirement.line() + "\n" for requirement in requirements)


__all__ = ["WHEELS", "Requirement", "RequirementsError", "normalized", "parse_requirements"]
