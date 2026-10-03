"""Run connectors into the corpus: log every message, derive deletions safely, merge the state."""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.kg.corpus import Corpus, LogWriter, kg_home
from evo_agents.kg.ids import parse_utc, utc_now, uuid7
from evo_agents.kg.project import Project, ProjectError, load_project_at, read_index, remember
from evo_agents.kg.protocol import StreamChecker
from evo_agents.kg.protocol.runner import ConnectorContext, ConnectorError, ConnectorRun, credential_env

if TYPE_CHECKING:
    from evo_agents.kg.build import BuildReport

DEFAULT_MAX_REMOVAL_RATIO = 0.3
DEFAULT_MIN_SCOPE = 10  # the ratio guard only applies once a scope holds this many items
REFRESH_RE = re.compile(r"([0-9]+)([smhd])")
REFRESH_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# A scheduled run starts about one interval after the last one did, but that run finished later than it
# started. Without this slack an hourly schedule would sync a 1h source only every other hour.
DUE_SLACK_SECONDS = 300


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
    issues: list[str] = field(default_factory=list)
    returncode: int | None = None
    exception: str | None = None
    stderr_tail: list[str] = field(default_factory=list)
    seconds: float = 0.0
    log: str | None = None

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


def _record_run(corpus: Corpus, result: SyncResult, connector: str, version: str | None, started: str) -> None:
    detail = {k: v for k, v in result.to_json().items() if k not in ("source", "run_id", "status")}
    corpus.db.execute(
        "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result.run_id,
            result.source,
            connector,
            version,
            result.status,
            started,
            utc_now(),
            json.dumps(detail, ensure_ascii=False),
        ),
    )


def sync_source(
    project: Project,
    source_id: str,
    *,
    kill_after: int | None = None,
    credential_getter=None,
    corpus: Corpus | None = None,
    env: dict | None = None,
) -> SyncResult:
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
            with corpus.db:
                _record_run(corpus, result, source["connector"], None, started)
            return result

        ctx = ConnectorContext(project.name, source, project.harness, corpus.cursor(source_id), env)
        writer = LogWriter(
            corpus, run_id, {"source": source_id, "connector": source["connector"], "project": project.name}
        )
        checker = StreamChecker(source_id)
        run = ConnectorRun(ctx, kill_after=kill_after)
        cursor, got_cursor = None, False
        try:
            for message in run:
                problems = checker.feed(message)
                if problems:
                    result.rejected += 1
                    result.issues.extend(str(p) for p in problems[:3])
                    continue
                kind = message["type"]
                if kind == "item":
                    writer.write(corpus.to_log_record(message))
                    result.items += 1
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
                    cursor, got_cursor = message["cursor"], True
                else:
                    if kind == "error":
                        result.errors += 1
                    writer.write(message)
        except ConnectorError as exc:
            result.issues.append(str(exc))
        result.returncode = run.returncode
        result.exception = run.exception
        result.stderr_tail = list(run.stderr_tail)[-10:]
        result.issues.extend(str(p) for p in checker.finish())
        if run.killed:
            result.issues.append("connector was killed before it finished")

        complete = checker.complete_run and not result.issues
        held_rows = []
        if complete:
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
                    held_rows.append((run_id, source_id, scope, reason, [r.item_id for r in removed]))
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

        result.status = "ok" if complete else "failed"
        writer.write(
            {
                "type": "run_end",
                "status": result.status,
                "items": result.items,
                "tombstones": result.tombstones,
                "removals": result.removals,
            }
        )
        result.log = str(writer.close())
        hello = checker.hello or {}
        with corpus.db:
            corpus.merge(writer.records, run_id)
            if got_cursor:
                corpus.db.execute(
                    "INSERT OR REPLACE INTO cursors VALUES (?, ?, ?)",
                    (source_id, json.dumps(cursor, ensure_ascii=False), run_id),
                )
            for row in held_rows:
                corpus.db.execute("INSERT OR REPLACE INTO held VALUES (?, ?, ?, ?, ?)", (*row[:4], json.dumps(row[4])))
            result.seconds = round(time.monotonic() - started_clock, 3)
            _record_run(corpus, result, hello.get("connector", source["connector"]), hello.get("version"), started)
    return result


def accept_removals(project: Project, source_id: str, corpus: Corpus | None = None) -> int:
    """Apply deletions a guard held back. Only items still alive and not seen since the held run go."""
    corpus = corpus or project.corpus()
    held = corpus.held(source_id)
    if not held:
        return 0
    with corpus.lock(source_id):
        run_id = uuid7()
        writer = LogWriter(
            corpus, run_id, {"source": source_id, "connector": "evo:accept-removals", "project": project.name}
        )
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
        writer.write({"type": "run_end", "status": "ok", "removals": removed})
        writer.close()
        with corpus.db:
            corpus.merge(writer.records, run_id)
            corpus.db.execute("DELETE FROM held WHERE source = ?", (source_id,))
    return removed


def sync_project(
    project: Project, sources: list[str] | None = None, *, accept: bool = False, **kwargs
) -> list[SyncResult]:
    remember(project)
    corpus = project.corpus()
    wanted = sources or [s["id"] for s in project.sources()]
    results = []
    for source_id in wanted:
        if accept:
            accept_removals(project, source_id, corpus)
        results.append(sync_source(project, source_id, corpus=corpus, **kwargs))
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


def due_sources(project: Project, corpus: Corpus | None = None, now: dt.datetime | None = None) -> list[str]:
    """Sources that declare ``refresh`` and either never had an ok run or whose last ok run finished at least
    ``refresh`` ago, less a slack of DUE_SLACK_SECONDS (at most a tenth of the interval). A source without
    ``refresh`` is never due; a failed run does not reset the clock."""
    corpus = corpus or project.corpus()
    now = now or dt.datetime.now(dt.timezone.utc)
    due = []
    for src in project.sources():
        if src.get("refresh") is None:
            continue
        try:
            interval = parse_refresh(src["refresh"])
        except ValueError as exc:
            raise ProjectError(f"project {project.name!r}, source {src['id']!r}: {exc}") from exc
        last = corpus.runs(src["id"], limit=1, status="ok")
        if not last or not last[0]["finished_at"]:
            due.append(src["id"])
            continue
        age = (now - parse_utc(last[0]["finished_at"])).total_seconds()
        if age >= interval - min(DUE_SLACK_SECONDS, interval // 10):
            due.append(src["id"])
    return due


@dataclass
class ProjectSync:
    """What ``kg sync --due`` did for one project."""

    project: str
    harness: str
    due: list[str] = field(default_factory=list)
    runs: list[SyncResult] = field(default_factory=list)
    build: BuildReport | None = None
    error: str | None = None

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
        }


def sync_due(project: Project, *, build: bool = False, now: dt.datetime | None = None, **kwargs) -> ProjectSync:
    """Sync the sources of one project that are due. With ``build``, build its graph only if one of them ran."""
    corpus = project.corpus()
    try:
        due = due_sources(project, corpus, now)
    finally:
        corpus.close()
    outcome = ProjectSync(project.name, str(project.harness.root), due)
    if due:
        outcome.runs = sync_project(project, due, **kwargs)
        if build:
            from evo_agents.kg.build import build_project

            outcome.build = build_project(project)
    return outcome


def indexed_projects(home: Path | None = None) -> list[tuple[str, Path]]:
    """Projects in projects.json whose harness root still exists, by name."""
    found = []
    for name, entry in sorted(read_index(home).items()):
        root = entry.get("harness_root") if isinstance(entry, dict) else None
        if root and Path(root).expanduser().is_dir():
            found.append((name, Path(root).expanduser()))
    return found


def sync_all_due(
    home: Path | None = None, *, build: bool = False, now: dt.datetime | None = None, **kwargs
) -> list[ProjectSync]:
    """``kg sync --due --all``: each indexed project in turn. A project that fails, from loading its harness
    to building its graph, gets the error on its outcome and does not stop the others."""
    home = home or kg_home()
    outcomes = []
    for name, root in indexed_projects(home):
        try:
            outcome = sync_due(load_project_at(root, home), build=build, now=now, **kwargs)
        except Exception as exc:  # isolate projects from each other; the caller reports and exits non-zero
            detail = str(exc) if isinstance(exc, ProjectError) else f"{type(exc).__name__}: {exc}"
            outcome = ProjectSync(name, str(root), error=detail)
        outcomes.append(outcome)
    return outcomes
