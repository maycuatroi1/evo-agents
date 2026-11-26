"""``evo-agents hub run candidates RUN [--json]``, for ``evo_agents.hub.cli.run``: who can run a failed run again, as the
hub computes it (GET /v1/projects/{p}/runs/{id}/rerun-candidates, ``evo_agents.hub.server.runs.service.candidates``). It
prints each worker of yours that serves the project, and each one another member shares with you there, each agent
profile of the project and each of your accounts bound to it, the workers and agents with whether they fit, their rank
and why. For a queued run it prints who can take it now, as the claim counts it (``queue_fit``): each worker that
could, the agent pools of the project, and why no worker can with the command that fixes it. Standard library only,
like the rest of the client."""

from __future__ import annotations

from evo_agents.clikit import Result, Table, Text, View, Views, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext
from evo_agents.hub.cli.run.options import RUN_ID
from evo_agents.hub.cli.run.resources import Runs

CANDIDATES_KEYS = (
    "run_id",
    "project",
    "kind",
    "state",
    "failure_cause",
    "workers",
    "agents",
    "accounts",
    "other_projects",
    "cause_inferred",
    "triage",
    "pools",
    "diagnosis",
)
WORKER_HEADER = ("WORKER", "FIT", "RUNTIMES", "FREE", "WHY")


def _worker(candidate: dict) -> str:
    """A worker as `--worker` names it: its name, or owner/name for one another member shares."""
    return f"{candidate['owner']}/{candidate['worker']}" if candidate.get("shared") else candidate["worker"]


def _fit(candidate: dict) -> str:
    return f"#{candidate['rank']}" if candidate["fit"] else "no"


def _workers(found: dict) -> list[tuple]:
    return [
        (_worker(w), _fit(w), ", ".join(w["runtimes"]) or "-", w["free_slots"], "; ".join(w["reasons"]))
        for w in found["workers"]
    ]


def _section(heading: str, header: tuple[str, ...], rows: list[tuple], empty: str) -> View:
    """A table under its heading, or the heading and ``empty``, indented, when it has no rows."""
    return Views(Text(heading), Table(header, rows, f"  {empty}"))


def queued_view(found: dict) -> View:
    """The answer of the hub for a queued run: why it waits, the workers and the agent pools."""
    lines = [f"Run #{found['run_id']} ({found['kind']}) is queued: who can take it now"]
    if found.get("diagnosis"):
        lines.append(f"No worker can take it: {found['diagnosis']}")
    parts: list[View] = [Text(*lines), _section("Workers", WORKER_HEADER, _workers(found), "none could take it")]
    pools = found.get("pools") or []
    if pools:
        rows = [
            (
                p["pool"],
                f"@{p['agent']}",
                p["nodes"],
                p["ready_nodes"],
                "yes" if p["takes_run"] else "no",
                "; ".join(p["reasons"]),
            )
            for p in pools
        ]
        parts += [Text("Agent pools"), Table(("POOL", "AGENT", "NODES", "READY", "THIS RUN", "WHY"), rows)]
    return Views(*parts)


def candidates_view(found: dict) -> View:
    """The answer of the hub as three tables, each under its heading (and a fourth for other projects named)."""
    if found.get("state") == "queued":
        return queued_view(found)
    agents = [
        (
            f"@{a['handle']}"
            + ("" if a.get("project", found["project"]) == found["project"] else f" ({a['project']})"),
            _fit(a),
            "; ".join(a["reasons"]),
        )
        for a in found["agents"]
    ]
    accounts = [
        (a["name"], a["state"], ", ".join(a["workers"]) or "-", "yes" if a["used_by_run"] else "no")
        for a in found["accounts"]
    ]
    parts = [
        Text(
            f"Run #{found['run_id']} ({found['kind']}) failed, cause {found['failure_cause'] or 'unknown'}: "
            "who can run it again"
        ),
        _section("Workers", WORKER_HEADER, _workers(found), "none of your workers serves the project"),
        _section("Agents", ("AGENT", "FIT", "WHY"), agents, "the project has no agent profile"),
        _section("Accounts", ("ACCOUNT", "STATE", "WORKERS", "USED"), accounts, "none is bound to the project"),
    ]
    if found.get("other_projects"):
        others = [
            (o["project"], o["role"] or "none", o["named"], "; ".join(o["reasons"])) for o in found["other_projects"]
        ]
        parts.append(_section("Other projects the error names", ("PROJECT", "YOUR ROLE", "NAMED", "WHY"), others, ""))
    return Views(*parts)


class RunCandidates(HubCommand):
    name = "candidates"
    help = (
        "who can run a failed run again, or take a queued one now: your workers and the project's agents or "
        "agent pools, each with why it fits or not, and your accounts (owner only)"
    )
    arguments = (RUN_ID, PROJECT)
    output = returns_object(*CANDIDATES_KEYS, schema="RerunCandidates")

    def run(self, ctx: HubContext) -> Result:
        found = Runs.of(ctx).candidates(ctx.args.run)
        return Result(found, candidates_view(found))
