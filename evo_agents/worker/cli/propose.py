"""``evo-agents worker propose``: the agent of a review run proposes a change with its draft plan, its promotion of
memories or its system change (POST /v1/worker/runs/{id}/proposals); the hub answers with its tier and state.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Result, Text
from evo_agents.hub.review import LENSES, MAX_BODY_BYTES
from evo_agents.hub.tiers import CHANGE_KINDS, split_target
from evo_agents.worker.agent_input import (
    attest,
    evidence_items,
    plan_file,
    probe_file,
    promotion_file,
    read_text,
    system_change_file,
)
from evo_agents.worker.cli.base import AgentCommand, AgentRunContext
from evo_agents.worker.home import WorkerStateError


def carried(args) -> dict:
    """What a proposal carries in place of the others: its draft plan, its promotion or its system change."""
    if args.plan_file:
        return {"plan": plan_file(args.plan_file)}
    if args.promote_file:
        return {"promotion": promotion_file(args.promote_file)}
    return {"system_change": system_change_file(args.system_change_file)}


def proposal_body(args) -> dict:
    """The proposal ``worker propose`` records, once what it rests on is given."""
    paths = []
    for text in args.path or []:
        found = split_target(text)
        if found is None:
            raise WorkerStateError(f"--path takes REPO:PATH, a path within a repo, not {text!r}")
        paths.append({"repo": found[0], "path": found[1]})
    evidence = evidence_items(args.evidence or [])
    if not evidence and not args.finding and args.fix_of is None:
        raise WorkerStateError(
            "a proposal rests on --finding ID, --evidence SPEC, or both: give at least one (a fix proposal, "
            "--fix-of CHANGE, rests on the probe results the hub adds)"
        )
    body: dict = {
        "lens": args.lens,
        "kind": args.kind,
        "title": " ".join(args.title.split()),
        "paths": paths,
        "finding_ids": list(dict.fromkeys(args.finding or [])),
        "evidence": evidence,
        **carried(args),
    }
    if args.summary_file:
        text = read_text(args.summary_file, "summary", MAX_BODY_BYTES)
        if text is not None:
            body["summary"] = text
    if args.probe_file:
        body["probe"] = probe_file(args.probe_file)
    if args.fix_of is not None:
        body["fix_of"] = args.fix_of
    return body


def proposed_lines(answer: dict) -> list[str]:
    """What ``worker propose`` prints of the hub's answer."""
    if answer.get("state") == "dropped":
        return [
            f"Proposal #{answer.get('id')} was dropped: it repeats proposal #{answer.get('duplicate_of')}, which the "
            "owner rejected lately, and its evidence is not twice as large."
        ]
    lines = [f"Proposal #{answer.get('id')} recorded: tier {answer.get('tier')}, {answer.get('state')}."]
    return lines + [f"  {reason}" for reason in answer.get("tier_reasons") or []]


class Propose(AgentCommand):
    name = "propose"
    help = "inside a review run: propose a change with its draft plan; prints its id, tier and state"
    kinds = ("review",)
    arguments = (
        Arg("--lens", required=True, choices=list(LENSES), help="the lens it comes from"),
        Arg("--kind", required=True, choices=list(CHANGE_KINDS), help="the kind of change"),
        Arg("--title", required=True, help="the change, in one line"),
    )
    after_draft = (
        Arg("--path", action="append", metavar="REPO:PATH", help="a file it would edit; repeat it"),
        Arg("--finding", action="append", type=int, metavar="ID", help="a finding it rests on; repeat it"),
        Arg("--evidence", action="append", metavar="SPEC", help="evidence of its own, as finding takes it; repeat it"),
        Arg("--summary-file", help="markdown: the problem, the change, what waiting costs (- for stdin)"),
        Arg(
            "--probe-file",
            help="the probe, YAML or JSON: repo, commit, task and verify; required when the evidence cites a "
            "correction and a path is an AGENTS.md, a CLAUDE.md, a skill or the repo's .claude settings or hooks",
        ),
        Arg(
            "--fix-of",
            type=int,
            metavar="CHANGE",
            help="the change the prompt asked you to fix, whose probe recurred two weeks in a row; at least tier 1, "
            "and the hub adds the probe results as evidence",
        ),
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        draft = parser.add_mutually_exclusive_group(required=True)
        draft.add_argument("--plan-file", help="the draft plan, YAML or JSON, in outcome steps")
        draft.add_argument("--promote-file", help="kind memory: the promotion of agents' memories, YAML or JSON")
        draft.add_argument(
            "--system-change-file",
            help="kind agent_config, lens agent_system: the change of an agent or a graph a system's curator "
            "optimizes, YAML or JSON",
        )
        for arg in self.after_draft:
            arg.add_to(parser)

    def run(self, ctx: AgentRunContext) -> Result:
        ctx.check()
        body = proposal_body(ctx.args)
        workspaces = ctx.workspaces()

        async def work(hub) -> dict:
            await attest(body["evidence"], workspaces)
            return await hub.proposal(ctx.run_id, body)

        answer = ctx.with_hub(work)
        return Result(answer, Text(*proposed_lines(answer)))


PROPOSE = Propose()
