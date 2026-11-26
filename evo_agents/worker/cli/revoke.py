"""``evo-agents worker revoke [--force]``: the owner ends this worker on the hub, with the machine token of
``evo-agents hub login``, and its token is deleted here; ``--force`` deletes it even when the hub cannot revoke it."""

from __future__ import annotations

from pathlib import Path

from evo_agents.clikit import Arg, Result, Stderr, Text, View, Views
from evo_agents.hub.api import paths
from evo_agents.hub.client import HubError
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext
from evo_agents.worker.membership import owner_hub


def service_file() -> Path | None:
    """The file of the background service when one is installed for this user, without asking its manager."""
    from evo_agents.worker import service

    try:
        path = service.manager().file()
    except service.ServiceError:  # neither launchd nor systemd here
        return None
    return path if path.exists() else None


class Revoke(WorkerCommand):
    name = "revoke"
    help = "end this worker on the hub and delete its token here (needs `hub login`)"
    arguments = (
        Arg("--force", action="store_true", help="delete the token here even when the hub cannot revoke the worker"),
    )

    def run(self, ctx: WorkerContext) -> Result:
        home = ctx.home
        config = home.load_config()
        warned: list[View] = []
        try:
            owner_hub(config).call("POST", paths.worker(config.worker_id, "revoke"))
            said = f"Revoked worker {config.name} (id {config.worker_id}) on {config.url}"
        except HubError as exc:
            if exc.status == 409:
                said = f"Worker {config.name} was revoked on {config.url} already"
            elif ctx.args.force:
                warned.append(
                    Stderr(Text(f"warning: the hub did not revoke the worker ({exc}); deleting its token here anyway"))
                )
                said = f"Forgot worker {config.name} (id {config.worker_id}); revoke it on the web"
            else:
                raise
        home.forget()
        lines = [f"{said}; its token is deleted here. Runs and worktrees under {home.root} are removed by the cleanup."]
        pid = home.read_pid()
        if pid:
            lines.append(f"The daemon running here (pid {pid}) stops at its next heartbeat.")
        installed = service_file()
        if installed is not None:
            lines.append(
                f"The background service ({installed}) does not start the daemon again; remove it with "
                "`evo-agents worker service uninstall`."
            )
        return Result(None, Views(*warned, Text(*lines)))


REVOKE = Revoke()
