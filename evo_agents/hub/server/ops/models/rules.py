"""What tier 0 of the ops queue reads of a run that failed, and of a worker's health (``ops.service.rules`` is the
design): the causes of a machine it hands again, the kinds of run it hands, the names it marks an item handled by."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from evo_agents.hub import ops_events

CAUSES = ("checkout", "worker_stopped", "lease_lost", "network", "hub_5xx", "account_auth")
KINDS = ("chat", "task")  # a step, plan or author run keeps its triage
REDELIVER = "tier0:redeliver"  # ops_items.handled_by of an item whose run the hub handed again
ORCHESTRATOR = "orchestrator"  # ops_items.handled_by of an item whose run an Orchestrator took
MAX_CHAIN = 50  # runs the check of a run's earlier redelivery walks back


@dataclass(frozen=True)
class Failed:
    """A run an item of an open batch tells failed of an environment cause."""

    run_id: int
    kind: str
    cause: str
    worker_id: int | None
    dispatched_by: int
    finished_at: datetime | None
    runtime: str | None


def healthy(health: dict | None, runtime: str | None, *, leased: bool = False) -> bool:
    """Whether a worker's stored health report says enough free disk and the login of ``runtime`` ok or leased
    (``ops_events.LOGGED_IN``; of any runtime for a run that asked for any); with ``leased`` (the run logs in with a hub
    account), enough free disk."""
    if not health or ops_events.disk_band(health) != "ok":
        return False
    if leased:
        return True
    logins = health.get("logins") or {}
    if runtime and runtime != "any":
        return logins.get(runtime) in ops_events.LOGGED_IN
    return any(state in ops_events.LOGGED_IN for state in logins.values())
