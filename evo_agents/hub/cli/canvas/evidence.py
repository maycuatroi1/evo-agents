"""``hub canvas evidence add|withdraw|confirm``: the evidence of a claim."""

from __future__ import annotations

from evo_agents.clikit import Arg, Group, Result, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.canvas.keys import ADDED_KINDS, CLAIM, DIRECTIONS, EVIDENCE_KEYS, PROJECT, SLUG
from evo_agents.hub.cli.canvas.resources import Canvases
from evo_agents.hub.cli.canvas.views import evidence_view
from evo_agents.hub.cli.features import answer

EVIDENCE_OUTPUT = returns_object(*EVIDENCE_KEYS, schema="CanvasEvidence")
EVIDENCE_ID = Arg("id", type=int, metavar="ID", help="the evidence's id, as show --claim prints it")


class EvidenceAdd(HubCommand):
    name = "add"
    help = "a piece of evidence of a claim, as yours"
    arguments = (
        SLUG,
        CLAIM,
        PROJECT,
        Arg("--kind", required=True, choices=ADDED_KINDS),
        Arg("--direction", required=True, choices=DIRECTIONS),
        Arg("--summary", required=True, metavar="TEXT"),
        Arg("--url", metavar="URL"),
        Arg("--on", metavar="YYYY-MM-DD", help="the day it was taken"),
        Arg("--synthetic", action="store_true", help="it rests on simulated people"),
    )
    output = EVIDENCE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"kind": args.kind, "direction": args.direction, "summary": args.summary, "synthetic": args.synthetic}
        body |= {key: value for key, value in (("url", args.url), ("observed_on", args.on)) if value}
        return answer(ctx, Canvases.of(ctx).add_evidence(args.slug, args.claim, body), evidence_view)


class EvidenceWithdraw(HubCommand):
    name = "withdraw"
    help = "withdraw a piece of evidence, with why; it stays, marked"
    arguments = (SLUG, CLAIM, EVIDENCE_ID, PROJECT, Arg("--reason", required=True, metavar="TEXT"))
    output = EVIDENCE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        done = Canvases.of(ctx).withdraw_evidence(args.slug, args.claim, args.id, args.reason)
        return answer(ctx, done, evidence_view)


class EvidenceConfirm(HubCommand):
    name = "confirm"
    help = "vouch for evidence an agent added, so it may validate the claim"
    arguments = (SLUG, CLAIM, EVIDENCE_ID, PROJECT)
    output = EVIDENCE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        return answer(ctx, Canvases.of(ctx).confirm_evidence(args.slug, args.claim, args.id), evidence_view)


EVIDENCE = Group(
    "evidence",
    "the evidence of a claim: add, withdraw, confirm",
    [EvidenceAdd(), EvidenceWithdraw(), EvidenceConfirm()],
)
