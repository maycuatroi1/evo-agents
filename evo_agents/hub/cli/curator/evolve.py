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
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from evo_agents.hub.cli.base import project_of
from evo_agents.hub.cli_client import call_object, client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_object
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


def _path(project: str, *parts: str) -> str:
    return "/".join((f"/v1/projects/{quote(project, safe='')}/curator/evolve", *parts))


def _what(found: dict) -> str:
    if found["target"] == "git":
        return f"{found['repo']} {', '.join(found['paths'])}"
    return f"agent {found['agent']} of system #{found['system_id']}"


def _asi_line(asi: dict) -> str:
    """One line of a task's ASI: the exit code and last line of output of its first failed check, else the agent's
    error, else its last message."""
    for check in asi.get("checks") or []:
        if isinstance(check, dict) and check.get("exit_code") not in (0, None):
            lines = [line for line in str(check.get("output_tail") or "").splitlines() if line.strip()]
            return f"exit {check['exit_code']}: {lines[-1] if lines else 'no output'}"[:ASI_LINE]
    text = asi.get("agent_error") or asi.get("summary") or "no check failed"
    return " ".join(str(text).split())[:ASI_LINE]


def _print_asi(candidates: list) -> None:
    for item in candidates:
        for ref, asi in sorted((item.get("asi") or {}).items()):
            if isinstance(asi, dict) and (item.get("scores") or {}).get(ref, 0) < 1:
                print(f"#{item['id']} {ref}: {_asi_line(asi)}")


def _print_evolution(found: dict) -> None:
    print(f"evolution #{found['id']} of {_what(found)} ({found['target']}): {found['state']}")
    run = f"run #{found['run_id']}" if found.get("run_id") else "no run yet"
    print(f"{run}, {found['iterations']} round(s), {len(found['tasks'])} task(s) of the select split and probes")
    if found.get("reason"):
        print(f"why: {found['reason']}")
    rows = [
        (
            f"#{item['id']}",
            item["origin"],
            ",".join(f"#{p}" for p in item["parents"]) or "-",
            str(item["iteration"]),
            f"{sum(item['scores'].values()):g}/{len(item['scores'])}",
            money(item["cost_usd"]) if item.get("cost_usd") is not None else "-",
            "yes" if item["on_front"] else "",
        )
        for item in found.get("candidates") or []
    ]
    if rows:
        table(("ID", "ORIGIN", "PARENTS", "ROUND", "SCORE", "COST", "FRONT"), rows)
        _print_asi(found.get("candidates") or [])
    front = ", ".join(f"#{item['candidate_id']} leads {item['freq']}" for item in found["frontier"]) or "none yet"
    print(f"front: {front}")
    if found.get("best_candidate_id"):
        print(f"best: #{found['best_candidate_id']}")


@client_command
def cmd_run(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    if args.repo:
        body = {"target": "git", "repo": args.repo, "paths": args.path or []}
    else:
        body = {"target": "agent_config", "system_id": args.system_id, "agent": args.agent}
    if args.proposal:
        body["proposal_id"] = args.proposal
    found = call_object(hub, "POST", _path(project), body)
    if args.json:
        print_json(found)
        return 0
    _print_evolution(found)
    return 0


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    found = call_object(hub, "GET", _path(project_of(args), str(args.evolution)))
    if args.json:
        print_json(found)
        return 0
    _print_evolution(found)
    return 0


@client_command
def cmd_status(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    query = {"limit": args.limit, **({"state": args.state} if args.state else {})}
    found = call_object(hub, "GET", _path(project) + f"?{urlencode(query)}")
    if args.json:
        print_json(found)
        return 0
    rows = [
        (f"#{item['id']}", item["target"], _what(item), item["state"], str(item["iterations"]), item["reason"] or "")
        for item in found["evolutions"]
    ]
    if rows:
        table(("ID", "TARGET", "WHAT", "STATE", "ROUNDS", "WHY"), rows)
    print(f"{len(rows)} evolution(s) of {project}")
    return 0


def register_evolve_run(esub, with_project) -> None:
    run = esub.add_parser("run", help="start an evolution of a repo's harness files or of an agent's configuration")
    with_project(run)
    run.add_argument("--repo", help="target git: the repo whose harness files it rewrites")
    run.add_argument("--path", action="append", help="target git: a file it rewrites, repeatable")
    run.add_argument("--system-id", type=int, help="target agent_config: the agent system")
    run.add_argument("--agent", help="target agent_config: the node's id or its profile's handle")
    run.add_argument("--proposal", type=int, help="the accepted proposal whose planned change takes the branch")
    json_option(run, returns_object(*EVOLVE_KEYS, schema="EvolveView"))
    run.set_defaults(func=cmd_run)


def register_evolve_show(esub, with_project) -> None:
    shown = esub.add_parser("show", help="one evolution: its candidates, their scores and the front")
    shown.add_argument("evolution", type=int, metavar="ID", help="the evolution's id, as `curator evolve status` lists")
    with_project(shown)
    json_option(shown, returns_object(*EVOLVE_KEYS, schema="EvolveView"))
    shown.set_defaults(func=cmd_show)


def register_evolve_status(esub, with_project) -> None:
    status = esub.add_parser("status", help="the project's evolutions, the newest first")
    with_project(status)
    status.add_argument("--state", choices=STATES, help="the evolutions in this state alone")
    status.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="N", help="1 to 100")
    json_option(status, returns_object(*EVOLVE_LIST_KEYS, schema="EvolveList"))
    status.set_defaults(func=cmd_status)


def register_evolve(csub, with_project) -> None:
    """``curator evolve`` and its commands."""
    parser = csub.add_parser("evolve", help="the evolutions of a harness or of an agent's configuration")
    esub = parser.add_subparsers(dest="evolve_command", required=True)
    for register in (register_evolve_run, register_evolve_show, register_evolve_status):
        register(esub, with_project)
