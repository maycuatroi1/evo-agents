"""``hub canvas proposal list|accept|reject``, ``baseline list|show|open|decide`` and ``brainstorm``: what @strategist
and the hub propose, and the reviews that put a claim's status in effect."""

from __future__ import annotations

from evo_agents.clikit import Arg, Group, Result, Table, Text, returns_array, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.canvas.keys import (
    BASELINE_DECISIONS,
    BASELINE_KEYS,
    BASELINE_SUMMARY_KEYS,
    BRAINSTORM_KEYS,
    CELLS,
    CLAIM,
    PROJECT,
    PROPOSAL_KEYS,
    PROPOSAL_STATUSES,
    SLUG,
)
from evo_agents.hub.cli.canvas.resources import Canvases
from evo_agents.hub.cli.canvas.views import baseline_view, brainstorm_view, proposal_lines, proposal_view
from evo_agents.hub.cli.features import answer

PROPOSAL_OUTPUT = returns_object(*PROPOSAL_KEYS, schema="CanvasProposal")
PROPOSAL_ID = Arg("id", type=int, metavar="ID", help="the proposal's id, as proposal list prints it")
BASELINE_OUTPUT = returns_object(*BASELINE_KEYS, schema="CanvasBaseline")
NUMBER = Arg("number", type=int, metavar="N", help="the baseline's number")


class ProposalList(HubCommand):
    name = "list"
    help = "the proposals of a canvas, newest first; the open ones by default"
    arguments = (
        SLUG,
        PROJECT,
        Arg("--status", action="append", choices=PROPOSAL_STATUSES, help="these statuses; repeatable"),
    )
    output = returns_array(*PROPOSAL_KEYS, schema="CanvasProposal")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Canvases.of(ctx).proposals(args.slug, args.status or [])
        return answer(ctx, found, lambda items: Text(*self.lines(items, args.status, args.slug)))

    @staticmethod
    def lines(found: list[dict], statuses: list[str] | None, slug: str) -> list[str]:
        lines = [] if found else [f"No {' or '.join(statuses or ('open',))} proposal of canvas {slug}."]
        return lines + [line for item in found for line in proposal_lines(item)]


class ProposalAccept(HubCommand):
    name = "accept"
    help = "write an open proposal as your own rev"
    arguments = (SLUG, PROPOSAL_ID, PROJECT)
    output = PROPOSAL_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        return answer(ctx, Canvases.of(ctx).accept(ctx.args.slug, ctx.args.id), proposal_view)


class ProposalReject(HubCommand):
    name = "reject"
    help = "close an open proposal without writing it"
    arguments = (SLUG, PROPOSAL_ID, PROJECT, Arg("--reason", metavar="TEXT", help="why, kept with the proposal"))
    output = PROPOSAL_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        return answer(ctx, Canvases.of(ctx).reject(args.slug, args.id, args.reason or ""), proposal_view)


PROPOSAL = Group(
    "proposal",
    "what an agent or the hub proposed: list, accept, reject",
    [ProposalList(), ProposalAccept(), ProposalReject()],
)

BASELINE_HEADER = ("number", "kind", "week", "status", "items", "waiting", "created")


class BaselineList(HubCommand):
    name = "list"
    help = "the baselines of a canvas, newest first"
    arguments = (SLUG, PROJECT)
    output = returns_array(*BASELINE_SUMMARY_KEYS, schema="CanvasBaselineSummary")

    def run(self, ctx: HubContext) -> Result:
        return answer(ctx, Canvases.of(ctx).baselines(ctx.args.slug), self.table)

    @staticmethod
    def table(found: list[dict]) -> Table:
        rows = [
            (b["number"], b["kind"], b["week"] or "", b["status"], b["item_count"], b["waiting"], b["created_at"][:16])
            for b in found
        ]
        return Table(BASELINE_HEADER, rows)


class BaselineShow(HubCommand):
    name = "show"
    help = "a baseline with its items and what you may decide of each"
    arguments = (SLUG, NUMBER, PROJECT)
    output = BASELINE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        return answer(ctx, Canvases.of(ctx).baseline(ctx.args.slug, ctx.args.number), baseline_view)


class BaselineOpen(HubCommand):
    name = "open"
    help = "open a baseline of one claim at once (a member, never a run)"
    arguments = (SLUG, CLAIM, PROJECT)
    output = BASELINE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        return answer(ctx, Canvases.of(ctx).open_baseline(ctx.args.slug, ctx.args.claim), baseline_view)


class BaselineDecide(HubCommand):
    name = "decide"
    help = "decide the item of one claim (a member, never a run)"
    arguments = (
        SLUG,
        NUMBER,
        CLAIM,
        Arg("decision", choices=BASELINE_DECISIONS),
        PROJECT,
        Arg("--deadline", metavar="YYYY-MM-DD", help="the new deadline of an extension"),
        Arg("--reason", metavar="TEXT", help="why, kept with the decision"),
    )
    output = BASELINE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"decision": args.decision, "deadline": args.deadline, "reason": args.reason or ""}
        return answer(ctx, Canvases.of(ctx).decide(args.slug, args.number, args.claim, body), baseline_view)


BASELINE = Group(
    "baseline",
    "the reviews that put claims' statuses in effect: list, open, decide",
    [BaselineList(), BaselineShow(), BaselineOpen(), BaselineDecide()],
)


class Brainstorm(HubCommand):
    name = "brainstorm"
    help = "hand @strategist a brainstorm of a canvas; it only proposes"
    arguments = (
        SLUG,
        PROJECT,
        Arg("--focus", metavar="TEXT", help="what to think about"),
        Arg("--cell", action="append", choices=CELLS, help="one cell to look at; repeatable"),
    )
    output = returns_object(*BRAINSTORM_KEYS, schema="CanvasBrainstorm")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"focus": args.focus or "", "cells": list(args.cell or ())}
        return answer(ctx, Canvases.of(ctx).brainstorm(args.slug, body), brainstorm_view)
