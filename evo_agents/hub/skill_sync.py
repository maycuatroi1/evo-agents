"""``evo-agents hub skills publish|list|sync``: skill directories on the hub, and their copies in the skills directories
of every agent runtime on this machine.

``publish`` packs a directory (``evo_agents.hub.skills``) and checks, before sending anything, what the hub would
refuse: a bundle over 10 MiB, a global skill without being a hub admin, a project skill without the writer role or a
hub sink. Then it asks for an upload URL, PUTs the bundle, commits it and creates the version.

``sync`` writes the latest version of every skill one sees into:

- for a global skill, ``skills/`` in the directory of each runtime that has one here: ``~/.claude`` (or
  $CLAUDE_CONFIG_DIR), ``~/.agents``, ``~/.codex`` (or $CODEX_HOME), ``~/.cursor``, ``~/.gemini``;
- for a skill of a hub project, ``.claude/skills`` and ``.agents/skills`` in the project's harness root, where
  ``hub registry pull`` places it, when that harness is on this machine. Such a directory keeps a ``.gitignore`` whose
  block between MANAGED_BEGIN and MANAGED_END lists the skills synced there, so a commit leaves them out; the rest of
  the file is the repo's own.

Every skill written holds MARKER (``.evo-hub.json``: name, scope, project, version, sha256, tree, synced_at), ``tree``
being the digest of its files as written (``tree_digest``). Sync changes only directories holding a marker of their
place, and never a symbolic link, ``synced/``, ``learned/``, ``.learned/``, ``.system/``, a hidden entry or a directory
without a marker. For each skill of a skills directory:

- missing: installed;
- a marker of an older version: updated;
- the latest version, but files changed since it was written: replaced, after the changed copy is saved;
- a directory without a marker: reported and left alone, unless ``--adopt``, which saves it, then replaces it;
- a marker of a skill the hub no longer lists there: saved, then removed;
- a symbolic link, or anything that is not a directory: reported and left alone.

Saved copies go to ~/.evo/hub/backups/<UTC timestamp>/<global | project/NAME>/<runtime>/<skill>, before anything
replaces them. ``--check`` only reads: it reports what a sync would do and writes nothing, downloads nothing.

A sync downloads every bundle it needs through the presigned GETs the hub hands out after its read check, and checks
each one's size and SHA-256 against what the hub recorded and its contents (``read_bundle``) before writing anything:
one bad bundle stops the sync with nothing written. A skill is then written under a hidden temporary name inside its
skills directory and renamed into place; a directory it replaces is renamed aside first, and renamed back if the
second rename fails. One sync runs at a time per machine, under a lock in ~/.evo/hub. Standard library only.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from evo_agents.harness import MANIFEST
from evo_agents.hub.access import ProjectRules, Refused
from evo_agents.hub.client import LOOPBACK, Hub, HubError, hub_dir, write_atomic
from evo_agents.hub.registry import cluster_of, default_registry, read_registry
from evo_agents.hub.skills import (
    KIND,
    MARKER,
    MAX_BUNDLE,
    BundleError,
    Contents,
    name_problem,
    pack,
    read_bundle,
    tree_digest,
    write_tree,
)

RUNTIMES = ("claude", "agents", "codex", "cursor", "gemini")
PROJECT_RUNTIMES = ("claude", "agents")  # the runtimes reading skills from a repo
RUNTIME_VARIABLES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
KEPT = frozenset({"synced", "learned", ".learned", ".system"})  # other tools' directories among the skills
SCOPES = ("global", "project")
PROJECT = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
COMMIT = re.compile(r"[0-9a-f]{7,64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
MANAGED_BEGIN = "# >>> skills synced by `evo-agents hub skills sync`: kept out of git, do not edit this block"
MANAGED_END = "# <<< evo-agents hub skills"
GITIGNORE = ".gitignore"
TEMP_PREFIX = ".evo-hub-"  # .evo-hub-new-<skill>-<random> while written, .evo-hub-old-<skill>-<random> while replaced
TEMP_ENTRY = re.compile(r"\.evo-hub-(new|old)-.+")
LOCK_FILE = "skills.lock"
LOCK_TIMEOUT = 60.0
TRANSFER_TIMEOUT = 120.0  # seconds for one PUT or GET of a bundle
CHUNK = 1024 * 1024
BACKUPS = "backups"
DIR_MODE = 0o700
FILE_MODE = 0o644

# What sync does to one skill of one skills directory; the first five write.
INSTALL, UPDATE, RESTORE, ADOPT, REMOVE = "install", "update", "restore", "adopt", "remove"
UNMANAGED, KEPT_AS_IS, CURRENT = "unmanaged", "kept", "current"
WRITES = (INSTALL, UPDATE, RESTORE, ADOPT, REMOVE)
DIFFERENCES = (*WRITES, UNMANAGED)
DONE = {INSTALL: "installed", UPDATE: "updated", RESTORE: "restored", ADOPT: "adopted", REMOVE: "removed"}


# Scopes and paths


def parse_scope(text: str, *, any_project: bool = False) -> tuple[str, str | None]:
    """``global`` or ``project:NAME`` (and ``project`` alone with ``any_project``) as (scope, project)."""
    if text == "global":
        return "global", None
    if any_project and text == "project":
        return "project", None
    kind, _, project = text.partition(":")
    if kind == "project" and PROJECT.fullmatch(project):
        return "project", project
    wanted = "global, project or project:NAME" if any_project else "global or project:NAME"
    raise HubError(f"the scope is {wanted} (NAME being a hub project), not {text!r}")


def skill_path(project: str | None, name: str) -> str:
    base = "/v1/skills/global" if project is None else f"/v1/skills/projects/{quote(project, safe='')}"
    return f"{base}/{quote(name, safe='')}"


def runtime_dir(runtime: str, home: Path, env: Mapping[str, str]) -> Path:
    """The directory of ``runtime`` here: its variable when set (CLAUDE_CONFIG_DIR, CODEX_HOME), else ~/.<runtime>."""
    variable = RUNTIME_VARIABLES.get(runtime)
    if variable and env.get(variable):
        return Path(env[variable]).expanduser()
    return home / f".{runtime}"


def parse_runtimes(text: str | None) -> tuple[str, ...]:
    if not text:
        return RUNTIMES
    names = [name.strip() for name in text.split(",") if name.strip()]
    unknown = sorted(set(names) - set(RUNTIMES))
    if unknown or not names:
        raise HubError(f"--runtime takes {', '.join(RUNTIMES)}, separated by commas, not {text!r}")
    return tuple(runtime for runtime in RUNTIMES if runtime in names)


# Transfers to and from the blob store


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _origin(url: str) -> str:
    """Where a presigned URL goes, without the path and query that make it a credential."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}"


def _transfer_url(url) -> str:
    parts = urllib.parse.urlsplit(url if isinstance(url, str) else "")
    if (parts.scheme == "https" and parts.hostname) or (parts.scheme == "http" and parts.hostname in LOOPBACK):
        return url
    raise HubError("the hub handed out a blob store URL that is neither https nor local; nothing was transferred")


def _open(request: urllib.request.Request, doing: str):
    try:
        return _OPENER.open(request, timeout=TRANSFER_TIMEOUT)
    except urllib.error.HTTPError as exc:
        with exc:
            raise HubError(
                f"the blob store at {_origin(request.full_url)} answered HTTP {exc.code} to {doing}"
            ) from None
    except urllib.error.URLError as exc:
        raise HubError(f"cannot reach the blob store at {_origin(request.full_url)}: {exc.reason}") from None
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        reason = f"no answer within {TRANSFER_TIMEOUT:g}s" if isinstance(exc, TimeoutError) else type(exc).__name__
        raise HubError(f"cannot reach the blob store at {_origin(request.full_url)}: {reason}") from None


def put_bundle(url: str, data: bytes) -> None:
    """PUT ``data`` to a presigned upload URL."""
    headers = {"Content-Type": "application/octet-stream"}
    request = urllib.request.Request(_transfer_url(url), data=data, method="PUT", headers=headers)
    with _open(request, "the upload") as response:
        status = response.status
    if not 200 <= status < 300:
        raise HubError(f"the blob store answered HTTP {status} to the upload; nothing was committed")


def get_bundle(url: str, size: int) -> bytes:
    """The bytes at a presigned download URL, reading at most ``size`` + 1 of them."""
    request = urllib.request.Request(_transfer_url(url), method="GET")
    chunks, read = [], 0
    try:
        with _open(request, "the download") as response:
            while read <= size:
                chunk = response.read(min(CHUNK, size + 1 - read))
                if not chunk:
                    break
                chunks.append(chunk)
                read += len(chunk)
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        raise HubError(f"the download from the blob store was cut off ({type(exc).__name__})") from None
    return b"".join(chunks)


# Publishing


def _preflight(hub: Hub, project: str | None) -> None:
    """HubError when the hub would refuse the version, before any byte is sent."""
    if project is None:
        me = hub.call("GET", "/v1/auth/whoami")
        if not isinstance(me, dict) or not me.get("admin"):
            raise HubError(
                "publishing a global skill needs a hub admin; pass --scope project:NAME to publish into a project"
            )
        return
    info = hub.call("GET", f"/v1/projects/{quote(project, safe='')}")
    try:
        rules = ProjectRules(project, info["levels"], info["locations"], info["sinks"], info["default_label"])
        rules.check_push(None, info.get("role"))
    except Refused as exc:
        raise HubError(str(exc).replace("pushing to", "publishing skills of")) from None
    except (KeyError, TypeError):
        raise HubError(f"the hub at {hub.url} did not describe project {project}") from None


def publish(
    hub: Hub,
    directory: str | os.PathLike,
    project: str | None = None,
    source_repo: str | None = None,
    source_commit: str | None = None,
) -> dict:
    """Publish skill directory ``directory`` as a global skill, or one of ``project``; the hub's answer, with what
    was packed under ``bundle``."""
    if (source_repo is None) != (source_commit is None):
        raise HubError("--source-repo and --source-commit go together")
    if source_commit is not None and not COMMIT.fullmatch(source_commit):
        raise HubError(f"--source-commit is a hex commit id of 7 to 64 digits, not {source_commit!r}")
    try:
        bundle = pack(directory)
    except BundleError as exc:
        raise HubError(str(exc)) from None
    _preflight(hub, project)
    holder = {} if project is None else {"project": project}
    item = {"sha256": bundle.sha256, "size": bundle.size, "kind": KIND}
    asked = hub.call("POST", "/v1/blobs/uploads", {**holder, "items": [item]})
    uploads = asked.get("uploads") if isinstance(asked, dict) else None
    if not isinstance(uploads, list) or any(
        not isinstance(u, dict) or u.get("sha256") != bundle.sha256 for u in uploads
    ):
        raise HubError(f"the hub at {hub.url} did not answer the upload request with this bundle")
    for ticket in uploads:  # none when the holder has the bundle already
        put_bundle(ticket.get("url"), bundle.data)
        hub.call("POST", "/v1/blobs/commit", {**holder, "upload_ids": [ticket["upload_id"]]})
    body = {"sha256": bundle.sha256, "size": bundle.size}
    if source_repo is not None:
        body.update(source_repo=source_repo, source_commit=source_commit)
    published = hub.call("POST", f"{skill_path(project, bundle.name)}/versions", body)
    if not isinstance(published, dict):
        raise HubError(f"the hub at {hub.url} did not answer the publication")
    files = len(bundle.contents.files)
    return {**published, "bundle": {"sha256": bundle.sha256, "size": bundle.size, "files": files}}


def list_skills(hub: Hub, scope: str | None = None, project: str | None = None) -> list[dict]:
    params = {key: value for key, value in (("scope", scope), ("project", project)) if value}
    found = hub.call("GET", "/v1/skills" + (f"?{urlencode(params)}" if params else ""))
    if not isinstance(found, list):
        raise HubError(f"the hub at {hub.url} did not answer with a list of skills")
    return found


# Sync


@dataclass(frozen=True)
class Wanted:
    """The latest version of a skill, as the hub lists it."""

    scope: str
    project: str | None
    name: str
    version: int
    sha256: str
    size: int

    @classmethod
    def read(cls, data, hub_url: str) -> Wanted:
        try:
            wanted = cls(data["scope"], data["project"], data["name"], data["version"], data["sha256"], data["size"])
        except (KeyError, TypeError):
            raise HubError(f"the hub at {hub_url} listed a skill this client cannot read") from None
        problem = name_problem(wanted.name)
        if (
            problem
            or wanted.scope not in SCOPES
            or (wanted.scope == "project") != isinstance(wanted.project, str)
            or not isinstance(wanted.sha256, str)
            or not SHA256.fullmatch(wanted.sha256)
            or type(wanted.size) is not int
            or not 0 < wanted.size <= MAX_BUNDLE
            or type(wanted.version) is not int
        ):
            raise HubError(f"the hub at {hub_url} listed skill {wanted.name!r} in a form this client refuses")
        return wanted


@dataclass(frozen=True)
class Target:
    """A skills directory sync writes into, and the place whose skills go there."""

    runtime: str
    directory: Path
    scope: str
    project: str | None

    @property
    def place(self) -> str:
        return "global" if self.project is None else f"project/{self.project}"

    def owns(self, marker: Mapping) -> bool:
        return marker.get("scope") == self.scope and marker.get("project") == self.project


@dataclass
class Action:
    target: Target
    name: str
    action: str
    wanted: Wanted | None = None
    detail: str = ""
    save: bool = False  # a copy goes to the backups before the directory is replaced or removed

    def as_json(self) -> dict:
        return {
            "name": self.name,
            "action": self.action,
            "version": self.wanted.version if self.wanted else None,
            "detail": self.detail,
        }


@dataclass
class Report:
    hub: str
    check: bool
    targets: list[dict] = field(default_factory=list)  # {runtime, scope, project, directory, actions, gitignore}
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    backup: str | None = None

    @property
    def differences(self) -> int:
        count = 0
        for target in self.targets:
            count += sum(1 for action in target["actions"] if action["action"] in DIFFERENCES)
            count += target["gitignore"] in ("changed", "would change")
        return count

    def counts(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for target in self.targets:
            for action in target["actions"]:
                found[action["action"]] = found.get(action["action"], 0) + 1
        return found

    def as_json(self) -> dict:
        return {
            "hub": self.hub,
            "check": self.check,
            "differences": self.differences,
            "counts": self.counts(),
            "targets": self.targets,
            "notes": self.notes,
            "errors": self.errors,
            "backup": self.backup,
        }


def read_marker(directory: Path) -> dict | None:
    """The marker of a skill directory written by sync; None when there is none or it cannot be read."""
    try:
        fd = os.open(directory / MARKER, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            data = json.loads(handle.read(64 * 1024))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("name"), str)
        or data.get("scope") not in SCOPES
        or not isinstance(data.get("sha256"), str)
    ):
        return None
    return data


def gitignore_text(existing: str | None, names: list[str]) -> str | None:
    """``existing`` with the managed block listing ``names``, the rest as it was; None when there is nothing to
    write (no names and no block to remove)."""
    text = existing or ""
    start = text.find(MANAGED_BEGIN)
    end = text.find(MANAGED_END, start) if start >= 0 else -1
    if start >= 0 and end >= 0:
        newline = text.find("\n", end)
        before, after = text[:start], text[len(text) if newline < 0 else newline + 1 :]
    elif not names:
        return None
    else:
        before, after = text + ("\n" if text and not text.endswith("\n") else ""), ""
    block = ""
    if names:
        lines = [MANAGED_BEGIN, *(f"/{name}/" for name in sorted(names, key=str.lower)), MANAGED_END]
        block = "\n".join(lines) + "\n"
    return before + block + after


def _owns_cluster(cluster: Mapping, project: Mapping, hub_url: str) -> bool:
    hub = cluster.get("hub")
    if isinstance(hub, dict) and hub.get("project") == project["name"] and hub.get("url") == hub_url:
        return True
    return cluster.get("name") == project["harness"]["name"]


@contextlib.contextmanager
def sync_lock(directory: Path, timeout: float = LOCK_TIMEOUT):
    """Hold the skills sync lock of this machine; HubError after ``timeout`` seconds of waiting."""
    try:
        import fcntl
    except ImportError:  # no flock here: syncs are not serialized
        yield
        return
    directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    fd = os.open(directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise HubError(
                        f"another `evo-agents hub skills sync` on this machine held {directory / LOCK_FILE} for "
                        f"{timeout:g}s; try again when it is done"
                    ) from None
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


def _now() -> datetime:
    return datetime.now(UTC)


class SkillSync:
    """One ``hub skills sync`` run against ``hub``."""

    def __init__(
        self,
        hub: Hub,
        *,
        runtimes: tuple[str, ...] = RUNTIMES,
        check: bool = False,
        adopt: bool = False,
        home: Path | None = None,
        env: Mapping[str, str] | None = None,
        registry: Path | None = None,
    ):
        self.hub = hub
        self.runtimes = runtimes
        self.check = check
        self.adopt = adopt
        self.home = home or Path.home()
        self.env = os.environ if env is None else env
        self.registry = (registry or default_registry()).expanduser()
        self.report = Report(hub.url, check)
        self._bundles: dict[str, Contents] = {}
        self._backup: Path | None = None
        self._started = _now()

    # Entry point

    def run(self) -> Report:
        projects = self.hub.call("GET", "/v1/projects")
        if not isinstance(projects, list):
            raise HubError(f"the hub at {self.hub.url} did not answer with a list of projects")
        wanted = [Wanted.read(item, self.hub.url) for item in list_skills(self.hub)]
        targets = self._targets(projects, wanted)
        plans = [(target, self._plan(target, wanted)) for target in targets]
        if self.check:
            for target, actions in plans:
                self._record(target, actions, self._gitignore(target, actions, write=False))
            return self.report
        self._fetch([action for _, actions in plans for action in actions])  # every check before any write
        with sync_lock(hub_dir()):
            plans = [(target, self._plan(target, wanted)) for target in targets]  # as it is now, under the lock
            self._fetch([action for _, actions in plans for action in actions])
            for target, actions in plans:
                if any(action.action in WRITES for action in actions):
                    try:
                        target.directory.mkdir(parents=True, exist_ok=True)
                        self._clean(target.directory)
                    except OSError as exc:
                        self.report.errors.append(f"{target.directory}: cannot write there ({exc.strerror or exc})")
                        continue
                done = [action for action in map(self._apply, actions) if action is not None]
                self._record(target, done, self._gitignore(target, done, write=True))
        if self._backup is not None:
            self.report.backup = str(self._backup)
        return self.report

    # Where skills go

    def _targets(self, projects: list, wanted: list[Wanted]) -> list[Target]:
        targets: list[Target] = []
        seen: dict[str, Path] = {}

        def add(target: Target) -> None:
            """``target``, unless a directory added before is the same one (a link between runtimes)."""
            real = os.path.realpath(target.directory)
            if real in seen:
                self.report.notes.append(f"{target.directory} is {seen[real]}, synced once")
                return
            seen[real] = target.directory
            targets.append(target)

        for runtime in self.runtimes:
            base = runtime_dir(runtime, self.home, self.env)
            if base.is_dir():
                add(Target(runtime, base / "skills", "global", None))
        registry, _ = read_registry(self.registry)
        clusters = [c for c in registry.get("clusters") or [] if isinstance(c, dict)]
        with_skills = {w.project for w in wanted if w.project}
        for project in sorted((p for p in projects if isinstance(p, dict)), key=lambda p: str(p.get("name"))):
            root = self._harness_root(project, clusters)
            if root is None:
                if project.get("name") in with_skills:
                    self.report.notes.append(
                        f"project {project['name']} has skills on the hub but its harness is not on this machine; run "
                        "`evo-agents hub registry pull`, then sync again"
                    )
                continue
            for runtime in self.runtimes:
                if runtime in PROJECT_RUNTIMES:
                    add(Target(runtime, root / f".{runtime}" / "skills", "project", project["name"]))
        return targets

    def _harness_root(self, project: Mapping, clusters: list[dict]) -> Path | None:
        """Where the harness of ``project`` is on this machine; None when it is not here."""
        if not isinstance(project.get("harness"), dict) or not isinstance(project.get("name"), str):
            return None
        held = next((c for c in clusters if _owns_cluster(c, project, self.hub.url)), None)
        candidates = []
        if held is not None and isinstance(held.get("root"), str):
            candidates.append(Path(held["root"]).expanduser())
        candidates.append(Path(cluster_of(project, self.hub.url, None)["root"]))
        return next((root for root in candidates if (root / MANIFEST).is_file()), None)

    # What to do

    def _plan(self, target: Target, wanted: list[Wanted]) -> list[Action]:
        here = [w for w in wanted if w.scope == target.scope and w.project == target.project]
        names = {w.name.lower() for w in here}
        actions = [self._decide(target, w) for w in sorted(here, key=lambda w: w.name.lower())]
        try:
            entries = sorted(os.scandir(target.directory), key=lambda entry: entry.name)
        except (FileNotFoundError, NotADirectoryError):
            entries = []
        for entry in entries:
            if (
                entry.name.lower() in names
                or entry.name.startswith(".")
                or entry.name in KEPT
                or entry.is_symlink()
                or not entry.is_dir(follow_symlinks=False)
            ):
                continue
            marker = read_marker(Path(entry.path))
            if marker is not None and target.owns(marker) and marker["name"].lower() == entry.name.lower():
                actions.append(
                    Action(target, entry.name, REMOVE, None, "no longer on the hub for this place; saved first", True)
                )
        return actions

    def _decide(self, target: Target, wanted: Wanted) -> Action:
        path = target.directory / wanted.name
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return Action(target, wanted.name, INSTALL, wanted, f"version {wanted.version}")
        if stat.S_ISLNK(mode):
            return Action(target, wanted.name, KEPT_AS_IS, wanted, "a symbolic link; left as it is")
        if not stat.S_ISDIR(mode):
            return Action(target, wanted.name, KEPT_AS_IS, wanted, "not a directory; left as it is")
        marker = read_marker(path)
        if marker is None or marker["name"].lower() != wanted.name.lower():
            if self.adopt:
                return Action(target, wanted.name, ADOPT, wanted, "not synced from the hub; saved, then replaced", True)
            return Action(target, wanted.name, UNMANAGED, wanted, "not synced from the hub; pass --adopt to replace it")
        if not target.owns(marker):
            return Action(target, wanted.name, KEPT_AS_IS, wanted, "synced for another place; left as it is")
        try:
            changed = tree_digest(path) != marker.get("tree")
        except OSError:
            changed = True
        held = marker.get("version")
        if marker["sha256"] != wanted.sha256:
            detail = f"version {held} to {wanted.version}" + ("; changed here, saved first" if changed else "")
            return Action(target, wanted.name, UPDATE, wanted, detail, changed)
        if changed:
            return Action(
                target, wanted.name, RESTORE, wanted, "changed here since the sync; saved, then restored", True
            )
        return Action(target, wanted.name, CURRENT, wanted, f"version {wanted.version}")

    # Downloads, all checked before anything is written

    def _fetch(self, actions: list[Action]) -> None:
        for action in actions:
            wanted = action.wanted
            if action.action not in WRITES or wanted is None or wanted.sha256 in self._bundles:
                continue
            ticket = self.hub.call("GET", f"{skill_path(wanted.project, wanted.name)}/bundle?version={wanted.version}")
            if (
                not isinstance(ticket, dict)
                or ticket.get("sha256") != wanted.sha256
                or ticket.get("size") != wanted.size
            ):
                raise HubError(
                    f"the hub at {self.hub.url} handed out another bundle for {wanted.name}; nothing was written"
                )
            data = get_bundle(ticket.get("url"), wanted.size)
            digest = hashlib.sha256(data).hexdigest()
            if len(data) != wanted.size or digest != wanted.sha256:
                raise HubError(
                    f"the bundle of {wanted.name} version {wanted.version} downloaded from the blob store has "
                    f"{len(data)} bytes and SHA-256 {digest}, not the {wanted.size} bytes and {wanted.sha256} the hub "
                    "recorded; the sync stopped and nothing was written"
                )
            try:
                self._bundles[wanted.sha256] = read_bundle(data, wanted.name)
            except BundleError as exc:
                raise HubError(
                    f"the bundle of {wanted.name} version {wanted.version} is not one this client writes ({exc}); "
                    "the sync stopped and nothing was written"
                ) from None

    # Writing

    def _backup_root(self) -> Path:
        if self._backup is None:
            base = hub_dir() / BACKUPS
            base.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            stamp = self._started.strftime("%Y%m%dT%H%M%SZ")
            candidate, number = base / stamp, 1
            while True:
                try:
                    candidate.mkdir(mode=DIR_MODE)
                    break
                except FileExistsError:  # two syncs within a second
                    number += 1
                    candidate = base / f"{stamp}-{number}"
            self._backup = candidate
        return self._backup

    def _save(self, action: Action) -> None:
        """Copy the directory ``action`` replaces or removes into the backups, links kept as links."""
        source = action.target.directory / action.name
        copy = self._backup_root() / action.target.place / action.target.runtime / action.name
        copy.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        shutil.copytree(source, copy, symlinks=True)

    def _clean(self, directory: Path) -> None:
        """Remove what an interrupted sync left: its own hidden temporary directories."""
        for entry in os.scandir(directory):
            if TEMP_ENTRY.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                shutil.rmtree(entry.path, ignore_errors=True)

    def _marker(self, action: Action, contents: Contents) -> bytes:
        wanted = action.wanted
        data = {
            "name": wanted.name,
            "scope": wanted.scope,
            "project": wanted.project,
            "version": wanted.version,
            "sha256": wanted.sha256,
            "tree": contents.tree,
            "synced_at": _now().isoformat(timespec="seconds"),
        }
        return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()

    def _apply(self, action: Action) -> Action | None:
        """Carry out ``action``; the action as done, or None when it failed (the error is reported)."""
        if action.action not in WRITES:
            return action
        directory = action.target.directory
        final = directory / action.name
        try:
            if action.save:
                self._save(action)
            if action.action == REMOVE:
                aside = directory / f"{TEMP_PREFIX}old-{action.name}-{secrets.token_hex(4)}"
                os.rename(final, aside)
                shutil.rmtree(aside, ignore_errors=True)  # what is left is hidden, and removed by the next sync
                return action
            contents = self._bundles[action.wanted.sha256]
            staging = Path(tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}new-{action.name}-", dir=directory))
            try:
                write_tree(contents, staging)
                write_atomic(staging / MARKER, self._marker(action, contents), FILE_MODE)
                if action.action == INSTALL:
                    if os.path.lexists(final):
                        raise HubError(f"{final} appeared while the sync ran; left as it is")
                    os.rename(staging, final)
                else:
                    self._swap(final, staging)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        except (OSError, HubError, BundleError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            self.report.errors.append(f"{final}: not {DONE[action.action]} ({reason})")
            return None
        return action

    def _swap(self, final: Path, staging: Path) -> None:
        """Put ``staging`` where ``final`` is: ``final`` is renamed aside first, and back when the second rename
        fails, then deleted (a copy that mattered is in the backups already)."""
        aside = final.parent / f"{TEMP_PREFIX}old-{final.name}-{secrets.token_hex(4)}"
        os.rename(final, aside)
        try:
            os.rename(staging, final)
        except BaseException:
            os.rename(aside, final)
            raise
        shutil.rmtree(aside, ignore_errors=True)

    def _gitignore(self, target: Target, actions: list[Action], *, write: bool) -> str:
        """Bring the managed block of a project's skills directory in line with the skills synced there."""
        if target.scope != "project":
            return "none"
        synced = [a.name for a in actions if a.action in (INSTALL, UPDATE, RESTORE, ADOPT, CURRENT)]
        path = target.directory / GITIGNORE
        try:
            existing = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = None
        except (OSError, UnicodeDecodeError) as exc:
            self.report.errors.append(f"{path}: cannot read it ({exc})")
            return "unreadable"
        text = gitignore_text(existing, synced)
        if text is None or text == existing:
            return "unchanged"
        if not write:
            return "would change"
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode) if existing is not None else FILE_MODE
            write_atomic(path, text.encode("utf-8"), mode)
        except OSError as exc:
            self.report.errors.append(f"{path}: not written ({exc.strerror or exc})")
            return "unwritten"
        return "changed"

    def _record(self, target: Target, actions: list[Action], gitignore: str) -> None:
        self.report.targets.append(
            {
                "runtime": target.runtime,
                "scope": target.scope,
                "project": target.project,
                "directory": str(target.directory),
                "actions": [action.as_json() for action in actions],
                "gitignore": gitignore,
            }
        )


def sync(hub: Hub, **options) -> Report:
    """Run ``hub skills sync`` with ``options`` (see SkillSync)."""
    return SkillSync(hub, **options).run()
