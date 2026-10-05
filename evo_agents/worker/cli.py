"""``evo-agents worker``: this machine as a worker of a hub.

- ``join --url URL --code CODE``: trade a pairing code from the web for a worker token.
- ``register --name NAME --project P [--project P ...] --slots N [--label L ...]``: register this machine directly,
  with the machine token of ``evo-agents hub login``.
- ``run``: the daemon in the foreground (``daemon``).
- ``status``: what this machine is to the hub, and what the daemon sees here.
- ``drain [--resume]``, ``revoke [--force]``: the owner's controls, with the machine token.

The worker token goes to ``~/.evo/worker/token`` (0600) and is never printed. A failure is one ``error:`` line on
stderr and exit status 1; a command that needs the worker extra and lacks it says how to install it (status 2).
``run`` needs the extra (aiohttp) and so does ``join``; the others need the core package only.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import platform
import sys
from datetime import datetime, timezone
from urllib.parse import quote

from evo_agents import __version__
from evo_agents.hub.client import Hub, HubError, check_url, host_name, load_credentials
from evo_agents.worker.home import PidLock, WorkerConfig, WorkerHome, WorkerStateError

EXIT_FAILED = 1
EXIT_USAGE = 2
EXTRA_HINT = "uv tool install 'evo-ak[worker]' (or python -m pip install 'evo-ak[worker]')"
MAX_SLOTS = 8


class _MissingExtra(Exception):
    pass


def _worker_command(func):
    """Turn the failures of ``func`` into an ``error:`` line and an exit status."""

    @functools.wraps(func)
    def run(args) -> int:
        try:
            return func(args)
        except _MissingExtra as exc:
            print(
                f"error: evo-agents worker {args.worker_command} needs the worker extra ({exc}): {EXTRA_HINT}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        except (HubError, WorkerStateError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_FAILED

    return run


def _hubapi():
    try:
        from evo_agents.worker import hubapi
    except ImportError as exc:
        raise _MissingExtra(f"missing module {exc.name}") from None
    return hubapi


def host_facts() -> dict:
    return {
        "hostname": host_name(),
        "os": (platform.system() or "unknown").lower()[:40],
        "arch": (platform.machine() or "unknown").lower()[:40],
        "agent_version": __version__,
    }


def _refuse_rejoin(home: WorkerHome, replace: bool) -> None:
    if not home.joined() or replace:
        return
    try:
        config = home.load_config()
        who = f"worker {config.name} (id {config.worker_id}) of {config.url}"
    except WorkerStateError:
        who = "a worker already"
    raise HubError(
        f"this machine is {who}: revoke it first (`evo-agents worker revoke`), or pass --replace to keep the old "
        "worker on the hub and use the new one here"
    )


def _config_of(url: str, answer: dict) -> tuple[WorkerConfig, str]:
    worker = answer.get("worker") if isinstance(answer, dict) else None
    token = answer.get("token") if isinstance(answer, dict) else None
    if not isinstance(worker, dict) or not isinstance(token, str) or not token:
        raise HubError(f"the hub at {url} did not answer with a worker and its token")
    config = WorkerConfig(
        url=url,
        worker_id=int(worker["id"]),
        name=str(worker["name"]),
        projects=list(worker.get("projects") or []),
        slots=int(worker.get("slots") or 1),
        labels=list(worker.get("labels") or []),
        allow_web_terminal=bool(worker.get("allow_web_terminal")),
        owner=worker.get("owner"),
        token_id=answer.get("token_id"),
        joined_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return config, token


def _member_hub(url: str) -> Hub | None:
    """The hub at ``url`` as the member signed in on this machine, when they are signed in to that hub."""
    try:
        credentials = load_credentials()
    except HubError:
        return None
    if credentials.url.rstrip("/") != url.rstrip("/"):
        return None
    return Hub(credentials.url, credentials.token)


def _owner_hub(config: WorkerConfig) -> Hub:
    hub = _member_hub(config.url)
    if hub is None:
        raise HubError(
            f"this needs the owner's machine token for {config.url}: run `evo-agents hub login --url {config.url}`, "
            "or do it on the web's Workers page"
        )
    return hub


def refresh_repos(config: WorkerConfig, hub: Hub | None = None) -> list[str]:
    """Keep the repos of the worker's projects, as the hub lists them, in ``config``; the projects it could not read.
    Needs a member of the hub signed in on this machine."""
    from evo_agents.worker.checkouts import hub_repos

    hub = hub or _member_hub(config.url)
    if hub is None:
        return list(config.projects)
    missed = []
    for project in config.projects:
        try:
            answer = hub.call("GET", f"/v1/projects/{quote(project, safe='')}")
        except HubError:
            missed.append(project)
            continue
        if isinstance(answer, dict):
            config.repos[project] = hub_repos(answer)
    return missed


def _print_checkouts(config: WorkerConfig) -> None:
    from evo_agents.worker.checkouts import discover

    found = discover(config)
    for key in sorted(found):
        print(f"  checkout {key}: {found[key]['path']}")
    missing = [p for p in config.projects if not any(key.startswith(f"{p}/") for key in found)]
    for project in missing:
        print(
            f"  no checkout of {project} found: run `evo-agents hub registry pull`, or clone its repos where the "
            "harness registry says"
        )


def _joined(home: WorkerHome, config: WorkerConfig, token: str, how: str) -> None:
    missed = refresh_repos(config)
    home.save(config, token)
    projects = ", ".join(config.projects) or "no project"
    print(f"{how} {config.url} as worker {config.name} (id {config.worker_id}) for {projects}, {config.slots} slot(s).")
    print(f"The worker token is in {home.token_path}.")
    if missed and _member_hub(config.url) is not None:
        print(f"  the repos of {', '.join(missed)} could not be read from the hub; the harness registry is used")
    _print_checkouts(config)
    print("Start it with `evo-agents worker run`.")


@_worker_command
def cmd_join(args) -> int:
    home = WorkerHome()
    _refuse_rejoin(home, args.replace)
    url = check_url(args.url)
    hubapi = _hubapi()

    async def join() -> dict:
        async with hubapi.new_session() as session:
            return await hubapi.WorkerHub(url, None, session).join(args.code, host_facts())

    try:
        answer = asyncio.run(join())
    except hubapi.HubProblem as exc:
        if exc.status == 429 and exc.retry_after is not None:
            raise HubError(f"{exc} (the hub says to wait {int(exc.retry_after)} seconds)") from None
        raise HubError(str(exc)) from None
    config, token = _config_of(url, answer)
    _joined(home, config, token, "Joined")
    return 0


@_worker_command
def cmd_register(args) -> int:
    home = WorkerHome()
    _refuse_rejoin(home, args.replace)
    credentials = load_credentials()
    hub = Hub(credentials.url, credentials.token)
    body = {
        "name": args.name,
        "projects": list(dict.fromkeys(args.project)),
        "slots": args.slots,
        "labels": list(dict.fromkeys(args.label or [])),
        "allow_web_terminal": args.allow_web_terminal,
        **host_facts(),
    }
    answer = hub.call("POST", "/v1/workers", body)
    config, token = _config_of(hub.url, answer)
    _joined(home, config, token, "Registered on")
    return 0


@_worker_command
def cmd_run(args) -> int:
    home = WorkerHome()
    config = home.load_config()
    token = home.load_token()
    try:
        import aiohttp  # noqa: F401

        from evo_agents.worker.daemon import Daemon
    except ImportError as exc:
        raise _MissingExtra(f"missing module {exc.name}") from None
    from evo_agents.worker import logs
    from evo_agents.worker.adapter import load_adapters

    home.ensure()
    lock = PidLock(home.pid_path)
    if not lock.acquire():
        raise HubError(f"a worker daemon runs here already (pid {home.read_pid() or 'unknown'})")
    try:
        secrets = [token]
        member = _member_hub(config.url)
        if member is not None and member.token:
            secrets.append(member.token)
        logs.configure(home.log_path, stderr=not args.quiet, secrets=secrets)
        if member is not None:
            refresh_repos(config, member)
            home.save(config)
        adapters = load_adapters()
        return asyncio.run(Daemon(home, config, token, adapters=adapters).run())
    except Exception:
        logging.getLogger("evo_agents.worker").exception("the worker daemon failed")
        return EXIT_FAILED
    finally:
        lock.release()


def _dir_bytes(path) -> int:
    total = 0
    if path.is_dir():
        for item in path.iterdir():
            if item.is_file():
                total += item.stat().st_size
    return total


@_worker_command
def cmd_status(args) -> int:
    from evo_agents.worker.adapter import detect_runtimes, load_adapters
    from evo_agents.worker.checkouts import discover

    home = WorkerHome()
    config = home.load_config()
    runtimes = detect_runtimes(load_adapters())
    found = discover(config)
    records = home.load_runs()
    pid = home.read_pid()
    hub_view = None
    hub_error = None
    member = _member_hub(config.url)
    if member is not None:
        try:
            hub_view = member.call("GET", f"/v1/workers/{config.worker_id}")
        except HubError as exc:
            hub_error = str(exc)
    status = {
        "url": config.url,
        "worker_id": config.worker_id,
        "name": config.name,
        "owner": config.owner,
        "projects": config.projects,
        "slots": config.slots,
        "labels": config.labels,
        "daemon_pid": pid,
        "runtimes": runtimes,
        "checkouts": found,
        "spool_bytes": _dir_bytes(home.spool_dir),
        "runs": [{k: r.get(k) for k in ("id", "state", "worktree", "finished_at")} for r in records],
        "hub": hub_view,
        "hub_error": hub_error,
        "state_dir": str(home.root),
    }
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    print(f"Worker {config.name} (id {config.worker_id}) of {config.url}, owner {config.owner or 'unknown'}")
    print(
        f"  projects: {', '.join(config.projects) or 'none'}; slots: {config.slots}; labels: "
        f"{', '.join(config.labels) or 'none'}"
    )
    print(f"  daemon: {'running, pid ' + str(pid) if pid else 'not running'}")
    if hub_view is not None:
        print(
            f"  on the hub: {hub_view.get('status')}, last heartbeat {hub_view.get('last_heartbeat_at') or 'never'}, "
            f"{hub_view.get('held_runs', 0)} run(s) held"
        )
    elif hub_error:
        print(f"  on the hub: unknown ({hub_error})")
    else:
        print(f"  on the hub: unknown (sign in with `evo-agents hub login --url {config.url}` to see it)")
    for name, report in runtimes.items():
        state = "available" if report["available"] else f"unavailable ({report.get('reason') or 'no reason given'})"
        print(f"  runtime {name}: {report.get('version') or '-'}, {state}")
    for key in sorted(found):
        print(f"  checkout {key}: {found[key]['path']} ({found[key].get('branch') or 'detached'})")
    if not found:
        print("  no checkout of the worker's projects found")
    unfinished = [r for r in records if not r.get("finished_at")]
    print(f"  runs kept here: {len(records)} ({len(unfinished)} not ended); spool: {status['spool_bytes']} bytes")
    print(f"  state: {home.root}")
    return 0


@_worker_command
def cmd_drain(args) -> int:
    home = WorkerHome()
    config = home.load_config()
    action = "undrain" if args.resume else "drain"
    answer = _owner_hub(config).call("POST", f"/v1/workers/{config.worker_id}/{action}")
    status = answer.get("status") if isinstance(answer, dict) else None
    if args.resume:
        print(f"Worker {config.name} claims runs again (status {status}).")
    else:
        print(
            f"Worker {config.name} is draining: it claims no new run and finishes the {answer.get('held_runs', 0)} "
            f"it holds (status {status}). Undo with `evo-agents worker drain --resume`."
        )
    return 0


@_worker_command
def cmd_revoke(args) -> int:
    home = WorkerHome()
    config = home.load_config()
    try:
        _owner_hub(config).call("POST", f"/v1/workers/{config.worker_id}/revoke")
        said = f"Revoked worker {config.name} (id {config.worker_id}) on {config.url}"
    except HubError as exc:
        if exc.status == 409:
            said = f"Worker {config.name} was revoked on {config.url} already"
        elif args.force:
            print(
                f"warning: the hub did not revoke the worker ({exc}); deleting its token here anyway", file=sys.stderr
            )
            said = f"Forgot worker {config.name} (id {config.worker_id}); revoke it on the web"
        else:
            raise
    home.forget()
    print(f"{said}; its token is deleted here. Runs and worktrees under {home.root} are removed by the cleanup.")
    pid = home.read_pid()
    if pid:
        print(f"The daemon running here (pid {pid}) stops at its next heartbeat.")
    return 0


def _slots(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number <= MAX_SLOTS:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {MAX_SLOTS}, not {value!r}")
    return number


def register(sub) -> None:
    worker = sub.add_parser(
        "worker",
        help="this machine as a worker of a hub: it runs plan steps the hub hands it (not `hub worker`, the "
        "server's job worker)",
    )
    wsub = worker.add_subparsers(dest="worker_command", required=True)

    join = wsub.add_parser("join", help="join a hub with a pairing code from its web's Workers page")
    join.add_argument("--url", required=True, help="the hub, such as https://hub.example.org")
    join.add_argument("--code", required=True, help="the pairing code, XXXX-XXXX; it works once, for 10 minutes")
    join.add_argument("--replace", action="store_true", help="join even when this machine is a worker already")
    join.set_defaults(func=cmd_join)

    reg = wsub.add_parser(
        "register", help="register this machine with the machine token of `evo-agents hub login`, without a code"
    )
    reg.add_argument("--name", required=True, help="the worker's name, unique among your workers")
    reg.add_argument(
        "--project", required=True, action="append", help="a project it takes runs of; repeat it for each one"
    )
    reg.add_argument("--slots", type=_slots, default=1, help="runs it holds at once, 1 to 8 (default 1)")
    reg.add_argument("--label", action="append", help="a label; repeat it for each one")
    reg.add_argument(
        "--allow-web-terminal", action="store_true", help="let the owner open a terminal on it from the web"
    )
    reg.add_argument("--replace", action="store_true", help="register even when this machine is a worker already")
    reg.set_defaults(func=cmd_register)

    run = wsub.add_parser(
        "run", help="run the daemon in the foreground: claim runs, run them, report; SIGTERM lets held runs end"
    )
    run.add_argument("--quiet", action="store_true", help="log to ~/.evo/worker/worker.log only, not to stderr")
    run.set_defaults(func=cmd_run)

    status = wsub.add_parser("status", help="this worker as the hub and this machine see it")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)

    from evo_agents.worker.selftest import add_parser as add_selftest  # `selftest --runtime NAME`

    add_selftest(wsub)

    drain = wsub.add_parser("drain", help="stop claiming new runs; the runs held finish (needs `hub login`)")
    drain.add_argument("--resume", action="store_true", help="claim runs again")
    drain.set_defaults(func=cmd_drain)

    revoke = wsub.add_parser("revoke", help="end this worker on the hub and delete its token here (needs `hub login`)")
    revoke.add_argument(
        "--force", action="store_true", help="delete the token here even when the hub cannot revoke the worker"
    )
    revoke.set_defaults(func=cmd_revoke)
