"""Run connectors into the corpus: log every message, derive deletions safely, merge the state."""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from evo_agents import __version__
from evo_agents.kg import audit
from evo_agents.kg.corpus import Corpus, ItemRecord, LogWriter, run_epoch
from evo_agents.kg.ids import parse_utc, utc_now, uuid7
from evo_agents.kg.project import Project, ProjectError, read_index, remember
from evo_agents.kg.protocol import StreamChecker, sha256
from evo_agents.kg.protocol.runner import ConnectorContext, ConnectorError, ConnectorRun, credential_env

DEFAULT_MAX_REMOVAL_RATIO = 0.3
DEFAULT_MIN_SCOPE = 10  # the ratio guard only applies once a scope holds this many items
REFRESH_RE = re.compile(r"([0-9]+)([smhd])")
REFRESH_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# A scheduled run starts about one interval after the last one did, but that run finished later than it
# started. Without this slack an hourly schedule would sync a 1h source only every other hour.
DUE_SLACK_SECONDS = 300
# A kind of secret a connector redacted from an item (props.redactions): only these names and positive counts go into
# the run's result and audit line, whatever else a connector puts there.
REDACTION_KIND = re.compile(r"[a-z][a-z0-9_]{0,39}")
# Keys of a source that say how or when it runs, what it may delete, how it is labelled, or which of the files it
# reads become items, not where it reads from. Every other key (a git source's repo, path or ref, an exec source's
# command, a connector's config) is part of its lineage, so changing one starts a new epoch (evo_agents.kg.corpus).
NOT_LINEAGE = frozenset(
    {
        "id",
        "label",
        "refresh",
        "deletion",
        "credentials",
        "timeout",
        "fetch",
        "pulls",
        "include",
        "exclude",
        "allow",
        "assets",
        "code",
        "branch",
    }
)


@dataclass
class SyncResult:
    source: str
    run_id: str
    status: str  # ok | failed
    items: int = 0
    tombstones: int = 0
    errors: int = 0
    rejected: int = 0
    removals: int = 0
    held: list[dict] = field(default_factory=list)
    redactions: dict[str, dict[str, int]] = field(default_factory=dict)  # item id -> secrets redacted, by kind
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    returncode: int | None = None
    exception: str | None = None
    stderr_tail: list[str] = field(default_factory=list)
    seconds: float = 0.0
    log: str | None = None
    unchanged: bool = False  # a git source at the commit and config its last ok run read: nothing read, no log

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_json(self) -> dict:
        return asdict(self)


def _guard(source: dict, alive: int, removed: int, listed: int) -> str | None:
    deletion = source.get("deletion") or {}
    ratio = float(deletion.get("max_removal_ratio", DEFAULT_MAX_REMOVAL_RATIO))
    min_scope = int(deletion.get("min_scope", DEFAULT_MIN_SCOPE))
    if listed == 0 and alive > 0:
        return f"empty listing would remove all {alive} item(s)"
    if removed and alive >= min_scope and removed / alive > ratio:
        return f"would remove {removed} of {alive} item(s), above max_removal_ratio {ratio}"
    return None


def count_item(result: SyncResult, item: dict) -> None:
    """Count an item into the run, with the secrets its connector redacted from it, by kind (``props.redactions``)."""
    result.items += 1
    redactions = (item.get("props") or {}).get("redactions")
    if not isinstance(redactions, dict):
        return
    counts = {
        kind: n
        for kind, n in redactions.items()
        if isinstance(kind, str) and REDACTION_KIND.fullmatch(kind) and type(n) is int and n > 0
    }
    if counts:
        result.redactions[item["id"]] = counts


def source_lineage(source: dict) -> str:
    """Digest of where ``source`` reads from: its config without the keys in NOT_LINEAGE. Run logs hold only the
    digest, so the config itself still never leaves the machine."""
    where = {key: value for key, value in source.items() if key not in NOT_LINEAGE}
    return sha256(json.dumps(where, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str))


def _open_log(project: Project, corpus: Corpus, source: dict, run_id: str) -> LogWriter:
    """Start the log of run ``run_id`` of ``source``; its header names the source's lineage and the run's epoch."""
    lineage = source_lineage(source)
    header = {
        "source": source["id"],
        "connector": source["connector"],
        "project": project.name,
        "lineage": lineage,
        "epoch": corpus.epoch_for(source["id"], lineage, run_id),
    }
    return LogWriter(corpus, run_id, header)


def _merge_run(corpus: Corpus, writer: LogWriter) -> None:
    """Merge the records of a sync into the state and record the lineage it read, in the caller's transaction."""
    header = writer.header
    corpus.merge(writer.records, header["run_id"])
    corpus.record_lineage(header["source"], header["lineage"], header["epoch"], header["run_id"])


def _record_run(corpus: Corpus, result: SyncResult, connector: str, version: str | None, started: str) -> str:
    """Insert the run's row; return its finished_at, which the audit line repeats."""
    detail = {k: v for k, v in result.to_json().items() if k not in ("source", "run_id", "status")}
    finished = utc_now()
    corpus.record_run(
        result.run_id,
        source=result.source,
        connector=connector,
        status=result.status,
        started_at=started,
        finished_at=finished,
        version=version,
        detail=detail,
    )
    return finished


def _finish(project: Project, corpus: Corpus, result: SyncResult, connector: str, version, started: str, then=None):
    """Commit the run: ``then()``, what the caller writes with it, and its row in the runs table, in one
    transaction; then its line in the audit log."""
    with corpus.transaction():
        if then is not None:
            then()
        finished = _record_run(corpus, result, connector, version, started)
    audit.record_sync(
        corpus.root,
        project.name,
        result,
        connector=connector,
        connector_version=version,
        started=started,
        finished=finished,
    )


def read_already(last_ok: bool, ctx: ConnectorContext, result: SyncResult) -> bool:
    """Whether a git source whose last run was ok (``last_ok``) would read the commit and config its cursor names
    (``connectors.git.read_already``). Such a sync reads nothing: no log, no change to the state or the cursor, and
    ``result`` is ok and ``unchanged``. When it fetched and found something new, the run that follows does not fetch
    again."""
    if ctx.source.get("connector") != "git" or not last_ok:
        return False
    from evo_agents.kg.connectors import git

    same, warnings = git.read_already(ctx)
    result.warnings.extend(warnings)
    if same:
        result.status, result.unchanged = "ok", True
    elif ctx.source.get("fetch"):
        ctx.source = {**ctx.source, "fetch": False}  # fetched just now; fetch is not part of the lineage
    return same


class Known(Protocol):
    """What a sync reads of the items a source holds already: a corpus here, or the state the hub holds of the source
    (``evo_agents.hub.kg_hub_sync``)."""

    def to_log_record(self, message: dict) -> dict: ...

    def get(self, item_id: str) -> ItemRecord | None: ...

    def alive_in_scope(self, scope: str) -> list[ItemRecord]: ...


def _read(run: ConnectorRun, checker: StreamChecker, corpus: Known, writer: LogWriter, result: SyncResult, states):
    """Log every message of the run and count it into ``result``; append each state message to ``states``."""
    for message in run:
        problems = checker.feed(message)
        if problems:
            result.rejected += 1
            result.issues.extend(str(p) for p in problems[:3])
            continue
        kind = message["type"]
        if kind == "item":
            writer.write(corpus.to_log_record(message))
            count_item(result, message)
        elif kind == "tombstone":
            record = dict(message)
            known = corpus.get(record["id"])
            if known is not None:
                record.setdefault("rev", known.rev)
                if known.record.get("rev_time"):
                    record.setdefault("rev_time", known.record["rev_time"])
            writer.write(record)
            result.tombstones += 1
        elif kind == "state":
            writer.sync()  # every item before this cursor is durable before the cursor counts
            writer.write(message)
            states.append(message)
        else:
            if kind == "error":
                result.errors += 1
            elif kind == "log" and message["level"] in ("warning", "error"):
                result.warnings.append(message["message"])
            writer.write(message)


def _removals(corpus: Known, source: dict, checker: StreamChecker, writer: LogWriter, result: SyncResult) -> list:
    """Tombstones for the items a complete listing no longer names, written to the log, unless the deletion guard
    holds them back: the rows of ``held`` for those."""
    held_rows = []
    for listing in checker.listings:
        if not listing.get("complete"):
            continue
        scope = listing["scope"]
        alive = corpus.alive_in_scope(scope)
        removed = [r for r in alive if r.item_id not in checker.seen and r.item_id not in checker.error_ids]
        if not removed:
            continue
        reason = _guard(source, len(alive), len(removed), listing["count"])
        if reason:
            held_rows.append((result.run_id, result.source, scope, reason, [r.item_id for r in removed]))
            result.held.append({"scope": scope, "reason": reason, "count": len(removed)})
            continue
        for r in removed:
            writer.write(
                {
                    "type": "tombstone",
                    "id": r.item_id,
                    "rev": r.rev,
                    "rev_time": r.record.get("rev_time", ""),
                    "derived": "listing",
                }
            )
            result.removals += 1
    return held_rows


def _listed(checker: StreamChecker) -> list[str]:
    """The scopes a run listed completely, each of which it worked out the removals of."""
    return sorted({listing["scope"] for listing in checker.listings if listing.get("complete")})


def read_run(
    ctx: ConnectorContext,
    checker: StreamChecker,
    corpus: Known,
    writer: LogWriter,
    result: SyncResult,
    states: list[dict],
    kill_after: int | None = None,
) -> list:
    """Run the connector of ``ctx`` into ``writer``'s log, its removals and its run_end included, and count it into
    ``result``, whose status it sets; append each state message to ``states``. Returns the rows of the removals the
    deletion guard held back, as ``_removals``."""
    run = ConnectorRun(ctx, kill_after=kill_after)
    try:
        _read(run, checker, corpus, writer, result, states)
    except ConnectorError as exc:
        result.issues.append(str(exc))
    result.returncode = run.returncode
    result.exception = run.exception
    result.stderr_tail = list(run.stderr_tail)[-10:]
    result.issues.extend(str(p) for p in checker.finish())
    if run.killed:
        result.issues.append("connector was killed before it finished")

    complete = checker.complete_run and not result.issues
    held_rows = _removals(corpus, ctx.source, checker, writer, result) if complete else []
    result.status = "ok" if complete else "failed"
    writer.write(
        {
            "type": "run_end",
            "status": result.status,
            "items": result.items,
            "tombstones": result.tombstones,
            "removals": result.removals,
            "finished_at": utc_now(),  # the hub keeps it (kg_ingests.run_finished_at), which due_sources reads
            # the hub keeps them too (kg_ingests.held, .listed), which kg sync --accept-removals reads from it: the
            # hold of a scope is that of the newest ok run that listed it completely
            **({"listed": listed} if (listed := _listed(checker) if complete else []) else {}),
            **({"held": [{"scope": r[2], "reason": r[3], "ids": r[4]} for r in held_rows]} if held_rows else {}),
        }
    )
    return held_rows


def sync_source(
    project: Project,
    source_id: str,
    *,
    kill_after: int | None = None,
    credential_getter=None,
    corpus: Corpus | None = None,
    env: dict | None = None,
    plans=None,
    force: bool = False,
) -> SyncResult:
    """Run one source's connector into the corpus. The run gets a row in the runs table and a line in the
    project's audit log, once its state is committed. A git source whose commit and config are those its last ok
    run read is not read again (``read_already``), unless ``force``: the worker's sync reads it again when the runs
    of other machines it merged changed what the source holds (``evo_agents.worker.kg_sync``). ``plans`` reads the
    plans of the hub for a harness source (``ConnectorContext.plans``)."""
    source = project.source(source_id)
    corpus = corpus or project.corpus()
    started_clock = time.monotonic()
    with corpus.lock(source_id):
        run_id = uuid7()
        started = utc_now()
        result = SyncResult(source_id, run_id, "failed")
        try:
            if env is None:
                env = credential_env(source, credential_getter)
        except ConnectorError as exc:
            result.issues.append(str(exc))
            _finish(project, corpus, result, source["connector"], None, started)
            return result

        ctx = ConnectorContext(project.name, source, project.harness, corpus.cursor(source_id), env, plans)
        last = corpus.runs(source_id, limit=1)
        if not force and read_already(bool(last) and last[0]["status"] == "ok", ctx, result):
            result.seconds = round(time.monotonic() - started_clock, 3)
            _finish(project, corpus, result, source["connector"], __version__, started)
            return result
        writer = _open_log(project, corpus, source, run_id)
        checker = StreamChecker(source_id)
        states: list[dict] = []
        held_rows = read_run(ctx, checker, corpus, writer, result, states, kill_after)
        result.log = str(writer.close())
        hello = checker.hello or {}
        connector, version = hello.get("connector", source["connector"]), hello.get("version")

        def commit_state() -> None:
            _merge_run(corpus, writer)
            if states:
                corpus.set_cursor(source_id, states[-1]["cursor"], run_id)
            for row in held_rows:
                corpus.hold(*row)
            result.seconds = round(time.monotonic() - started_clock, 3)

        _finish(project, corpus, result, connector, version, started, commit_state)
    return result


def merge_pulled(corpus: Corpus, path: Path) -> int:
    """Merge the log at ``path``, of a run that another machine wrote and the hub holds, into ``corpus``, which keeps
    it in its log: its records with the run's epoch, as the hub's build merges them, and its lineage when its epoch is
    above the recorded one (``Corpus.adopt_lineage``). Its cursor is not taken: a source's cursor stays that of the
    last run read here, so whether the source moved is still asked of the commit read here. Returns how many items
    changed; 0 for a file that is not the log of a run."""
    run_id, records = corpus.run_records(path)
    if not run_id:
        return 0
    header = records[0]
    with corpus.transaction():
        changed = corpus.merge(records, run_id)
        lineage, epoch = header.get("lineage"), run_epoch(header)
        if isinstance(header.get("source"), str) and isinstance(lineage, str) and epoch:
            corpus.adopt_lineage(header["source"], lineage, epoch, run_id)
    return changed


def accept_removals(project: Project, source_id: str, corpus: Corpus | None = None) -> int:
    """Apply deletions a guard held back. Only items still alive and not seen since the held run go. The tombstones
    take the source's current epoch, which no record of the source exceeds."""
    corpus = corpus or project.corpus()
    held = corpus.held(source_id)
    if not held:
        return 0
    with corpus.lock(source_id):
        run_id = uuid7()
        header = {
            "source": source_id,
            "connector": "evo:accept-removals",
            "project": project.name,
            "epoch": corpus.current_epoch(source_id),
        }
        writer = LogWriter(corpus, run_id, header)
        removed = 0
        for entry in held:
            for item_id in entry["ids"]:
                record = corpus.get(item_id)
                if record is None or record.deleted or record.run_id > entry["run_id"]:
                    continue
                writer.write(
                    {
                        "type": "tombstone",
                        "id": item_id,
                        "rev": record.rev,
                        "rev_time": record.record.get("rev_time", ""),
                        "derived": "accepted",
                    }
                )
                removed += 1
        writer.write({"type": "run_end", "status": "ok", "removals": removed, "finished_at": utc_now()})
        writer.close()
        with corpus.transaction():
            corpus.merge(writer.records, run_id)
            corpus.drop_held(source_id)
    return removed


def sync_project(
    project: Project, sources: list[str] | None = None, *, accept: bool = False, **kwargs
) -> list[SyncResult]:
    remember(project)
    corpus = project.corpus()
    wanted = sources or [s["id"] for s in project.sources()]
    results = []
    try:
        for source_id in wanted:
            if accept:
                accept_removals(project, source_id, corpus)
            results.append(sync_source(project, source_id, corpus=corpus, **kwargs))
    finally:
        corpus.close()  # a connection left open waits for the cycle collector, and holds the file on Windows
    return results


def log_dir(project: Project) -> Path:
    return project.root / "log"


# -- refresh intervals: kg sync --due [--all] ---------------------------------------------------------


def parse_refresh(value) -> int:
    """A source's ``refresh`` in seconds: a positive number and a unit s, m, h or d (``30m``, ``1h``, ``1d``)."""
    match = REFRESH_RE.fullmatch(value) if isinstance(value, str) else None
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"refresh {value!r} is not a positive number followed by s, m, h or d, like 30m or 6h")
    return int(match.group(1)) * REFRESH_UNITS[match.group(2)]


def last_ok_here(corpus: Corpus) -> Callable[[str], str | None]:
    """When the last ok run of a source finished, as the corpus here records it."""

    def finished(source: str) -> str | None:
        last = corpus.runs(source, limit=1, status="ok")
        return last[0]["finished_at"] if last else None

    return finished


def due_sources(
    project: Project, corpus: Corpus | None = None, now: dt.datetime | None = None, *, last_ok=None
) -> list[str]:
    """Sources that declare ``refresh`` and either never had an ok run or whose last ok run finished at least
    ``refresh`` ago, less a slack of DUE_SLACK_SECONDS (at most a tenth of the interval). A source without
    ``refresh`` is never due; a failed run does not reset the clock. ``last_ok(source)`` says when the last ok run of
    a source finished, None when there is none: the hub's state of the source for ``kg sync --due``
    (``evo_agents.hub.kg_hub_sync``), ``corpus`` by default."""
    if last_ok is None:
        last_ok = last_ok_here(corpus or project.corpus())
    now = now or dt.datetime.now(dt.UTC)
    due = []
    for src in project.sources():
        if src.get("refresh") is None:
            continue
        try:
            interval = parse_refresh(src["refresh"])
        except ValueError as exc:
            raise ProjectError(f"project {project.name!r}, source {src['id']!r}: {exc}") from exc
        finished = last_ok(src["id"])
        if not finished:
            due.append(src["id"])
            continue
        age = (now - parse_utc(finished)).total_seconds()
        if age >= interval - min(DUE_SLACK_SECONDS, interval // 10):
            due.append(src["id"])
    return due


@dataclass
class ProjectSync:
    """What ``kg sync --due`` did for one project (``evo_agents.hub.kg_hub_sync.sync_due``)."""

    project: str
    harness: str
    due: list[str] = field(default_factory=list)
    runs: list[SyncResult] = field(default_factory=list)
    build: HubBuild | None = None
    error: str | None = None
    skipped: str | None = None  # why the project was not synced: the hub takes no run of it from the caller

    @property
    def ok(self) -> bool:
        if self.error or any(not r.ok or r.held for r in self.runs):
            return False
        return self.build is None or self.build.ok

    def to_json(self) -> dict:
        return {
            "project": self.project,
            "harness": self.harness,
            "ok": self.ok,
            "due": self.due,
            "runs": [r.to_json() for r in self.runs],
            "build": self.build.to_json() if self.build is not None else None,
            "error": self.error,
            "skipped": self.skipped,
        }


@dataclass
class HubBuild:
    """The build of a project's graph on the hub that ``kg sync --build`` asked for and waited on, as the hub answers it
    (GET /v1/kg/{project}/builds/{id}); ``error`` when asking or waiting failed."""

    build: dict | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.build is not None and self.build.get("status") == "succeeded"

    def to_json(self) -> dict:
        return {"ok": self.ok, "build": self.build, "error": self.error}

    def summary_line(self) -> str:
        if self.build is None:
            return f"FAIL build on the hub: {self.error}"
        line = f"build {self.build.get('id')} on the hub: {self.build.get('status')}"
        failed = self.error or self.build.get("error")
        return f"FAIL {line}: {failed}" if not self.ok and failed else line


def indexed_projects(home: Path | None = None) -> list[tuple[str, Path]]:
    """Projects in projects.json whose harness root still exists, by name."""
    found = []
    for name, entry in sorted(read_index(home).items()):
        root = entry.get("harness_root") if isinstance(entry, dict) else None
        if root and Path(root).expanduser().is_dir():
            found.append((name, Path(root).expanduser()))
    return found
