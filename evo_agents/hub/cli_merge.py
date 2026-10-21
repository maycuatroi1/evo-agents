"""``evo-agents hub merge list|resume``: the hub's merge queue on GitLab of a project (plan gitlab-merge-queue), over
GET /v1/projects/{p}/merge-queue and POST .../merge-queue/repos/{repo}/resume.

``list`` prints the breaker of each repo the queue passed over, then its merge requests (``--repo``'s alone when
given), newest first: where each comes from, where it stands and why it waits or is blocked. ``resume REPO`` resets the
breaker of a repo that tripped, which takes the admin role on the project: its paused merge requests wait for their
turn again, at their place. Every member of the project reads the queue.

Every command finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the
current directory. ``--json`` prints what the hub answered. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.cli_policy import PROJECT_HELP
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.mirror import harness_project, harness_root

MAX_LIST = 200  # merge requests the hub lists at most (merge_view.MAX_LIST)
WHY_CHARS = 80  # of a merge request's reason in the table
# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
QUEUE_KEYS = ("project", "requests", "samples", "breakers")
BREAKER_KEYS = ("repo", "state", "cause", "reason", "tripped_at", "trips", "resumed_by", "resumed_at", "red_since")


def _project(args) -> str:
    if args.project:
        return args.project
    try:
        root = harness_root(None)
    except HubError:
        raise HubError(
            "which project? pass --project, or run this inside a harness whose harness.yaml has hub.project"
        ) from None
    return harness_project(root)


def _path(project: str, rest: str = "") -> str:
    return f"/v1/projects/{quote(project, safe='')}/merge-queue{rest}"


def _short(text: str | None) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= WHY_CHARS else line[: WHY_CHARS - 3] + "..."


def _breaker_lines(project: str, breaker: dict) -> list[str]:
    repo = breaker["repo"]
    if breaker["state"] != "tripped":
        return [f"Breaker of {repo}: armed"]
    return [
        f"Breaker of {repo}: tripped at {when(breaker['tripped_at'])} UTC ({breaker['cause']}): {breaker['reason']}",
        f"  an admin resets it: evo-agents hub merge resume {repo} --project {project}",
    ]


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    project = _project(args)
    query = {"limit": args.limit, **({"repo": args.repo} if args.repo else {})}
    found = hub.call("GET", _path(project, f"?{urlencode(query)}"))
    if args.json:
        print_json(found)
        return 0
    for breaker in found["breakers"]:
        print("\n".join(_breaker_lines(project, breaker)))
    if not found["requests"]:
        print(f"The merge queue of project {project} holds no merge request.")
        return 0
    rows = [
        (
            f"!{item['iid']}" if item["iid"] is not None else "-",
            item["repo"],
            item["source"],
            item["state"],
            _short(item["reason"]) or "-",
        )
        for item in found["requests"]
    ]
    table(("MR", "REPO", "SOURCE", "STATE", "WHY"), rows)
    return 0


@client_command
def cmd_resume(args) -> int:
    hub, _ = signed_in()
    project = _project(args)
    reset = hub.call("POST", _path(project, f"/repos/{quote(args.repo, safe='')}/resume"))
    if args.json:
        print_json(reset)
        return 0
    resumed = reset["resumed"]
    waiting = ", ".join(f"!{iid}" for iid in resumed)
    print(
        f"Reset the breaker of {args.repo} in project {project}: "
        + (f"{waiting} wait for their turn again." if resumed else "no merge request was paused.")
    )
    return 0


def register_merge(hsub) -> None:
    merge = hsub.add_parser("merge", help="the merge queue of a project on GitLab and the breaker of each repo")
    msub = merge.add_subparsers(dest="merge_command", required=True)

    listed = msub.add_parser(
        "list", help="the breaker of each repo, then the merge requests of the queue, newest first"
    )
    listed.add_argument("--repo", help="the merge requests of this repo alone")
    listed.add_argument(
        "--limit", type=int, default=50, choices=range(1, MAX_LIST + 1), metavar="N", help="at most N (default 50)"
    )
    listed.add_argument("--project", help=PROJECT_HELP)
    json_option(listed, returns_object(*QUEUE_KEYS, schema="MergeQueueView"))
    listed.set_defaults(func=cmd_list)

    resume = msub.add_parser("resume", help="reset the breaker of a repo that tripped (the admin role on the project)")
    resume.add_argument("repo", metavar="REPO", help="the repo whose breaker tripped")
    resume.add_argument("--project", help=PROJECT_HELP)
    json_option(resume, returns_object(*BREAKER_KEYS, "resumed", schema="BreakerReset"))
    resume.set_defaults(func=cmd_resume)
