"""``evo-agents hub curator evolve``: the evolutions of a harness or of an agent's configuration
(``evo_agents.hub.evolve``).

``curator evolve run`` starts one (POST /v1/projects/{p}/curator/evolve): of a repo's harness files with ``--repo`` and
``--path`` (target git), or of an agent of an agent system with ``--system-id`` and ``--agent`` (target agent_config);
its run is queued at once when the night shift's gate is open, else the evolution waits, with why. ``curator evolve
show ID`` shows one with each candidate, its scores and the Pareto front, then a line of each task a candidate did not
pass from its ASI (the failed check's exit code and the end of its output, else the agent's error) (GET
.../curator/evolve/{id}); ``--json`` has each candidate's whole ASI; ``curator
evolve status`` lists them, the newest first (GET .../curator/evolve).

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library only, like the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Evolutions`
is the gateway to the routes, :func:`evolution_body` turns the options of ``run`` into its body.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource
from evo_agents.hub.cli_client import call_object
from evo_agents.hub.curator import money

EVOLVE_KEYS = (
    "id",
    "project",
    "run_id",
    "target",
    "repo",
    "paths",
    "system_id",
    "agent",
    "state",
    "runtime",
    "model",
    "budget",
    "tasks",
    "iterations",
    "frontier",
    "best_candidate_id",
    "proposal_id",
    "change_id",
    "reason",
    "created_at",
    "updated_at",
    "candidates",
)
EVOLVE_LIST_KEYS = ("project", "evolutions")
STATES = ("queued", "running", "done", "failed")
ASI_LINE = 160  # characters of a task's ASI on its line of ``show``
EVOLUTION = returns_object(*EVOLVE_KEYS, schema="EvolveView")
CANDIDATE_COLUMNS = ("ID", "ORIGIN", "PARENTS", "ROUND", "SCORE", "COST", "FRONT")


class Evolutions(ProjectResource):
    """The evolutions of a project: POST and GET /v1/projects/{p}/curator/evolve, GET .../curator/evolve/{id}."""

    collection = "curator"

    def start(self, body: dict) -> dict:
        return call_object(self.hub, "POST", self.path("evolve"), body)

    def get(self, evolution: int) -> dict:
        return call_object(self.hub, "GET", self.path("evolve", evolution))

    def list(self, limit: int, state: str | None = None) -> dict:
        return call_object(self.hub, "GET", self.path("evolve", limit=limit, state=state))


def evolution_body(args: argparse.Namespace) -> dict:
    """The body of ``evolve run``: target git with ``--repo``, else target agent_config."""
    if args.repo:
        body = {"target": "git", "repo": args.repo, "paths": args.path or []}
    else:
        body = {"target": "agent_config", "system_id": args.system_id, "agent": args.agent}
    if args.proposal:
        body["proposal_id"] = args.proposal
    return body


def what_of(found: dict) -> str:
    """What an evolution rewrites: a repo's files, or an agent of a system."""
    if found["target"] == "git":
        return f"{found['repo']} {', '.join(found['paths'])}"
    return f"agent {found['agent']} of system #{found['system_id']}"


def asi_line(asi: dict) -> str:
    """One line of a task's ASI: the exit code and last line of output of its first failed check, else the agent's
    error, else its last message."""
    for check in asi.get("checks") or []:
        if isinstance(check, dict) and check.get("exit_code") not in (0, None):
            lines = [line for line in str(check.get("output_tail") or "").splitlines() if line.strip()]
            return f"exit {check['exit_code']}: {lines[-1] if lines else 'no output'}"[:ASI_LINE]
    text = asi.get("agent_error") or asi.get("summary") or "no check failed"
    return " ".join(str(text).split())[:ASI_LINE]


def asi_lines(candidates: list) -> list[str]:
    """A line of each task a candidate did not pass, from its ASI."""
    return [
        f"#{item['id']} {ref}: {asi_line(asi)}"
        for item in candidates
        for ref, asi in sorted((item.get("asi") or {}).items())
        if isinstance(asi, dict) and (item.get("scores") or {}).get(ref, 0) < 1
    ]


def candidate_row(item: dict) -> tuple:
    return (
        f"#{item['id']}",
        item["origin"],
        ",".join(f"#{p}" for p in item["parents"]) or "-",
        str(item["iteration"]),
        f"{sum(item['scores'].values()):g}/{len(item['scores'])}",
        money(item["cost_usd"]) if item.get("cost_usd") is not None else "-",
        "yes" if item["on_front"] else "",
    )


def evolution_view(found: dict) -> Views:
    """An evolution for a person: its head, its candidates with the ASI of the tasks they failed, its front."""
    run = f"run #{found['run_id']}" if found.get("run_id") else "no run yet"
    head = [
        f"evolution #{found['id']} of {what_of(found)} ({found['target']}): {found['state']}",
        f"{run}, {found['iterations']} round(s), {len(found['tasks'])} task(s) of the select split and probes",
    ]
    if found.get("reason"):
        head.append(f"why: {found['reason']}")
    candidates = found.get("candidates") or []
    front = ", ".join(f"#{item['candidate_id']} leads {item['freq']}" for item in found["frontier"]) or "none yet"
    tail = [f"front: {front}"]
    if found.get("best_candidate_id"):
        tail.append(f"best: #{found['best_candidate_id']}")
    return Views(
        Text(*head),
        Table(CANDIDATE_COLUMNS, [candidate_row(item) for item in candidates]),
        Text(*asi_lines(candidates)),
        Text(*tail),
    )


class EvolveRun(HubCommand):
    name = "run"
    help = "start an evolution of a repo's harness files or of an agent's configuration"
    arguments = (
        PROJECT,
        Arg("--repo", help="target git: the repo whose harness files it rewrites"),
        Arg("--path", action="append", help="target git: a file it rewrites, repeatable"),
        Arg("--system-id", type=int, help="target agent_config: the agent system"),
        Arg("--agent", help="target agent_config: the node's id or its profile's handle"),
        Arg("--proposal", type=int, help="the accepted proposal whose planned change takes the branch"),
    )
    output = EVOLUTION

    def run(self, ctx: HubContext) -> Result:
        found = Evolutions.of(ctx).start(evolution_body(ctx.args))
        return Result(found, evolution_view(found))


class EvolveShow(HubCommand):
    name = "show"
    help = "one evolution: its candidates, their scores and the front"
    arguments = (
        Arg("evolution", type=int, metavar="ID", help="the evolution's id, as `curator evolve status` lists"),
        PROJECT,
    )
    output = EVOLUTION

    def run(self, ctx: HubContext) -> Result:
        found = Evolutions.of(ctx).get(ctx.args.evolution)
        return Result(found, evolution_view(found))


class EvolveStatus(HubCommand):
    name = "status"
    help = "the project's evolutions, the newest first"
    arguments = (
        PROJECT,
        Arg("--state", choices=STATES, help="the evolutions in this state alone"),
        Arg("--limit", type=int, default=20, choices=range(1, 101), metavar="N", help="1 to 100"),
    )
    output = returns_object(*EVOLVE_LIST_KEYS, schema="EvolveList")
    columns = ("ID", "TARGET", "WHAT", "STATE", "ROUNDS", "WHY")

    def run(self, ctx: HubContext) -> Result:
        found = Evolutions.of(ctx).list(ctx.args.limit, ctx.args.state)
        rows = [
            (
                f"#{item['id']}",
                item["target"],
                what_of(item),
                item["state"],
                str(item["iterations"]),
                item["reason"] or "",
            )
            for item in found["evolutions"]
        ]
        said = f"{len(rows)} evolution(s) of {ctx.project}"
        return Result(found, Views(Table(self.columns, rows), Text(said)))


EVOLVE = Group(
    "evolve",
    "the evolutions of a harness or of an agent's configuration",
    (EvolveRun(), EvolveShow(), EvolveStatus()),
)
