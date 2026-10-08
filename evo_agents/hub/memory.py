"""``evo-agents hub memory push|pull|search``: Claude Code's memory files, kept on the hub.

Claude Code keeps the auto-memory of a working directory in ``~/.claude/projects/<slug>/memory/`` (under
``$CLAUDE_CONFIG_DIR`` rather than ``~/.claude`` when that is set): one markdown file per memory whose frontmatter
holds ``name``, ``description`` and ``metadata.type``, and the index ``MEMORY.md``. The slug is the absolute path of
the working directory with every character outside [A-Za-z0-9] turned into ``-``.

Where a directory's memories live on the hub, as (scope, project, location):

- a slug equal to the slug of the harness root or of a repo of a hub project the person sees, placed on this machine
  the way ``hub registry pull`` places it, belongs to that project, at location ``harness`` or the repo's name. A
  directory several projects claim goes to the strongest claim: the harness root of a project, then the binding of
  ``evo-agents kg bind`` among the projects listing it as a repo, then the one project listing it; anything else is
  refused and never guessed (``Places``);
- any other slug is personal. Its location is the slug without the home directory's slug and the ``-`` after it
  (``github-blog`` for ``~/github/blog``); ``~`` stands for the home directory itself, ``~`` and the rest of the slug
  for a path whose first part under the home directory starts with a character the rule turns into ``-``
  (``~--claude`` for ``~/.claude``), and a slug outside the home directory is kept whole. Another machine puts a
  personal memory under its own home directory.

A file goes to the hub whole, so name, description and metadata.type come back as they were; its type
(metadata.type, else type, else ``user``, the private one) decides who sees it. MEMORY.md is never pushed: a pull
appends the pointer lines it lacks for the files it brings and removes none. Conflict copies, hidden files and
anything that is not a regular file (a symlink among them) are never pushed either.

~/.evo/hub/memory-state.json holds what the last sync left, per hub and per file (``<slug>/<name>``): the memory id,
its revision and the sha256 of the file as synced. A file whose bytes match it is not sent again, and a sync that
finds nothing changed writes nothing. A push names the revision it last saw. When the hub holds another one, or a
pull finds a file changed both here and on the hub, the hub's version stays under the file's name, this machine's is
written next to it as ``<name>.conflict-<host>.md``, and the conflict is reported. Deletions cross only with
``--prune``: ``push --prune`` turns the files deleted here into tombstones on the hub, and ``pull --prune`` deletes
the files the hub holds a tombstone for when they did not change here since the last sync. Without it a deletion is
reported and nothing else happens; a pull brings back a file deleted only here.

A directory never moves a memory out of a project into the personal scope, and a personal memory is never pulled into
a directory that belongs to a project here: either would move data across the line the hub's rules draw. Every file
is written atomically, and one sync runs at a time per machine, under a lock next to the state. Standard library and
PyYAML only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode

import yaml

from evo_agents.hub.client import Hub, HubError, host_name, hub_dir, write_atomic
from evo_agents.hub.registry import cluster_of, default_registry, read_registry
from evo_agents.kg.project import binding_of, bound_project, read_bindings

TYPES = ("user", "feedback", "project", "reference")
SHARED_TYPES = frozenset({"project", "reference"})  # in a project, its members see these; their owner the others
PRIVATE_TYPE = "user"  # what a file without a readable type counts as: only its owner sees it
SCOPES = ("project", "personal")
HARNESS = "harness"  # the location of a project's harness root
INDEX = "MEMORY.md"
AGENT_SINK = "claude-code@anthropic"  # what reads the files a pull writes: Claude Code sessions
MAX_BODY = 256 * 1024  # bytes of UTF-8 in one memory file
MAX_NAME = 255  # bytes in a file name, as file systems allow
STATE_FILE = "memory-state.json"
LOCK_FILE = "memory.lock"
LOCK_TIMEOUT = 60.0  # seconds to wait for another sync on this machine
PAGE = 200  # memories per listing request
MAX_PAGES = 10_000
FILE_MODE = 0o600
DIR_MODE = 0o700
STATE_VERSION = 1

SLUG = re.compile(r"[A-Za-z0-9-]{1,255}")
PERSONAL_LOCATION = re.compile(r"[~A-Za-z0-9-][A-Za-z0-9-]{0,254}")
CONFLICT_COPY = re.compile(r"\.conflict-[A-Za-z0-9-]+\.md\Z")
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# The slug rule and the places of memory directories


def slug(path: str | os.PathLike) -> str:
    """Claude Code's directory name for ``path``: every character outside [A-Za-z0-9] becomes ``-``, as many as
    JavaScript counts UTF-16 units in it."""
    return "".join(ch if ch.isascii() and ch.isalnum() else "-" * (2 if ord(ch) > 0xFFFF else 1) for ch in str(path))


def claude_projects(env: Mapping[str, str] | None = None) -> Path:
    """Where Claude Code keeps one directory per working directory: $CLAUDE_CONFIG_DIR/projects, else
    ~/.claude/projects."""
    env = os.environ if env is None else env
    config = env.get("CLAUDE_CONFIG_DIR")
    return (Path(config).expanduser() if config else Path.home() / ".claude") / "projects"


def personal_location(name: str, home: Path) -> str:
    """The location of the personal memories of directory ``name`` (a slug)."""
    base = slug(home)
    if name == base:
        return "~"
    if name.startswith(base + "-"):
        rest = name[len(base) + 1 :]
        return rest if rest[:1].isalnum() else "~" + name[len(base) :]
    return name


def personal_slug(location: str, home: Path) -> str | None:
    """The directory of personal memories at ``location`` under ``home``; None for a location no slug gives."""
    if not isinstance(location, str) or not PERSONAL_LOCATION.fullmatch(location):
        return None
    base = slug(home)
    if location.startswith("~"):
        name = base + location[1:]
    elif location.startswith("-"):
        name = location
    else:
        name = f"{base}-{location}"
    return name if SLUG.fullmatch(name) else None


@dataclass(frozen=True)
class Place:
    """Where a memory directory goes on the hub."""

    scope: str  # project or personal
    project: str | None
    location: str

    def __str__(self) -> str:
        return f"project {self.project}, {self.location}" if self.scope == "project" else f"personal, {self.location}"

    def filters(self) -> dict:
        found = {"scope": self.scope, "location": self.location}
        return {**found, "project": self.project} if self.project else found


def _place(entry: Mapping) -> Place:
    return Place(entry["scope"], entry.get("project"), entry["location"])


def _shared(scope: str, kind: str) -> bool:
    return scope == "project" and kind in SHARED_TYPES


def _owns_cluster(cluster: Mapping, project: Mapping, hub_url: str) -> bool:
    hub = cluster.get("hub")
    if isinstance(hub, dict) and hub.get("project") == project["name"] and hub.get("url") == hub_url:
        return True
    return cluster.get("name") == project["harness"]["name"]


def _listed(names) -> str:
    return ", ".join(sorted(names))


class Places:
    """The memory directories of the hub projects a person sees, at the paths ``hub registry pull`` gives their
    harness and repos on this machine. Any other directory is personal.

    A directory several projects claim belongs to the one whose claim is strongest, by this precedence:

    1. the harness root of a project. Two projects whose harness root is one directory here are refused, whatever
       else claims it: one of the two registrations is a mistake, and guessing would put memories in the wrong one.
    2. ``evo-agents kg bind``: the project bound.json binds the directory to, by its own binding or that of its
       nearest bound ancestor, as the knowledge graph reads it. It chooses among the projects that list the
       directory as a repo; a binding to any other project refuses the directory, since that project has no place
       for it on the hub.
    3. a repo listing, when one project alone lists the directory.

    Anything else, such as one repo of two projects without a binding, is refused, and the message names the
    binding that settles it. So the memories of a directory go to another project only when a stronger claim says
    so, never because of how projects happen to list their repos; and a memory synced with one project is never
    moved to another by a push (``MemorySync._push_file``). A binding never makes a directory a project's that no
    project claims: it has no location there, so it stays personal."""

    def __init__(self, home: Path, bindings: Mapping | None = None):
        self.home = home
        self.bindings = dict(bindings or {})  # bound.json, as ``evo_agents.kg.project.read_bindings`` reads it
        self._claims: dict[str, set[Place]] = {}
        self._roots: dict[str, set[str]] = {}  # per directory, the projects whose harness root it is
        self._paths: dict[str, set[str]] = {}  # per directory, the paths that claimed it
        self._slugs: dict[Place, str] = {}
        self._decided: dict[str, str | None | HubError] = {}  # what project_of found, per directory

    def add(self, place: Place, path: str, *, root: bool = False) -> None:
        """``path`` holds ``place`` here, as the harness root of its project when ``root``; the first path added for
        a place is where its memories are pulled to."""
        self._decided.clear()
        self._slugs.setdefault(place, slug(path))
        for name in {slug(path), slug(os.path.realpath(path))}:
            self._claims.setdefault(name, set()).add(place)
            self._paths.setdefault(name, set()).add(os.path.normpath(path))
            if root:
                self._roots.setdefault(name, set()).add(place.project)

    @classmethod
    def from_hub(
        cls,
        projects: list,
        registry: Mapping,
        hub_url: str,
        home: Path | None = None,
        bindings: Mapping | None = None,
    ) -> Places:
        """``projects`` as GET /v1/projects lists them; the workspace of each is the one its cluster in ``registry``
        names, or the one it was registered with. ``bindings`` is bound.json of ``evo-agents kg bind``."""
        places = cls(home or Path.home(), bindings)
        clusters = [c for c in registry.get("clusters") or [] if isinstance(c, dict)]
        for project in projects:
            if not isinstance(project, dict) or not isinstance(project.get("harness"), dict):
                continue  # registered without its harness paths: no directory of this machine is its own
            held = next((c for c in clusters if _owns_cluster(c, project, hub_url)), None)
            workspace = held.get("workspace") if held else None
            base = Path(workspace).expanduser() if isinstance(workspace, str) and workspace else None
            cluster = cluster_of(project, hub_url, base)
            harness = Place("project", project["name"], HARNESS)
            places.add(harness, cluster["root"], root=True)
            if held and isinstance(held.get("root"), str):
                places.add(harness, held["root"], root=True)
            for repo, path in zip(project.get("repos") or [], cluster["repos"], strict=True):
                places.add(Place("project", project["name"], repo["name"]), path)
        return places

    def _shown(self, name: str) -> str:
        paths = sorted(self._paths.get(name, ()))
        return " and ".join(paths) if paths else name

    def _bound(self, name: str) -> tuple[set, list[str]]:
        """The projects the bindings of directory ``name``'s paths name (None for a binding that names none), and
        the bound directories they come from."""
        projects, where = set(), []
        for path in sorted(self._paths.get(name, ())):
            found = binding_of(path, self.bindings)
            if found is not None:
                where.append(found[0])
                projects.add(bound_project(found[1]))
        return projects, where

    def project_of(self, name: str) -> str | None:
        """The project directory ``name`` (a slug) belongs to here, by the precedence of the class: None when no
        project claims it, so it is personal; HubError naming the way out when the claims do not settle it. Every
        caller (push, pull, the hooks) decides through here."""
        if name not in self._decided:
            try:
                self._decided[name] = self._decide(name)
            except HubError as exc:
                self._decided[name] = exc
        found = self._decided[name]
        if isinstance(found, HubError):
            raise HubError(str(found))
        return found

    def _decide(self, name: str) -> str | None:
        claims = self._claims.get(name)
        if not claims:
            return None
        shown = self._shown(name)
        roots = self._roots.get(name, set())
        if len(roots) > 1:
            raise HubError(
                f"{shown}: the harness root of projects {_listed(roots)} at once on this machine, so its memories are "
                "not synced; a harness is the root of one hub project, so one of these registrations has to go"
            )
        if roots:
            return next(iter(roots))  # the harness root wins over a binding and over repo listings
        listing = {place.project for place in claims}
        fix = f"`evo-agents kg bind --project <name> {sorted(self._paths.get(name, {name}))[0]}`"
        bound, where = self._bound(name)
        if bound:
            if len(bound) == 1 and next(iter(bound)) in listing:
                return next(iter(bound))
            named = _listed(p or "no project" for p in bound)
            raise HubError(
                f"{shown}: bound to {named} (`evo-agents kg bind`, at {', '.join(where)}), while the hub projects "
                f"listing it as a repo here are {_listed(listing)}; its memories are not synced. Choose one of those "
                f"with {fix}"
            )
        if len(listing) == 1:
            return next(iter(listing))
        raise HubError(
            f"{shown}: a repo of projects {_listed(listing)} at once on this machine and the harness root of none, so "
            f"its memories are not synced; choose its project with {fix}"
        )

    def candidates(self, name: str) -> list[Place]:
        """The places directory ``name`` may sync with, the preferred first: its project's harness before its repos,
        else the personal place. HubError when ``project_of`` cannot tell its project."""
        project = self.project_of(name)
        if project is None:
            return [Place("personal", None, personal_location(name, self.home))]
        claims = (place for place in self._claims[name] if place.project == project)
        return sorted(claims, key=lambda place: (place.location != HARNESS, place.location))

    def directory(self, place: Place) -> str | None:
        """The directory (slug) the memories of ``place`` are pulled into here; None when there is none: a project
        location no harness or repo of this machine holds, a personal location that is not a slug, a directory that
        belongs to another project here, or one whose project cannot be told, or for a personal memory a directory
        any project claims here."""
        if place.scope == "personal":
            name = personal_slug(place.location, self.home)
            return None if name is None or name in self._claims else name
        name = self._slugs.get(place)
        if name is None or not SLUG.fullmatch(name):
            return None
        try:
            return name if self.project_of(name) == place.project else None
        except HubError:
            return None


# Files


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


def _points_at(index: str, name: str) -> bool:
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
    missing = [name for name in sorted(files) if not _points_at(held, name)]
    if not missing:
        return []
    data = raw or b""
    if data and not data.endswith(b"\n"):
        data += b"\n"
    data += "".join(pointer_line(name, files[name]) + "\n" for name in missing).encode("utf-8")
    write_atomic(index, data, _mode(index))
    return missing


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mode(path: Path) -> int:
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
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
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


# State and lock


class State:
    """memory-state.json: per hub and per file (``<slug>/<name>``), what the last sync left there."""

    def __init__(self, path: Path, hub_url: str):
        self.path = path
        self.dirty = False
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raw = None
        except OSError as exc:
            raise HubError(f"cannot read {path}: {exc.strerror or exc}") from None
        data = {"version": STATE_VERSION, "hubs": {}}
        if raw is not None:
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if (
                not isinstance(data, dict)
                or data.get("version") != STATE_VERSION
                or not isinstance(data.get("hubs"), dict)
            ):
                raise HubError(
                    f"{path} is not a memory state this client reads. Move it away to start over: the next sync then "
                    "keeps both versions of every file that differs from the hub, as conflict copies"
                )
        self.data = data
        held = data["hubs"].setdefault(hub_url, {})
        self.files: dict[str, dict] = held.setdefault("files", {}) if isinstance(held, dict) else {}

    def get(self, key: str) -> dict | None:
        entry = self.files.get(key)
        return entry if isinstance(entry, dict) and isinstance(entry.get("id"), int) else None

    def set(self, key: str, memory: Mapping) -> None:
        entry = {
            "id": memory["id"],
            "revision": memory["revision"],
            "sha256": None if memory["deleted"] else _sha(memory["body"].encode("utf-8")),
            "scope": memory["scope"],
            "project": memory.get("project"),
            "location": memory["location"],
            "type": memory["type"],
        }
        if self.files.get(key) != entry:
            self.files[key] = entry
            self.dirty = True

    def drop(self, key: str) -> None:
        if self.files.pop(key, None) is not None:
            self.dirty = True

    def save(self) -> None:
        if self.dirty:
            write_atomic(self.path, (json.dumps(self.data, indent=2, sort_keys=True) + "\n").encode(), FILE_MODE)
            self.dirty = False


@contextlib.contextmanager
def sync_lock(directory: Path, timeout: float = LOCK_TIMEOUT):
    """Hold the memory sync lock of this machine; HubError after ``timeout`` seconds of waiting."""
    try:
        import fcntl
    except ImportError:  # no flock here: syncs are not serialized
        yield
        return
    directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    fd = os.open(directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise HubError(
                        f"another `evo-agents hub memory` run on this machine held {directory / LOCK_FILE} for "
                        f"{timeout:g}s; try again when it is done"
                    ) from None
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)  # releases the lock


# Sync


@dataclass
class Report:
    command: str
    hub: str
    dry_run: bool = False
    counts: Counter = field(default_factory=Counter)
    places: Counter = field(default_factory=Counter)  # memory files per place: "project <name>" or "personal"
    conflicts: list[dict] = field(default_factory=list)  # {file, copy, hub_revision, deleted_on_hub}
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "command": self.command,
            "hub": self.hub,
            "dry_run": self.dry_run,
            "counts": dict(sorted(self.counts.items())),
            "places": dict(sorted(self.places.items())),
            "conflicts": self.conflicts,
            "errors": self.errors,
            "notes": self.notes,
        }


def _well_formed(memory) -> bool:
    return (
        isinstance(memory, dict)
        and type(memory.get("id")) is int
        and type(memory.get("revision")) is int
        and memory.get("scope") in SCOPES
        and isinstance(memory.get("location"), str)
        and name_problem(memory.get("name")) is None
        and memory.get("type") in TYPES
        and isinstance(memory.get("body"), str)
        and isinstance(memory.get("deleted"), bool)
        and isinstance(memory.get("owner"), str)
        and (memory["scope"] == "personal" or isinstance(memory.get("project"), str))
    )


class MemorySync:
    """One push or pull of the memories of this machine with one hub, as ``login``."""

    def __init__(
        self,
        hub: Hub,
        login: str,
        *,
        sink: str = AGENT_SINK,
        dry_run: bool = False,
        prune: bool = False,
        base: Path | None = None,
        home: Path | None = None,
        registry: Path | None = None,
        host: str | None = None,
        lock_timeout: float = LOCK_TIMEOUT,
    ):
        self.hub = hub
        self.login = login
        self.sink = sink
        self.dry_run = dry_run
        self.prune = prune
        self.base = base or claude_projects()
        self.home = home or Path.home()
        self.registry = registry or default_registry()
        self.host = host_tag(host)
        self.lock_timeout = lock_timeout  # seconds to wait for another sync of this machine
        self.report = Report("", hub.url, dry_run)
        self.state: State
        self.places: Places

    # Entry points

    def push(self, target: Path | None = None, *, everything: bool = False) -> Report:
        """Send the memory files of ``target``'s directory (default: the current directory), or of every directory
        with ``everything``, to the hub."""
        return self._run("push", target, everything)

    def pull(self, target: Path | None = None, *, everything: bool = False) -> Report:
        """Bring the hub's memories of ``target``'s directory, or every memory one sees with ``everything``."""
        return self._run("pull", target, everything)

    def _run(self, command: str, target: Path | None, everything: bool) -> Report:
        self.report = Report(command, self.hub.url, self.dry_run)
        state_dir = hub_dir()
        lock = contextlib.nullcontext() if self.dry_run else sync_lock(state_dir, self.lock_timeout)
        with lock:
            self.state = State(state_dir / STATE_FILE, self.hub.url)
            self.places = self._load_places()
            try:
                if command == "push":
                    self._push(target, everything)
                else:
                    self._pull(target, everything)
            finally:
                if not self.dry_run:  # what was done is kept, even when a later request failed
                    self.state.save()
        return self.report

    def pending(self, target: Path | None = None) -> bool:
        """Whether ``push(target)`` has a file to send, judged on this machine alone: a memory file of the directory
        whose bytes differ from what the last sync with this hub left, or that no sync left. Sends no request and
        takes no lock. A deletion does not count, since only ``--prune`` sends one; a file the push would refuse to
        read (a symlink, one too large) does not either."""
        name = self.target(target)
        directory = self._memory_dir(name)
        if not directory.is_dir():
            return False
        state = State(hub_dir() / STATE_FILE, self.hub.url)
        self.report = Report("push", self.hub.url, dry_run=True)
        files, _ = self._files(directory)
        for file_name, data in files.items():
            entry = state.get(f"{name}/{file_name}")
            if entry is None or entry.get("sha256") != _sha(data):
                return True
        return False

    def project_of(self, target: Path | None = None) -> str | None:
        """The hub project directory ``target`` (default: the current one) belongs to here, as push and pull decide it
        (``Places``); None for a personal directory. Asks the hub for the projects the person sees; HubError when the
        claims of several projects do not settle it."""
        return self._load_places().project_of(self.target(target))

    def _load_places(self) -> Places:
        projects = self.hub.call("GET", "/v1/projects")
        if not isinstance(projects, list):
            raise HubError(f"the hub at {self.hub.url} did not answer with a list of projects")
        registry, _ = read_registry(self.registry.expanduser())
        return Places.from_hub(projects, registry, self.hub.url, self.home, read_bindings())

    def target(self, target: Path | None) -> str:
        """The directory (slug) of ``target``: a working directory, or a directory under the Claude Code projects
        directory (``<slug>`` or ``<slug>/memory``)."""
        given = Path(os.path.abspath((target or Path.cwd()).expanduser()))
        real = Path(os.path.realpath(given))
        for path, base in ((given, Path(os.path.abspath(self.base))), (real, Path(os.path.realpath(self.base)))):
            if path != base and path.is_relative_to(base):
                name = path.relative_to(base).parts[0]
                if SLUG.fullmatch(name):
                    return name
        name, resolved = slug(given), slug(real)
        if resolved != name and not self._memory_dir(name).is_dir() and self._memory_dir(resolved).is_dir():
            return resolved
        return name

    def _memory_dir(self, name: str) -> Path:
        return self.base / name / "memory"

    def _local_dirs(self) -> list[str]:
        try:
            entries = list(os.scandir(self.base))
        except FileNotFoundError:
            return []
        return sorted(
            entry.name
            for entry in entries
            if SLUG.fullmatch(entry.name)
            and entry.is_dir(follow_symlinks=False)
            and self._memory_dir(entry.name).is_dir()
        )

    def _note_place(self, place: Place, count: int) -> None:
        self.report.places["personal" if place.scope == "personal" else f"project {place.project}"] += count

    # Push

    def _push(self, target: Path | None, everything: bool) -> None:
        names = self._local_dirs() if everything else [self.target(target)]
        for name in names:
            if not self._memory_dir(name).is_dir():
                self.report.notes.append(f"no memory directory at {self._memory_dir(name)}: nothing to push")
                continue
            try:
                candidates = self.places.candidates(name)
            except HubError as exc:
                self.report.errors.append(str(exc))
                continue
            self._push_dir(name, candidates)

    def _files(self, directory: Path) -> tuple[dict[str, bytes], list[str]]:
        """The memory files of ``directory`` by name, and the conflict copies waiting there."""
        files, copies = {}, []
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            name = entry.name
            if not name.endswith(".md") or name.lower() == INDEX.lower() or name.startswith("."):
                continue
            if CONFLICT_COPY.search(name):
                copies.append(name)
                continue
            problem = name_problem(name)
            if problem:
                self.report.errors.append(f"{directory / name}: not pushed, {problem}")
                continue
            try:
                data = read_memory(directory / name)
            except LeftAlone as exc:
                self.report.errors.append(f"{directory / name}: not pushed, {exc}")
                continue
            if data is not None:
                files[name] = data
        return files, copies

    def _push_dir(self, name: str, candidates: list[Place]) -> None:
        directory = self._memory_dir(name)
        files, copies = self._files(directory)
        self._note_place(candidates[0], len(files))
        for file_name, data in files.items():
            try:
                self._push_file(name, directory, file_name, data, candidates)
            except LeftAlone as exc:
                self.report.errors.append(f"{directory / file_name}: left alone, {exc}")
        prefix = f"{name}/"
        kept = []
        for key, entry in list(self.state.files.items()):
            file_name = key[len(prefix) :]
            if not key.startswith(prefix) or "/" in file_name or file_name in files:
                continue
            if self.state.get(key) is None or entry.get("sha256") is None or os.path.lexists(directory / file_name):
                continue  # a deletion the hub holds already, or a file skipped above for another reason
            if not self.prune:
                kept.append(file_name)
                continue
            try:
                self._delete(key, directory / file_name, entry)
            except LeftAlone as exc:
                self.report.errors.append(f"{directory / file_name}: left alone, {exc}")
        if kept:
            self.report.counts["kept_on_hub"] += len(kept)
            self.report.notes.append(
                f"{directory}: {len(kept)} file(s) deleted here stay on the hub ({', '.join(kept)}); "
                "`evo-agents hub memory push --prune` deletes them there"
            )
        if copies:
            self.report.notes.append(
                f"{directory}: conflict copies wait to be merged and deleted ({', '.join(copies)}); "
                "they are never pushed"
            )

    def _push_file(self, name: str, directory: Path, file_name: str, data: bytes, candidates: list[Place]) -> None:
        key, path = f"{name}/{file_name}", directory / file_name
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self.report.errors.append(f"{path}: not pushed, not UTF-8 text")
            return
        problem = body_problem(text)
        if problem:
            self.report.errors.append(f"{path}: not pushed, {problem}")
            return
        kind = memory_type(text)
        entry = self.state.get(key)
        previous = _place(entry) if entry else None
        if previous is not None and previous in candidates:
            place = previous  # a file keeps the place it was synced with while that place is still here
        elif previous is not None and previous.scope == "project" and candidates[0].scope == "personal":
            self.report.errors.append(
                f"{path}: synced with project {previous.project} before, whose directories this one no longer is "
                "here; not pushed as personal. Run `evo-agents hub registry pull`, or move the file"
            )
            return
        elif (
            previous is not None
            and previous.scope == "project"
            and previous.project != candidates[0].project
            and entry.get("sha256") is not None
        ):
            # Two projects need not share members or labels: a push never moves a live memory from one to the other.
            self.report.errors.append(
                f"{path}: synced with project {previous.project} before, and this directory is project "
                f"{candidates[0].project}'s here now; not pushed, a memory never moves between projects by itself. "
                f"Rename the file to push it to {candidates[0].project} as a new memory (`evo-agents hub memory push "
                f"--prune` then deletes it from {previous.project})"
            )
            return
        else:
            place = candidates[0]
        same = previous == place and _shared(entry["scope"], entry["type"]) == _shared(place.scope, kind)
        sha = _sha(data)
        if same and entry.get("sha256") == sha:
            self.report.counts["unchanged"] += 1
            return
        moving = entry is not None and not same and entry.get("sha256") is not None
        if moving and _shared(entry["scope"], entry["type"]) and not self.prune:
            self.report.counts["held"] += 1
            self.report.notes.append(
                f"{path}: now a {kind} memory at {place}, while members of project {entry['project']} still see it "
                f"as a {entry['type']} memory; `evo-agents hub memory push --prune` moves it and deletes their copy"
            )
            return
        if self.dry_run:
            self.report.counts["updated" if same else "created"] += 1
            return
        body = {
            "scope": place.scope,
            "project": place.project,
            "location": place.location,
            "name": file_name,
            "type": kind,
            "body": text,
            "if_revision": entry["revision"] if same else None,
        }
        try:
            written = self.hub.call("PUT", "/v1/memories?" + urlencode({"sink": self.sink}), body)
        except HubError as exc:
            if exc.status == 409:
                self._resolve(key, path, data, (exc.payload or {}).get("current"), str(exc))
            elif exc.status in (403, 404, 413, 422):
                self.report.errors.append(f"{path}: not pushed: {exc}")
            else:
                raise  # signed out, the hub or the network failing: stop here
            return
        if not _well_formed(written):
            raise HubError(f"the hub at {self.hub.url} answered a push with something that is not a memory")
        self.state.set(key, written)
        self.report.counts[
            "created" if written.get("created") else "updated" if written.get("changed") else "unchanged"
        ] += 1
        if moving and entry["id"] != written["id"]:
            self._retire(path, entry)

    def _retire(self, path: Path, entry: dict) -> None:
        """Delete the copy a file had at its previous place on the hub, now that it moved there: one only its owner
        saw, or, with --prune, one the members of a project saw."""
        try:
            self.hub.call("DELETE", self._delete_path(entry))
        except HubError as exc:
            if exc.status not in (403, 404, 409, 422):
                raise
            self.report.notes.append(f"{path}: the copy at its previous place on the hub stays ({exc})")
            return
        self.report.counts["moved"] += 1

    def _delete_path(self, entry: Mapping) -> str:
        return f"/v1/memories/{entry['id']}?" + urlencode({"if_revision": entry["revision"], "sink": self.sink})

    def _delete(self, key: str, path: Path, entry: dict) -> None:
        """--prune: the tombstone of a file deleted here."""
        if self.dry_run:
            self.report.counts["deleted"] += 1
            return
        try:
            deleted = self.hub.call("DELETE", self._delete_path(entry))
        except HubError as exc:
            if exc.status == 409:  # changed on the hub since: its version comes back here rather than going
                current = (exc.payload or {}).get("current")
                if _well_formed(current) and not current["deleted"]:
                    self._write(path, current["body"].encode("utf-8"), None)
                    self.state.set(key, current)
                    self.report.counts["conflicts"] += 1
                    self.report.conflicts.append(
                        {"file": str(path), "copy": None, "hub_revision": current["revision"], "deleted_on_hub": False}
                    )
                    return
            if exc.status in (403, 404, 409, 422):
                self.report.errors.append(f"{path}: not deleted on the hub: {exc}")
                return
            raise
        if _well_formed(deleted):
            self.state.set(key, deleted)
        self.report.counts["deleted"] += 1

    # Pull

    def _list(self, filters: Mapping) -> list[dict]:
        """Every memory of ``filters`` the caller sees through the sink, tombstones included."""
        found: dict[int, dict] = {}
        cursor = None
        for _ in range(MAX_PAGES):
            query = {**filters, "deleted": "true", "limit": PAGE, "sink": self.sink}
            if cursor:
                query["cursor"] = cursor
            page = self.hub.call("GET", "/v1/memories?" + urlencode(query))
            items = page.get("items") if isinstance(page, dict) else None
            if not isinstance(items, list) or not all(_well_formed(item) for item in items):
                raise HubError(f"the hub at {self.hub.url} answered a listing this client cannot read")
            for memory in items:  # a memory changed during the listing comes again, later: keep the newest
                held = found.get(memory["id"])
                if held is None or memory["revision"] > held["revision"]:
                    found[memory["id"]] = memory
            following = page.get("next_cursor")
            if not following:
                return list(found.values())
            if following == cursor:
                raise HubError(f"the hub at {self.hub.url} handed out the same cursor twice")
            cursor = following
        raise HubError(f"the hub at {self.hub.url} listed more than {MAX_PAGES} pages of memories")

    def _pull(self, target: Path | None, everything: bool) -> None:
        if everything:
            groups: dict[str, list[dict]] = {}
            for memory in self._list({}):
                place = Place(memory["scope"], memory.get("project"), memory["location"])
                name = self.places.directory(place)
                if name is None:
                    if not memory["deleted"]:
                        self.report.counts["no_directory"] += 1
                        self.report.notes.append(f"{memory['name']} ({place}): no directory for it on this machine")
                    continue
                groups.setdefault(name, []).append(memory)
            for name in sorted(groups):
                self._pull_dir(name, groups[name])
            return
        name = self.target(target)
        try:
            candidates = self.places.candidates(name)
        except HubError as exc:
            self.report.errors.append(str(exc))
            return
        memories = [memory for place in candidates for memory in self._list(place.filters())]
        self._pull_dir(name, memories)

    def _choose(self, name: str, memories: list[dict]) -> dict[str, dict]:
        """One memory per file name. When several land on one name (a shared memory and one's own of the same
        name), the one this machine synced the file with wins, then a live one over a tombstone, then one's own,
        then the oldest; the others are reported."""
        groups: dict[str, list[dict]] = {}
        for memory in memories:
            groups.setdefault(memory["name"], []).append(memory)
        chosen = {}
        for file_name, group in groups.items():
            entry = self.state.get(f"{name}/{file_name}")
            group.sort(
                key=lambda m: (
                    not (entry and entry["id"] == m["id"]),
                    m["deleted"],
                    m["owner"].lower() != self.login.lower(),
                    m["id"],
                )
            )
            chosen[file_name] = group[0]
            for other in group[1:]:
                if not other["deleted"]:
                    self.report.notes.append(
                        f"{self._memory_dir(name) / file_name}: the hub also holds a {other['type']} memory of "
                        f"this name by {other['owner']} (id {other['id']}), not written here; rename one of them"
                    )
        return chosen

    def _pull_dir(self, name: str, memories: list[dict]) -> None:
        directory = self._memory_dir(name)
        chosen = self._choose(name, memories)
        live = [m for m in chosen.values() if not m["deleted"]]
        if live:
            self._note_place(Place(live[0]["scope"], live[0].get("project"), live[0]["location"]), len(live))
        present = {}
        for file_name, memory in sorted(chosen.items()):
            try:
                text = self._pull_file(name, directory, file_name, memory)
            except LeftAlone as exc:
                self.report.errors.append(f"{directory / file_name}: left alone, {exc}")
                continue
            if text is not None:
                present[file_name] = text
        if not present:
            return
        if self.dry_run:
            index = directory / INDEX
            held = index.read_text(encoding="utf-8", errors="replace") if index.is_file() else ""
            self.report.counts["indexed"] += sum(1 for n in present if not _points_at(held, n))
            return
        self.report.counts["indexed"] += len(add_pointers(directory, present))

    def _pull_file(self, name: str, directory: Path, file_name: str, memory: dict) -> str | None:
        """Bring one memory; the text of the file there afterwards, None when there is none."""
        key, path = f"{name}/{file_name}", directory / file_name
        entry = self.state.get(key)
        tracked = entry is not None and entry["id"] == memory["id"]
        local = read_memory(path)
        local_sha = _sha(local) if local is not None else None
        counts = self.report.counts
        if memory["deleted"]:
            if local is None:
                if tracked and not self.dry_run:
                    self.state.drop(key)
                return None
            if tracked and entry.get("sha256") is None:
                return local.decode("utf-8", "replace")  # written here after the deletion: it waits for a push
            if not self.prune:
                counts["kept_here"] += 1
                self.report.notes.append(
                    f"{path}: deleted on the hub, kept here; `evo-agents hub memory pull --prune` deletes it"
                )
                return local.decode("utf-8", "replace")
            if tracked and local_sha == entry.get("sha256"):
                if not self.dry_run:
                    self._remove(path, local_sha)
                    self.state.drop(key)
                counts["deleted"] += 1
                return None
            self._resolve(key, path, local, memory)
            return None
        body = memory["body"].encode("utf-8")
        if local is None:
            restored = tracked and entry.get("sha256") is not None and entry["revision"] == memory["revision"]
            counts["restored" if restored else "pulled"] += 1
            if not self.dry_run:
                self._write(path, body, None)
                self.state.set(key, memory)
            return memory["body"]
        if local_sha == _sha(body):
            counts["unchanged"] += 1
            if not self.dry_run:
                self.state.set(key, memory)
            return memory["body"]
        if tracked and local_sha == entry.get("sha256"):
            counts["updated"] += 1
            if not self.dry_run:
                self._write(path, body, local_sha)
                self.state.set(key, memory)
            return memory["body"]
        if tracked and entry.get("sha256") is not None and entry["revision"] == memory["revision"]:
            counts["changed_here"] += 1  # waits for a push
            return local.decode("utf-8", "replace")
        self._resolve(key, path, local, memory)
        return memory["body"]

    # Files on disk

    @staticmethod
    def _unchanged(path: Path, expected: str | None) -> None:
        """LeftAlone unless ``path`` still holds what the sync read there: bytes of sha256 ``expected``, or nothing
        when ``expected`` is None. Whoever wrote the file meanwhile wins."""
        held = read_regular(path)
        if (None if held is None else _sha(held)) != expected:
            raise LeftAlone("changed while the sync ran")

    def _write(self, path: Path, data: bytes, expected: str | None) -> None:
        self._unchanged(path, expected)
        path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        write_atomic(path, data, _mode(path))

    def _remove(self, path: Path, expected: str) -> None:
        self._unchanged(path, expected)
        path.unlink()

    def _resolve(self, key: str, path: Path, local: bytes, current, message: str = "") -> None:
        """A conflict: the hub's version (``current``) stays under the file's name, this machine's goes next to it
        as a conflict copy. Without a version of the hub to keep, the file is left as it is and reported."""
        if not _well_formed(current):
            self.report.errors.append(f"{path}: not pushed: {message or 'the hub refused it'}")
            return
        copy = conflict_path(path, self.host)
        if not self.dry_run:
            self._unchanged(path, _sha(local))
            write_atomic(copy, local, _mode(path))  # first, so this machine's version is safe whatever happens next
            if current["deleted"]:
                self._remove(path, _sha(local))
            else:
                self._write(path, current["body"].encode("utf-8"), _sha(local))
            self.state.set(key, current)
        self.report.counts["conflicts"] += 1
        self.report.conflicts.append(
            {
                "file": str(path),
                "copy": str(copy),
                "hub_revision": current["revision"],
                "deleted_on_hub": current["deleted"],
            }
        )


def search(hub: Hub, query: str, *, project: str | None = None, limit: int = 10, sink: str = AGENT_SINK) -> list:
    """The memories one sees whose name or text matches ``query``, best first."""
    params = {"q": query, "limit": limit, "sink": sink, **({"project": project} if project else {})}
    found = hub.call("GET", "/v1/memories/search?" + urlencode(params))
    items = found.get("items") if isinstance(found, dict) else None
    if not isinstance(items, list) or not all(_well_formed(item) for item in items):
        raise HubError(f"the hub at {hub.url} answered a search this client cannot read")
    return items
