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

:data:`REVIEW` is a class command of ``evo_agents.clikit``: :class:`KgReview` is the gateway to its routes,
:class:`ReviewApply` the value object that turns the options of ``apply`` into its body.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from evo_agents.clikit import Arg, JsonOutput, Result, Table, Text, UsageError, View, Views, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.kg.resources import KG_PROJECT, KgResource
from evo_agents.kg.review import RULES

NODES_SHOWN = 3  # nodes a text line names per item; --json lists them all
REVIEW_QUEUE = ("graph", "computed", "reason", "can_dismiss", "counts", "items")
DISMISSAL = ("reason", "by", "at", "build_id")
UNDISMISSED = ("fingerprint", "was_by")
APPLY_RESULT = ("graph", "rev", "dry_run", "max_decisions", "decided", "skipped", "items", "report", "left")
FINGERPRINT_SAFE = ":"  # a fingerprint's ':' stays as it is in the path


class KgReview(KgResource):
    """The review queue of a project's graph: GET /v1/kg/{p}/review, POST and DELETE .../review/{fp}/dismiss, POST
    .../review/apply."""

    collection = "review"

    def queue(self, state: str, rule: str | None = None, cluster: str | None = None) -> dict:
        return self.call("GET", state=state, rule=rule, cluster=cluster)

    def dismiss(self, fingerprint: str, reason: str) -> dict:
        return self.call("POST", fingerprint, "dismiss", body={"reason": reason}, safe=FINGERPRINT_SAFE)

    def undismiss(self, fingerprint: str) -> dict:
        return self.call("DELETE", fingerprint, "dismiss", safe=FINGERPRINT_SAFE)

    def apply(self, body: dict) -> dict:
        return self.call("POST", "apply", body=body)


@dataclass(frozen=True)
class ReviewApply:
    """What ``review apply`` asks: the most decisions, of which rule, by which loop and run, and whether to decide."""

    origin: str
    max_decisions: int
    dry_run: bool
    run_id: int | None = None
    rule: str | None = None

    @classmethod
    def of(cls, args: argparse.Namespace) -> ReviewApply:
        return cls(args.origin, args.max, args.dry_run, args.run, args.rule or None)

    def body(self) -> dict:
        body: dict = {"origin": self.origin, "max_decisions": self.max_decisions, "dry_run": self.dry_run}
        if self.run_id is not None:
            body["run_id"] = self.run_id
        if self.rule:
            body["rules"] = [self.rule]
        return body


def names(nodes: list[dict]) -> str:
    return ", ".join(n["name"] or n["id"] for n in nodes[:NODES_SHOWN])


def item_what(item: dict) -> str:
    """The nodes of a queue item, and the mentions, samples or path its rule found."""
    detail = item["detail"]
    more = f" (+{len(item['nodes']) - NODES_SHOWN})" if len(item["nodes"]) > NODES_SHOWN else ""
    extra = []
    if detail.get("mentions"):
        extra.append(f"{detail['mentions']} mentions")
    if detail.get("samples"):
        extra.append("e.g. " + "; ".join(detail["samples"][:2]))
    if detail.get("path"):
        extra.append(detail["path"])
    return names(item["nodes"]) + more + (f": {', '.join(extra)}" if extra else "")


def subject(d: dict) -> str:
    if d["kind"] == "merge":
        return f"{d['src']} = {d['dst']}"
    return f"{d['src']} -{d['rel']}-> {d['dst']}" if d["rel"] else d["src"]


def queue_view(project: str, found: dict) -> View:
    if not found["computed"]:
        return Text(f"project {project} has no review queue: {found['reason']}")
    if found["items"]:
        rows = [
            (i["fingerprint"], i["rule"], i["cluster_label"] or "-", i["action"], item_what(i)) for i in found["items"]
        ]
        shown: View = Table(("ITEM", "RULE", "CLUSTER", "ACTION", "WHAT"), rows)
    else:
        shown = Text("no item to show")
    counts = ", ".join(f"{c['rule']} {c['open']} open, {c['dismissed']} dismissed" for c in found["counts"])
    return Views(shown, Text("\n" + counts))


def applied_view(found: dict) -> View:
    rows = [
        (d["number"] if d["number"] is not None else "-", d["kind"], i["rule"], subject(d), d["reason"])
        for i in found["items"]
        for d in i["decisions"]
    ]
    verb = "would take" if found["dry_run"] else "took"
    left = ", ".join(f"{rule} {n}" for rule, n in sorted(found["left"].items())) or "none"
    lines = [
        f"{verb} {found['decided']} decisions (at most {found['max_decisions']}), {found['skipped']} said already; "
        f"items with a fix left: {left}; decision revision {found['rev']}"
    ]
    for item in found["report"]:
        lines.append(
            f"report: {item['rule']} {item['cluster_label'] or item['cluster'] or '-'}: {names(item['nodes'])}"
        )
    return Views(Table(("DECISION", "KIND", "RULE", "SUBJECT", "REASON"), rows), Text(*lines))


class KgReviewCommand(HubCommand):
    name = "review"
    help = "the review queue of a project's graph you can see, or dismiss an item with `dismiss FP`"
    arguments = (
        Arg(
            "action",
            nargs="?",
            choices=["dismiss", "undismiss", "apply"],
            help="dismiss one item, take its dismissal back, or apply the open items' fixes as decisions (the writer role)",
        ),
        Arg("fingerprint", metavar="FINGERPRINT", nargs="?", help="with dismiss or undismiss: the item's fingerprint"),
        KG_PROJECT,
        Arg("--rule", choices=RULES, help="only the items of this rule"),
        Arg("--cluster", help="only the items of this cluster (its id)"),
        Arg("--state", choices=["open", "dismissed", "all"], default="open", help="which items to list (default open)"),
        Arg("--reason", help="with dismiss: why the item needs nothing done"),
        Arg("--max", type=int, default=25, help="with apply: the most decisions to take (default 25)"),
        Arg("--run", type=int, help="with apply: the run of the loop that applies them"),
        Arg("--origin", default="kg-optimize", help="with apply: the loop's name (default kg-optimize)"),
        Arg("--dry-run", action="store_true", help="with apply: say what would be decided, decide nothing"),
    )
    output = JsonOutput(
        "object",
        REVIEW_QUEUE,
        "ReviewQueue",
        variants=(
            ("dismiss", returns_object(*DISMISSAL, schema="Dismissal")),
            ("undismiss", returns_object(*UNDISMISSED, schema="Undismissed")),
            ("apply", returns_object(*APPLY_RESULT, schema="ApplyResult")),
        ),
    )
    json_help = (
        "machine-readable output: the queue, or with dismiss the dismissal, with undismiss who had dismissed it, "
        "with apply the decisions taken"
    )

    def check(self, args: argparse.Namespace) -> None:
        if args.action == "dismiss" and not (args.fingerprint and args.reason):
            raise UsageError("review dismiss needs an item's FINGERPRINT and --reason")
        if args.action == "undismiss" and not args.fingerprint:
            raise UsageError("review undismiss needs an item's FINGERPRINT")
        if args.action == "apply" and args.fingerprint:
            raise UsageError("review apply takes no FINGERPRINT")
        if args.action is None and args.fingerprint:
            raise UsageError("give an item after dismiss: review dismiss FINGERPRINT --reason TEXT")

    def run(self, ctx: HubContext) -> Result:
        args, review = ctx.args, KgReview.of(ctx)
        if args.action == "dismiss":
            found = review.dismiss(args.fingerprint, args.reason)
            return Result(found, Text(f"dismissed {args.fingerprint} in project {ctx.project}: {found['reason']}"))
        if args.action == "undismiss":
            found = review.undismiss(args.fingerprint)
            line = f"{args.fingerprint} in project {ctx.project} is open again; {found['was_by']} had dismissed it"
            return Result(found, Text(line))
        if args.action == "apply":
            found = review.apply(ReviewApply.of(args).body())
            return Result(found, applied_view(found))
        found = review.queue(args.state, args.rule or None, args.cluster or None)
        return Result(found, queue_view(ctx.project, found))


REVIEW = KgReviewCommand()
NODES = (REVIEW,)
