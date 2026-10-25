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

from evo_agents.hub.client import Hub, HubError, load_credentials
from evo_agents.hub.run_cli_workers import worker_id as named_worker
from evo_agents.worker.home import WorkerConfig, WorkerHome


def _target(args) -> tuple[Hub, int, WorkerHome | None, WorkerConfig | None]:
    """(the hub as the owner, the worker's id, this machine's worker home and config or None): the worker
    ``--worker`` names among the workers of the hub this machine is signed in to, else this machine's worker."""
    from evo_agents.worker.cli import owner_hub  # the cli imports this module

    if args.worker is None:
        home = WorkerHome()
        config = home.load_config()
        return owner_hub(config), config.worker_id, home, config
    credentials = load_credentials()
    hub = Hub(credentials.url, credentials.token)
    return hub, named_worker(hub, credentials.login, args.worker), None, None


def cmd_projects(args) -> int:
    from evo_agents.worker.cli import print_checkouts, refresh_repos

    hub, worker_id, home, config = _target(args)
    path = f"/v1/workers/{worker_id}"
    worker = hub.call("GET", path)
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
            print(f"Worker {worker['name']} (id {worker_id}) serves {', '.join(served)} already; nothing changed.")
        else:
            print(f"Worker {worker['name']} (id {worker_id}) serves {', '.join(served) or 'no project'}.")
        return 0
    answer = hub.call("PATCH", path, {"projects": wanted})
    now = list(answer.get("projects") or wanted)
    print(f"Worker {answer['name']} (id {worker_id}) serves {', '.join(now)} now.")
    if home is None:
        print("Its daemon takes the new projects at its next heartbeat.")
        return 0
    config.projects = now
    missed = refresh_repos(config, hub)
    home.save(config)
    if missed:
        print(f"  the repos of {', '.join(missed)} could not be read from the hub; the harness registry is used")
    print_checkouts(config)
    return 0


def register_projects(wsub) -> None:
    from evo_agents.worker.command import worker_command

    projects = wsub.add_parser(
        "projects",
        help="show or change the projects the worker serves, in place: its id, token and settings stay (needs "
        "`hub login`)",
    )
    projects.add_argument("--add", action="append", metavar="PROJECT", help="a project to serve; repeat it")
    projects.add_argument("--remove", action="append", metavar="PROJECT", help="a project to stop serving; repeat it")
    projects.add_argument(
        "--worker",
        help="another of your workers, by id or name, on the hub this machine is signed in to (default: this "
        "machine's worker)",
    )
    projects.set_defaults(func=worker_command(cmd_projects))
