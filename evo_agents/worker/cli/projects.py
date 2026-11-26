"""``evo-agents worker projects``: the projects a worker serves, shown or changed in place by its owner, with their
machine token (PATCH /v1/workers/{id}, ``docs/workers.md``, The projects a worker serves); the worker keeps its id, its
token, its other settings and its secret bindings.

- ``projects``: the projects it serves, as the hub holds them.
- ``projects --add P --remove P``, each repeated as needed: the list changed. A project to remove that it does not serve
  is refused before anything is sent; one to add that it serves already changes nothing.

Without ``--worker`` it is this machine's worker, whose config.json takes the new list and the repos of its projects at
once, as ``join`` keeps them; ``--worker`` names another of the owner's workers, by id or name, from any machine
signed in to its hub, and its daemon takes the list from the settings of its next heartbeat answer.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text
from evo_agents.hub.api import paths
from evo_agents.hub.cli.run.workers import worker_id as named_worker
from evo_agents.hub.client import Hub, HubError, load_credentials
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext
from evo_agents.worker.home import WorkerConfig, WorkerHome
from evo_agents.worker.membership import checkout_lines, object_of, owner_hub, refresh_repos


def target_of(ctx: WorkerContext) -> tuple[Hub, int, WorkerHome | None, WorkerConfig | None]:
    """(the hub as the owner, the worker's id, this machine's worker home and config or None): the worker
    ``--worker`` names among the workers of the hub this machine is signed in to, else this machine's worker."""
    if ctx.args.worker is None:
        config = ctx.home.load_config()
        return owner_hub(config), config.worker_id, ctx.home, config
    credentials = load_credentials()
    hub = Hub(credentials.url, credentials.token)
    return hub, named_worker(hub, credentials.login, ctx.args.worker), None, None


class Projects(WorkerCommand):
    name = "projects"
    help = (
        "show or change the projects the worker serves, in place: its id, token and settings stay (needs `hub login`)"
    )
    arguments = (
        Arg("--add", action="append", metavar="PROJECT", help="a project to serve; repeat it"),
        Arg("--remove", action="append", metavar="PROJECT", help="a project to stop serving; repeat it"),
        Arg(
            "--worker",
            help="another of your workers, by id or name, on the hub this machine is signed in to (default: this "
            "machine's worker)",
        ),
    )

    def run(self, ctx: WorkerContext) -> Result:
        args = ctx.args
        hub, worker_id, home, config = target_of(ctx)
        path = paths.worker(worker_id)
        worker = object_of(hub, "GET", path)
        served = list(worker.get("projects") or [])
        added, removed = list(dict.fromkeys(args.add or [])), list(dict.fromkeys(args.remove or []))
        unknown = [project for project in removed if project not in served]
        if unknown:
            raise HubError(
                f"worker {worker['name']} does not serve {', '.join(unknown)}; it serves {', '.join(served) or 'none'}"
            )
        wanted = [project for project in served if project not in removed]
        wanted += [project for project in added if project not in wanted]
        if sorted(wanted) == sorted(served):
            if added or removed:
                said = f"Worker {worker['name']} (id {worker_id}) serves {', '.join(served)} already; nothing changed."
            else:
                said = f"Worker {worker['name']} (id {worker_id}) serves {', '.join(served) or 'no project'}."
            return Result(worker, Text(said))
        answer = object_of(hub, "PATCH", path, {"projects": wanted})
        now = list(answer.get("projects") or wanted)
        lines = [f"Worker {answer['name']} (id {worker_id}) serves {', '.join(now)} now."]
        if home is None or config is None:
            return Result(answer, Text(*lines, "Its daemon takes the new projects at its next heartbeat."))
        config.projects = now
        missed = refresh_repos(config, hub)
        home.save(config)
        if missed:
            lines.append(
                f"  the repos of {', '.join(missed)} could not be read from the hub; the harness registry is used"
            )
        return Result(answer, Text(*lines, *checkout_lines(config)))


PROJECTS = Projects()
