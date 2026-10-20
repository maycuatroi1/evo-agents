"""The core plugins of the hub's image: what deploy/hub/Dockerfile runs around pip, with the image's own Python
(``python -I -m evo_agents.plugins.core_install COMMAND``), before and after it installs them (docs/hub-deployment.md,
section Core plugins in the image).

Two sources, each a list of requirements by the rules of an isolated plugin's backend (``evo_agents.plugins.
requirements``): ``name==version`` and at least one ``--hash=sha256:<64 hex>``, nothing else.

- ``deploy/hub/core-plugins.txt``, in the repository: the core plugins evo-agents ships with the hub, from PyPI.
- The build argument ``EVO_HUB_EXTRA_CORE_PLUGINS``: a hub's own, separated by line breaks or spaces, a token that
  does not start with ``--hash=`` starting the next requirement. It names the plugins and every dependency of theirs
  the image lacks, since pip installs them with ``--no-deps``. They come from the index the build secret
  ``core_plugin_index`` names (compose takes it from ``EVO_HUB_CORE_PLUGIN_INDEX_URL``), else from PyPI.

The commands:

- ``pinned FILE`` prints the requirements of the pinned file as pip reads them; it refuses evo-ak (the hub itself)
  and a package named twice.
- ``extra FILE`` prints those of the variable. It refuses evo-ak, a package named twice, a package the pinned
  ``FILE`` names, and a package the image has at another version: a plugin may not change a library of the hub. A
  package the image has at the version asked for is left out.
- ``index [FILE]`` checks the secret (``/run/secrets/core_plugin_index``), when there is one: one or more URLs
  separated by spaces, each https, or http to localhost. A wrong URL fails the build without showing it, since it
  may hold a credential.
- ``check`` fails when a core plugin the image declares would not load in the hub: an entry point of the group
  ``evo_agents.plugins`` that ``core_plugins()`` leaves out (a wrong manifest, ``engines`` that do not cover this
  release, an API the plugin ``require``s that this hub does not serve, an import that fails), or a name two
  distributions declare. It names the entry point, its distribution and version, and the reason, and exits 1.

An error goes to stderr and exits 1; what pip reads goes to stdout. Standard library only, as all of
``evo_agents.plugins``.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import sys
from collections.abc import Iterator
from importlib.metadata import distributions
from pathlib import Path
from urllib.parse import urlsplit

from evo_agents.plugins import core
from evo_agents.plugins.requirements import (
    MAX_REQUIREMENTS,
    SHOWN,
    Requirement,
    RequirementsError,
    normalized,
    parse_requirements,
)

EXTRA_VARIABLE = "EVO_HUB_EXTRA_CORE_PLUGINS"  # the build argument
INDEX_SECRET = "core_plugin_index"  # the build secret, from EVO_HUB_CORE_PLUGIN_INDEX_URL of the compose file
INDEX_VARIABLE = "EVO_HUB_CORE_PLUGIN_INDEX_URL"
INDEX_FILE = Path("/run/secrets") / INDEX_SECRET
HUB = "evo-ak"  # the hub's own distribution, which the image installs from its source
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
FORM = "a core plugin is installed only from lines `name==version --hash=sha256:<64 hex>`"
NOT_LOADED = "core plugin not loaded"  # what core_plugins() logs for an entry point it leaves out


class InstallError(Exception):
    """A requirement, a secret or a plugin the image refuses; the message says which and why."""


def canonical(name: str) -> str:
    """A distribution's name as PEP 503 compares names."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _refuse_hub_and_twice(requirements: list[Requirement], where: str) -> None:
    seen: set[str] = set()
    for requirement in requirements:
        name = canonical(requirement.name)
        if name == HUB:
            raise InstallError(
                f"{where}: {requirement.name} is the hub itself, which the image installs from its own source"
            )
        if name in seen:
            raise InstallError(f"{where}: {requirement.name} is named twice")
        seen.add(name)


def read_pinned(path: Path) -> list[Requirement]:
    """The requirements of the pinned file ``path``; InstallError naming the file and the line."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"cannot read {path}: {exc.strerror or exc}") from None
    try:
        found = parse_requirements(text, form=FORM)
    except RequirementsError as exc:
        raise InstallError(f"{path}, {exc}") from None
    _refuse_hub_and_twice(found, str(path))
    return found


def extra_requirements(value: str) -> list[Requirement]:
    """The requirements of the variable's ``value``: a token that does not start with ``--hash=`` starts the next
    one; a backslash at the end of a line continues it, as in a requirements file."""
    lines: list[list[str]] = []
    for token in value.replace("\\\n", " ").split():
        if not lines or not token.startswith("--hash="):
            lines.append([])
        lines[-1].append(token)
    if len(lines) > MAX_REQUIREMENTS:
        raise InstallError(f"{EXTRA_VARIABLE}: {len(lines)} requirements, over the {MAX_REQUIREMENTS} it takes")
    found = []
    for number, tokens in enumerate(lines, 1):
        line = " ".join(tokens)
        try:
            parsed = parse_requirements(line, form=FORM)
        except RequirementsError as exc:
            reason = str(exc).partition(": ")[2]  # "line 1: ..." of a text of one line
            raise InstallError(f"{EXTRA_VARIABLE}, requirement {number}: {reason}") from None
        if len(parsed) != 1:  # a comment, which a requirements file ends at the line's end and the variable never
            raise InstallError(f"{EXTRA_VARIABLE}, requirement {number}: {FORM}; not {line[:SHOWN]!r}")
        found.extend(parsed)
    _refuse_hub_and_twice(found, EXTRA_VARIABLE)
    return found


def installed_versions() -> dict[str, str]:
    """name -> version of every distribution this Python has, the image's /opt/venv when the build runs it."""
    found: dict[str, str] = {}
    for dist in distributions():
        name = dist.metadata["Name"]
        if name:
            found.setdefault(canonical(name), dist.version)
    return found


def extra(pinned: Path, value: str, installed: dict[str, str]) -> tuple[list[Requirement], list[str]]:
    """What pip installs of the variable's ``value``, and a note for each requirement left out because the image
    has it already; InstallError for one the rules refuse."""
    pinned_names = {canonical(requirement.name) for requirement in read_pinned(pinned)}
    keep, notes = [], []
    for requirement in extra_requirements(value):
        name = canonical(requirement.name)
        have = installed.get(name)
        if name in pinned_names:
            raise InstallError(
                f"{EXTRA_VARIABLE}: {requirement.name} is pinned in {pinned}, at the version evo-agents ships with "
                "the hub; leave it out of the variable"
            )
        if have is None:
            keep.append(requirement)
        elif have == requirement.version:
            notes.append(f"{requirement.name} {have} is in the image already, not installed again")
        else:
            raise InstallError(
                f"{EXTRA_VARIABLE}: {requirement.name}=={requirement.version}, but the image has {requirement.name} "
                f"{have}; a core plugin may not change a library of the hub: ask for {have}, or leave it out"
            )
    return keep, notes


def _index_ok(url: str) -> bool:
    if not all(" " < character <= "~" for character in url):  # what the shell of the build keeps in one word
        return False
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError:
        return False
    if not parts.hostname or parts.fragment:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)


def index_urls(text: str) -> list[str]:
    """The URLs of the secret's ``text``, separated by spaces, tabs or line breaks as the build's shell splits them;
    InstallError for a wrong one, which never shows the URL."""
    urls = [url for url in re.split(r"[ \t\n]+", text) if url]
    for number, url in enumerate(urls, 1):
        if not _index_ok(url):
            raise InstallError(
                f"URL {number} of {len(urls)} in the build secret {INDEX_SECRET} ({INDEX_VARIABLE}) is not https, or "
                "http to localhost, with a host, no fragment and printable ASCII only; it is not shown, since it may "
                "hold a credential"
            )
    return urls


def read_index(path: Path) -> list[str]:
    """The URLs of the secret at ``path``, none when there is no secret."""
    if not path.is_file():
        return []
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError(f"cannot read the build secret {INDEX_SECRET}: {type(exc).__name__}") from None
    return index_urls(text)


@contextlib.contextmanager
def _left_out() -> Iterator[dict[tuple[str, str], str]]:
    """(entry point, value) -> the error of each entry point core_plugins() leaves out while in the block, from
    the line it logs; nothing reaches the root logger meanwhile."""
    reasons: dict[tuple[str, str], str] = {}

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.msg == NOT_LOADED and hasattr(record, "entry_point"):
                reasons[(record.entry_point, record.value)] = record.error

    logger = logging.getLogger(core.__name__)
    handler, level, propagate = Collect(logging.WARNING), logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        yield reasons
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate


def _source(point) -> str:
    dist = getattr(point, "dist", None)
    if dist is None:
        return "an unknown distribution"
    return f"{dist.metadata['Name'] or dist.name} {dist.version}"


def check_problems(points: list) -> tuple[list[str], list[str]]:
    """What loads of entry points ``points`` of the group, a line each, and what is wrong with them, a line each."""
    with _left_out() as reasons:
        loaded = core.core_plugins()
    sources = {(point.name, point.value): _source(point) for point in points}
    problems = []
    names: dict[str, list] = {}
    for point in points:
        names.setdefault(point.name, []).append(point)
    for name, declared in sorted(names.items()):
        if len(declared) > 1:
            each = "; ".join(f"{_source(point)} ({point.value})" for point in declared)
            problems.append(f"entry point {name} is declared by {len(declared)} distributions: {each}")
    found = {(plugin.entry_point, plugin.value) for plugin in loaded}
    for point in points:
        if (point.name, point.value) not in found:
            reason = reasons.get((point.name, point.value), "core_plugins() left it out")
            problems.append(f"entry point {point.name} ({point.value}) of {_source(point)} does not load: {reason}")
    lines = [
        f"core plugin {plugin.manifest.id} {plugin.manifest.version}: entry point {plugin.entry_point} of "
        f"{sources.get((plugin.entry_point, plugin.value), 'an unknown distribution')}"
        for plugin in loaded
    ]
    return lines, problems


def check(out, err) -> int:
    try:
        points = list(core.entry_points(group=core.ENTRY_POINT_GROUP))
    except Exception as exc:  # a broken distribution's metadata
        print(f"cannot read the entry points of {core.ENTRY_POINT_GROUP}: {type(exc).__name__}: {exc}", file=err)
        return 1
    lines, problems = check_problems(points)
    for line in lines:
        print(line, file=out)
    if problems:
        for problem in problems:
            print(problem, file=err)
        print(
            f"{len(problems)} problem(s) with the core plugins of this image: the hub would run without them. Pin a "
            "version that runs on this release (deploy/hub/core-plugins.txt or EVO_HUB_EXTRA_CORE_PLUGINS).",
            file=err,
        )
        return 1
    print(f"{len(lines)} core plugin(s) load" if lines else "no core plugin is installed", file=out)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -I -m evo_agents.plugins.core_install",
        description="Read and check the core plugins of the hub's image (deploy/hub/Dockerfile).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    pinned = sub.add_parser("pinned", help="print the requirements of the pinned file as pip reads them")
    pinned.add_argument("file", type=Path, help="deploy/hub/core-plugins.txt")
    added = sub.add_parser("extra", help=f"print the requirements of {EXTRA_VARIABLE} that pip installs")
    added.add_argument("file", type=Path, help="the pinned file, whose packages the variable may not name")
    index = sub.add_parser("index", help=f"check the URLs of the build secret {INDEX_SECRET}")
    index.add_argument("file", type=Path, nargs="?", default=INDEX_FILE, help=f"default {INDEX_FILE}")
    sub.add_parser("check", help="fail when a core plugin of this Python does not load")
    return parser


def main(argv: list[str] | None = None, *, out=None, err=None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    args = _parser().parse_args(argv)
    try:
        if args.command == "pinned":
            out.write(normalized(read_pinned(args.file)))
        elif args.command == "extra":
            keep, notes = extra(args.file, os.environ.get(EXTRA_VARIABLE, ""), installed_versions())
            for note in notes:
                print(note, file=err)
            out.write(normalized(keep))
        elif args.command == "index":
            urls = read_index(args.file)
            source = f"the {len(urls)} URL(s) of the build secret {INDEX_SECRET}" if urls else "PyPI"
            print(f"{EXTRA_VARIABLE} installs from {source}", file=err)
        else:
            return check(out, err)
    except InstallError as exc:
        print(f"core_install {args.command}: {exc}", file=err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
