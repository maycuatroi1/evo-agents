"""``evo-agents kg connector test``: what every connector must satisfy before a harness relies on it.

Checks: protocol (schema, order, hashes), determinism (two runs, same IDs and hashes), replay and
permutation (the log merged in any order or twice gives one state), truncated listing (a run cut short
derives no deletion), golden output (diffable summary), labels (a connector never lowers a label).
"""

from __future__ import annotations

import json
import random
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.harness import Harness
from evo_agents.kg.corpus import SCHEMA, Corpus
from evo_agents.kg.policy import DEFAULT_LEVELS
from evo_agents.kg.project import Project
from evo_agents.kg.protocol import StreamChecker
from evo_agents.kg.protocol.runner import ConnectorContext, ConnectorRun


@dataclass
class Check:
    name: str
    ok: bool
    details: list[str] = field(default_factory=list)


@dataclass
class ConformanceReport:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def to_json(self) -> dict:
        return {"ok": self.ok, "checks": [c.__dict__ for c in self.checks]}


def _collect(source: dict, harness: Harness | None, env: dict, kill_after: int | None = None):
    run = ConnectorRun(ConnectorContext("conformance", source, harness, None, env), kill_after=kill_after)
    messages = list(run)
    return messages, run


def summary(messages: list[dict]) -> list[dict]:
    """The golden form of a run: stable, small, and readable in a diff."""
    out = []
    for m in messages:
        kind = m.get("type")
        if kind == "item":
            out.append(
                {
                    "item": m["id"],
                    "kind": m["kind"],
                    "rev": m["rev"],
                    "hash": m["hash"],
                    "anchors": [f["anchor"] for f in m.get("fragments", [])],
                }
            )
        elif kind == "tombstone":
            out.append({"tombstone": m["id"]})
        elif kind == "listing":
            out.append({"listing": m["scope"], "complete": m["complete"], "count": m["count"]})
        elif kind == "error":
            out.append({"error": m.get("id"), "failure": m["failure"]})
    return sorted(out, key=lambda e: json.dumps(e, sort_keys=True))


def _digest_after(corpus: Corpus, runs: list[tuple[str, list[dict]]]) -> str:
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    for run_id, records in runs:
        corpus.merge(records, run_id, db)
    return corpus.state_digest(db)


def run_conformance(
    source: dict,
    *,
    harness: Harness | None = None,
    env: dict | None = None,
    golden: Path | None = None,
    update_golden: bool = False,
    levels: list[str] | None = None,
) -> ConformanceReport:
    env = env or {}
    checks: list[Check] = []

    first, run = _collect(source, harness, env)
    checker = StreamChecker(source["id"])
    for m in first:
        checker.feed(m)
    checker.finish()
    proto = Check("protocol", not checker.issues, [str(i) for i in checker.issues[:20]])
    if run.exception:
        proto.ok = False
        proto.details.append(f"connector raised {run.exception}")
    if run.returncode not in (None, 0):
        proto.ok = False
        proto.details.append(f"exit code {run.returncode}; stderr: {' | '.join(list(run.stderr_tail)[-3:])}")
    checks.append(proto)

    second, _ = _collect(source, harness, env)
    same = summary(first) == summary(second)
    checks.append(Check("determinism", same, [] if same else ["two runs gave different IDs, hashes or listings"]))

    with tempfile.TemporaryDirectory() as tmp:
        corpus = Corpus("conformance", Path(tmp))
        records = [corpus.to_log_record(m) for m in first if m.get("type") in ("item", "tombstone")]
        base = _digest_after(corpus, [("run-1", records)])
        shuffled = list(records) * 2
        random.Random(42).shuffle(shuffled)
        replayed = _digest_after(corpus, [("run-1", shuffled)])
        two_runs = _digest_after(corpus, [("run-2", records), ("run-1", records)])
        ok = base == replayed == _digest_after(corpus, [("run-1", records), ("run-2", records)]) == two_runs
        checks.append(Check("replay", ok, [] if ok else ["merge depends on message order or repetition"]))

        items = [m for m in first if m.get("type") == "item"]
        if len(items) >= 2 and any(m.get("type") == "listing" and m.get("complete") for m in first):
            project = Project(
                "conformance",
                # Without a harness, exec connectors run where the test was started, as in the first run.
                harness or Harness(Path.cwd(), {"name": "conformance"}),
                {"project": "conformance", "sources": [source]},
                Path(tmp) / "knowledge.yaml",
                Path(tmp) / "home",
            )
            from evo_agents.kg.sync import sync_source

            full = sync_source(project, source["id"], env=env)
            cut = sync_source(project, source["id"], kill_after=1 + len(items) // 2, env=env)
            alive = len(project.corpus().items(source["id"]))
            ok = full.ok and not cut.ok and cut.removals == 0 and alive == full.items
            details = (
                []
                if ok
                else [
                    f"full run ok={full.ok}, cut run ok={cut.ok}, removals={cut.removals}, "
                    f"alive={alive} of {full.items}"
                ]
            )
            checks.append(Check("truncated-listing", ok, details))
        else:
            checks.append(Check("truncated-listing", True, ["skipped: needs two items and a complete listing"]))

    if golden is not None:
        current = "\n".join(json.dumps(e, ensure_ascii=False, sort_keys=True) for e in summary(first)) + "\n"
        if update_golden or not golden.exists():
            golden.parent.mkdir(parents=True, exist_ok=True)
            golden.write_text(current, encoding="utf-8")
            checks.append(Check("golden", True, [f"wrote {golden}"]))
        else:
            ok = golden.read_text(encoding="utf-8") == current
            checks.append(
                Check(
                    "golden",
                    ok,
                    []
                    if ok
                    else [
                        f"output differs from {golden}; diff it, then rerun with --update-golden if the change is right"
                    ],
                )
            )

    levels = levels or DEFAULT_LEVELS
    declared = (source.get("label") or {}).get("level")
    if declared in levels:
        floor = levels.index(declared)
        lowered = [
            m["id"]
            for m in first
            if m.get("type") == "item"
            and (m.get("label") or {}).get("level") in levels
            and levels.index(m["label"]["level"]) < floor
        ]
        checks.append(Check("labels", not lowered, [f"lowers the label of {i}" for i in lowered[:10]]))
    else:
        checks.append(Check("labels", True, ["skipped: the source declares no label level"]))
    return ConformanceReport(checks)
