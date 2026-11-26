"""``hub srs findings|waive``: the conflicts @ba found in a document, how each was settled, and an admin's waiver of
an open one; an open finding of level decide holds approval."""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text, returns_array, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.features import answer
from evo_agents.hub.cli.srs.keys import FINDING_KEYS, FINDING_STATUSES, PROJECT, SLUG
from evo_agents.hub.cli.srs.resources import Documents
from evo_agents.hub.cli.srs.views import finding_lines


def findings_view(slug: str, status: str | None, found: list[dict]) -> Text:
    if not found:
        return Text(f"{slug} has no finding{' ' + status if status else ''}.")
    holding = [f for f in found if f["level"] == "decide" and f["status"] == "open"]
    lines = [f"{len(holding)} open finding(s) of level decide hold the approval of {slug}."] if holding else []
    return Text(*lines, *(line for finding in found for line in finding_lines(finding)))


class Findings(HubCommand):
    name = "findings"
    help = "the conflicts @ba found in a document, and how each was settled"
    arguments = (SLUG, PROJECT, Arg("--status", choices=FINDING_STATUSES, help="only the findings in this status"))
    output = returns_array(*FINDING_KEYS, schema="SrsFinding")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Documents.of(ctx).findings(args.slug, args.status)
        return answer(ctx, found, lambda listed: findings_view(args.slug, args.status, listed))


class Waive(HubCommand):
    name = "waive"
    help = "waive an open finding, so it holds no approval (an admin)"
    arguments = (
        SLUG,
        Arg("finding", type=int, metavar="FINDING", help="the finding's id, as `srs findings` prints it"),
        PROJECT,
        Arg("--reason", required=True, metavar="TEXT", help="why, kept as the finding's resolution"),
    )
    output = returns_object(*FINDING_KEYS, schema="SrsFinding")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Documents.of(ctx).waive(args.slug, args.finding, args.reason)
        return answer(
            ctx,
            found,
            lambda done: Text(
                f"Waived finding {done['id']} of {args.slug}: it holds no approval now.", *finding_lines(done)
            ),
        )
