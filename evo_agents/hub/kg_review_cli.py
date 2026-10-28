"""``evo-agents hub kg review``: the review queue of a project's graph you can see on the hub.

``review --project P`` lists the open items (GET /v1/kg/{project}/review), grouped by rule, most connected first,
then the counts per rule; ``--rule``, ``--cluster`` and ``--state`` (open, dismissed, all) narrow the list, and with
``--json`` it prints the hub's ReviewQueue. ``review dismiss FINGERPRINT --reason TEXT --project P`` dismisses one
item (the writer role): it stays dismissed in later builds that find it, and ``--json`` prints the hub's Dismissal.
``review undismiss FINGERPRINT --project P`` takes a dismissal back, whoever made it (a member or a core plugin), so
the item is open again; ``--json`` prints the hub's Undismissed. ``review apply --project P`` applies the fixes of
the open items as decisions (the writer role; POST /v1/kg/{project}/review/apply), at most ``--max`` (default 25),
of ``--rule`` alone when given, naming ``--origin`` (default kg-optimize) and ``--run``; ``--dry-run`` decides
nothing, and ``--json`` prints the hub's ApplyResult. ``hub kg decisions undo N`` takes a decision back.
What an item shows is what you can see (``evo_agents.hub.kg_review``). Standard library only.
"""

from __future__ import annotations

import sys
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import _client_command, _print_json, _signed_in, _table
from evo_agents.hub.contract import JsonOutput, json_option, returns_object
from evo_agents.kg.review import RULES

NODES_SHOWN = 3  # nodes a text line names per item; --json lists them all


def _base(project: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}"


def _what(item: dict) -> str:
    detail = item["detail"]
    names = ", ".join(n["name"] or n["id"] for n in item["nodes"][:NODES_SHOWN])
    more = f" (+{len(item['nodes']) - NODES_SHOWN})" if len(item["nodes"]) > NODES_SHOWN else ""
    extra = []
    if detail.get("mentions"):
        extra.append(f"{detail['mentions']} mentions")
    if detail.get("samples"):
        extra.append("e.g. " + "; ".join(detail["samples"][:2]))
    if detail.get("path"):
        extra.append(detail["path"])
    return names + more + (f": {', '.join(extra)}" if extra else "")


def _list(hub, args) -> int:
    params = {"state": args.state}
    if args.rule:
        params["rule"] = args.rule
    if args.cluster:
        params["cluster"] = args.cluster
    found = hub.call("GET", f"{_base(args.project)}/review?{urlencode(params)}")
    if args.json:
        _print_json(found)
        return 0
    if not found["computed"]:
        print(f"project {args.project} has no review queue: {found['reason']}")
        return 0
    if found["items"]:
        _table(
            ("ITEM", "RULE", "CLUSTER", "ACTION", "WHAT"),
            [(i["fingerprint"], i["rule"], i["cluster_label"] or "-", i["action"], _what(i)) for i in found["items"]],
        )
    else:
        print("no item to show")
    print("\n" + ", ".join(f"{c['rule']} {c['open']} open, {c['dismissed']} dismissed" for c in found["counts"]))
    return 0


def _dismiss(hub, args) -> int:
    path = f"{_base(args.project)}/review/{quote(args.fingerprint, safe=':')}/dismiss"
    found = hub.call("POST", path, {"reason": args.reason})
    if args.json:
        _print_json(found)
        return 0
    print(f"dismissed {args.fingerprint} in project {args.project}: {found['reason']}")
    return 0


def _undismiss(hub, args) -> int:
    path = f"{_base(args.project)}/review/{quote(args.fingerprint, safe=':')}/dismiss"
    found = hub.call("DELETE", path)
    if args.json:
        _print_json(found)
        return 0
    print(f"{args.fingerprint} in project {args.project} is open again; {found['was_by']} had dismissed it")
    return 0


def _apply(hub, args) -> int:
    body = {"origin": args.origin, "max_decisions": args.max, "dry_run": args.dry_run}
    if args.run is not None:
        body["run_id"] = args.run
    if args.rule:
        body["rules"] = [args.rule]
    found = hub.call("POST", f"{_base(args.project)}/review/apply", body)
    if args.json:
        _print_json(found)
        return 0
    rows = [
        (d["number"] if d["number"] is not None else "-", d["kind"], i["rule"], _subject(d), d["reason"])
        for i in found["items"]
        for d in i["decisions"]
    ]
    if rows:
        _table(("DECISION", "KIND", "RULE", "SUBJECT", "REASON"), rows)
    verb = "would take" if found["dry_run"] else "took"
    left = ", ".join(f"{rule} {n}" for rule, n in sorted(found["left"].items())) or "none"
    print(
        f"{verb} {found['decided']} decisions (at most {found['max_decisions']}), {found['skipped']} said already; "
        f"items with a fix left: {left}; decision revision {found['rev']}"
    )
    for item in found["report"]:
        names = ", ".join(n["name"] or n["id"] for n in item["nodes"][:NODES_SHOWN])
        print(f"report: {item['rule']} {item['cluster_label'] or item['cluster'] or '-'}: {names}")
    return 0


def _subject(d: dict) -> str:
    if d["kind"] == "merge":
        return f"{d['src']} = {d['dst']}"
    return f"{d['src']} -{d['rel']}-> {d['dst']}" if d["rel"] else d["src"]


@_client_command
def cmd_review(args) -> int:
    if args.action == "dismiss" and not (args.fingerprint and args.reason):
        print("error: review dismiss needs an item's FINGERPRINT and --reason", file=sys.stderr)
        return 2
    if args.action == "undismiss" and not args.fingerprint:
        print("error: review undismiss needs an item's FINGERPRINT", file=sys.stderr)
        return 2
    if args.action == "apply" and args.fingerprint:
        print("error: review apply takes no FINGERPRINT", file=sys.stderr)
        return 2
    if args.action is None and args.fingerprint:
        print("error: give an item after dismiss: review dismiss FINGERPRINT --reason TEXT", file=sys.stderr)
        return 2
    hub, _ = _signed_in()
    if args.action == "dismiss":
        return _dismiss(hub, args)
    if args.action == "apply":
        return _apply(hub, args)
    return _undismiss(hub, args) if args.action == "undismiss" else _list(hub, args)


REVIEW_QUEUE = ("graph", "computed", "reason", "can_dismiss", "counts", "items")
DISMISSAL = ("reason", "by", "at", "build_id")
UNDISMISSED = ("fingerprint", "was_by")
APPLY_RESULT = ("graph", "rev", "dry_run", "max_decisions", "decided", "skipped", "items", "report", "left")


def register(ksub) -> None:
    review = ksub.add_parser(
        "review", help="the review queue of a project's graph you can see, or dismiss an item with `dismiss FP`"
    )
    review.add_argument(
        "action",
        nargs="?",
        choices=["dismiss", "undismiss", "apply"],
        help="dismiss one item, take its dismissal back, or apply the open items' fixes as decisions (the writer role)",
    )
    review.add_argument(
        "fingerprint", metavar="FINGERPRINT", nargs="?", help="with dismiss or undismiss: the item's fingerprint"
    )
    review.add_argument("--project", required=True, help="project name on the hub")
    review.add_argument("--rule", choices=RULES, help="only the items of this rule")
    review.add_argument("--cluster", help="only the items of this cluster (its id)")
    review.add_argument(
        "--state", choices=["open", "dismissed", "all"], default="open", help="which items to list (default open)"
    )
    review.add_argument("--reason", help="with dismiss: why the item needs nothing done")
    review.add_argument("--max", type=int, default=25, help="with apply: the most decisions to take (default 25)")
    review.add_argument("--run", type=int, help="with apply: the run of the loop that applies them")
    review.add_argument("--origin", default="kg-optimize", help="with apply: the loop's name (default kg-optimize)")
    review.add_argument("--dry-run", action="store_true", help="with apply: say what would be decided, decide nothing")
    json_option(
        review,
        JsonOutput(
            "object",
            REVIEW_QUEUE,
            "ReviewQueue",
            variants=(
                ("dismiss", returns_object(*DISMISSAL, schema="Dismissal")),
                ("undismiss", returns_object(*UNDISMISSED, schema="Undismissed")),
                ("apply", returns_object(*APPLY_RESULT, schema="ApplyResult")),
            ),
        ),
        help="machine-readable output: the queue, or with dismiss the dismissal, with undismiss who had dismissed it, "
        "with apply the decisions taken",
    )
    review.set_defaults(func=cmd_review)
