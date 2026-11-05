"""Where the memory directories of Claude Code go on the hub: the slug rule of their names, and the places
(``Place``) of the hub projects a person sees, at the paths ``hub registry pull`` gives their harness and repos on
this machine (``Places``). ``evo_agents.hub.memory`` syncs the files of each directory with the place it finds here;
its docstring says how a directory's place is decided.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from evo_agents.hub.client import HubError
from evo_agents.hub.registry import cluster_of
from evo_agents.kg.project import binding_of, bound_project

HARNESS = "harness"  # the location of a project's harness root

SLUG = re.compile(r"[A-Za-z0-9-]{1,255}")
PERSONAL_LOCATION = re.compile(r"[~A-Za-z0-9-][A-Za-z0-9-]{0,254}")


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
        self._roots: dict[str, set[str | None]] = {}  # per directory, the projects whose harness root it is
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
            if cluster is None:
                continue  # an empty harness names no paths either
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
