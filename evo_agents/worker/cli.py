"""``evo-agents worker``: this machine as a worker of a hub.

- ``join --url URL --code CODE``: trade a pairing code from the web for a worker token.
- ``register --name NAME --project P [--project P ...] --slots N [--label L ...]``: register this machine directly,
  with the machine token of ``evo-agents hub login``.
- ``run``: the daemon in the foreground (``daemon``).
- ``status``: what this machine is to the hub, and what the daemon sees here.
- ``doctor [--json]``: what this machine holds or allows that a worker should not, such as another administrator or a
  long-lived credential of its owner (``doctor``); exit status 2 when a finding is high.
- ``attach N``: this terminal on the tmux session of run N while a person drives its agent (``evo-run-N``).
- ``drain [--resume]``, ``revoke [--force]``, ``projects``: the owner's controls, with the machine token.
- ``step KEY STATUS``, ``ask``, ``wait``, ``notify``, ``plan``: the commands of a plan run's agent, which report a
  step (the verify commands run again, the repo committed and pushed), ask the run's owner a decision, wait for a time
  or an event of the hub (``evo_agents.hub.server.run_waits``), send the owner a notice, and print the plan as the hub
  holds it now. They read the run from EVO_RUN_ID, EVO_WORKER_HOME and the run's record there, call the hub with the
  worker's token, and refuse to run outside a plan run of this worker; ``ask`` and ``plan`` work inside an author run
  too, whose agent asks its owner a question with options as a decision. ``ask`` and ``wait`` note the decision's id in
  the run's file of decisions (``home.decisions_path``), which the daemon waits on.
- ``finding`` and ``propose``: the commands of a review run's agent (the Curator's Reviewer), which record a finding
  and a proposal with their evidence (``evo_agents.hub.review.parse_evidence``); the hub computes a proposal's tier.
  Code evidence must be a line of a file in the run's worktree of its repo, and goes with the commit the worktree is at.
  They refuse to run outside a review run of this worker.
- ``chat post|react|create|mention``: the commands of a run's agent in group chats (``chat_cli``).
- ``output FILE...``: the command of any run's agent that sends files to the run's outputs on the hub, as ``ask
  --attach`` and ``chat post --attach`` do for a question and a message (``output_cli``).
- ``repo open REPO``: the command of a chat run's agent that has the daemon open a repo of the project in the run, and
  prints its worktree's path (``chat_cli``).
- ``triage rerun|plan``: what the agent of a triage thread does for a failed run (``triage_cli``).
- ``playbook add|helpful|harmful|retire``: a review run's agent's deltas of the Curator's playbook (``playbook_cli``).
- ``probe add``: a review run's agent's probe of a merged change that has none (``probe_cli``).
- ``put FILE [--if-revision N]``: the command of an author run's agent, which puts the plan it wrote on the hub, in the
  run's project, as the member who dispatched the run (``evo_agents.hub.author``); ``plan`` reads it back, with its
  revision, once the run has one. It refuses to run outside an author run of this worker.
- ``git-credential --run N get|store|erase``: git's credential helper for run N, which the run's git configuration
  names; ``get`` answers from the run's leases through its socket, ``store`` and ``erase`` do nothing.
  ``env --run N``: the variables run N's leases add to its agent's environment, as export lines, for the script of an
  interactive pane to evaluate (``credentials``). Both need the core package only, and print nothing of a run that
  holds no socket here.

The worker token goes to ``~/.evo/worker/token`` (0600) and is never printed. A failure is one ``error:`` line on
stderr and exit status 69, 75 or 1 (``client.exit_status``); a command lacking the worker extra says how to get it (2).
``run`` on a machine that is not a worker, such as one revoked with ``revoke``, exits ``home.EXIT_REVOKED`` (3, or
what EVO_WORKER_REVOKED_EXIT says), so a service does not start it again and again. ``run`` needs the extra (aiohttp)
and so does ``join``; the others need the core package only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import sys
from datetime import UTC, datetime
from urllib.parse import quote

from evo_agents import __version__
from evo_agents.hub import runs
from evo_agents.hub.client import Hub, HubError, check_url, host_name, load_credentials
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.worker import (
    backup_cli,
    chat_cli,
    list_cli,
    output_cli,
    playbook_cli,
    probe_cli,
    projects_cli,
    step_verify,
    triage_cli,
)
from evo_agents.worker.agent_cli import (
    AGENT_NOTICE_KINDS,
    AGENT_STEP_STATUSES,
    cmd_ask,
    cmd_finding,
    cmd_notify,
    cmd_plan,
    cmd_propose,
    cmd_put,
    cmd_step,
    cmd_wait,
)
from evo_agents.worker.browser import register_browser
from evo_agents.worker.command import EXIT_FAILED, MissingExtra, load_hubapi, worker_command
from evo_agents.worker.home import (
    NotJoined,
    PidLock,
    WorkerConfig,
    WorkerHome,
    WorkerStateError,
    dir_bytes,
    revoked_exit,
)

MAX_SLOTS = runs.MAX_SLOTS  # 16, as the hub bounds workers.slots
# The parsers of a run's agent in group chats, of `repo open` of a chat run's, of a triage thread's for a failed run, of
# a review run's on the playbook, and of any run's outputs
AGENT_PARSERS = (chat_cli.register_chat, chat_cli.register_repo, triage_cli.register_triage)
AGENT_PARSERS += (playbook_cli.register_playbook, output_cli.register_output, backup_cli.register_backup)
AGENT_PARSERS += (probe_cli.register_probe,)  # a review run's probe of a merged change


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
        joined_at=datetime.now(UTC).isoformat(timespec="seconds"),
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


def owner_hub(config: WorkerConfig) -> Hub:
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


def print_checkouts(config: WorkerConfig) -> None:
    from evo_agents.worker.checkouts import discover

    found = discover(config)
    for key in sorted(found):
        print(f"  checkout {key}: {found[key]['path']}")
    missing = [p for p in config.projects if not any(key.startswith(f"{p}/") for key in found)]
    for project in missing:
        print(
            f"  no checkout of {project} found: a run of it clones the repos it needs; `evo-agents hub registry "
            "pull` finds those cloned here already"
        )


def _joined(home: WorkerHome, config: WorkerConfig, token: str, how: str) -> None:
    missed = refresh_repos(config)
    home.save(config, token)
    projects = ", ".join(config.projects) or "no project"
    print(f"{how} {config.url} as worker {config.name} (id {config.worker_id}) for {projects}, {config.slots} slot(s).")
    print(f"The worker token is in {home.token_path}.")
    if missed and _member_hub(config.url) is not None:
        print(f"  the repos of {', '.join(missed)} could not be read from the hub; the harness registry is used")
    print_checkouts(config)
    print("Start it with `evo-agents worker run`.")


@worker_command
def cmd_join(args) -> int:
    home = WorkerHome()
    _refuse_rejoin(home, args.replace)
    url = check_url(args.url)
    hubapi = load_hubapi()

    async def join() -> dict:
        async with hubapi.new_session() as session:
            return await hubapi.WorkerHub(url, None, session).join(args.code, host_facts())

    try:
        answer = asyncio.run(join())
    except hubapi.HubProblem as exc:
        if exc.status == 429 and exc.retry_after is not None:
            raise exc.hub_error(f"{exc} (the hub says to wait {int(exc.retry_after)} seconds)") from None
        raise exc.hub_error() from None
    config, token = _config_of(url, answer)
    _joined(home, config, token, "Joined")
    return 0


@worker_command
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


@worker_command
def cmd_run(args) -> int:
    home = WorkerHome()
    try:
        config = home.load_config()
        token = home.load_token()
    except NotJoined as exc:  # not a worker, or not any more: starting the daemon again changes nothing
        print(f"error: {exc}", file=sys.stderr)
        return revoked_exit(os.environ)
    try:
        import aiohttp  # noqa: F401

        from evo_agents.worker.daemon import Daemon
    except ImportError as exc:
        raise MissingExtra(f"missing module {exc.name}") from None
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


@worker_command
def cmd_attach(args) -> int:
    from evo_agents.worker import interactive

    home = WorkerHome()
    record = next((item for item in home.load_runs() if item["id"] == args.run), {})
    socket = record.get("tmux_socket") if isinstance(record.get("tmux_socket"), str) else None
    tmux = interactive.Tmux.from_env(os.environ, socket=socket)
    if not tmux.available:
        raise WorkerStateError("tmux is not on PATH: interactive runs need it on the worker")
    name = interactive.session_name(args.run)
    if not tmux.has_session(name):
        raise WorkerStateError(
            f"run {args.run} has no terminal on this machine: tmux session {name} is not there. A run has one while "
            "it is interactive, after a takeover from the web or `evo-agents hub run takeover`."
        )
    argv, env = tmux.attach_command(name, os.environ)
    sys.stdout.flush()
    os.execvpe(argv[0], argv, env)
    return 0  # pragma: no cover - execvpe does not return


@worker_command
def cmd_git_credential(args) -> int:
    from evo_agents.worker import credential_commands

    return credential_commands.git_credential(args.run, args.action, sys.stdin, sys.stdout, sys.stderr)


@worker_command
def cmd_env(args) -> int:
    from evo_agents.worker import credential_commands

    return credential_commands.print_env(args.run, sys.stdout, sys.stderr)


@worker_command
def cmd_status(args) -> int:
    from evo_agents.worker.adapter import detect_runtimes, load_adapters
    from evo_agents.worker.checkouts import discover
    from evo_agents.worker.interactive import Tmux

    home = WorkerHome()
    config = home.load_config()
    runtimes = detect_runtimes(load_adapters())
    tmux = Tmux.from_env(os.environ)
    found = discover(config)
    records = home.load_runs()
    pid = home.read_pid()
    hub_view = hub_error = None
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
        "tmux": {"path": tmux.binary, "version": tmux.version()},
        "allow_web_terminal": config.allow_web_terminal,
        "checkouts": found,
        "spool_bytes": dir_bytes(home.spool_dir),
        "runs": [{k: r.get(k) for k in ("id", "state", "worktree", "finished_at")} for r in records],
        "hub": hub_view,
        "hub_error": hub_error,
        "kg_sync": home.kg_sync(),
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
    if tmux.available:
        terminal = "allowed" if config.allow_web_terminal else "not allowed"
        print(f"  interactive runs: tmux {status['tmux']['version'] or '?'} at {tmux.binary}; web terminal {terminal}")
    else:
        print("  interactive runs: unsupported (tmux is not on PATH); headless runs only")
    for key in sorted(found):
        print(f"  checkout {key}: {found[key]['path']} ({found[key].get('branch') or 'detached'})")
    if not found:
        print("  no checkout of the worker's projects found: each run clones the repos it needs")
    unfinished = [r for r in records if not r.get("finished_at")]
    print(f"  runs kept here: {len(records)} ({len(unfinished)} not ended); spool: {status['spool_bytes']} bytes")
    print(*(f"  {line}" for line in [*home.kg_sync_lines(), f"state: {home.root}"]), sep="\n")
    return 0


@worker_command
def cmd_drain(args) -> int:
    home = WorkerHome()
    config = home.load_config()
    action = "undrain" if args.resume else "drain"
    answer = owner_hub(config).call("POST", f"/v1/workers/{config.worker_id}/{action}")
    status = answer.get("status") if isinstance(answer, dict) else None
    if args.resume:
        print(f"Worker {config.name} claims runs again (status {status}).")
    else:
        print(
            f"Worker {config.name} is draining: it claims no new run and finishes the {answer.get('held_runs', 0)} "
            f"it holds (status {status}). Undo with `evo-agents worker drain --resume`."
        )
    return 0


@worker_command
def cmd_revoke(args) -> int:
    home = WorkerHome()
    config = home.load_config()
    try:
        owner_hub(config).call("POST", f"/v1/workers/{config.worker_id}/revoke")
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
    installed = _service_file()
    if installed is not None:
        print(
            f"The background service ({installed}) does not start the daemon again; remove it with "
            "`evo-agents worker service uninstall`."
        )
    return 0


def _service_file():
    """The file of the background service when one is installed for this user, without asking its manager."""
    from evo_agents.worker import service

    try:
        path = service.manager().file()
    except service.ServiceError:  # neither launchd nor systemd here
        return None
    return path if path.exists() else None


def _slots(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number <= MAX_SLOTS:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {MAX_SLOTS}, not {value!r}")
    return number


def _register_step(wsub) -> None:
    """The parser of `evo-agents worker step`."""
    step = wsub.add_parser(
        "step", help="inside a plan run: report a step of the plan; done runs its verify commands again and pushes"
    )
    step.add_argument("key", help="the step's id, or its order")
    step.add_argument(
        "status",
        choices=(*AGENT_STEP_STATUSES, "verify"),
        help="in_progress, done, pending to hand it back, or verify to run the verify as a job of the worker alone",
    )
    step.add_argument("--repo", help="the run's repo the step is in (default: the plan's repo of the step)")
    step.add_argument("--evidence", help="what was done and how it was checked; a line per decision taken")
    step.add_argument(
        "--verify",
        action="append",
        metavar="COMMAND",
        help="a command that checks the step, run in the repo's worktree; repeat it; done and verify need one",
    )
    step.add_argument(
        "--wait",
        type=float,
        metavar="SECONDS",
        help=f"how long done and verify wait on the verify (default {step_verify.verify_jobs.WAIT:g}; -1: until it "
        f"ends); it goes on after, and they exit {step_verify.verify_jobs.EXIT_RUNNING}",
    )
    step.set_defaults(func=cmd_step)


def _register_review(wsub) -> None:
    """``worker finding`` and ``worker propose``, which the agent of a review run runs; outside one they refuse."""
    from evo_agents.hub.review import LENSES, SEVERITIES
    from evo_agents.hub.tiers import CHANGE_KINDS

    finding = wsub.add_parser("finding", help="inside a review run: record a finding with its evidence; prints its id")
    finding.add_argument("--lens", required=True, choices=list(LENSES), help="the lens it was found through")
    finding.add_argument("--title", required=True, help="what was found, in one line")
    finding.add_argument(
        "--evidence",
        action="append",
        required=True,
        metavar="SPEC",
        help="session:ID[:FIELD:INDEX], transcript:ID[:FILE]:LINE, run:ID:SEQ or code:REPO:PATH[:LINE]; repeat it",
    )
    finding.add_argument("--severity", choices=SEVERITIES, default="medium", help="low, medium (default) or high")
    finding.add_argument("--body-file", help="markdown that explains it, at most 16 KiB (- for stdin)")
    finding.add_argument(
        "--fix-of",
        type=int,
        metavar="CHANGE",
        help="the change the prompt asked you to fix: why you propose no fix of its rule; the hub adds the probe "
        "results",
    )
    finding.set_defaults(func=cmd_finding)

    propose = wsub.add_parser(
        "propose", help="inside a review run: propose a change with its draft plan; prints its id, tier and state"
    )
    propose.add_argument("--lens", required=True, choices=list(LENSES), help="the lens it comes from")
    propose.add_argument("--kind", required=True, choices=list(CHANGE_KINDS), help="the kind of change")
    propose.add_argument("--title", required=True, help="the change, in one line")
    draft = propose.add_mutually_exclusive_group(required=True)
    draft.add_argument("--plan-file", help="the draft plan, YAML or JSON, in outcome steps")
    draft.add_argument("--promote-file", help="kind memory: the promotion of agents' memories, YAML or JSON")
    propose.add_argument("--path", action="append", metavar="REPO:PATH", help="a file it would edit; repeat it")
    propose.add_argument("--finding", action="append", type=int, metavar="ID", help="a finding it rests on; repeat it")
    propose.add_argument(
        "--evidence", action="append", metavar="SPEC", help="evidence of its own, as finding takes it; repeat it"
    )
    propose.add_argument("--summary-file", help="markdown: the problem, the change, what waiting costs (- for stdin)")
    propose.add_argument(
        "--probe-file",
        help="the probe, YAML or JSON: repo, commit, task and verify; required when the evidence cites a correction "
        "and a path is an AGENTS.md, a CLAUDE.md, a skill or the repo's .claude settings or hooks",
    )
    propose.add_argument(
        "--fix-of",
        type=int,
        metavar="CHANGE",
        help="the change the prompt asked you to fix, whose probe recurred two weeks in a row; at least tier 1, and "
        "the hub adds the probe results as evidence",
    )
    propose.set_defaults(func=cmd_propose)


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
    reg.add_argument("--slots", type=_slots, default=1, help=f"runs it holds at once, 1 to {MAX_SLOTS} (default 1)")
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

    from evo_agents.worker import service

    service.register(wsub)

    status = wsub.add_parser("status", help="this worker as the hub and this machine see it")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)

    from evo_agents.worker import doctor, selftest

    doctor.add_parser(wsub)  # `doctor [--json]`
    selftest.add_parser(wsub)  # `selftest --runtime NAME`

    attach = wsub.add_parser(
        "attach", help="put this terminal on the tmux session of an interactive run (evo-run-N), as tmux attach does"
    )
    attach.add_argument("run", type=int, help="the run's id, as the web and `evo-agents hub run list` show it")
    attach.set_defaults(func=cmd_attach)

    drain = wsub.add_parser("drain", help="stop claiming new runs; the runs held finish (needs `hub login`)")
    drain.add_argument("--resume", action="store_true", help="claim runs again")
    drain.set_defaults(func=cmd_drain)

    revoke = wsub.add_parser("revoke", help="end this worker on the hub and delete its token here (needs `hub login`)")
    revoke.add_argument(
        "--force", action="store_true", help="delete the token here even when the hub cannot revoke the worker"
    )
    revoke.set_defaults(func=cmd_revoke)

    # git and an interactive pane of a run run these, with the run's socket.
    helper = wsub.add_parser(
        "git-credential",
        help="git's credential helper for a run of this worker: get answers from the run's leases; store and erase "
        "do nothing",
    )
    helper.add_argument("--run", type=int, required=True, help="the run's id")
    helper.add_argument("action", help="get, store or erase, as git calls its helper")
    helper.set_defaults(func=cmd_git_credential)

    env = wsub.add_parser(
        "env", help="print what a run's leases add to its agent's environment, as export lines for a shell to eval"
    )
    env.add_argument("--run", type=int, required=True, help="the run's id")
    env.set_defaults(func=cmd_env)

    # The agent of a plan run runs these, and of an author run ask and plan; outside one they refuse.
    _register_step(wsub)

    ask = wsub.add_parser(
        "ask", help="inside a plan run or an author run: ask the run's owner a decision; prints its id"
    )
    ask.add_argument("--category", required=True, choices=runs.ASKED_DECISION_CATEGORIES, help="what kind of decision")
    ask.add_argument("--question", required=True, help="the question, in a sentence or two")
    ask.add_argument("--context-file", help="a markdown file of context for the owner, at most 16 KiB (- for stdin)")
    ask.add_argument(
        "--option",
        action="append",
        required=True,
        metavar="KEY=LABEL[:DESCRIPTION]",
        help="an option; repeat it, 2 to 6 times",
    )
    ask.add_argument("--recommended", metavar="KEY", help="the key of the option you recommend")
    ask.add_argument(
        "--wait-option",
        action="append",
        metavar="KEY",
        help="an option that says not yet: choosing it leaves the decision open and the run waiting, and you hear "
        "nothing; repeat it for another",
    )
    ask.add_argument("--step", help="the step of the run's plan it is about (an author run: once it put the plan)")
    output_cli.attach_option(ask, "the question")
    ask.set_defaults(func=cmd_ask)

    wait = wsub.add_parser(
        "wait",
        help="inside a plan run: wait for a time or an event of the hub rather than ask the owner when it came; "
        "prints the wait's id",
    )
    wait.add_argument("--reason", required=True, help="what the run waits for and why, for the owner to read")
    wait.add_argument(
        "--until",
        metavar="TIME",
        help=f"wake at this time at the latest: ISO 8601 with a timezone (2026-10-30T09:00Z) or a span (+6h, +90m, "
        f"+5d), at most {runs.MAX_WAIT_DAYS} days ahead; without it, {runs.MAX_WAIT_DAYS} days",
    )
    wait.add_argument(
        "--on", metavar="KIND", help="wake sooner on an event of the hub of this kind, such as canvas.baseline"
    )
    wait.add_argument(
        "--contains",
        action="append",
        metavar="KEY=VALUE",
        help="the event's payload has KEY equal to VALUE (read as JSON when it is); repeat it",
    )
    wait.add_argument("--step", help="the step of the run's plan it is about")
    wait.set_defaults(func=cmd_wait)

    notify = wsub.add_parser(
        "notify", help="inside a plan run: tell the run's owner of a push or merge into a default branch"
    )
    notify.add_argument("--kind", required=True, choices=AGENT_NOTICE_KINDS)
    notify.add_argument("--title", required=True, help="one line")
    notify.add_argument("--body", help="what happened, at most 16 KiB")
    notify.add_argument("--repo", help="the run's repo")
    notify.add_argument("--branch", help="the branch pushed or merged into")
    notify.add_argument("--commit", action="append", metavar="SHA", help="a commit pushed or merged; repeat it")
    notify.set_defaults(func=cmd_notify)

    _register_review(wsub)

    plan = wsub.add_parser(
        "plan", help="inside a plan run or an author run: print the run's plan as the hub holds it now"
    )
    from evo_agents.hub.plan_cli import PLAN_KEYS, WRITTEN

    json_option(plan, returns_object(*PLAN_KEYS, schema="Plan"), help="the hub's answer as JSON")  # seam hub-cli-v1
    plan.set_defaults(func=cmd_plan)

    # The agent of an author run runs this; outside one it refuses.
    put = wsub.add_parser(
        "put",
        help="inside an author run: put the plan you wrote on the hub, as the member who asked for it; replacing it "
        "needs --if-revision",
    )
    put.add_argument("file", metavar="FILE", help="the plan, YAML or JSON, as plan.schema.json has it (- for stdin)")
    put.add_argument(
        "--if-revision",
        type=int,
        metavar="REVISION",
        help="the revision on the hub this replaces, as `evo-agents worker plan --json` read it; leave it out to "
        "create the plan",
    )
    json_option(put, WRITTEN, help="the hub's answer as JSON")  # seam hub-cli-v1
    put.set_defaults(func=cmd_put)
    for extra in (*AGENT_PARSERS, projects_cli.register_projects, list_cli.register_list, register_browser):
        extra(wsub)
