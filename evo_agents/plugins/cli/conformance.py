"""``evo-agents plugin test DIR``: the conformance checks of ``evo_agents.plugins.conformance``; exit 1 when one
fails."""

from __future__ import annotations

from evo_agents.clikit import Arg, Context, Result, Text
from evo_agents.plugins.cli.base import JSON, PluginCommand


class Conformance(PluginCommand):
    name = "test"
    help = "check that a plugin directory speaks plugin/1 and packs into a bundle"
    arguments = (
        Arg("directory", metavar="DIR", help="the plugin directory, evo-plugin.json at its top"),
        Arg("--timeout", type=float, default=10.0, help="seconds the backend has for each answer"),
        JSON,
    )

    def run(self, ctx: Context) -> Result:
        from evo_agents.plugins.conformance import run_conformance

        report = run_conformance(ctx.args.directory, timeout=ctx.args.timeout)
        lines = []
        for check in report.checks:
            lines.append(f"{'OK  ' if check.ok else 'FAIL'} {check.name}")
            lines.extend(f"       {detail}" for detail in check.details)
        failed = [check.name for check in report.checks if not check.ok]
        lines.append(
            f"{len(report.checks)} check(s), {len(failed)} failed" + (f": {', '.join(failed)}" if failed else "")
        )
        return Result(report.to_json(), Text(*lines), 0 if report.ok else 1)


TEST = Conformance()
