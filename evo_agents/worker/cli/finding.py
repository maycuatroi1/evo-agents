"""``evo-agents worker finding``: the agent of a review run records a finding with its evidence
(POST /v1/worker/runs/{id}/findings); code evidence is checked against the run's worktrees first (``attest``).
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text
from evo_agents.hub.review import LENSES, MAX_BODY_BYTES, SEVERITIES
from evo_agents.worker.agent_input import attest, evidence_items, read_text
from evo_agents.worker.cli.base import AgentCommand, AgentRunContext


class Finding(AgentCommand):
    name = "finding"
    help = "inside a review run: record a finding with its evidence; prints its id"
    kinds = ("review",)
    arguments = (
        Arg("--lens", required=True, choices=list(LENSES), help="the lens it was found through"),
        Arg("--title", required=True, help="what was found, in one line"),
        Arg(
            "--evidence",
            action="append",
            required=True,
            metavar="SPEC",
            help="session:ID[:FIELD:INDEX], transcript:ID[:FILE]:LINE, run:ID:SEQ or code:REPO:PATH[:LINE]; repeat it",
        ),
        Arg("--severity", choices=SEVERITIES, default="medium", help="low, medium (default) or high"),
        Arg("--body-file", help="markdown that explains it, at most 16 KiB (- for stdin)"),
        Arg(
            "--fix-of",
            type=int,
            metavar="CHANGE",
            help="the change the prompt asked you to fix: why you propose no fix of its rule; the hub adds the probe "
            "results",
        ),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        ctx.check()
        args = ctx.args
        body: dict = {"lens": args.lens, "severity": args.severity, "title": " ".join(args.title.split())}
        if args.body_file:
            text = read_text(args.body_file, "body", MAX_BODY_BYTES)
            if text is not None:
                body["body"] = text
        body["evidence"] = evidence_items(args.evidence)
        if args.fix_of is not None:
            body["fix_of"] = args.fix_of
        workspaces = ctx.workspaces()

        async def work(hub) -> dict:
            await attest(body["evidence"], workspaces)
            return await hub.finding(ctx.run_id, body)

        answer = ctx.with_hub(work)
        return Result(
            answer, Text(f"Finding #{answer.get('id')} recorded ({args.lens}, {args.severity}): {body['title']}")
        )


FINDING = Finding()
