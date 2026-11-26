"""``evo-agents kg connector``: connector tooling. ``test`` runs the conformance test of a connector: an ad hoc one
(``-- COMMAND``), a builtin one (``--builtin``) or a source the project declares (``--source``), checked against a
golden summary when one is given (``evo_agents.kg.protocol.conformance``)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evo_agents.clikit import Arg, Group, Result, Text
from evo_agents.kg.cli.base import WITH_PROJECT, Failed, KgCommand, KgContext, shown


class ConnectorTest(KgCommand):
    name = "test"
    help = "conformance test for a connector"
    arguments = (
        *WITH_PROJECT,
        Arg("--id", default="test", help="source id for an ad hoc connector (default: test)"),
        Arg("--builtin", help="test a builtin connector, e.g. git"),
        Arg("--source", help="test a source declared in the project's knowledge.yaml"),
        Arg("--config", help="JSON config for an ad hoc connector"),
        Arg("--label", help="declared label level, to check the connector never lowers it"),
        Arg("--golden", help="golden summary file to compare against"),
        Arg("--update-golden", action="store_true", help="rewrite the golden file"),
        Arg("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]"),
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg.protocol.conformance import run_conformance
        from evo_agents.kg.protocol.runner import ConnectorError, credential_env

        args = ctx.args
        harness = None
        env: dict = {}
        if args.command:
            command = args.command[1:] if args.command[0] == "--" else args.command
            config = json.loads(args.config) if args.config else {}
            source = {"id": args.id, "connector": "exec", "command": command, "config": config}
        elif args.builtin:
            source = {"id": args.id, "connector": args.builtin, **(json.loads(args.config) if args.config else {})}
        elif args.source:
            project = ctx.project
            harness = project.harness
            source = project.source(args.source)
            try:
                env = credential_env(source)
            except ConnectorError as exc:
                raise Failed(str(exc), 2) from exc
        else:
            raise Failed("give a command after --, --builtin NAME, or --source ID", 2)
        if args.label:
            source["label"] = {"level": args.label}
        report = run_conformance(
            source,
            harness=harness,
            env=env,
            golden=Path(args.golden) if args.golden else None,
            update_golden=args.update_golden,
        )
        lines = []
        for check in report.checks:
            lines.append(f"{'PASS' if check.ok else 'FAIL'} {check.name}")
            lines += [f"     {line}" for line in check.details]
        return shown(ctx, report.to_json(), Text(*lines), 0 if report.ok else 1)


CONNECTOR = Group("connector", "connector tooling", [ConnectorTest()])
