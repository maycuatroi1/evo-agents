"""``evo-agents worker list``: the workers of the hub this machine is signed in to, as GET /v1/workers lists them: the
caller's own and the live ones other members share with them, every worker for a hub admin (``docs/workers.md``,
Commands). One line each says what an owner would otherwise ask each machine over ssh: whether it is online, the
evo-agents its daemon runs, whether it updates itself to the tag nightly, the runs it holds and its last heartbeat.
It needs ``hub login`` only, not a worker on this machine."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from evo_agents.hub.client import Hub, load_credentials


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


def describe(worker: dict, now: datetime) -> str:
    """One line for ``worker``, a row of GET /v1/workers."""
    version = worker.get("agent_version") or "?"
    if worker.get("agent_commit"):
        version += f" at {worker['agent_commit'][:7]}"
    shared = ", shared" if worker.get("shared") else ""
    return (
        f"#{worker['id']} {worker['owner']}/{worker['name']} on {worker.get('hostname') or '?'}: {worker['status']}"
        f"{shared}, evo-agents {version}, {_updating(worker)}, {worker.get('held_runs', 0)} of "
        f"{worker.get('slots', 0)} slot(s) held, last heartbeat {_ago(worker.get('last_heartbeat_at'), now)}"
    )


def cmd_list(args) -> int:
    credentials = load_credentials()
    hub = Hub(credentials.url, credentials.token)
    workers = hub.call("GET", "/v1/workers?revoked=true" if args.revoked else "/v1/workers")
    if args.json:
        print(json.dumps(workers, ensure_ascii=False, indent=2))
        return 0
    if not workers:
        print(f"No worker of yours on {credentials.url}, and no member shares one with you.")
        return 0
    nightly = next((worker["nightly"] for worker in workers if worker.get("nightly")), None)
    head = f"Workers on {credentials.url}"
    print(head + (f", nightly at {nightly['commit'][:7]}:" if nightly else ":"))
    now = datetime.now(UTC)
    for worker in workers:
        print(f"  {describe(worker, now)}")
    return 0


def register_list(wsub) -> None:
    from evo_agents.worker.cli import worker_command  # the cli imports this module

    listed = wsub.add_parser(
        "list",
        help="the workers of the hub this machine is signed in to, yours and those shared with you: status, "
        "evo-agents version, self-update, runs held, last heartbeat (needs `hub login`)",
    )
    listed.add_argument("--revoked", action="store_true", help="also list revoked workers")
    listed.add_argument("--json", action="store_true", help="the rows of GET /v1/workers as they are")
    listed.set_defaults(func=worker_command(cmd_list))
