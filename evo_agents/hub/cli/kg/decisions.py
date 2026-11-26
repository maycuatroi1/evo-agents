"""``evo-agents hub kg decisions``: the decisions on a project's graph you can see on the hub, and a writer's own.

``decisions --project P`` lists them, newest first (GET /v1/kg/{project}/decisions); ``--state`` (proposed, active,
rejected, undone, orphan) narrows the list, and with ``--json`` it prints the array of the hub's decisions, each with
its ``state`` and history. A writer takes one with ``deprecate ID [--rel REL --to ID2]``, ``merge ID --to ID2`` or
``link ID --rel REL --to ID2``, and steps one with ``accept N``, ``reject N`` or ``undo N``; each needs ``--reason``
and names the decision revision it read: ``--base-rev``, else the revision the list gives just before
(``evo_agents.hub.server.kg_decisions``). A stale revision is refused and nothing changes. Standard library only.

:data:`DECISIONS` is a class command of ``evo_agents.clikit``: :class:`KgDecisions` is the gateway to its routes,
:class:`DecisionWrite` the value object that checks the options of a write and turns them into its body.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from evo_agents.clikit import Arg, JsonOutput, Result, Table, Text, UsageError, Views, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.kg.resources import KG_PROJECT, KgResource

TAKE = ("deprecate", "merge", "link")
STEP = ("accept", "reject", "undo")
STATES = ("proposed", "active", "rejected", "undone", "orphan")
DECISION = ("number", "kind", "state", "src", "rel", "dst", "history")


class KgDecisions(KgResource):
    """The decisions on a project's graph: GET and POST /v1/kg/{p}/decisions, POST .../decisions/{n}/{step}."""

    collection = "decisions"

    def list(self, state: str | None = None) -> dict:
        return self.call("GET", state=state)

    def take(self, body: dict) -> dict:
        return self.call("POST", body=body)

    def step(self, number: int, action: str, body: dict) -> dict:
        return self.call("POST", number, action, body=body)


def what(d: dict) -> str:
    """The node, edge or merge a decision is about."""
    if d["kind"] == "merge":
        return f"{d['src']} = {d['dst']}"
    if d["rel"] is not None:
        return f"{d['src']} -{d['rel']}-> {d['dst']}"
    return d["src"]


def problem(args: argparse.Namespace) -> str | None:
    """Why the options of ``decisions`` name no list and no write the hub takes; None when they do."""
    if args.action is None:
        return (
            "give an action before a target: decisions deprecate|merge|link ID, accept|reject|undo N"
            if (args.target)
            else None
        )
    if not args.target or not args.reason:
        return f"decisions {args.action} needs a target and --reason"
    if args.action in STEP and not args.target.isdigit():
        return f"decisions {args.action} needs a decision's number"
    if args.action in ("merge", "link") and not args.to:
        return f"decisions {args.action} needs --to"
    if args.action == "link" and not args.rel:
        return "decisions link needs --rel"
    if args.action == "deprecate" and bool(args.rel) != bool(args.to):
        return "decisions deprecate names a node, or an edge with both --rel and --to"
    return None


@dataclass(frozen=True)
class DecisionWrite:
    """A decision taken (deprecate, merge, link) or stepped (accept, reject, undo), against the revision read."""

    action: str
    target: str
    rel: str | None
    to: str | None
    reason: str

    @classmethod
    def of(cls, args: argparse.Namespace) -> DecisionWrite:
        return cls(args.action, args.target, args.rel, args.to, args.reason)

    def send(self, decisions: KgDecisions, base_rev: int) -> dict:
        if self.action in STEP:
            return decisions.step(int(self.target), self.action, {"reason": self.reason, "base_rev": base_rev})
        body = {"kind": self.action, "src": self.target, "rel": self.rel, "dst": self.to}
        return decisions.take({**body, "reason": self.reason, "base_rev": base_rev})


class KgDecisionsCommand(HubCommand):
    name = "decisions"
    help = (
        "the decisions on a project's graph you can see; a writer deprecates, merges, links, accepts, rejects or "
        "undoes one"
    )
    arguments = (
        Arg("action", nargs="?", choices=[*TAKE, *STEP], help="take or step a decision (the writer role)"),
        Arg(
            "target",
            metavar="TARGET",
            nargs="?",
            help="a node id for deprecate, merge and link; a number for the others",
        ),
        KG_PROJECT,
        Arg("--state", choices=STATES, help="only the decisions in this state"),
        Arg("--rel", help="the edge type: of the edge deprecated, or of the link"),
        Arg("--to", help="the other node: of the edge deprecated, merged or linked"),
        Arg("--reason", help="why; every write needs one"),
        Arg("--base-rev", type=int, help="the decision revision you read (default: the hub's now)"),
    )
    output = JsonOutput(
        "array",
        DECISION,
        "KgDecision",
        variants=tuple((a, returns_object(*DECISION, schema="KgDecision")) for a in (*TAKE, *STEP)),
    )
    json_help = "machine-readable output: the decisions, or with an action the decision it wrote"

    def check(self, args: argparse.Namespace) -> None:
        found = problem(args)
        if found:
            raise UsageError(found)

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        decisions = KgDecisions.of(ctx)
        if args.action is not None:
            base_rev = args.base_rev if args.base_rev is not None else decisions.list()["rev"]
            found = DecisionWrite.of(args).send(decisions, base_rev)
            return Result(
                found, Text(f"decision {found['number']} of project {ctx.project} is {found['state']}: {what(found)}")
            )
        found = decisions.list(args.state or None)
        listed = found["decisions"]
        rows = [(d["number"], d["kind"], d["state"], what(d), d["history"][0]["reason"]) for d in listed]
        shown = Table(("N", "KIND", "STATE", "WHAT", "WHY"), rows) if listed else Text("no decision to show")
        rev = f"\nrevision {found['rev']}" + ("" if found["can_decide"] else "; deciding needs the writer role")
        return Result(listed, Views(shown, Text(rev)))


DECISIONS = KgDecisionsCommand()
NODES = (DECISIONS,)
