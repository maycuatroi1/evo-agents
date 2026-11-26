"""``evo-agents hub curator findings`` and ``curator figures``: what the review runs found, and what a night counted."""

from __future__ import annotations

import json

from evo_agents.clikit import Arg, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, when
from evo_agents.hub.cli.curator.resources import Figures, Findings
from evo_agents.hub.review import LENSES


class CuratorFindings(HubCommand):
    name = "findings"
    help = "what the review runs found, newest first"
    arguments = (
        PROJECT,
        Arg("--run", type=int, metavar="ID", help="of this review run"),
        Arg("--lens", choices=list(LENSES), help="of this lens"),
        Arg("--limit", type=int, default=50, choices=range(1, 201), metavar="N", help="1 to 200"),
        Arg("--offset", type=int, default=0, metavar="N", help="the findings to pass over"),
    )
    output = returns_object("findings", "total", "limit", "offset", schema="FindingList")
    columns = ("ID", "SEVERITY", "LENS", "RUN", "EVIDENCE", "TITLE")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Findings.of(ctx).list(args.run, args.lens, args.limit, args.offset or None)
        rows = [self.row(finding) for finding in found["findings"]]
        footer = Text(f"{len(rows)} of {found['total']} finding(s) of {ctx.project}")
        return Result(found, Views(Table(self.columns, rows), footer))

    @staticmethod
    def row(f: dict) -> tuple:
        return (f"#{f['id']}", f["severity"], f["lens"], f"#{f['run_id']}", len(f["evidence"]), f["title"])


class CuratorFigures(HubCommand):
    name = "figures"
    help = "the figures curator.collect counted for the latest night, or --night"
    arguments = (PROJECT, Arg("--night", metavar="YYYY-MM-DD", help="the night, the local date its window opened"))
    output = returns_object(
        "project", "night", "since", "until", "run_id", "figures", "created_at", schema="NightFiguresView"
    )

    def run(self, ctx: HubContext) -> Result:
        found = Figures.of(ctx).get(ctx.args.night)
        review = f", review run #{found['run_id']}" if found.get("run_id") else ""
        head = (
            f"Figures of {found['project']} for the night of {found['night']}{review}: sessions and runs from "
            f"{when(found['since'])} to {when(found['until'])} UTC"
        )
        return Result(found, Text(head, json.dumps(found["figures"], ensure_ascii=False, indent=2)))
