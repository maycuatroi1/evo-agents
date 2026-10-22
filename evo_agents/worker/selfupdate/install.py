"""What the daemon runs from: the uv tool install of evo-ak, read from the receipt uv writes into its environment
(``uv-receipt.toml``) and from the package's dist-info (its version, and the commit ``direct_url.json`` names for an
install from git), and the uv that can install it again.

An install can be updated when uv installed it as a tool, from this repository by git or from an index (PyPI), with
nothing else beside it (no ``--with``, constraints or overrides an update would drop), and its receipt names the
``evo-agents`` command. Anything else has a ``reason``, which the daemon reports as its state ``unsupported``.

An update installs the extras of the receipt and always the worker extra the daemon runs on: a receipt can lack it
(evo-ak installed again without it, by a person or a tool, under a daemon that still runs), and nightly installed with
the receipt's extras alone has no aiohttp, so its ``worker run`` does not start.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from evo_agents.hub import runs

PACKAGE = "evo-ak"
REPO_URL = runs.NIGHTLY_REPO_URL  # the hub reads where its tag points from the same place
TAG = runs.NIGHTLY_TAG
COMMAND = "evo-agents"
RECEIPT = "uv-receipt.toml"
COMMIT = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")  # OBJECT_NAME of the hub
UV_PLACES = ("~/.local/bin", "~/.cargo/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
UPDATABLE = ("git", "pypi")
DROPPED_KEYS = ("constraints", "overrides", "build-constraint-dependencies")
WORKER_EXTRA = "worker"  # aiohttp and the runtimes' SDKs, without which `evo-agents worker run` does not start


@dataclass(frozen=True)
class Identity:
    """What lies in an install now: the name of evo-ak's dist-info directory (its version) and the commit it was built
    from. Another daemon's update, or a person's, changes it under a running daemon."""

    dist_info: str | None
    commit: str | None


@dataclass(frozen=True)
class Install:
    prefix: Path  # the tool's environment, sys.prefix of the daemon
    kind: str  # git, pypi: it can be updated; none, editable, local, url, foreign, other: not
    reason: str | None = None  # why it cannot be updated; None when it can
    version: str | None = None
    commit: str | None = None
    extras: tuple[str, ...] = ()  # as the receipt lists them
    entrypoint: Path | None = None  # the evo-agents command uv put on the PATH
    python: str | None = None  # the interpreter the environment was made from, which an update keeps
    commands: tuple[Path, ...] = ()  # every command of the receipt, the entrypoint among them

    @property
    def requirement_extras(self) -> str:
        """The extras of the requirement an update installs: the receipt's, and the worker extra whether it lists it
        or not."""
        return f"[{','.join(sorted({*self.extras, WORKER_EXTRA}))}]"

    def identity(self) -> Identity:
        return read_identity(self.prefix)


def dist_info(prefix: Path) -> Path | None:
    """evo-ak's dist-info directory in the environment at ``prefix``, or None."""
    found = sorted(prefix.glob("lib/python*/site-packages/evo_ak-*.dist-info"))
    return found[-1] if found else None


def read_identity(prefix: Path) -> Identity:
    found = dist_info(prefix)
    if found is None:
        return Identity(None, None)
    try:
        direct = json.loads((found / "direct_url.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        direct = None
    vcs = direct.get("vcs_info") if isinstance(direct, dict) else None
    commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
    return Identity(found.name, commit if isinstance(commit, str) and COMMIT.match(commit) else None)


def version_of(identity: Identity) -> str | None:
    """0.22.0 of evo_ak-0.22.0.dist-info."""
    name = identity.dist_info or ""
    if not name.startswith("evo_ak-") or not name.endswith(".dist-info"):
        return None
    return name[len("evo_ak-") : -len(".dist-info")] or None


def _normalized(name) -> str:
    return re.sub(r"[-_.]+", "-", name).lower() if isinstance(name, str) else ""


def repo_of(url: str) -> str:
    """The repository of a git URL as a receipt writes it, without ``git+``, the revision and ``.git``."""
    base = url.removeprefix("git+").split("#", 1)[0].split("?", 1)[0].rstrip("/")
    head, _, last = base.rpartition("/")
    if "@" in last:  # .../evo-agents@nightly, as pip writes a revision
        base = f"{head}/{last.split('@', 1)[0]}"
    return base.removesuffix(".git")


def _kind(requirement: dict) -> tuple[str, str | None]:
    """The source of evo-ak's requirement in a receipt, and why it cannot be updated."""
    if requirement.get("editable"):
        return "editable", f"an editable install of {requirement['editable']}"
    for key in ("directory", "path"):
        if requirement.get(key):
            return "local", f"installed from the local path {requirement[key]}"
    if requirement.get("url"):
        return "url", f"installed from the URL {requirement['url']}"
    if requirement.get("git"):
        repo = repo_of(str(requirement["git"]))
        if repo != REPO_URL:
            return "foreign", f"installed from {repo}, not {REPO_URL}"
        return "git", None
    return "pypi", None


def _parse_receipt(data) -> tuple[dict | None, str | None]:
    """evo-ak's requirement in a receipt, or why the receipt does not allow an update."""
    tool = data.get("tool") if isinstance(data, dict) else None
    if not isinstance(tool, dict):
        return None, "the receipt has no [tool] table"
    requirements = [item for item in tool.get("requirements") or [] if isinstance(item, dict)]
    ours = [item for item in requirements if _normalized(item.get("name")) == PACKAGE]
    others = sorted(str(item.get("name")) for item in requirements if item not in ours)
    if len(ours) != 1:
        return None, f"the receipt names {PACKAGE} {len(ours)} times"
    if others:
        return None, f"installed with {', '.join(others)} beside it, which an update would drop"
    dropped = [key for key in DROPPED_KEYS if tool.get(key)]
    if dropped:
        return None, f"installed with {', '.join(dropped)}, which an update would drop"
    return ours[0], None


def _commands(data) -> dict[str, Path]:
    """The commands uv put on the PATH for the tool, by name."""
    found = {}
    for item in data["tool"].get("entrypoints") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("install-path"), str):
            found[item["name"]] = Path(item["install-path"])
    return found


def read_install(prefix: Path, python: str | None) -> Install:
    """The install whose environment is ``prefix``, made from the interpreter ``python``
    (``sys._base_executable``)."""
    prefix = Path(prefix)
    identity = read_identity(prefix)
    facts = {"version": version_of(identity), "commit": identity.commit, "python": python}
    receipt = prefix / RECEIPT
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Install(prefix, "none", f"not installed with `uv tool install` ({prefix} has no {RECEIPT})", **facts)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return Install(prefix, "other", f"cannot read {receipt}: {exc}", **facts)
    requirement, problem = _parse_receipt(data)
    if requirement is None:
        return Install(prefix, "other", problem, **facts)
    kind, problem = _kind(requirement)
    extras = tuple(sorted(str(item) for item in requirement.get("extras") or []))
    commands = _commands(data)
    entrypoint = commands.get(COMMAND)
    if problem is None and entrypoint is None:
        problem = f"the receipt names no {COMMAND} command"
    if problem is None and _normalized(prefix.name) != PACKAGE:
        problem = f"the tool's environment {prefix} is not named {PACKAGE}"
    if problem is None and not python:
        problem = "the interpreter of the install is not known"
    if problem is None and identity.dist_info is None:
        problem = f"no dist-info of {PACKAGE} in {prefix}"
    found = {"extras": extras, "entrypoint": entrypoint, "commands": tuple(commands.values())}
    return Install(prefix, kind, problem, **found, **facts)


def find_uv(path: str | None) -> str | None:
    """uv on ``path`` (the service's PATH), else in the places installers put it; None when it is in neither."""
    found = shutil.which("uv", path=path) if path else None
    if found:
        return found
    for place in UV_PLACES:
        candidate = Path(place).expanduser() / "uv"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None
