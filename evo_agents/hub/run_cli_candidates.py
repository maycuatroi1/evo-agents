"""``evo-agents hub run candidates RUN [--json]``, for ``evo_agents.hub.run_cli``: who can run a failed run again, as the
hub computes it (GET /v1/projects/{p}/runs/{id}/rerun-candidates, ``evo_agents.hub.server.runs.service.candidates``).
It prints each worker of yours that serves the project, each agent profile of the project and each of your accounts
bound to it, the workers and agents with whether they fit, their rank and why. Standard library only, like the rest
of the client."""

from __future__ import annotations

from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_object

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
)


def _fit(candidate: dict) -> str:
    return f"#{candidate['rank']}" if candidate["fit"] else "no"


def _print(found: dict) -> None:
    """The answer of the hub as three tables, each under its heading."""
    print(
        f"Run #{found['run_id']} ({found['kind']}) failed, cause {found['failure_cause'] or 'unknown'}: who can run it again"
    )
    sections = (
        (
            "Workers",
            ("WORKER", "FIT", "RUNTIMES", "FREE", "WHY"),
            [
                (w["worker"], _fit(w), ", ".join(w["runtimes"]) or "-", w["free_slots"], "; ".join(w["reasons"]))
                for w in found["workers"]
            ],
            "none of your workers serves the project",
        ),
        (
            "Agents",
            ("AGENT", "FIT", "WHY"),
            [
                (
                    f"@{a['handle']}"
                    + ("" if a.get("project", found["project"]) == found["project"] else f" ({a['project']})"),
                    _fit(a),
                    "; ".join(a["reasons"]),
                )
                for a in found["agents"]
            ],
            "the project has no agent profile",
        ),
        (
            "Accounts",
            ("ACCOUNT", "STATE", "WORKERS", "USED"),
            [
                (a["name"], a["state"], ", ".join(a["workers"]) or "-", "yes" if a["used_by_run"] else "no")
                for a in found["accounts"]
            ],
            "none is bound to the project",
        ),
    )
    if found.get("other_projects"):
        sections += (
            (
                "Other projects the error names",
                ("PROJECT", "YOUR ROLE", "NAMED", "WHY"),
                [
                    (o["project"], o["role"] or "none", o["named"], "; ".join(o["reasons"]))
                    for o in found["other_projects"]
                ],
                "",
            ),
        )
    for heading, header, rows, empty in sections:
        print(heading)
        table(header, rows) if rows else print(f"  {empty}")


def register_candidates(rsub, with_run, project_of) -> None:
    """Add ``run candidates`` to the subparsers ``rsub`` of ``run``, its ``RUN`` and ``--project`` by ``with_run``;
    ``project_of`` reads the project of the parsed arguments."""

    @client_command
    def command(args) -> int:
        hub, _ = signed_in()
        found = hub.call("GET", f"/v1/projects/{quote(project_of(args), safe='')}/runs/{args.run}/rerun-candidates")
        if args.json:
            print_json(found)
        else:
            _print(found)
        return 0

    candidates = rsub.add_parser(
        "candidates",
        help="who can run a failed run again: your workers and the project's agents, each with why it fits or not, "
        "and your accounts (owner only)",
    )
    with_run(candidates)
    json_option(candidates, returns_object(*CANDIDATES_KEYS, schema="RerunCandidates"))
    candidates.set_defaults(func=command)
