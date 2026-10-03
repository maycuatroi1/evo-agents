"""Audit log of one project: one JSON line per source sync and per build, appended to
``<kg home>/<project>/audit.jsonl``, a file of mode 0600 in the 0700 project directory.

A line holds identifiers, times, counts and versions, never item content, titles, URIs or error text: the
audit log says what ran, when and how it ended, the corpus log what it read. The shape is flat, close to an
OpenLineage RunEvent, and stable: keys keep their meaning, and a key that does not apply is null. ``v`` is the
shape version::

    sync   v, event="sync", run_id, project, source, status, started_at, finished_at, trigger, evo_agents,
           connector, connector_version, items, tombstones, removals, rejected, errors, issues, held
    build  v, event="build", build_id, project, status, started_at, finished_at, trigger, evo_agents,
           content_hash, items, units, nodes, edges, errors, issues, verified, rebuilt_from, exception

``status`` is ok or failed. ``trigger`` is schedule when EVO_KG_TRIGGER=schedule, which the LaunchAgent sets,
and manual otherwise. ``errors`` and ``issues`` are counts; ``held`` counts the removals a guard held back.
A build that failed before writing has a null build_id and content_hash; one that raised names the
exception type in ``exception``; ``verified`` is null unless the build was verified. Appending never fails
the run it records: an OSError becomes a warning on stderr.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents import __version__

if TYPE_CHECKING:
    from evo_agents.kg.build import BuildReport
    from evo_agents.kg.sync import SyncResult

AUDIT_VERSION = 1
AUDIT_FILE = "audit.jsonl"


def audit_path(root: Path) -> Path:
    return root / AUDIT_FILE


def trigger() -> str:
    return "schedule" if os.environ.get("EVO_KG_TRIGGER") == "schedule" else "manual"


def append(root: Path, line: dict) -> None:
    """Append one line to the audit log under the project directory ``root`` with a single write."""
    path = audit_path(root)
    data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"warning: could not append to the audit log {path}: {exc}", file=sys.stderr)


def record_sync(
    root: Path,
    project: str,
    result: SyncResult,
    *,
    connector: str,
    connector_version: str | None,
    started: str,
    finished: str,
) -> None:
    append(
        root,
        {
            "v": AUDIT_VERSION,
            "event": "sync",
            "run_id": result.run_id,
            "project": project,
            "source": result.source,
            "status": result.status,
            "started_at": started,
            "finished_at": finished,
            "trigger": trigger(),
            "evo_agents": __version__,
            "connector": connector,
            "connector_version": connector_version,
            "items": result.items,
            "tombstones": result.tombstones,
            "removals": result.removals,
            "rejected": result.rejected,
            "errors": result.errors,
            "issues": len(result.issues),
            "held": sum(h["count"] for h in result.held),
        },
    )


def record_build(root: Path, report: BuildReport, *, started: str, finished: str, exception: str | None = None) -> None:
    # Per-source counts are complete, where report.issues keeps only the first 50 messages.
    sources = report.coverage.get("sources", [])
    append(
        root,
        {
            "v": AUDIT_VERSION,
            "event": "build",
            "build_id": report.build_id,
            "project": report.project,
            "status": "ok" if report.ok and exception is None else "failed",
            "started_at": started,
            "finished_at": finished,
            "trigger": trigger(),
            "evo_agents": __version__,
            "content_hash": report.content_hash,
            "items": sum(s["items"] for s in sources),
            "units": report.units,
            "nodes": report.nodes,
            "edges": report.edges,
            "errors": len(report.errors),
            "issues": sum(s["issues"] for s in sources),
            "verified": None if report.verify is None else bool(report.verify.get("match")),
            "rebuilt_from": report.rebuilt_from,
            "exception": exception,
        },
    )
