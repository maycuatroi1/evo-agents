"""``kg sync`` against the hub: run a source's connector and push its run straight to the hub, with no corpus.sqlite
(plan kg-centralized-on-hub, step 3).

What a sync needs of what the source holds already, it reads from the hub's state of the source (GET
``{base}/sources/{source}/state``, ``evo_agents.hub.kg_source_state``):

- the cursor the run resumes from, and whether the last run was ok, which a git source still at the commit its cursor
  names reads nothing for (``evo_agents.kg.sync.read_already``);
- the lineage and epoch, so the run takes the epoch of the source when it reads from the same place and starts a new one
  when it does not (``evo_agents.kg.corpus.next_epoch``);
- the live items with their rev and rev_time, which the tombstones of the run name and which a complete listing derives
  its removals from, through the same deletion guard (``evo_agents.kg.sync._removals``). They are those of the hub's
  latest build: a run pushed since is not in them yet, so a deletion it made is written again, which changes nothing,
  and one of an item it added waits for the next sync. When the state says runs pushed before migration 0252 are unread,
  a member's sync asks for a build and waits on it first, since the cursor and lineage may lie in them; the worker on
  duty, which the hub does not let build, reads nothing of the source until a build read them (``UnreadRuns``). A scope of more items
  than the state lists is asked for alone; one still cut fails the run rather than delete what it does not see;
- the removals the guard held back (``held``), which ``--accept-removals`` applies: a run of connector
  ``evo:accept-removals`` with a tombstone for each held item still alive, whose ``listed`` names the scopes it applied,
  so the hub drops their hold;
- when the last ok run finished, which ``--due`` reads (``items=false``).

The run's log and blobs go to a spool directory under the project's directory, deleted once the run is pushed or not
(``evo_agents.hub.kg_push.Pusher.push_log``); a run the hub did not take fails, and the next sync starts again from the
cursor the hub holds. A source whose label the project's hub sink does not clear is not read at all, since its run
could not leave the machine. The project's directory keeps the lock of each source and the audit log, nothing else.

A member syncs through ``member_routes`` and sends the project's knowledge config first; the worker on duty syncs
through ``worker_routes`` and leaves the config as the members sent it (``evo_agents.worker.kg_sync``).
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import quote, urlencode

from evo_agents import filelock
from evo_agents.hub.api import paths
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.kg_push import Pusher, PushReport, member_routes
from evo_agents.kg import audit
from evo_agents.kg.corpus import BASE_EPOCH, ItemRecord, LogWriter, next_epoch, to_log_record
from evo_agents.kg.ids import utc_now, uuid7
from evo_agents.kg.project import Project, ProjectError, remember
from evo_agents.kg.protocol import StreamChecker, sha256, source_of
from evo_agents.kg.protocol.runner import ConnectorContext, ConnectorError, credential_env
from evo_agents.kg.sync import HubBuild, ProjectSync, SyncResult, due_sources, read_already, read_run, source_lineage

ACCEPT_CONNECTOR = "evo:accept-removals"
BUILD_WAIT = 1800.0  # seconds --build waits for the hub's build at most
WAIT_INTERVAL = 2.0
FINISHED = ("succeeded", "failed")


def wait_for(hub: Hub, base: str, build_id: int, timeout: float, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Build ``build_id`` of the project whose kg routes are ``base`` once it has finished; a HubError when it has
    not after ``timeout`` seconds (``hub kg build --wait``, ``kg sync --build``)."""
    deadline = clock() + timeout
    while True:
        build = hub.call("GET", f"{base}/builds/{build_id}") or {}
        if build["status"] in FINISHED:
            return build
        if clock() >= deadline:
            raise HubError(f"build {build_id} has not finished after {timeout:g}s")
        sleep(WAIT_INTERVAL)


class UnreadRuns(RuntimeError):
    """The hub's state of a source has runs no build has read yet, and this sync may not ask for a build: the worker on
    duty reads nothing of the source until a member's build reads them."""


class TooManyItems(RuntimeError):
    """A scope holds more items than the hub's state lists: its removals cannot be worked out."""


class Spool:
    """One run of a sync: its log and blobs in a directory of their own, and the items the hub holds of the source,
    read as a corpus would be (``evo_agents.kg.sync.Known``)."""

    def __init__(self, root: Path, source: str, state: dict, scoped: Callable[[str], dict]):
        self.root = root
        self.log_dir = root / "log"
        self.blob_dir = root / "blobs" / "sha256"
        self.log_dir.mkdir(parents=True)
        self.blob_dir.mkdir(parents=True)
        self.source = source
        self.cut = bool(state.get("items_cut"))
        self.items = {item["id"]: self._record(item) for item in state.get("items") or ()}
        self.scoped = scoped

    def _record(self, item: dict) -> ItemRecord:
        record = {"id": item["id"], "rev": item.get("rev") or "", "rev_time": item.get("rev_time") or ""}
        return ItemRecord(item["id"], self.source, False, "", record)

    def blob_path(self, ref: str) -> Path:
        digest = ref.split(":", 1)[1]
        return self.blob_dir / digest[:2] / digest[2:]

    def put_blob(self, text: str) -> str:
        data = text.encode("utf-8")
        ref = sha256(data)
        path = self.blob_path(ref)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return ref

    def to_log_record(self, message: dict) -> dict:
        return to_log_record(message, self.put_blob)

    def get(self, item_id: str) -> ItemRecord | None:
        return self.items.get(item_id)

    def alive_in_scope(self, scope: str) -> list[ItemRecord]:
        if source_of(scope) != self.source:
            return []
        if not self.cut:
            return [record for item_id, record in self.items.items() if item_id.startswith(scope)]
        state = self.scoped(scope)
        if state.get("items_cut"):
            raise TooManyItems(f"scope {scope} holds more items than the hub lists; its removals are not worked out")
        return [self._record(item) for item in state.get("items") or ()]

    def record_on_hub(self, run_ids, hub: str) -> None:
        """Nothing to record: the spool goes once its run is pushed."""


class HubSync:
    """The syncs of one project's sources against the hub. ``routes`` and ``send_config`` as ``Pusher`` takes them;
    ``builds`` False for routes that may not ask for a build (the worker's); ``plans`` reads the hub's plans for a harness source (``ConnectorContext.plans``)."""

    def __init__(
        self,
        hub: Hub,
        project: Project,
        *,
        routes: tuple[str, str] | None = None,
        send_config: bool = True,
        builds: bool = True,
        plans=None,
        credential_getter=None,
        kill_after: int | None = None,
        progress: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.hub = hub
        self.project = project
        self.routes = routes or member_routes(project.name)
        self.base = self.routes[0]
        self.send_config = send_config
        self.builds = builds
        self.plans = plans
        self.credential_getter = credential_getter
        self.kill_after = kill_after
        self.progress = progress
        self.sleep = sleep
        self.config_sent = False
        self.cursors: dict[str, object] = {}  # the cursor each source synced here holds now on the hub

    # -- the hub

    def state(self, source: str, *, scope: str | None = None, items: bool = True) -> dict:
        query = {key: value for key, value in (("scope", scope), ("items", "false" if not items else None)) if value}
        path = f"{self.base}/sources/{quote(source, safe='')}/state"
        return self.hub.call("GET", path + (f"?{urlencode(query)}" if query else "")) or {}

    def build(self, timeout: float = BUILD_WAIT) -> HubBuild:
        """Ask the hub for a build of the project's graph and wait until it has finished."""
        try:
            queued = self.hub.call("POST", f"{self.base}/builds") or {}
            build = queued.get("build")
            if build is None:
                return HubBuild(error="the hub queued a build but names none")
            return HubBuild(wait_for(self.hub, self.base, build["id"], timeout, self.sleep))
        except HubError as exc:
            return HubBuild(error=str(exc))

    def last_ok(self, source: str) -> str | None:
        """When the last ok run of ``source`` the hub holds finished, None when it holds none."""
        last = self.state(source, items=False).get("last_ok")
        return last.get("finished_at") if last else None

    def _pusher(self) -> Pusher:
        return Pusher(
            self.hub,
            self.project,
            routes=self.routes,
            send_config=self.send_config,
            progress=self.progress,
            sleep=self.sleep,
        )

    def _push(self, spool: Spool, path: Path, run_id: str) -> str | None:
        """Push the run logged at ``path``; why the hub did not take it, None when it did."""
        pusher = self._pusher()
        try:
            if self.send_config and not self.config_sent:
                pusher.put_config()
                self.config_sent = True
            pusher.push_log(spool, path, run_id, PushReport(self.project.name))
        except (HubError, ProjectError) as exc:
            if isinstance(exc, HubError) and exc.status == 401:
                raise  # the credential: no run can go
            return f"the hub did not take the run: {exc}"
        return None

    # -- a run

    @contextlib.contextmanager
    def _locked(self, source: str) -> Iterator[None]:
        """One sync of ``source`` at a time on this machine."""
        locks = self.project.root / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        with open(locks / f"{source}.lock", "a", encoding="utf-8") as fh:
            if not filelock.try_lock(fh.fileno()):
                raise RuntimeError(f"another sync of source {source!r} is running")
            try:
                yield
            finally:
                filelock.unlock(fh.fileno())

    @contextlib.contextmanager
    def _spool(self, source: str, state: dict) -> Iterator[Spool]:
        self.project.root.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="spool-", dir=self.project.root))
        try:
            yield Spool(root, source, state, lambda scope: self.state(source, scope=scope))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _audit(self, result: SyncResult, connector: str, version: str | None, started: str) -> None:
        audit.record_sync(
            self.project.root,
            self.project.name,
            result,
            connector=connector,
            connector_version=version,
            started=started,
            finished=utc_now(),
        )

    def _fresh_state(self, source: str) -> dict:
        """The hub's state of ``source``, after a build when runs pushed before migration 0252 are unread in it."""
        state = self.state(source)
        if state.get("unread") and not self.builds:
            raise UnreadRuns(
                f"the hub has {state['unread']} run(s) of source {source} no build has read yet; the source syncs once "
                "a build on the hub reads them"
            )
        if state.get("unread"):
            built = self.build()
            if not built.ok:
                raise HubError(f"the hub has runs of source {source} no build has read yet: {built.summary_line()}")
            state = self.state(source)
        return state

    def sync_source(self, source_id: str, *, force: bool = False) -> SyncResult:
        """Run one source's connector and push its run to the hub (see the module). ``force`` reads a git source again
        although it is at the commit its last ok run read."""
        source = self.project.source(source_id)
        started_clock = time.monotonic()
        with self._locked(source_id):
            result = SyncResult(source_id, uuid7(), "failed")
            started = utc_now()
            connector, version = source["connector"], None
            try:
                found = self._context(source, result, force)
                if found is not None:
                    connector, version = self._run(*found, result)
            except (ConnectorError, HubError, TooManyItems) as exc:
                if isinstance(exc, HubError) and exc.status == 401:
                    raise
                result.status = "failed"
                result.issues.append(str(exc))
            result.seconds = round(time.monotonic() - started_clock, 3)
            self._audit(result, connector, version, started)
        return result

    def _context(self, source: dict, result: SyncResult, force: bool) -> tuple[ConnectorContext, dict] | None:
        """The context of the connector's run and the hub's state of the source; None when the source reads nothing
        this time (``result`` says why)."""
        source_id = source["id"]
        if self.project.policy.hub_clears(source_id) is False:
            raise ConnectorError(f"the hub sink of project {self.project.name} does not clear source {source_id}")
        env = credential_env(source, self.credential_getter)
        state = self._fresh_state(source_id)
        cursor = (state.get("cursor") or {}).get("cursor")
        self.cursors[source_id] = cursor
        ctx = ConnectorContext(self.project.name, source, self.project.harness, cursor, env, self.plans)
        last_ok = (state.get("last_run") or {}).get("status") == "ok"
        if not force and read_already(last_ok, ctx, result):
            return None
        return ctx, state

    def _run(self, ctx: ConnectorContext, state: dict, result: SyncResult) -> tuple[str, str | None]:
        """Run the connector into a spool and push the run; its connector and version, as its hello names them."""
        source = ctx.source
        recorded = state.get("lineage")
        lineage = source_lineage(source)
        header = {
            "source": source["id"],
            "connector": source["connector"],
            "project": self.project.name,
            "lineage": lineage,
            "epoch": next_epoch((recorded["lineage"], recorded["epoch"]) if recorded else None, lineage, result.run_id),
        }
        with self._spool(source["id"], state) as spool:
            writer = LogWriter(spool, result.run_id, header)
            checker = StreamChecker(source["id"])
            states: list[dict] = []
            read_run(ctx, checker, spool, writer, result, states, self.kill_after)
            path = writer.close()
            if (problem := self._push(spool, path, result.run_id)) is not None:
                result.status = "failed"
                result.issues.append(problem)
            elif states:
                self.cursors[source["id"]] = states[-1]["cursor"]
        hello = checker.hello or {}
        return hello.get("connector", source["connector"]), hello.get("version")

    def accept_removals(self, source_id: str) -> int:
        """Apply the removals the deletion guard held back in the source's scopes, as the hub holds them: a tombstone
        for each held item the hub's latest build still holds alive. How many."""
        self.project.source(source_id)  # a ProjectError for a source the project does not declare
        with self._locked(source_id):
            state = self._fresh_state(source_id)
            held = state.get("held") or []
            if not held:
                return 0
            run_id = uuid7()
            header = {
                "source": source_id,
                "connector": ACCEPT_CONNECTOR,
                "project": self.project.name,
                "epoch": (state.get("lineage") or {}).get("epoch") or BASE_EPOCH,
            }
            with self._spool(source_id, state) as spool:
                writer = LogWriter(spool, run_id, header)
                removed = 0
                for entry in held:
                    try:
                        alive = {record.item_id: record for record in spool.alive_in_scope(entry["scope"])}
                    except TooManyItems as exc:
                        raise HubError(str(exc)) from exc
                    for item_id in entry["ids"]:
                        record = alive.get(item_id)
                        if record is None:
                            continue
                        tombstone = {"type": "tombstone", "id": item_id, "rev": record.rev, "derived": "accepted"}
                        writer.write({**tombstone, "rev_time": record.record.get("rev_time", "")})
                        removed += 1
                scopes = sorted({entry["scope"] for entry in held})
                end = {"type": "run_end", "status": "ok", "removals": removed, "finished_at": utc_now()}
                writer.write({**end, "listed": scopes})
                problem = self._push(spool, writer.close(), run_id)
            if problem is not None:
                raise HubError(problem)
        return removed


def hub_sync(project: Project, **kwargs) -> HubSync:
    """A member's sync of ``project``, signed in to the hub of this machine; a HubError when it is not."""
    from evo_agents.hub.client import load_credentials

    credentials = load_credentials()
    return HubSync(Hub(credentials.url, credentials.token), project, **kwargs)


def cleared_sources(project: Project) -> tuple[list[str], list[str]]:
    """The ids of the project's sources a sync of every source reads, and of those it leaves out: the sources whose
    label the hub sink does not clear, whose runs the hub never takes."""
    ids = [source["id"] for source in project.sources()]
    left = [source_id for source_id in ids if project.policy.hub_clears(source_id) is False]
    return [source_id for source_id in ids if source_id not in left], left


def sync_project(
    syncer: HubSync, sources: list[str] | None = None, *, accept: bool = False, build: bool = False
) -> tuple[list[SyncResult], HubBuild | None]:
    """``kg sync``: each of ``sources`` (every source by default), after applying its held removals with ``accept``;
    then, with ``build``, the build of the project's graph on the hub, waited on."""
    project = syncer.project
    remember(project)
    results = []
    for source_id in sources or cleared_sources(project)[0]:
        if accept:
            syncer.accept_removals(source_id)
        results.append(syncer.sync_source(source_id))
    return results, (syncer.build() if build else None)


def sync_due(syncer: HubSync, *, build: bool = False, accept: bool = False, now=None) -> ProjectSync:
    """``kg sync --due`` of one project: its sources whose refresh is due by the last ok run the hub holds of each;
    with ``build``, a build on the hub if one of them read something."""
    project = syncer.project
    cleared = set(cleared_sources(project)[0])
    due = [source for source in due_sources(project, now=now, last_ok=syncer.last_ok) if source in cleared]
    outcome = ProjectSync(project.name, str(project.harness.root), due)
    if due:
        outcome.runs, _ = sync_project(syncer, due, accept=accept)
        if build and not all(r.unchanged for r in outcome.runs):
            outcome.build = syncer.build()
    return outcome


def sync_all_due(hub: Hub, projects: list[tuple[str, Project | Exception]], **kwargs) -> list[ProjectSync]:
    """``kg sync --due --all``: each of ``projects`` (name, the project or why it did not load) that is on the hub and
    takes a run from the caller, in turn; the others come back skipped, saying why. A project that fails gets the error
    on its outcome and does not stop the others; a credential the hub refuses stops them all."""
    from evo_agents.hub.kg_push import pushable

    listed = {p["name"]: p for p in hub.call("GET", paths.PROJECTS) or ()}
    outcomes = []
    for name, project in projects:
        reason = "not on the hub, or not visible to you" if name not in listed else pushable(listed[name])
        if reason is not None or isinstance(project, Exception):
            root = "" if isinstance(project, Exception) else str(project.harness.root)
            outcome = ProjectSync(name, root, skipped=reason)
            if reason is None:
                outcome.error = f"the project does not load here: {project}"
            outcomes.append(outcome)
            continue
        try:
            outcomes.append(sync_due(HubSync(hub, project, plans=kwargs.get("plans")), **_due_options(kwargs)))
        except HubError as exc:
            if exc.status == 401:
                raise
            outcomes.append(ProjectSync(name, str(project.harness.root), error=str(exc)))
        except Exception as exc:  # isolate projects from each other; the caller reports and exits non-zero
            detail = str(exc) if isinstance(exc, ProjectError) else f"{type(exc).__name__}: {exc}"
            outcomes.append(ProjectSync(name, str(project.harness.root), error=detail))
    return outcomes


def _due_options(kwargs: dict) -> dict:
    return {key: kwargs[key] for key in ("build", "now") if key in kwargs}
