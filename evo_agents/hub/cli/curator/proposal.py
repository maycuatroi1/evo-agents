"""``evo-agents hub curator proposal list|show|accept|reject|defer|plan``: the proposals of a project's review runs.

``list`` filters by ``--state`` and ``--tier`` (each repeats), ``--lens`` and ``--run``; ``show ID`` prints one with
its tier's reasons, its evidence, its draft plan and the change accepting it made. ``accept``, ``reject`` and
``defer`` answer it (:class:`ProposalAnswer` builds the body), and ``plan ID`` makes an accepted proposal of a planned
tier that has no change yet the Curator's plan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, when
from evo_agents.hub.cli.curator.charter import labelled
from evo_agents.hub.cli.curator.resources import CHANGE, PROPOSAL, Proposals
from evo_agents.hub.review import DEFER_DAYS, LENSES, PROPOSAL_STATES, evidence_text

PROPOSAL_ID = Arg("id", type=int, help="the proposal's id")
NOTE = Arg("--note", help="why, in one line")
DAYS = Arg("--days", type=int, choices=range(DEFER_DAYS[0], DEFER_DAYS[1] + 1), metavar="N", help="1 to 90")


@dataclass(frozen=True)
class ProposalAnswer:
    """An answer to a proposal: its action, the note and, for defer, the days."""

    action: str
    note: str | None = None
    days: int | None = None

    def body(self) -> dict:
        body: dict = {"action": self.action}
        if self.note:
            body["note"] = self.note
        if self.action == "defer" and self.days is not None:
            body["defer_days"] = self.days
        return body


def _change_lines(change: dict) -> list[tuple[str, str]]:
    where = "no plan"
    if change["plan_id"]:
        where = f"plan {change['plan_id']} on {change['branch']} of {change['repo']}"
    pull = f", {change['pr_url']}" if change["pr_url"] else ""
    lines = [("change", f"#{change['id']}, {change['state']}: {where}{pull}")]
    if change["reason"]:
        lines.append(("reason", change["reason"]))
    return lines


def _facts(proposal: dict) -> list[tuple[str, str]]:
    """(label, value) of what ``proposal show`` lists under the title."""
    lines = [
        ("tier", str(proposal["tier"])),
        ("state", proposal["state"] + (f" (repeats #{proposal['duplicate_of']})" if proposal["duplicate_of"] else "")),
        ("kind", proposal["kind"] + (f" of #{proposal['revert_of']}" if proposal.get("revert_of") else "")),
        *([("fixes", f"change #{proposal['fix_of']}, whose probe recurred")] if proposal.get("fix_of") else []),
        ("lens", proposal["lens"]),
        ("run", f"#{proposal['run_id']}, {when(proposal['created_at'])} UTC"),
        ("paths", ", ".join(f"{p['repo']}:{p['path']}" for p in proposal["paths"]) or "none"),
        ("findings", ", ".join(f"#{item}" for item in proposal["finding_ids"]) or "none"),
        ("evidence", f"{proposal['evidence_count']} pieces"),
    ]
    if proposal.get("answered_by"):
        lines.append(("answered", f"by {proposal['answered_by']} at {when(proposal['answered_at'])} UTC"))
    if proposal.get("deferred_until"):
        lines.append(("deferred", f"until {when(proposal['deferred_until'])} UTC"))
    if proposal.get("note"):
        lines.append(("note", proposal["note"]))
    if proposal.get("change"):
        lines += _change_lines(proposal["change"])
    return lines


def proposal_lines(proposal: dict) -> list[str]:
    """The proposal as ``proposal show`` prints it."""
    lines = [f"Proposal #{proposal['id']} of {proposal['project']}: {proposal['title']}", *labelled(_facts(proposal))]
    lines.append("  why this tier:")
    lines += [f"    {reason}" for reason in proposal["tier_reasons"]]
    if proposal["evidence"]:
        lines.append("  its own evidence:")
        lines += [f"    {evidence_text(item)} ({item.get('resolved')})" for item in proposal["evidence"]]
    if proposal.get("summary"):
        lines.append("\n" + proposal["summary"].rstrip())
    lines += ["\nDraft plan:", json.dumps(proposal["plan"], ensure_ascii=False, indent=2)]
    return lines


class ProposalList(HubCommand):
    name = "list"
    help = "the proposals, newest first"
    arguments = (
        PROJECT,
        Arg("--state", action="append", choices=PROPOSAL_STATES, help="any of these states; repeat it"),
        Arg("--tier", action="append", type=int, choices=range(4), help="any of these tiers; repeat it"),
        Arg("--lens", choices=list(LENSES), help="of this lens"),
        Arg("--run", type=int, metavar="ID", help="of this review run"),
        Arg("--limit", type=int, default=50, choices=range(1, 201), metavar="N", help="1 to 200"),
        Arg("--offset", type=int, default=0, metavar="N", help="the proposals to pass over"),
    )
    output = returns_object("proposals", "total", "limit", "offset", "counts", schema="ProposalList")
    columns = ("ID", "TIER", "STATE", "KIND", "LENS", "RUN", "EVIDENCE", "TITLE")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Proposals.of(ctx).list(
            state=args.state or [],
            tier=args.tier or [],
            lens=args.lens,
            run_id=args.run,
            limit=args.limit,
            offset=args.offset or None,
        )
        rows = [self.row(proposal) for proposal in found["proposals"]]
        footer = Text(f"{len(rows)} of {found['total']} proposal(s) of {ctx.project}")
        return Result(found, Views(Table(self.columns, rows), footer))

    @staticmethod
    def row(p: dict) -> tuple:
        return (
            f"#{p['id']}",
            p["tier"],
            p["state"],
            p["kind"],
            p["lens"],
            f"#{p['run_id']}",
            p["evidence_count"],
            p["title"],
        )


class ProposalShow(HubCommand):
    name = "show"
    help = "one proposal: its tier and why, its evidence, its draft plan"
    arguments = (PROPOSAL_ID, PROJECT)
    output = PROPOSAL

    def run(self, ctx: HubContext) -> Result:
        proposal = Proposals.of(ctx).get(ctx.args.id)
        return Result(proposal, Text(*proposal_lines(proposal)))


def answered(ctx: HubContext, answer: ProposalAnswer) -> Result:
    """Give ``answer`` to the proposal ``ctx.args.id`` and say where it stands now."""
    proposal = Proposals.of(ctx).answer(ctx.args.id, answer.body())
    until = f" until {when(proposal['deferred_until'])} UTC" if proposal.get("deferred_until") else ""
    said = f"Proposal #{proposal['id']} of {ctx.project} is {proposal['state']}{until}: {proposal['title']}"
    return Result(proposal, Text(said))


class ProposalAccept(HubCommand):
    name = "accept"
    help = "accept it: one of tier 0 to 2 becomes the Curator's plan; an admin of the project only"
    arguments = (PROPOSAL_ID, NOTE, PROJECT)
    output = PROPOSAL

    def run(self, ctx: HubContext) -> Result:
        return answered(ctx, ProposalAnswer("accept", ctx.args.note))


class ProposalReject(HubCommand):
    name = "reject"
    help = "reject it: one like it is dropped for 30 days, unless its evidence doubles"
    arguments = (PROPOSAL_ID, NOTE, PROJECT)
    output = PROPOSAL

    def run(self, ctx: HubContext) -> Result:
        return answered(ctx, ProposalAnswer("reject", ctx.args.note))


class ProposalDefer(HubCommand):
    name = "defer"
    help = "defer it: it opens again after --days (7 by default)"
    arguments = (PROPOSAL_ID, NOTE, DAYS, PROJECT)
    output = PROPOSAL

    def run(self, ctx: HubContext) -> Result:
        return answered(ctx, ProposalAnswer("defer", ctx.args.note, ctx.args.days))


def planned_lines(change: dict, project: str) -> list[str]:
    """What ``proposal plan`` says of the change it made."""
    if change["plan_id"]:
        return [
            f"Proposal #{change['proposal_id']} of {project} is the Curator's plan {change['plan_id']}, on "
            f"{change['branch']} of {change['repo']}: change #{change['id']}, {change['state']}; the night shift "
            "builds it"
        ]
    lines = [
        f"Proposal #{change['proposal_id']} of {project} did not become the Curator's plan: change #{change['id']} is "
        f"{change['state']}, and nothing of it runs"
    ]
    if change["reason"]:
        lines.append(change["reason"])
    return lines


class ProposalPlan(HubCommand):
    name = "plan"
    help = "make an accepted proposal of tier 0 to 2 without a change the Curator's plan; an admin only"
    arguments = (PROPOSAL_ID, PROJECT)
    output = CHANGE

    def run(self, ctx: HubContext) -> Result:
        change = Proposals.of(ctx).plan(ctx.args.id)
        return Result(change, Text(*planned_lines(change, ctx.project)))


CURATOR_PROPOSAL = Group(
    "proposal",
    "the proposals of the review runs: list, show, accept, reject, defer, plan",
    [ProposalList(), ProposalShow(), ProposalAccept(), ProposalReject(), ProposalDefer(), ProposalPlan()],
)
