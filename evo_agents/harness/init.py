"""``evo-agents harness init``: a minimal harness.yaml and knowledge.yaml for the git repos of one directory.

The directory becomes the harness root. Its repos are the directory itself when it is the top of a git checkout (it
holds ``.git``) and each direct subdirectory that is one, hidden ones aside. A repo is named by its directory and keeps
the origin its git remote names (an https URL without the user or password it may carry) and its default branch: the
one ``origin/HEAD`` points at, else the branch checked out. Repo paths are relative to the workspace, the directory
above the harness, where harness.yaml leaves it by default. The project, named by the directory unless the caller
names it, is the harness's name, its ``hub.project`` and knowledge.yaml's project.

knowledge.yaml follows knowledge.schema.json as it is: the default ladder of labels, a sink for Claude Code sessions
and one of kind hub, both clearing ``internal``, a harness source and one git source per repo, each labelled
``internal``. That is what ``evo-agents hub project register`` needs for memories and plans without a label of their
own to be pushed. A git source reads ``origin/<branch>`` with ``fetch: true`` when its repo has an origin with that
branch, and the local branch otherwise. The harness source leaves the repos to their git sources: it skips the
subdirectories that are repos, and of a harness that is a repo itself it reads the harness's own files alone.

Nothing is overwritten: when harness.yaml or knowledge.yaml is there already, init writes neither and names them.
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from evo_agents.harness import DEFAULT_KNOWLEDGE, MANIFEST

PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")  # a hub project name (harness.schema.json, the hub's table)
SOURCE_ID = re.compile(r"[^a-z0-9_.-]+")  # what knowledge.schema.json does not take in a source id
LEVELS = ["public", "internal", "customer", "secret"]  # evo_agents.kg.policy.DEFAULT_LEVELS, written out to edit
LABEL = {"level": "internal", "integrity": "U"}
SINKS = [
    {"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "internal"}},
    {"id": "evo-hub", "kind": "hub", "clearance": {"level": "internal"}},
]
HARNESS_FILES = [MANIFEST, DEFAULT_KNOWLEDGE, "contracts.yaml", "plans/*"]  # of a harness that is a repo itself
GIT_TIMEOUT = 10  # seconds one git command that reads a repo's remote or branch may take
EXIT_USAGE = 2

HEADER = {
    MANIFEST: (
        "# Written by `evo-agents harness init`. Repo paths are relative to the workspace, the directory above this\n"
        "# one. `evo-agents harness validate` checks this file and knowledge.yaml.\n"
    ),
    DEFAULT_KNOWLEDGE: (
        "# Written by `evo-agents harness init`: the sources of the project's knowledge graph and their labels.\n"
        "# Raise a label (levels below) for a repo whose content must not reach a sink that clears less.\n"
    ),
}


class InitError(Exception):
    """Why init wrote nothing; ``code`` is the exit status (2 for a bad argument)."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Repo:
    name: str
    directory: Path
    origin: str | None
    branch: str | None
    on_origin: bool  # the branch is read from origin/<branch>


@dataclass(frozen=True)
class Written:
    root: Path
    project: str
    repos: tuple[Repo, ...]
    files: tuple[Path, ...]


def _git(directory: Path, *args: str) -> str | None:
    """What ``git -C directory <args>`` prints, stripped; None when git fails or prints nothing."""
    try:
        done = subprocess.run(
            ["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = done.stdout.strip()
    return text if done.returncode == 0 and text else None


def without_secret(origin: str) -> str:
    """``origin`` without the user and password an http(s) URL may carry; any other form as it is."""
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or "@" not in parts.netloc:
        return origin
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


def read_repo(directory: Path) -> Repo:
    """The repo checked out at ``directory``: its origin and its default branch, as git has them."""
    origin = _git(directory, "config", "--get", "remote.origin.url")
    head = _git(directory, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if origin and head and head.startswith("origin/"):
        branch, on_origin = head.removeprefix("origin/"), True
    else:
        branch = _git(directory, "symbolic-ref", "--quiet", "--short", "HEAD")  # None on a detached HEAD
        tracked = branch and _git(directory, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
        on_origin = bool(origin and tracked)
    return Repo(directory.name, directory, without_secret(origin) if origin else None, branch, on_origin)


def find_repos(root: Path) -> list[Repo]:
    """The repos of ``root``: itself when it is the top of a checkout, then its direct subdirectories that are."""
    found = [root] if (root / ".git").exists() else []
    found += sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".") and (d / ".git").exists())
    return [read_repo(directory) for directory in found]


def project_name(text: str) -> str | None:
    """``text`` (a directory's name) as a hub project name, or None when nothing of it is left."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    name = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")[:100].rstrip("-")
    return name or None


def _source_ids(repos: list[Repo]) -> list[str]:
    """One source id per repo, from its name, none of them ``harness`` and no two alike."""
    taken, ids = {"harness"}, []
    for repo in repos:
        base = SOURCE_ID.sub("-", repo.name.lower()).lstrip("_.-") or "repo"
        sid, n = base, 1
        while sid in taken:
            n += 1
            sid = f"{base}-{n}"
        taken.add(sid)
        ids.append(sid)
    return ids


def manifest(root: Path, project: str, repos: list[Repo]) -> dict:
    """harness.yaml: the project's name, its hub project and its repos, placed relative to the workspace."""
    entries = []
    for repo in repos:
        path = root.name if repo.directory == root else f"{root.name}/{repo.directory.name}"
        entry = {"name": repo.name, "path": path}
        if repo.origin:
            entry["origin"] = repo.origin
        if repo.branch:
            entry["default_branch"] = repo.branch
        entries.append(entry)
    return {
        "version": 1,
        "name": project,
        "knowledge_file": DEFAULT_KNOWLEDGE,
        "hub": {"project": project},
        "repos": entries,
    }


def knowledge(root: Path, project: str, repos: list[Repo]) -> dict:
    """knowledge.yaml: the default ladder, a session sink and a hub sink, the harness source and a git source a repo."""
    harness = {"id": "harness", "connector": "harness"}
    if any(repo.directory == root for repo in repos):
        harness.update(include=list(HARNESS_FILES), assets=False)
    else:
        inside = [f"{repo.directory.name}/*" for repo in repos]
        if inside:
            harness["exclude"] = inside
    harness["label"] = dict(LABEL)
    sources = [harness]
    for repo, sid in zip(repos, _source_ids(repos), strict=True):
        source = {"id": sid, "connector": "git", "repo": repo.name}
        if repo.branch:
            source["ref"] = f"origin/{repo.branch}" if repo.on_origin else repo.branch
        if repo.on_origin:
            source["fetch"] = True
        source["label"] = dict(LABEL)
        sources.append(source)
    policy = {"levels": list(LEVELS), "sinks": [{**sink, "clearance": dict(sink["clearance"])} for sink in SINKS]}
    return {"version": 1, "project": project, "policy": policy, "sources": sources}


def _project(root: Path, asked: str | None) -> str:
    if asked is not None:
        if not PROJECT_NAME.fullmatch(asked):
            raise InitError(
                f"--project {asked!r} is not a hub project name: a-z, 0-9 and -, starting with a letter or a digit, "
                "100 characters at most",
                EXIT_USAGE,
            )
        return asked
    name = project_name(root.name)
    if name is None:
        raise InitError(f"the name of {root} makes no hub project name: give one with --project NAME", EXIT_USAGE)
    return name


class _Flow(dict):
    """A short mapping written on one line, ``{level: internal, integrity: U}``."""


class _FlowList(list):
    """A short list written on one line, ``[public, internal]``."""


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(_Flow, lambda dumper, data: dumper.represent_mapping("tag:yaml.org,2002:map", data, True))
_Dumper.add_representer(_FlowList, lambda dumper, data: dumper.represent_sequence("tag:yaml.org,2002:seq", data, True))
FLOW = {"label": _Flow, "clearance": _Flow, "levels": _FlowList}


def _flow(data):
    """``data`` with its labels, clearances and the ladder marked to be written on one line each."""
    if isinstance(data, dict):
        return {key: FLOW[key](value) if key in FLOW else _flow(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_flow(value) for value in data]
    return data


def _dump(name: str, data: dict) -> str:
    text = yaml.dump(_flow(data), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)
    return HEADER[name] + text


def init_harness(path: str | Path = ".", project: str | None = None) -> Written:
    """Write harness.yaml and knowledge.yaml in ``path`` for the git repos found there; InitError says why not."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise InitError(f"{root} is not a directory")
    there = [name for name in (MANIFEST, DEFAULT_KNOWLEDGE) if (root / name).exists()]
    if there:
        raise InitError(
            f"{' and '.join(there)} {'is' if len(there) == 1 else 'are'} in {root} already; harness init overwrites "
            "nothing, so it wrote no file: edit what is there, or run it in another directory"
        )
    name = _project(root, project)
    repos = find_repos(root)
    if not repos:
        raise InitError(
            f"no git repo in {root}: run harness init in a repo's directory, or in one whose direct subdirectories "
            "are repos"
        )
    texts = {MANIFEST: _dump(MANIFEST, manifest(root, name, repos))}
    texts[DEFAULT_KNOWLEDGE] = _dump(DEFAULT_KNOWLEDGE, knowledge(root, name, repos))
    files = []
    for file_name, text in texts.items():
        target = root / file_name
        try:
            with open(target, "x", encoding="utf-8") as fh:  # "x": a file made meanwhile is not overwritten either
                fh.write(text)
        except FileExistsError:
            raise InitError(f"{target} appeared while harness init ran; it left that file as it is") from None
        files.append(target)
    return Written(root, name, tuple(repos), tuple(files))
