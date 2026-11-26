"""``evo-agents worker service install|uninstall|status``: the worker daemon as a background service of this user, a
LaunchAgent on macOS and a systemd user unit on Linux (``evo_agents.worker.service``, which writes and reads them).

``--agent HANDLE`` names the node of an agent, a service of its own (default: EVO_WORKER_AGENT, else the one node of
the machine). A service that cannot be installed, removed or read is one ``error:`` line and exit status 1;
``install`` without the worker extra exits 2.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from evo_agents.clikit import Arg, Group, Result, Stderr, Text, Views
from evo_agents.worker import service
from evo_agents.worker.cli.base import EXIT_FAILED, EXIT_USAGE, WorkerCommand, WorkerContext
from evo_agents.worker.extra import EXTRA_HINT
from evo_agents.worker.home import HOME_VARIABLE

AGENT = Arg(
    "--agent",
    metavar="HANDLE",
    help="the node of this agent (handle): a service of its own, next to the nodes of other agents; needs "
    f"{HOME_VARIABLE} for its state directory. Default: ${service.AGENT_VARIABLE}, else the one node of the machine",
)


def manager_of(ctx: WorkerContext) -> service.Manager:
    """The manager of the node ``--agent`` names (default: ``$EVO_WORKER_AGENT``, else the one node of the machine)."""
    return service.manager(agent=getattr(ctx.args, "agent", None))


def installed_lines(manager: service.Manager, spec: service.Spec) -> list[str]:
    """What ``install`` says of the service it installed, but how its daemon is."""
    found = service.runtimes_on(spec.path)
    lines = [
        f"Installed {manager.service} ({manager.name}): {manager.file()}",
        f"  runs: {shlex.join(spec.program)}, now and at each login, and again {service.RESTART_SECONDS} s after it "
        "exits with an error",
        f"  on the PATH it keeps: {', '.join(f'{name} {path}' for name, path in found.items() if path) or 'none'}",
    ]
    absent = [name for name, path in found.items() if not path]
    if absent:
        lines.append(f"  not on that PATH: {', '.join(absent)}; install again once they are, so the daemon finds them")
    if spec.kept:
        lines.append(f"  kept from the service installed before: {', '.join(spec.kept)}")
    lines += [f"  state: {spec.home}", f"  log: {service.logs_of(manager, spec.home)}"]
    if isinstance(manager, service.Systemd) and not service.Systemd.lingering():
        lines.append(
            "  systemd stops user services when you log out; to keep the worker running without a session: "
            "loginctl enable-linger"
        )
    return lines


def warning_lines(spec: service.Spec) -> list[str]:
    """The warnings of ``install``: the doctor's high and medium findings, and a daemon that cannot update itself."""
    try:
        warned = service.doctor_warnings(spec)
    except Exception as exc:  # the doctor only warns: nothing it meets stops the install
        warned = [f"warning: `evo-agents worker doctor` did not finish ({type(exc).__name__}: {exc})"]
    try:
        stays = service.self_update_warning()
    except Exception as exc:  # it only warns, like the doctor
        stays = f"warning: could not read whether the daemon can update itself ({type(exc).__name__}: {exc})"
    return [*warned, *([stays] if stays else [])]


class Install(WorkerCommand):
    name = "install"
    help = (
        "run the daemon now and at each login, again after it exits with an error, with PATH as it is now; "
        "installing again keeps the EVO_WORKER_* variables the installed service sets"
    )
    word = "service"
    problems = (service.ServiceError,)
    arguments = (AGENT,)

    def run(self, ctx: WorkerContext) -> Result:
        home = ctx.home
        manager = manager_of(ctx)
        if manager.agent and not os.environ.get(HOME_VARIABLE):
            raise service.ServiceError(
                f"the node of agent {manager.agent} needs a state directory of its own: set {HOME_VARIABLE} (for "
                f"example ~/.evo/worker-{manager.agent}) for `worker join` and for this command, so it does not share "
                "the directory of another node"
            )
        home.load_config()  # a machine that is not a worker yet: the daemon would only fail and be started again
        home.load_token()
        missing = service.missing_extra()
        if missing:
            said = f"error: evo-agents worker service install needs the worker extra (missing module {missing}): "
            return Result(None, Stderr(Text(said + EXTRA_HINT)), EXIT_USAGE)
        service.refuse_foreign_daemon(home, manager)
        spec = service.build_spec(home, kept=manager.installed_environment(), agent=manager.agent)
        home.ensure()
        status = manager.install(spec)
        installed = Text(*installed_lines(manager, spec))
        warnings = warning_lines(spec)
        if not status.running:
            said = (
                f"error: the service is installed, but the daemon is {service.describe(status)}: see "
                f"{service.logs_of(manager, spec.home)}"
            )
            return Result(None, Views(installed, Stderr(Text(*warnings, said))), EXIT_FAILED)
        return Result(None, Views(installed, Stderr(Text(*warnings)), Text(f"  daemon: {service.describe(status)}")))


class Uninstall(WorkerCommand):
    name = "uninstall"
    help = "stop the daemon and remove the service"
    word = "service"
    problems = (service.ServiceError,)
    arguments = (AGENT,)

    def run(self, ctx: WorkerContext) -> Result:
        manager = manager_of(ctx)
        if manager.stop():
            said = (
                f"Uninstalled {manager.service} ({manager.name}): the daemon is stopped and {manager.file()} deleted."
            )
        else:
            said = f"{manager.service} ({manager.name}) is not installed."
        return Result(None, Text(said))


class Status(WorkerCommand):
    name = "status"
    help = "whether the service is installed and the daemon runs"
    word = "service"
    problems = (service.ServiceError,)
    arguments = (Arg("--json", action="store_true", help="machine-readable output"), AGENT)

    def run(self, ctx: WorkerContext) -> Result:
        manager = manager_of(ctx)
        status = manager.status()
        home = manager.installed_home() or Path(os.path.abspath(ctx.home.root))
        report = {**status.to_json(), "program": manager.program(), "home": str(home), "log": str(home / "worker.log")}
        if not status.installed and not status.loaded:
            said = (
                f"{manager.service} ({manager.name}) is not installed: `evo-agents worker service install` starts it."
            )
            return Result(report, Text(said))
        lines = [
            f"{manager.service} ({manager.name}): {service.describe(status)}",
            f"  file: {status.file}{'' if status.installed else ' (missing; uninstall, then install again)'}",
        ]
        program = report["program"]
        if program:
            lines.append(f"  runs: {program}")
        if status.restarts:
            lines.append(f"  started again {status.restarts} time(s) since it was loaded")
        if status.running and status.last_exit not in (None, "0"):
            lines.append(f"  last exit: {status.last_exit}")
        lines += [f"  state: {home}", f"  log: {service.logs_of(manager, home)}"]
        return Result(report, Text(*lines))


INSTALL, UNINSTALL, STATUS = Install(), Uninstall(), Status()
SERVICE = Group(
    "service",
    "keep the daemon running in the background: a LaunchAgent on macOS, a systemd user unit on Linux",
    (INSTALL, UNINSTALL, STATUS),
    dest="service_command",
)
