"""The worker on duty for a project's night shift keeps the project's knowledge graph fresh on the hub (feature kg_sync of
the daemon): it syncs the git sources of the project's knowledge.yaml whose ref is ``origin/<branch>`` and pushes their
runs with its own token, the way a member's ``kg sync --push`` does, with no model involved, so the graph follows the
default branch while every member's laptop is off.

The heartbeat answer names the projects (``kg_sync``, ``evo_agents.hub.server.runs.service.kg_duties``): those the
worker serves whose night shift names it and whose graph is on. The daemon looks at each one every KG_SYNC_EVERY
seconds (EVO_WORKER_KG_SYNC_SECONDS; EVO_WORKER_KG_SYNC=off turns it off), day and night, and at once when the answer says
the hub took a run of the project since it last looked. A look (``KgSync.look``):

1. reads the project's harness.yaml and knowledge.yaml as the harness's default branch on origin has them, fetched
   first, from the worker's checkout of the harness (a checkout of the project with harness.yaml at its root): never
   the checkout's working tree, which may lag or hold another branch. It writes them, with each repo's path pointing at
   the worker's checkout of that repo, to ``<worker home>/kg/harness/<project>``, and loads the project from there with
   its corpus under ``<worker home>/kg/home``. knowledge.yaml is written byte for byte, so each source's lineage is the
   one a member's machine computes (``evo_agents.kg.sync.source_lineage``).
2. takes the git sources whose ref is origin/<branch> and that name a repo of harness.yaml. A source of another
   connector (the harness's working tree, an exec command) is read on members' machines alone, and a git source with
   another ref, with ``path:`` or with no ref is skipped and named so in the report.
3. for each source, merges first the runs of it the hub holds that the corpus here lacks, which other machines pushed
   (GET /v1/worker/kg/{p}/runs?source=, then each log, ``evo_agents.kg.sync.merge_pulled``), so the deletions its own
   run derives from its complete listing cover every item the hub holds, and its runs take the epoch the hub's newest
   runs of the source carry. Then it syncs the source as ``fetch: true`` says, under the daemon's lock of the
   checkout: a source still at the commit it read last is not read again, unless the runs merged changed what the
   source holds alive since this worker last read it, in which case it reads the commit again so the hub's state is
   that of the commit, whatever an older or partial run of another machine left.
4. pushes the runs here the hub lacks (``evo_agents.hub.kg_push`` on ``worker_routes``, the knowledge config left as
   the members sent it). Nothing is pushed when nothing was read and every run is on the hub, besides a check of what
   the hub holds at least every PUSH_CHECK_EVERY. The hub builds the graph from the runs as from any push.

What the look found goes in ``<worker home>/kg/state.json`` and in every heartbeat (``kg_sync``): for each project the
time of the look and what stopped it, and for each source its status (``runs.KG_SYNC_STATES``), the commit it holds,
how far behind the default branch that commit was, when it was read and pushed, and the error of what failed, every
error cut to KG_SYNC_ERROR_CHARS with anything that looks like a secret masked. ``evo-agents worker status`` prints it.

Standard library and the core package only, besides PyYAML; git through its command line.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import yaml

from evo_agents.hub import runs
from evo_agents.hub.client import Hub, HubError, get_presigned
from evo_agents.hub.kg_ingest import log_header, run_id_of
from evo_agents.hub.kg_push import Pusher, worker_routes
from evo_agents.hub.redact import redact
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.project import Project, ProjectError, load_project_at
from evo_agents.kg.sync import SyncResult, merge_pulled, sync_source

ENABLED_VARIABLE = "EVO_WORKER_KG_SYNC"  # "off" turns the sync off
EVERY_VARIABLE = "EVO_WORKER_KG_SYNC_SECONDS"
KG_SYNC_EVERY = 900.0  # seconds between two looks at a project
PUSH_CHECK_EVERY = timedelta(hours=24)  # the push asks what the hub holds at least this often, with nothing new here
FETCH_TIMEOUT = 120.0
GIT_TIMEOUT = 60.0
ORIGIN_REF = re.compile(r"^origin/(?P<branch>[^\s~^:?*\[\\]+)$")
PULL_SUFFIX = ".pull.jsonl.gz"  # a log being fetched: no run id names it, so neither a push nor a pull counts it


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


class GitProblem(RuntimeError):
    pass


def git(repo: Path, *args: str, timeout: float = GIT_TIMEOUT) -> str:
    """The output of ``git -C repo args``, never prompting; GitProblem with git's last line when it fails."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise GitProblem(f"git {args[0]} timed out after {timeout:g}s in {repo}") from None
    except OSError as exc:
        raise GitProblem(f"git cannot run: {exc}") from None
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        raise GitProblem(f"git {args[0]} failed in {repo}: {lines[-1] if lines else 'exit ' + str(done.returncode)}")
    return done.stdout


def default_branch(repo: Path) -> str:
    """The default branch of origin as the clone recorded it, main when it recorded none."""
    with contextlib.suppress(GitProblem):
        name = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD").strip()
        if name.startswith("origin/") and len(name) > len("origin/"):
            return name[len("origin/") :]
    return "main"


def fetch(repo: Path, branch: str) -> None:
    """Update origin/<branch> of the clone at ``repo`` from origin."""
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    git(repo, "fetch", "--quiet", "--no-tags", "origin", refspec, timeout=FETCH_TIMEOUT)


def shown(repo: Path, ref: str, path: str) -> str:
    """The text of ``path`` at ``ref``."""
    return git(repo, "show", f"{ref}:{path}")


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


def _harness_text(checkout: Path, project: str) -> tuple[str, str, dict, dict] | None:
    """(knowledge.yaml's text, the ref read, harness.yaml, knowledge.yaml) of ``checkout`` at origin's default branch,
    fetched first; None when its knowledge.yaml names another project."""
    branch = default_branch(checkout)
    fetch(checkout, branch)
    ref = f"origin/{branch}"
    manifest = yaml.safe_load(shown(checkout, ref, "harness.yaml")) or {}
    if not isinstance(manifest, dict):
        return None
    knowledge_text = shown(checkout, ref, str(manifest.get("knowledge_file") or "knowledge.yaml"))
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


def _write(view: Path, relative: str, text: str) -> None:
    path = view / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_view(root: Path, project: str, checkouts: dict[str, Path], lock: Callable) -> View:
    """Write the project's harness as origin's default branch has it under ``kg_dir(root)/harness/<project>`` and load
    it (see the module's docstring, step 1). ``checkouts`` maps each repo of the project to the worker's checkout of
    it. Raises ProjectError naming what is missing, GitProblem when git fails."""
    for checkout in sorted(set(checkouts.values())):
        if not (checkout / "harness.yaml").is_file():
            continue
        with lock(checkout):
            found = _harness_text(checkout, project)
        if found is None:
            continue
        knowledge_text, ref, manifest, knowledge = found
        view = kg_dir(root) / "harness" / project
        manifest = view_manifest(manifest, checkouts)
        _write(view, "harness.yaml", yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True))
        _write(view, manifest["knowledge_file"], knowledge_text)
        ontology = knowledge.get("ontology")
        if isinstance(ontology, str) and ontology:
            relative = str(Path(manifest["knowledge_file"]).parent / ontology)
            _write(view, relative, shown(checkout, ref, relative))
        return View(load_project_at(view, kg_dir(root) / "home"), checkouts, checkout)
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
    pulled: int = 0
    pushed: int = 0
    digest: str | None = None  # what the source held alive after this worker's last look (Corpus.alive_digest)

    def report(self) -> dict:
        shown = {key: value for key, value in self.__dict__.items() if key != "digest" and value is not None}
        return shown

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
    push_checked_at: str | None = None
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
            "push_checked_at": self.push_checked_at,
            "sources": {sid: state.__dict__ for sid, state in sorted(self.sources.items())},
        }

    @classmethod
    def from_json(cls, project: str, data) -> ProjectState:
        data = data if isinstance(data, dict) else {}
        found = cls(project, data.get("checked_at"), data.get("error"), data.get("push_checked_at"))
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
        directory = kg_dir(self.root)
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / "state.json.tmp"
        data = {name: state.to_json() for name, state in sorted(self.state.items())}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, directory / "state.json")

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
        corpus = view.project.corpus()
        try:
            read = self._sources(view, corpus, state)
            self._push(view, corpus, state, read)
        except HubError as exc:
            state.error = cut(f"the hub: {exc}", self.secrets)
        except Exception as exc:  # whatever else, the report says it and the next look tries again
            state.error = cut(f"{type(exc).__name__}: {exc}", self.secrets)
        finally:
            corpus.close()
            self.save()
            self.reports[project] = state.report()
        return state

    def _sources(self, view: View, corpus: Corpus, state: ProjectState) -> bool:
        """Pull and sync each git source of the project; whether one of them read something."""
        repos = {repo["name"] for repo in view.project.harness.repos() if isinstance(repo.get("name"), str)}
        sources = git_sources(view.project)
        for name in set(state.sources) - {src.get("id") for src in sources}:
            del state.sources[name]  # gone from knowledge.yaml
        read = False
        for source in sources[: runs.MAX_KG_SYNC_SOURCES]:
            if self.stop.is_set():
                break
            sid = source["id"]
            found = state.sources.setdefault(sid, SourceState(sid))
            found.checked_at, found.ref, found.pulled, found.pushed = _now(), source.get("ref"), 0, 0
            reason = skipped_reason(source, repos)
            if reason is not None:
                found.status, found.error = "skipped", reason
                continue
            checkout = view.checkouts.get(source["repo"])
            if checkout is None:
                found.status = "failed"
                found.error = (
                    f"no checkout of repo {source['repo']} of project {view.project.name} on this worker: add it to "
                    "the harness registry or to checkouts in config.json"
                )
                continue
            source["fetch"] = True  # as fetch: true reads it; fetch is not part of the lineage nor of what is read
            read |= self._one(view, corpus, found, checkout)
        return read

    def _one(self, view: View, corpus: Corpus, found: SourceState, checkout: Path) -> bool:
        """Pull and sync one source; whether it read something."""
        sid = found.source
        try:
            found.pulled = self.pull(view.project.name, corpus, sid)
        except HubError as exc:
            found.status, found.error = "failed", cut(f"merging the hub's runs: {exc}", self.secrets)
            return False
        force = found.digest is not None and corpus.alive_digest(sid) != found.digest
        try:
            with self.lock(checkout):
                result = sync_source(view.project, sid, corpus=corpus, force=force)
        except Exception as exc:  # a sync of the source running already, a corpus that cannot be written
            found.status, found.error = "failed", cut(f"{type(exc).__name__}: {exc}", self.secrets)
            return False
        found.digest = corpus.alive_digest(sid)
        cursor = corpus.cursor(sid)
        cursor = cursor if isinstance(cursor, dict) else {}
        found.commit, found.behind = cursor.get("commit"), cursor.get("behind")
        problem = sync_failure(result)
        found.status = "ok" if problem is None else "failed"
        found.error = None if problem is None else cut(problem, self.secrets)
        if result.ok and not result.unchanged:
            found.read_at = _now()
        return not result.unchanged

    def pull(self, project: str, corpus: Corpus, source: str) -> int:
        """Merge the runs of ``source`` the hub holds and the corpus lacks, each log checked against the SHA-256 and the
        size the hub recorded and named after its run; how many."""
        base = worker_routes(project)[0]
        for stale in corpus.log_dir.glob(f"*{PULL_SUFFIX}"):
            stale.unlink(missing_ok=True)
        listed = self.hub.call("GET", f"{base}/runs?source={quote(source, safe='')}")["ingested"]
        here = {run_id_of(path) for path in corpus.log_dir.iterdir()}
        pulled = 0
        for run_id in sorted(set(listed) - here):
            if self.stop.is_set():
                raise HubError("the daemon is stopping")
            log = self.hub.call("GET", f"{base}/runs/{run_id}/log")
            if log.get("source") != source:
                continue
            part = corpus.log_dir / f"{run_id}{PULL_SUFFIX}"
            if get_presigned(log["url"], part, int(log["log_size"])) != log["log_sha256"]:
                part.unlink(missing_ok=True)
                raise HubError(f"the log of run {run_id} the blob store sent does not have its recorded SHA-256")
            header = log_header(part) or {}
            if header.get("run_id") != run_id or header.get("source") != source:
                part.unlink(missing_ok=True)
                raise HubError(f"the log of run {run_id} on the hub is not the log of that run of source {source}")
            merge_pulled(corpus, part)  # merged before it is named after its run: a log named so is merged already
            os.replace(part, corpus.log_dir / f"{run_id}.jsonl.gz")
            pulled += 1
        corpus.record_on_hub(sorted(set(listed)), self.hub.url)
        return pulled

    def _push(self, view: View, corpus: Corpus, state: ProjectState, read: bool) -> None:
        """Push the runs the hub lacks, when there is one here or the last check of the hub is PUSH_CHECK_EVERY old."""
        logs = {run_id for path in corpus.log_dir.iterdir() if (run_id := run_id_of(path))}
        waiting = logs - corpus.runs_on_hub()
        checked = state.push_checked_at
        due = checked is None or datetime.fromisoformat(checked) <= datetime.now(UTC) - PUSH_CHECK_EVERY
        if not waiting and not read and not due:
            return
        pushed = Pusher(self.hub, view.project, routes=worker_routes(view.project.name), send_config=False).push()
        state.push_checked_at = _now()

        def source_of(run_id: str) -> SourceState | None:
            header = log_header(corpus.log_dir / f"{run_id}.jsonl.gz") or {}
            return state.sources.get(header.get("source")) if isinstance(header.get("source"), str) else None

        for run_id in pushed.pushed:
            found = source_of(run_id)
            if found is not None:
                found.pushed += 1
                found.pushed_at = state.push_checked_at
        if pushed.errors:
            state.error = cut(f"pushing to the hub: {pushed.errors[0]}", self.secrets)
            for run_id in sorted(logs - corpus.runs_on_hub()):
                found = source_of(run_id)
                if found is not None and found.status == "ok":
                    found.status, found.error = "failed", state.error
