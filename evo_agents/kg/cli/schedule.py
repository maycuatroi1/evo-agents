"""``evo-agents kg schedule``: print, install or uninstall the hourly LaunchAgent that runs ``kg sync --due --all``
(``evo_agents.kg.schedule``, macOS)."""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text, Verbatim
from evo_agents.kg.cli.base import Failed, KgCommand, KgContext


class KgSchedule(KgCommand):
    name = "schedule"
    help = "hourly LaunchAgent running kg sync --due --all: it syncs, and the hub builds (macOS)"
    arguments = (
        Arg("action", choices=["print", "install", "uninstall"], help="print the plist, or load or unload it"),
    )

    def run(self, ctx: KgContext) -> Result:
        from evo_agents.kg import schedule

        if ctx.args.action == "print":
            return Result(None, Verbatim(schedule.render(schedule.build_plist())))
        try:
            if ctx.args.action == "install":
                path = schedule.install()
                plist = schedule.build_plist()
                return Result(
                    None,
                    Text(
                        f"installed {schedule.LABEL} from {path}",
                        f"runs every {schedule.INTERVAL // 60} min: {' '.join(plist['ProgramArguments'])}",
                        f"log: {plist['StandardOutPath']}",
                    ),
                )
            existed = schedule.uninstall()
        except schedule.ScheduleError as exc:
            raise Failed(str(exc)) from exc
        return Result(None, Text(f"uninstalled {schedule.LABEL}" if existed else f"{schedule.LABEL} was not installed"))


SCHEDULE = KgSchedule()
