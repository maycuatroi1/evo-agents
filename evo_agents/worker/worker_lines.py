"""One line for a worker as GET /v1/workers lists it (``evo-agents worker list``): whether it is online, the
evo-agents its daemon runs, whether it updates itself to the tag nightly, the runs it holds, the free disk of its
worktrees and each runtime's login, and its last heartbeat. Standard library only."""

from __future__ import annotations

from datetime import datetime


def _ago(value: str | None, now: datetime) -> str:
    """``value``, an ISO time from the hub, as a span before ``now``: 40s ago, 12m ago, 3h ago, 2d ago."""
    if not value:
        return "never"
    seconds = max(0, int((now - datetime.fromisoformat(value)).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def _updating(worker: dict) -> str:
    if worker.get("version_locked"):
        return "version locked"
    report = worker.get("self_update") or {}
    if not report.get("state"):
        return "self-update not reported"
    reason = f" ({report['reason']})" if report.get("reason") else ""
    return f"self-update {report['state']}{reason}"


def _health(worker: dict) -> str:
    """The free disk of its worktrees and each runtime's login, as its last heartbeat said; empty from an older
    daemon or hub."""
    health = worker.get("health") or {}
    parts = []
    disk = health.get("disk") or {}
    if isinstance(disk.get("free_gb"), int | float):
        pct = f" ({disk['free_pct']:g}%)" if isinstance(disk.get("free_pct"), int | float) else ""
        parts.append(f"disk {disk['free_gb']:g} GB free{pct}")
    logins = health.get("logins") or {}
    if logins:
        parts.append("login " + " ".join(f"{name}={state}" for name, state in sorted(logins.items())))
    return "".join(f", {part}" for part in parts)


def describe(worker: dict, now: datetime) -> str:
    """One line for ``worker``, a row of GET /v1/workers."""
    version = worker.get("agent_version") or "?"
    if worker.get("agent_commit"):
        version += f" at {worker['agent_commit'][:7]}"
    shared = ", shared" if worker.get("shared") else ""
    if worker.get("ready") is False:  # a node of a pool that takes no run now; an older hub says nothing of it
        shared += f", not ready: {worker.get('ready_reason') or '?'}"
    return (
        f"#{worker['id']} {worker['owner']}/{worker['name']} on {worker.get('hostname') or '?'}: {worker['status']}"
        f"{shared}, evo-agents {version}, {_updating(worker)}, {worker.get('held_runs', 0)} of "
        f"{worker.get('slots', 0)} slot(s) held{_health(worker)}, last heartbeat "
        f"{_ago(worker.get('last_heartbeat_at'), now)}"
    )
