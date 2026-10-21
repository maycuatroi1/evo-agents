"""Retention of a kg home: what this machine keeps of old run logs, of the history of its graph stores and of memo
rows no key uses any more, and how it deletes the rest (``evo-agents kg prune``, ``evo_agents.kg.cli_prune``).

Three periods, each ``0`` or a number with a unit m, h, d or w (``parse_period``), say what is old: ``logs``,
``history`` and ``memo`` (DEFAULTS). What a pass deletes in a project directory ``<kg home>/<project>/``:

- Run logs, as ``kg scrub --purge`` treats them (``evo_agents.kg.scrub``). A finished log of a run that started before
  the logs period is deleted when no record of it is the one the merged state (``corpus.sqlite``) holds, and rewritten
  without the records the merged state does not hold when some are (``scrub.rewrite_log``): the logs left still replay
  to the merged state. A log the hub has not received is never touched. A run counts as received when ``hub kg push``
  recorded that the hub holds it (``Corpus.record_on_hub``), and goes to no hub from here when its source's label is
  above the project's hub sink, or when the project declares no hub sink and no push recorded one of its runs. A
  project whose knowledge config does not load keeps every log no push recorded.
- Blobs older than the logs period that no log left and no item of the merged state refers to.
- Rows of the runs table of runs older than the logs period whose log is gone, or never was (a sync that found its
  git source unchanged writes none), but the newest run and the newest ok run of each source, which ``kg sync --due``
  and ``kg status`` read, and the runs whose records the merged state holds; and the receipts of runs whose log is
  gone.
- History of ``graph.sqlite``: the builds that finished before the history period, but the latest two, and the rows
  only they read (``Store.history_floor``).
- Memo rows no key used since the memo period nor in the latest build that recorded its uses
  (``evo_agents.kg.memo.unused``): a project whose corpus stays the same keeps the rows of its latest build.

None of this changes what a build makes. A build reads the merged state, the blobs its items refer to, the rows of the
latest build and memo rows, which hold what a task returns; so ``kg build --verify --cold`` gives the hash it gave
before, and the next build the graph it would have made.

``plan`` only counts. ``apply`` deletes what a plan found, then compacts each SQLite file whose free pages are at
least FREE_SHARE of it, and appends a ``prune`` line with the counts to the project's audit log; run both holding
every source's lock (``scrub.holding_sources``), so no sync writes a log or a blob meanwhile. The schedule: with
``scheduled`` on in ``<kg home>/retention.json`` (``kg prune --schedule on``, off until the owner turns it on),
``kg sync --due --all``, which the LaunchAgent runs, prunes every project whose last prune is a day old
(``prune_scheduled``).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents import __version__
from evo_agents.kg import audit
from evo_agents.kg import memo as memo_module
from evo_agents.kg.corpus import Corpus, kg_home, run_epoch
from evo_agents.kg.ids import parse_utc, utc_now, utc_text, uuid7_time
from evo_agents.kg.policy import Policy
from evo_agents.kg.project import load_project_at, read_index
from evo_agents.kg.scrub import (
    RUN,
    ScrubBusy,
    blob_files,
    blob_refs,
    compact,
    holding_sources,
    log_files,
    project_dirs,
    rewrite_log,
    winners_of,
    won,
)
from evo_agents.kg.store import StaleSchema, Store

PERIODS = ("logs", "history", "memo")
# Nothing reads a superseded run log the hub holds, nor a build older than the latest two: a day, to look at what a
# recent run read or a recent build held. Memo rows are kept longer: a branch switched back to finds its rows.
DEFAULTS = {"logs": "1d", "history": "1d", "memo": "14d"}
PERIOD = re.compile(r"([0-9]{1,6})([mhdw]?)")
UNITS = {"": 0, "m": 60, "h": 3600, "d": 86400, "w": 604800}
CONFIG_FILE = "retention.json"
CONFIG_VERSION = 1
EVERY = dt.timedelta(days=1)  # a scheduled pass prunes a project at most once in this long
FREE_SHARE = 0.1  # a SQLite file is compacted once this share of its pages is free
# Why a run log stays, and what happens to the others.
WINDOW, HUB, IN_USE, WRITING = "window", "hub", "in_use", "writing"
REWRITE, DELETE = "rewrite", "delete"
KEPT = (WINDOW, HUB, IN_USE, WRITING)


class RetentionError(ValueError):
    """A period, or a retention config, that cannot be read."""


def parse_period(value) -> dt.timedelta:
    """A retention period: ``0``, or a positive number with a unit m, h, d or w (``12h``, ``3d``, ``2w``)."""
    match = PERIOD.fullmatch(value.strip()) if isinstance(value, str) else None
    if not match or (not match[2] and int(match[1]) != 0):
        raise RetentionError(f"period {value!r} is not 0 or a number followed by m, h, d or w, like 12h or 3d")
    return dt.timedelta(seconds=int(match[1]) * UNITS[match[2]])


# The config


@dataclass
class Config:
    scheduled: bool = False
    periods: dict[str, str] = field(default_factory=lambda: dict(DEFAULTS))
    changed_at: str | None = None

    def to_json(self) -> dict:
        return {
            "v": CONFIG_VERSION,
            "scheduled": self.scheduled,
            "periods": self.periods,
            "changed_at": self.changed_at,
        }


def config_path(home: Path | None = None) -> Path:
    return (home or kg_home()) / CONFIG_FILE


def checked_periods(periods: dict) -> dict[str, str]:
    """``periods`` over DEFAULTS, each read once so a bad one raises RetentionError here."""
    out = {**DEFAULTS, **{name: value for name, value in periods.items() if name in PERIODS and value is not None}}
    for value in out.values():
        parse_period(value)
    return out


def load_config(home: Path | None = None) -> Config:
    """The retention config of the kg home; the defaults, scheduled off, when it has none."""
    path = config_path(home)
    if not path.exists():
        return Config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RetentionError(f"{path} cannot be read: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("periods", {}), dict):
        raise RetentionError(f"{path} is not a retention config")
    return Config(data.get("scheduled") is True, checked_periods(data.get("periods", {})), data.get("changed_at"))


def save_config(config: Config, home: Path | None = None) -> Path:
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config.to_json(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


# What a pass finds


@dataclass
class Prune:
    """What a pass found in one project directory, and once ``apply`` ran, what it deleted."""

    project: str
    root: Path
    periods: dict[str, str]
    logs: list[str] = field(default_factory=list)  # file names of the run logs to delete
    log_bytes: int = 0
    rewrite: dict[str, int] = field(default_factory=dict)  # run logs in use to rewrite -> the records they lose
    kept: Counter = field(default_factory=Counter)  # run logs that stay, by why (KEPT)
    runs: list[str] = field(default_factory=list)  # ids of the rows of the runs table to delete
    blobs: list[str] = field(default_factory=list)  # hashes of the blobs to delete
    blob_bytes: int = 0
    blobs_held: int = 0
    history_floor: int | None = None  # the oldest build of graph.sqlite that stays
    history: dict[str, int] = field(default_factory=dict)  # table -> rows to delete; "builds" -> builds to delete
    memo: int = 0  # memo rows to delete
    memo_rows: int = 0
    memo_cut: str | None = None  # a memo row with no use since then is deleted; None: no build recorded its uses
    size: int = 0  # bytes of the project directory
    busy: str | None = None  # a sync held a source: nothing was read or deleted, the next pass tries again
    error: str | None = None  # the pass failed in this project
    deleted: dict | None = None
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "logs": len(self.logs),
            "log_bytes": self.log_bytes,
            "rewrite": len(self.rewrite),
            "dropped": sum(self.rewrite.values()),
            "runs": len(self.runs),
            "blobs": len(self.blobs),
            "blob_bytes": self.blob_bytes,
            "history": sum(n for table, n in self.history.items() if table != "builds"),
            "builds": self.history.get("builds", 0),
            "memo": self.memo,
        }

    @property
    def empty(self) -> bool:
        return not any(self.counts().values())

    def to_json(self) -> dict:
        return {
            "project": self.project,
            "root": str(self.root),
            "periods": self.periods,
            "counts": self.counts(),
            "kept_logs": {reason: self.kept[reason] for reason in KEPT},
            "blobs_held": self.blobs_held,
            "memo": {"rows": self.memo_rows, "tracked": self.memo_cut is not None},
            "size": self.size,
            "busy": self.busy,
            "error": self.error,
            "deleted": self.deleted,
            "warnings": self.warnings,
        }


def dir_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        with contextlib.suppress(OSError):
            if path.is_file():
                total += path.stat().st_size
    return total


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


# Run logs, blobs and runs rows


@dataclass
class _Corpus:
    """What the merged state says about the run logs of a project, read once."""

    winners: dict  # item id -> the order key of the record the merged state holds
    on_hub: set[str]  # runs a push recorded the hub holds
    goes: Callable[[str | None], bool]  # whether a run of a source goes to a hub from here
    in_use: set[str] = field(init=False)  # runs a record of which the merged state holds

    def __post_init__(self) -> None:
        self.in_use = {key[RUN] for key in self.winners.values()}


def _runs_on_hub(root: Path) -> set[str]:
    path = root / "corpus.sqlite"
    if not path.exists():
        return set()
    db = _read_only(path)
    try:
        return {row[0] for row in db.execute("SELECT run_id FROM hub_runs")}
    except sqlite3.OperationalError:  # a corpus no push of this version has recorded into
        return set()
    finally:
        db.close()


def goes_to_hub(policy: Policy | None, pushed: bool) -> Callable[[str | None], bool]:
    """Whether a run of a source goes to a hub from this machine, so its log waits until the hub has it: unless the
    hub sink does not clear the source; for a project with no hub sink, only once a push recorded one of its runs; for
    a project whose policy is unknown (None), always."""
    if policy is None:
        return lambda source: True
    if policy.hub_sink() is None:
        return lambda source: pushed
    return lambda source: not isinstance(source, str) or policy.hub_clears(source) is not False


def _run_time(run_id: str, path: Path) -> dt.datetime:
    """When a run started: the time its id holds, else the time its log was last written."""
    found = uuid7_time(run_id)
    return found or dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC)


def _refs(path: Path) -> set[str]:
    return {ref for record in Corpus.read_log(path) if record.get("type") == "item" for ref in blob_refs(record)}


def _source(path: Path) -> str | None:
    with contextlib.closing(Corpus.read_log(path)) as records:
        header = next(records, {})
    source = header.get("source") if header.get("type") == "run" else None
    return source if isinstance(source, str) else None


def _won_refs(path: Path, winners: dict) -> tuple[set[str], int]:
    """The blobs the records of the log at ``path`` that the merged state holds refer to, and how many item and
    tombstone records it holds that the merged state does not (as ``scrub.rewrite_log`` reads them)."""
    run_id, epoch, refs, lost = None, "", set(), 0
    for record in Corpus.read_log(path):
        kind = record.get("type")
        if kind == "run":
            epoch = run_epoch(record)
            run_id = run_id or record.get("run_id")
        if kind not in ("item", "tombstone"):
            continue
        if won(record, run_id, epoch, winners):
            refs.update(blob_refs(record) if kind == "item" else ())
        else:
            lost += 1
    return refs, lost


def _judge(path: Path, cutoff: dt.datetime, state: _Corpus) -> tuple[str, set[str], int]:
    """What becomes of the run log at ``path``: why it stays (KEPT), or REWRITE or DELETE; the blobs it refers to
    once that is done, and the records a rewrite drops."""
    if path.suffix == ".jsonl":
        return WRITING, _refs(path), 0
    run_id = path.name.split(".", 1)[0]
    if _run_time(run_id, path) >= cutoff:
        return WINDOW, _refs(path), 0
    if run_id not in state.on_hub and state.goes(_source(path)):
        return HUB, _refs(path), 0
    if run_id not in state.in_use:
        return DELETE, set(), 0
    refs, lost = _won_refs(path, state.winners)
    return (REWRITE if lost else IN_USE), refs, lost


def _plan_logs(found: Prune, cutoff: dt.datetime, policy: Policy | None) -> set[str]:
    """Judge every run log of the project; the blobs the logs left and the merged state refer to."""
    winners, live = winners_of(found.root)
    on_hub = _runs_on_hub(found.root)
    state = _Corpus(winners, on_hub, goes_to_hub(policy, bool(on_hub)))
    used = set(live)
    for path in log_files(found.root):
        fate, refs, lost = _judge(path, cutoff, state)
        used |= refs
        if fate == DELETE:
            found.logs.append(path.name)
            found.log_bytes += path.stat().st_size
        elif fate == REWRITE:
            found.rewrite[path.name] = lost
        else:
            found.kept[fate] += 1
    _plan_runs(found, cutoff, state.in_use)
    return used


def _plan_runs(found: Prune, cutoff: dt.datetime, in_use: set[str]) -> None:
    path = found.root / "corpus.sqlite"
    if not path.exists():
        return
    gone = {name.split(".", 1)[0] for name in found.logs}
    logs = {p.name.split(".", 1)[0] for p in log_files(found.root)} - gone
    db = _read_only(path)
    try:
        rows = db.execute("SELECT run_id, source, status, started_at FROM runs ORDER BY run_id DESC").fetchall()
    finally:
        db.close()
    newest: set[str] = set()
    for status in (None, "ok"):  # the newest run, and the newest ok run, of each source
        sources: set[str] = set()
        for run_id, source, run_status, _ in rows:
            if source not in sources and status in (None, run_status):
                sources.add(source)
                newest.add(run_id)
    for run_id, _, _, started in rows:
        if run_id in newest or run_id in in_use or run_id in logs:
            continue
        when = uuid7_time(run_id) or _started(started)
        if when is not None and when < cutoff:
            found.runs.append(run_id)


def _started(value) -> dt.datetime | None:
    try:
        return parse_utc(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _plan_blobs(found: Prune, used: set[str], cutoff: dt.datetime) -> None:
    before = cutoff.timestamp()
    for sha, path in blob_files(found.root):
        if sha in used:
            found.blobs_held += 1
            continue
        stat = path.stat()
        if stat.st_mtime >= before:  # written within the period: a sync may be about to name it
            found.blobs_held += 1
            continue
        found.blobs.append(sha)
        found.blob_bytes += stat.st_size


# Graph history and memo


def _plan_history(found: Prune, cutoff: str) -> None:
    path = found.root / "graph.sqlite"
    if not path.exists():
        return
    try:
        store = Store.open_file(path)
    except StaleSchema:
        found.warnings.append("graph.sqlite has an older schema: kg build rebuilds it whole; its history stays")
        return
    except sqlite3.DatabaseError as exc:
        found.warnings.append(f"graph.sqlite was not read: {type(exc).__name__}")
        return
    try:
        found.history_floor = store.history_floor(cutoff)
        if found.history_floor is not None:
            found.history = store.history(found.history_floor)
    finally:
        store.close()


def _plan_memo(found: Prune, cutoff: str) -> None:
    try:
        found.memo, found.memo_rows, found.memo_cut = memo_module.unused(found.root / "memo.sqlite", cutoff)
    except sqlite3.DatabaseError as exc:
        found.warnings.append(f"memo.sqlite was not read: {type(exc).__name__}")


def plan(
    name: str, root: Path, periods: dict[str, str], *, now: dt.datetime | None = None, policy: Policy | None = None
) -> Prune:
    """Read the kg directory ``root`` of project ``name`` and say what ``periods`` make old (see the module), with
    ``policy`` the project's, None when it is not known. Changes nothing."""
    now = now or dt.datetime.now(dt.UTC)
    periods = checked_periods(periods)
    cut = {period: now - parse_period(periods[period]) for period in PERIODS}
    found = Prune(name, root, periods)
    if (root / "corpus.sqlite").exists():  # without the merged state, no log and no blob is known to be unused
        _plan_blobs(found, _plan_logs(found, cut["logs"], policy), cut["logs"])
    _plan_history(found, utc_text(cut["history"]))
    _plan_memo(found, utc_text(cut["memo"]))
    found.size = dir_size(root)
    return found


# Deleting


def _rewrite(found: Prune) -> tuple[int, int, set[str]]:
    """Rewrite the logs of ``found.rewrite``; how many were rewritten, the records they dropped, and the blobs of the
    logs left as they were, which stay."""
    if not found.rewrite:
        return 0, 0, set()
    winners, _ = winners_of(found.root)
    by_run: dict[str, set[str]] = {}
    for item_id, key in winners.items():
        by_run.setdefault(key[RUN], set()).add(item_id)
    rewritten, dropped, pinned = 0, 0, set()
    for name in found.rewrite:
        path = found.root / "log" / name
        result = rewrite_log(path, winners, by_run)
        if result is None:
            found.warnings.append(f"run log {name} was left as it was: it lacks a record of its run the corpus holds")
            pinned |= _refs(path)
            continue
        rewritten, dropped = rewritten + 1, dropped + result
    return rewritten, dropped, pinned


def _drop_runs(found: Prune) -> int:
    """Delete the runs rows of ``found.runs`` and the receipts of runs whose log is gone; how many runs rows went."""
    path = found.root / "corpus.sqlite"
    if not path.exists():
        return 0
    logs = {p.name.split(".", 1)[0] for p in log_files(found.root)}
    db = sqlite3.connect(path, timeout=30)
    try:
        with db:
            deleted = db.executemany("DELETE FROM runs WHERE run_id = ?", [(r,) for r in found.runs]).rowcount
            with contextlib.suppress(sqlite3.OperationalError):  # no receipts table: nothing was recorded
                receipts = [row[0] for row in db.execute("SELECT run_id FROM hub_runs")]
                db.executemany("DELETE FROM hub_runs WHERE run_id = ?", [(r,) for r in receipts if r not in logs])
    finally:
        db.close()
    return max(deleted, 0)


def _drop_history(found: Prune) -> tuple[int, int]:
    """Delete the history of ``found.history``; the rows and the builds that went."""
    if found.history_floor is None or not any(found.history.values()):
        return 0, 0
    store = Store(found.root / "graph.sqlite")
    try:
        store.db.execute("PRAGMA busy_timeout = 30000")
        dropped = store.drop_history(found.history_floor)
    finally:
        store.close()
    return sum(n for table, n in dropped.items() if table != "builds"), dropped["builds"]


def compact_if_free(path: Path, fts: str | None = None) -> str | None:
    """Compact the SQLite file at ``path`` (``scrub.compact``) when at least FREE_SHARE of its pages are free; a
    warning when it could not be, None otherwise."""
    if not path.exists():
        return None
    db = _read_only(path)
    try:
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        free = db.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        db.close()
    if not pages or free < pages * FREE_SHARE:
        return None
    return compact(path, fts=fts)


def _delete_files(found: Prune, pinned: set[str]) -> tuple[int, int]:
    for name in found.logs:
        (found.root / "log" / name).unlink(missing_ok=True)
    blobs, size = 0, 0
    for sha in found.blobs:
        if sha in pinned:
            continue
        path = found.root / "blobs" / "sha256" / sha[:2] / sha[2:]
        with contextlib.suppress(FileNotFoundError):
            size += path.stat().st_size
            path.unlink()
            blobs += 1
    return blobs, size


def apply(found: Prune) -> dict:
    """Delete what ``found`` holds (see the module), compact, and append a ``prune`` line to the project's audit log;
    what went. Call it holding the project's sources (``scrub.holding_sources``), with a plan made while holding
    them."""
    started, clock = utc_now(), time.monotonic()
    rewritten, dropped, pinned = _rewrite(found)  # first: no log ever names a blob that is gone
    blobs, size = _delete_files(found, pinned)
    deleted = {"logs": len(found.logs), "log_bytes": found.log_bytes, "rewritten": rewritten, "dropped": dropped}
    deleted.update(blobs=blobs, blob_bytes=size, runs=_drop_runs(found))
    deleted["history"], deleted["builds"] = _drop_history(found)
    memo = found.root / "memo.sqlite"
    deleted["memo"] = memo_module.drop_unused(memo, found.memo_cut) if found.memo and found.memo_cut else 0
    for path, fts in ((found.root / "corpus.sqlite", None), (found.root / "graph.sqlite", "node_fts"), (memo, None)):
        warning = compact_if_free(path, fts)
        if warning:
            found.warnings.append(warning)
    deleted.update(size_after=dir_size(found.root), seconds=round(time.monotonic() - clock, 3))
    found.deleted = deleted
    _record(found, started)
    return deleted


AUDIT_COUNTS = ("logs", "log_bytes", "rewritten", "dropped", "blobs", "blob_bytes", "runs", "history", "builds", "memo")


def _record(found: Prune, started: str) -> None:
    deleted = found.deleted or {}
    line = {
        "v": audit.AUDIT_VERSION,
        "event": "prune",
        "project": found.project,
        "started_at": started,
        "finished_at": utc_now(),
        "trigger": audit.trigger(),
        "evo_agents": __version__,
        "periods": found.periods,
        **{key: deleted.get(key, 0) for key in AUDIT_COUNTS},
        "size_before": found.size,
        "size_after": deleted.get("size_after"),
    }
    audit.append(found.root, line)


# Running a pass


def policy_of(name: str, home: Path) -> Policy | None:
    """The policy of project ``name`` from the knowledge config of the harness projects.json names; None when there
    is none or it does not load, so retention keeps every log no push recorded."""
    entry = read_index(home).get(name)
    harness = entry.get("harness_root") if isinstance(entry, dict) else None
    if not isinstance(harness, str):
        return None
    try:
        project = load_project_at(Path(harness).expanduser(), home)
    except Exception:  # any harness that does not load: its logs wait for the hub
        return None
    return project.policy if project.name == name else None


def prune_project(
    name: str, root: Path, periods: dict[str, str], *, home: Path | None = None, now: dt.datetime | None = None
) -> Prune:
    """Plan and delete in one project directory, holding its sources; a project a sync holds is left for the next
    pass, with ``busy`` saying why, and one that fails says how in ``error``."""
    try:
        with holding_sources(root):
            found = plan(name, root, periods, now=now, policy=policy_of(name, home or kg_home()))
            apply(found)
    except ScrubBusy as exc:
        found = Prune(name, root, checked_periods(periods), busy=str(exc))
    except (OSError, sqlite3.Error) as exc:  # one project's trouble does not stop the others
        found = Prune(name, root, checked_periods(periods), error=f"{type(exc).__name__}: {exc}")
    return found


def last_pruned(root: Path) -> dt.datetime | None:
    """When a pass last deleted in the project directory ``root``: the last ``prune`` line of its audit log."""
    last = None
    try:
        with open(audit.audit_path(root), encoding="utf-8") as handle:
            for line in handle:
                if '"event": "prune"' in line:
                    with contextlib.suppress(ValueError):
                        last = json.loads(line).get("finished_at") or last
    except OSError:
        return None
    try:
        return parse_utc(last) if isinstance(last, str) else None
    except ValueError:
        return None


def prune_scheduled(home: Path | None = None, now: dt.datetime | None = None) -> list[Prune] | None:
    """The pass of a scheduled sync: None while the schedule is off, else the projects pruned now, those whose last
    prune is at least EVERY old. Raises RetentionError on a config it cannot read."""
    home = home or kg_home()
    config = load_config(home)
    if not config.scheduled:
        return None
    now = now or dt.datetime.now(dt.UTC)
    done = []
    for name, root in project_dirs(home):
        last = last_pruned(root)
        if last is None or now - last >= EVERY:
            done.append(prune_project(name, root, config.periods, home=home, now=now))
    return done
