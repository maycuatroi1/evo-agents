"""``evo-agents hub kg decisions``: the decisions on a project's graph you can see on the hub, and a writer's own.

``decisions --project P`` lists them, newest first (GET /v1/kg/{project}/decisions); ``--state`` (proposed, active,
rejected, undone, orphan) narrows the list, and with ``--json`` it prints the array of the hub's decisions, each with
its ``state`` and history. A writer takes one with ``deprecate ID [--rel REL --to ID2]``, ``merge ID --to ID2`` or
``link ID --rel REL --to ID2``, and steps one with ``accept N``, ``reject N`` or ``undo N``; each needs ``--reason``
and names the decision revision it read: ``--base-rev``, else the revision the list gives just before
(``evo_agents.hub.server.kg_decisions``). A stale revision is refused and nothing changes. Standard library only.
"""

from __future__ import annotations

import sys
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in, _table
from evo_agents.hub.contract import JsonOutput, json_option, returns_object

TAKE = ("deprecate", "merge", "link")
STEP = ("accept", "reject", "undo")
STATES = ("proposed", "active", "rejected", "undone", "orphan")
DECISION = ("number", "kind", "state", "src", "rel", "dst", "history")


def _base(project: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}/decisions"


def _what(d: dict) -> str:
    if d["kind"] == "merge":
        return f"{d['src']} = {d['dst']}"
    if d["rel"] is not None:
        return f"{d['src']} -{d['rel']}-> {d['dst']}"
    return d["src"]


def _list(hub, args) -> int:
    query = f"?{urlencode({'state': args.state})}" if args.state else ""
    found = hub.call("GET", _base(args.project) + query)
    if args.json:
        _print_json(found["decisions"])
        return 0
    if found["decisions"]:
        _table(
            ("N", "KIND", "STATE", "WHAT", "WHY"),
            [(d["number"], d["kind"], d["state"], _what(d), d["history"][0]["reason"]) for d in found["decisions"]],
        )
    else:
        print("no decision to show")
    print(f"\nrevision {found['rev']}" + ("" if found["can_decide"] else "; deciding needs the writer role"))
    return 0


def _write(hub, args) -> int:
    base_rev = args.base_rev
    if base_rev is None:
        base_rev = hub.call("GET", _base(args.project))["rev"]
    if args.action in STEP:
        body = {"reason": args.reason, "base_rev": base_rev}
        found = hub.call("POST", f"{_base(args.project)}/{int(args.target)}/{args.action}", body)
    else:
        body = {"kind": args.action, "src": args.target, "rel": args.rel, "dst": args.to}
        found = hub.call("POST", _base(args.project), {**body, "reason": args.reason, "base_rev": base_rev})
    if args.json:
        _print_json(found)
        return 0
    print(f"decision {found['number']} of project {args.project} is {found['state']}: {_what(found)}")
    return 0


def _problem(args) -> str | None:
    if args.action is None:
        return (
            "error: give an action before a target: decisions deprecate|merge|link ID, accept|reject|undo N"
            if (args.target)
            else None
        )
    if not args.target or not args.reason:
        return f"error: decisions {args.action} needs a target and --reason"
    if args.action in STEP and not args.target.isdigit():
        return f"error: decisions {args.action} needs a decision's number"
    if args.action in ("merge", "link") and not args.to:
        return f"error: decisions {args.action} needs --to"
    if args.action == "link" and not args.rel:
        return "error: decisions link needs --rel"
    if args.action == "deprecate" and bool(args.rel) != bool(args.to):
        return "error: decisions deprecate names a node, or an edge with both --rel and --to"
    return None


@_client_command
def cmd_decisions(args) -> int:
    problem = _problem(args)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    hub, _ = _signed_in()
    return _list(hub, args) if args.action is None else _write(hub, args)


def register(ksub) -> None:
    decisions = ksub.add_parser(
        "decisions",
        help="the decisions on a project's graph you can see; a writer deprecates, merges, links, accepts, rejects or "
        "undoes one",
    )
    decisions.add_argument(
        "action", nargs="?", choices=[*TAKE, *STEP], help="take or step a decision (the writer role)"
    )
    decisions.add_argument(
        "target", metavar="TARGET", nargs="?", help="a node id for deprecate, merge and link; a number for the others"
    )
    decisions.add_argument("--project", required=True, help="project name on the hub")
    decisions.add_argument("--state", choices=STATES, help="only the decisions in this state")
    decisions.add_argument("--rel", help="the edge type: of the edge deprecated, or of the link")
    decisions.add_argument("--to", help="the other node: of the edge deprecated, merged or linked")
    decisions.add_argument("--reason", help="why; every write needs one")
    decisions.add_argument("--base-rev", type=int, help="the decision revision you read (default: the hub's now)")
    taken = returns_object(*DECISION, schema="KgDecision")
    json_option(
        decisions,
        JsonOutput("array", DECISION, "KgDecision", variants=tuple((a, taken) for a in (*TAKE, *STEP))),
        help="machine-readable output: the decisions, or with an action the decision it wrote",
    )
    decisions.set_defaults(func=cmd_decisions)
