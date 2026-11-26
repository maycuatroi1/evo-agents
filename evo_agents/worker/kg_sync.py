"""The worker on duty for a project's night shift keeps the project's knowledge graph fresh on the hub (feature kg_sync of
the daemon): it syncs the git sources of the project's knowledge.yaml whose ref is ``origin/<branch>`` and pushes their
runs with its own token, the way a member's ``kg sync`` does, with no model involved and no corpus on the worker, so
the graph follows the default branch while every member's laptop is off.

The heartbeat answer names the projects (``kg_sync``, ``evo_agents.hub.server.runs.service.kg_duties``): those the
worker serves whose night shift names it and whose graph is on. The daemon looks at each one every KG_SYNC_EVERY
seconds (EVO_WORKER_KG_SYNC_SECONDS; EVO_WORKER_KG_SYNC=off turns it off), day and night, and at once when the answer says
the hub took a run of the project since it last looked. A look (``KgSync.look``):

1. reads the project's harness.yaml and knowledge.yaml as the harness's default branch on origin has them, fetched
   first, from the worker's checkout of the harness (a checkout of the project with harness.yaml at its root): never
   the checkout's working tree, which may lag or hold another branch. It writes them, with each repo's path pointing at
   the worker's checkout of that repo, to ``<worker home>/kg/harness/<project>``, and loads the project from there with
   its directory (the lock of each source, the audit log) under ``<worker home>/kg/home``. knowledge.yaml is written byte for byte, so each source's lineage is the
   one a member's machine computes (``evo_agents.kg.sync.source_lineage``). A repo a source of step 2 names that has no
   checkout on the worker, but an https:// origin in harness.yaml, is cloned once (``--no-checkout --filter=blob:none``)
   to ``<worker home>/kg/checkouts/<project>/<repo>``, which later looks reuse.
2. takes the git sources whose ref is origin/<branch> and that name a repo of harness.yaml. A source of another
   connector (the harness's working tree, an exec command) is read on members' machines alone, and a git source with
   another ref, with ``path:`` or with no ref is skipped and named so in the report.
3. syncs each source as ``fetch: true`` says, under the daemon's lock of the checkout, against the state the hub
   holds of it (GET /v1/worker/kg/{p}/sources/{s}/state, ``evo_agents.hub.kg_hub_sync``): its cursor, the epoch its
   runs take, and the items its complete listing derives deletions from are those of every machine's runs, so a
   source whose newest run, of any machine, read the commit its ref names now is not read again. A run goes to the hub
   as it ends (``worker_routes``, the knowledge config left as the members sent it); the hub builds the graph from it
   as from any push.

What the look found goes in ``<worker home>/kg/state.json`` and in every heartbeat (``kg_sync``): for each project the
time of the look and what stopped it, and for each source its status (``runs.KG_SYNC_STATES``), the commit it holds,
how far behind the default branch that commit was, when it was read and pushed, and the error of what failed, every
error cut to KG_SYNC_ERROR_CHARS with anything that looks like a secret masked. ``evo-agents worker status`` prints it.

Standard library and the core package only, besides PyYAML; git through its command line.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from evo_agents.hub import runs
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.kg_hub_sync import HubSync, UnreadRuns
from evo_agents.hub.kg_push import worker_routes
from evo_agents.hub.redact import redact
from evo_agents.infra.files import JsonFile
from evo_agents.infra.git import NO_PROMPT, Git, GitError, GitFailed, GitTimeout, GitUnavailable
from evo_agents.kg.project import Project, ProjectError, load_project_at
from evo_agents.kg.sync import SyncResult

ENABLED_VARIABLE = "EVO_WORKER_KG_SYNC"  # "off" turns the sync off
EVERY_VARIABLE = "EVO_WORKER_KG_SYNC_SECONDS"
KG_SYNC_EVERY = 900.0  # seconds between two looks at a project
FETCH_TIMEOUT = 120.0
GIT_TIMEOUT = 60.0
ORIGIN_REF = re.compile(r"^origin/(?P<branch>[^\s~^:?*\[\\]+)$")


def enabled(env: Mapping[str, str]) -> bool:
    return (env.get(ENABLED_VARIABLE) or "").strip().lower() not in ("off", "0", "false", "no")


def every(env: Mapping[str, str]) -> float:
    try:
        value = float(env.get(EVERY_VARIABLE) or KG_SYNC_EVERY)
    except ValueError:
        value = KG_SYNC_EVERY
    return max(value, 1.0)


def kg_dir(root: Path) -> Path:
    """Where the sync keeps its harness views, its corpora and its state, under the worker's home ``root``."""
    return root / "kg"


def load_state(root: Path) -> dict:
    """What the last looks found, by project, as ``state.json`` holds it; {} when there is none."""
    try:
        data = json.loads((kg_dir(root) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class GitProblem(GitError, RuntimeError):
    pass


class Checkout(Git):
    """``git -C repo`` of a sync, never prompting, within GIT_TIMEOUT unless a call says otherwise; GitProblem with
    git's last line when it fails."""

    def __init__(self, repo: Path):
        super().__init__(repo, timeout=GIT_TIMEOUT, env=NO_PROMPT)

    def fail(self, error: GitError) -> BaseException:
        if isinstance(error, GitTimeout):
            return GitProblem(f"git {error.command} timed out after {error.timeout:g}s in {self.directory}")
        if isinstance(error, GitUnavailable):
            return GitProblem(f"git cannot run: {error.cause}")
        lines = error.stderr.strip().splitlines() if isinstance(error, GitFailed) else []
        code = error.returncode if isinstance(error, GitFailed) else "?"
        return GitProblem(f"git {error.command} failed in {self.directory}: {lines[-1] if lines else f'exit {code}'}")


def default_branch(repo: Path) -> str:
    """The default branch of origin as the clone recorded it, main when it recorded none."""
    with contextlib.suppress(GitProblem):
        name = Checkout(repo).out("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD").strip()
        if name.startswith("origin/") and len(name) > len("origin/"):
            return name[len("origin/") :]
    return "main"


def clone(origin: str, dest: Path) -> None:
    """Clone ``origin`` to ``dest`` without a working tree nor blobs, which a sync reads from the ref as it needs them."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    Checkout(dest.parent).out(
        "clone",
        "--quiet",
        "--no-checkout",
        "--filter=blob:none",
        "--",
        origin,
        dest.name,
        timeout=FETCH_TIMEOUT,
    )


def fetch(repo: Path, branch: str) -> None:
    """Update origin/<branch> of the clone at ``repo`` from origin."""
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    Checkout(repo).out("fetch", "--quiet", "--no-tags", "origin", refspec, timeout=FETCH_TIMEOUT)


def shown(repo: Path, ref: str, path: str) -> str:
    """The text of ``path`` at ``ref``."""
    return Checkout(repo).out("show", f"{ref}:{path}")


def cut(text: str, secrets: tuple[str | None, ...] = ()) -> str:
    """An error as the report carries it: one line, anything that looks like a secret masked, at most
    KG_SYNC_ERROR_CHARS."""
    line = " ".join(redact(str(text), secrets).split()) or "unknown error"
    limit = runs.KG_SYNC_ERROR_CHARS
    return line if len(line) <= limit else line[: limit - 3].rstrip() + "..."


# The harness as origin has it


@dataclass
class View:
    """The project as the worker reads it: the loaded project, the checkout of each repo by name, the harness's."""

    project: Project
    checkouts: dict[str, Path]
    harness: Path
    missing: dict[str, str] = field(default_factory=dict)  # why a repo a source names has no checkout, by name


def shown_if_present(repo: Path, ref: str, path: str) -> str | None:
    """The text of ``path`` at ``ref``, or None when ``ref`` holds no file at ``path``."""
    try:
        Checkout(repo).out("cat-file", "-e", f"{ref}:{path}")
    except GitProblem:
        return None
    return shown(repo, ref, path)


def _local_matches(checkout: Path, project: str) -> bool:
    """Whether the working tree's harness.yaml or knowledge file already names ``project`` (an ordering hint only)."""
    try:
        manifest = yaml.safe_load((checkout / "harness.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    if not isinstance(manifest, dict):
        return False
    hub = manifest.get("hub")
    if isinstance(hub, dict) and hub.get("project") == project:
        return True
    kfile = checkout / str(manifest.get("knowledge_file") or "knowledge.yaml")
    try:
        knowledge = yaml.safe_load(kfile.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return manifest.get("name") == project
    return isinstance(knowledge, dict) and knowledge.get("project") == project


def _harness_text(checkout: Path, project: str) -> tuple[str, str, dict, dict] | None:
    """(knowledge.yaml's text, the ref read, harness.yaml, knowledge.yaml) of ``checkout`` at origin's default branch,
    fetched first; None when origin's default branch has no harness/knowledge file or names another project."""
    branch = default_branch(checkout)
    fetch(checkout, branch)
    ref = f"origin/{branch}"
    manifest_text = shown_if_present(checkout, ref, "harness.yaml")
    if manifest_text is None:
        return None
    manifest = yaml.safe_load(manifest_text) or {}
    if not isinstance(manifest, dict):
        return None
    knowledge_text = shown_if_present(checkout, ref, str(manifest.get("knowledge_file") or "knowledge.yaml"))
    if knowledge_text is None:
        return None
    knowledge = yaml.safe_load(knowledge_text) or {}
    if not isinstance(knowledge, dict) or knowledge.get("project") != project:
        return None
    return knowledge_text, ref, manifest, knowledge


def view_manifest(manifest: dict, checkouts: dict[str, Path]) -> dict:
    """harness.yaml as the view writes it: each repo's path the worker's checkout of it, none for a repo without one,
    and no workspace, every path being absolute."""
    repos = []
    for repo in manifest.get("repos") or []:
        if not isinstance(repo, dict):
            continue
        entry = {key: value for key, value in repo.items() if key != "path"}
        if isinstance(repo.get("name"), str) and repo["name"] in checkouts:
            entry["path"] = str(checkouts[repo["name"]])
        repos.append(entry)
    shown = {key: value for key, value in manifest.items() if key != "workspace"}
    return {**shown, "repos": repos, "knowledge_file": str(manifest.get("knowledge_file") or "knowledge.yaml")}


def managed_checkouts(
    root: Path, project: str, manifest: dict, knowledge: dict, checkouts: dict[str, Path], lock: Callable
) -> tuple[dict[str, Path], dict[str, str]]:
    """``checkouts`` with the worker's own clone of each repo a synced git source names that has none, cloned under
    ``kg_dir(root)/checkouts/<project>`` from its https:// origin in harness.yaml when not cloned yet; and why a repo
    still has none, by name."""
    repos = {repo["name"]: repo for repo in manifest.get("repos") or [] if isinstance(repo, dict) and repo.get("name")}
    found, missing = dict(checkouts), {}
    for source in knowledge.get("sources") or []:
        if not isinstance(source, dict) or source.get("connector") != "git" or skipped_reason(source, set(repos)):
            continue
        name = source["repo"]
        if name in found or name in missing:
            continue
        origin = repos[name].get("origin")
        if not isinstance(origin, str) or not origin.startswith("https://"):
            missing[name] = "harness.yaml gives it no https:// origin to clone"
            continue
        if any(Path(part).name != part or part in ("", ".", "..") or part.startswith("-") for part in (project, name)):
            missing[name] = "its name is not one directory's, to clone it under"
            continue
        dest = kg_dir(root) / "checkouts" / project / name
        try:
            with lock(dest):
                if not (dest / ".git").exists():
                    clone(origin, dest)
        except GitProblem as exc:
            missing[name] = f"cloning its origin: {exc}"
            continue
        found[name] = dest
    return found, missing


def _write(view: Path, relative: str, text: str) -> None:
    path = view / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_view(root: Path, project: str, checkouts: dict[str, Path], lock: Callable) -> View:
    """Write the project's harness as origin's default branch has it under ``kg_dir(root)/harness/<project>`` and load
    it (see the module's docstring, step 1). ``checkouts`` maps each repo of the project to the worker's checkout of
    it. Raises ProjectError naming what is missing, GitProblem when git fails and no checkout resolves the project."""
    candidates = [c for c in sorted(set(checkouts.values())) if (c / "harness.yaml").is_file()]
    candidates.sort(key=lambda c: (not _local_matches(c, project), str(c)))
    first_git_error: GitProblem | None = None
    for checkout in candidates:
        try:
            with lock(checkout):
                found = _harness_text(checkout, project)
        except GitProblem as exc:
            if first_git_error is None:
                first_git_error = exc
            continue
        if found is None:
            continue
        knowledge_text, ref, manifest, knowledge = found
        view = kg_dir(root) / "harness" / project
        checkouts, missing = managed_checkouts(root, project, manifest, knowledge, checkouts, lock)
        manifest = view_manifest(manifest, checkouts)
        _write(view, "harness.yaml", yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))
        _write(view, manifest["knowledge_file"], knowledge_text)
        ontology = knowledge.get("ontology")
        if isinstance(ontology, str) and ontology:
            relative = str(Path(manifest["knowledge_file"]).parent / ontology)
            _write(view, relative, shown(checkout, ref, relative))
        return View(load_project_at(view, kg_dir(root) / "home"), checkouts, checkout, missing)
    if first_git_error is not None:
        raise first_git_error
    raise ProjectError(
        f"no checkout of the harness of project {project} on this worker: a checkout of the project with "
        "harness.yaml at its root, whose knowledge.yaml names the project at origin's default branch"
    )


# What a look found


@dataclass
class SourceState:
    """One source as the state file and the report hold it."""

    source: str
    status: str = "ok"
    error: str | None = None
    ref: str | None = None
    commit: str | None = None
    behind: int | None = None
    checked_at: str | None = None
    read_at: str | None = None
    pushed_at: str | None = None
    pushed: int = 0

    def report(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if value is not None}

    @classmethod
    def from_json(cls, data) -> SourceState | None:
        if not isinstance(data, dict) or not isinstance(data.get("source"), str):
            return None
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass
class ProjectState:
    project: str
    checked_at: str | None = None
    error: str | None = None
    sources: dict[str, SourceState] = field(default_factory=dict)

    def report(self) -> dict:
        shown = {"project": self.project, "checked_at": self.checked_at or _now()}
        if self.error:
            shown["error"] = self.error
        shown["sources"] = [state.report() for _, state in sorted(self.sources.items())]
        return shown

    def to_json(self) -> dict:
        return {
            "project": self.project,
            "checked_at": self.checked_at,
            "error": self.error,
            "sources": {sid: state.__dict__ for sid, state in sorted(self.sources.items())},
        }

    @classmethod
    def from_json(cls, project: str, data) -> ProjectState:
        data = data if isinstance(data, dict) else {}
        found = cls(project, data.get("checked_at"), data.get("error"))
        for sid, raw in (data.get("sources") or {}).items() if isinstance(data.get("sources"), dict) else ():
            state = SourceState.from_json(raw)
            if state is not None and state.source == sid:
                found.sources[sid] = state
        return found


def shown_state(root: Path) -> list[dict]:
    """What the last look at each project found, as the heartbeat reports it, by project (``worker status``)."""
    return [ProjectState.from_json(name, raw).report() for name, raw in sorted(load_state(root).items())]


def status_lines(projects: list[dict]) -> list[str]:
    """``shown_state`` as ``evo-agents worker status`` prints it: a line per project, then one per source."""
    lines = []
    for project in projects:
        failed = f", failed: {project['error']}" if project.get("error") else ""
        lines.append(f"graph of {project['project']}: looked at {project.get('checked_at') or 'never'}{failed}")
        for source in project.get("sources") or []:
            where = (source.get("commit") or "")[:12] or "no commit"
            behind = f", {source['behind']} behind" if source.get("behind") else ""
            error = f": {source['error']}" if source.get("error") else ""
            lines.append(f"  source {source['source']}: {source['status']} at {where}{behind}{error}")
    return lines


def skipped_reason(source: dict, harness_repos: set[str]) -> str | None:
    """Why the worker does not read ``source``, a git source; None when it does."""
    ref = source.get("ref")
    if source.get("path") or not source.get("repo"):
        return "a path: source is read on members' machines, which have its checkout"
    if not ref:
        return "it names no ref and reads the checkout's HEAD, which only a member's machine has"
    if not isinstance(ref, str) or ORIGIN_REF.fullmatch(ref) is None:
        return f"its ref {ref!r} is not origin/<branch>: the worker reads only a ref it can fetch"
    if source["repo"] not in harness_repos:
        return f"repo {source['repo']!r} is not in harness.yaml"
    return None


def git_sources(project: Project) -> list[dict]:
    return [src for src in project.sources() if isinstance(src, dict) and src.get("connector") == "git"]


def sync_failure(result: SyncResult) -> str | None:
    """What went wrong with a sync: for a failed one, its first issue, else the exception of its connector, else its
    stderr; for an ok one, a fetch that failed, so the commit read may lag the remote. None when nothing did."""
    if result.ok:
        return next((w for w in result.warnings if w.startswith(("fetch failed", "fetch: true ignored"))), None)
    for found in (result.issues, [result.exception], result.stderr_tail[-1:]):
        first = next((item for item in found if item), None)
        if first:
            return str(first)
    return f"the connector ended with status {result.returncode}"


# The look


class KgSync:
    """The looks of one daemon: ``hub`` holds the worker's token and the protocol header; ``lock(path)`` is a context
    manager that holds the daemon's lock of a checkout while git writes to it (fetching), the null one by default."""

    def __init__(self, root: Path, hub: Hub, *, lock: Callable | None = None, secrets: tuple[str | None, ...] = ()):
        self.root = root
        self.hub = hub
        self.lock = lock or (lambda path: contextlib.nullcontext())
        self.secrets = secrets
        self.stop = threading.Event()  # set when the daemon stops: a look ends at the next source or log
        self.state = {
            name: ProjectState.from_json(name, raw) for name, raw in load_state(root).items() if isinstance(name, str)
        }
        # what the heartbeat sends, by project: replaced whole at the end of a look, so the daemon's loop reads it while
        # a look in its thread changes the state
        self.reports = {name: state.report() for name, state in self.state.items()}

    def save(self) -> None:
        data = {name: state.to_json() for name, state in sorted(self.state.items())}
        JsonFile(kg_dir(self.root) / "state.json", mode=0o644, durable=False, ensure_ascii=False, indent=2).write(data)

    def report(self, projects: list[str]) -> dict:
        """kg_sync of the heartbeat: what the last look at each of ``projects`` found, those looked at already."""
        shown = [found for name in projects if (found := self.reports.get(name)) is not None]
        return {"projects": shown[: runs.MAX_KG_SYNC_PROJECTS]}

    def look(self, project: str, checkouts: dict[str, Path]) -> ProjectState:
        """Look at ``project`` once (see the module's docstring), its repos' checkouts by name; what it found, saved."""
        state = self.state.setdefault(project, ProjectState(project))
        state.checked_at, state.error = _now(), None
        try:
            view = write_view(self.root, project, checkouts, self.lock)
        except (ProjectError, GitProblem, OSError, yaml.YAMLError) as exc:
            state.error = cut(exc, self.secrets)
            self.save()
            self.reports[project] = state.report()
            return state
        syncer = HubSync(self.hub, view.project, routes=worker_routes(project), send_config=False, builds=False)
        try:
            self._sources(view, syncer, state)
        except HubError as exc:
            state.error = cut(f"the hub: {exc}", self.secrets)
        except Exception as exc:  # whatever else, the report says it and the next look tries again
            state.error = cut(f"{type(exc).__name__}: {exc}", self.secrets)
        finally:
            self.save()
            self.reports[project] = state.report()
        return state

    def _sources(self, view: View, syncer: HubSync, state: ProjectState) -> None:
        """Sync each git source of the project."""
        repos = {repo["name"] for repo in view.project.harness.repos() if isinstance(repo.get("name"), str)}
        sources = git_sources(view.project)
        for name in set(state.sources) - {src.get("id") for src in sources}:
            del state.sources[name]  # gone from knowledge.yaml
        for source in sources[: runs.MAX_KG_SYNC_SOURCES]:
            if self.stop.is_set():
                break
            sid = source["id"]
            found = state.sources.setdefault(sid, SourceState(sid))
            found.checked_at, found.ref, found.pushed = _now(), source.get("ref"), 0
            reason = skipped_reason(source, repos)
            if reason is not None:
                found.status, found.error = "skipped", reason
                continue
            checkout = view.checkouts.get(source["repo"])
            if checkout is None:
                found.status = "failed"
                why = view.missing.get(source["repo"])
                found.error = cut(
                    f"no checkout of repo {source['repo']} of project {view.project.name} on this worker"
                    + (f" ({why})" if why else "")
                    + ": add it to the harness registry or to checkouts in config.json",
                    self.secrets,
                )
                continue
            source["fetch"] = True  # as fetch: true reads it; fetch is not part of the lineage nor of what is read
            self._one(syncer, found, checkout)

    def _one(self, syncer: HubSync, found: SourceState, checkout: Path) -> None:
        """Sync one source and push its run."""
        sid = found.source
        try:
            with self.lock(checkout):
                result = syncer.sync_source(sid)
        except HubError as exc:
            if exc.status == 401:
                raise  # the worker's token: no source can go
            found.status, found.error = "failed", cut(f"the hub: {exc}", self.secrets)
            return
        except UnreadRuns as exc:  # the next look tries again, once a member's build read them
            found.status, found.error = "skipped", cut(str(exc), self.secrets)
            return
        except Exception as exc:  # a sync of the source running already, a directory that cannot be written
            found.status, found.error = "failed", cut(f"{type(exc).__name__}: {exc}", self.secrets)
            return
        cursor = syncer.cursors.get(sid)
        cursor = cursor if isinstance(cursor, dict) else {}
        found.commit, found.behind = cursor.get("commit"), cursor.get("behind")
        problem = sync_failure(result)
        found.status = "ok" if problem is None else "failed"
        found.error = None if problem is None else cut(problem, self.secrets)
        if result.ok and not result.unchanged:
            found.read_at = found.pushed_at = _now()
            found.pushed = 1
