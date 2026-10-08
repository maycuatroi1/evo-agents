"""``evo-agents worker``: this machine as a worker of a hub.

- ``join --url URL --code CODE``: trade a pairing code from the web for a worker token.
- ``register --name NAME --project P [--project P ...] --slots N [--label L ...]``: register this machine directly,
  with the machine token of ``evo-agents hub login``.
- ``run``: the daemon in the foreground (``daemon``).
- ``status``: what this machine is to the hub, and what the daemon sees here.
- ``doctor [--json]``: what this machine holds or allows that a worker should not, such as another administrator or a
  long-lived credential of its owner (``doctor``); exit status 2 when a finding is high.
- ``attach N``: this terminal on the tmux session of run N while a person drives its agent (``evo-run-N``).
- ``drain [--resume]``, ``revoke [--force]``: the owner's controls, with the machine token.
- ``step KEY STATUS``, ``ask``, ``notify``, ``plan``: the commands of a plan run's agent, which report a step (the
  verify commands run again, the repo committed and pushed), ask the run's owner a decision, send the owner a notice,
  and print the plan as the hub holds it now. They read the run from EVO_RUN_ID, EVO_WORKER_HOME and the run's record
  there, call the hub with the worker's token, and refuse to run outside a plan run of this worker.
- ``finding`` and ``propose``: the commands of a review run's agent (the Curator's Reviewer), which record a finding
  and a proposal with their evidence (``evo_agents.hub.review.parse_evidence``); the hub computes a proposal's tier.
  Code evidence must be a line of a file in the run's worktree of its repo, and goes with the commit the worktree is at.
  They refuse to run outside a review run of this worker.
- ``git-credential --run N get|store|erase``: git's credential helper for run N, which the run's git configuration
  names; ``get`` answers from the run's leases through its socket, ``store`` and ``erase`` do nothing.
  ``env --run N``: the variables run N's leases add to its agent's environment, as export lines, for the script of an
  interactive pane to evaluate (``credentials``). Both need the core package only, and print nothing of a run that
  holds no socket here.

The worker token goes to ``~/.evo/worker/token`` (0600) and is never printed. A failure is one ``error:`` line on
stderr and exit status 1; a command that needs the worker extra and lacks it says how to install it (status 2).
``run`` on a machine that is not a worker, such as one revoked with ``revoke``, exits ``home.EXIT_REVOKED`` (3, or
what EVO_WORKER_REVOKED_EXIT says), so a service does not start it again and again. ``run`` needs the extra (aiohttp)
and so does ``join``; the others need the core package only.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import platform
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from evo_agents import __version__
from evo_agents.hub import runs
from evo_agents.hub.client import Hub, HubError, check_url, host_name, load_credentials
from evo_agents.worker.home import NotJoined, PidLock, WorkerConfig, WorkerHome, WorkerStateError, revoked_exit

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


@_worker_command
def cmd_git_credential(args) -> int:
    from evo_agents.worker import credentials

    return credentials.git_credential(args.run, args.action, sys.stdin, sys.stdout, sys.stderr)


@_worker_command
def cmd_env(args) -> int:
    from evo_agents.worker import credentials

    return credentials.print_env(args.run, sys.stdout, sys.stderr)


@_worker_command
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
        "tmux": {"path": tmux.binary, "version": tmux.version()},
        "allow_web_terminal": config.allow_web_terminal,
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
    if tmux.available:
        terminal = "allowed" if config.allow_web_terminal else "not allowed"
        print(f"  interactive runs: tmux {status['tmux']['version'] or '?'} at {tmux.binary}; web terminal {terminal}")
    else:
        print("  interactive runs: unsupported (tmux is not on PATH); headless runs only")
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


# The commands of a plan run's agent

RUN_VARIABLE = "EVO_RUN_ID"
AGENT_STEP_STATUSES = ("in_progress", "done", "pending")  # what POST /v1/worker/runs/{id}/steps/{key} takes
AGENT_NOTICE_KINDS = ("push_default_branch", "merge_default_branch")  # the hub sends plan_finished and run_failed
MAX_VERIFY_COMMANDS = 50
MAX_VERIFY_CHARS = 2000
MAX_EVIDENCE_CHARS = 16 * 1024  # a step report's evidence, as the hub takes it
OUTPUT_TAIL = 4000  # characters of a failed verify command's output printed


@dataclass
class _AgentRun:
    """The plan run an agent's command acts for: the worker's state, its token and the run's record."""

    run_id: int
    home: WorkerHome
    config: WorkerConfig
    token: str
    record: dict


def _agent_run(command: str, kinds: tuple[str, ...] = ("plan",)) -> _AgentRun:
    """The run of EVO_RUN_ID on this worker, of one of ``kinds``; WorkerStateError outside one."""
    raw = (os.environ.get(RUN_VARIABLE) or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        raise WorkerStateError(
            f"`evo-agents worker {command}` works only inside a {kinds[0]} run of this worker: {RUN_VARIABLE} is not "
            "set. The worker sets it, with EVO_RUN_KIND and EVO_WORKER_HOME, for the agent of each run."
        )
    run_id = int(raw)
    home = WorkerHome()
    config = home.load_config()
    token = home.load_token()
    record = home.load_run(run_id)
    if record is None or record.get("finished_at"):
        raise WorkerStateError(
            f"run {run_id} is not running on this worker (its state is under {home.root}): "
            f"`evo-agents worker {command}` works only inside it"
        )
    kind = record.get("kind") or "step"
    if kind not in kinds:
        if kinds == ("review",):
            raise WorkerStateError(
                f"run {run_id} is a {kind} run: only the agent of a review run uses `evo-agents worker {command}`"
            )
        if kind == "review":
            raise WorkerStateError(
                f"run {run_id} is a review run, which reads only: it records what it finds with `evo-agents worker "
                f"finding` and `evo-agents worker propose`, not `evo-agents worker {command}`"
            )
        if kind == "judge":
            raise WorkerStateError(
                f"run {run_id} is a judge run, which reads and judges only: it writes its verdict to "
                f"{runs.RESULT_DIR}/verdict.json, not with `evo-agents worker {command}`"
            )
        raise WorkerStateError(
            f"run {run_id} is a run of one step: only the agent of a plan run uses `evo-agents worker {command}`; a "
            f"run of one step writes {runs.RESULT_FILE} instead"
        )
    return _AgentRun(run_id, home, config, token, record)


def _with_hub(agent: _AgentRun, work):
    """``await work(hub)`` with the worker's client of the hub; the hub's refusal, or no answer, is a HubError."""
    hubapi = _hubapi()
    from evo_agents.worker import gitops

    async def go():
        async with hubapi.new_session() as session:
            return await work(hubapi.WorkerHub(agent.config.url, agent.token, session))

    try:
        return asyncio.run(go())
    except hubapi.HubProblem as exc:
        raise HubError(str(exc), exc.status, exc.code) from None
    except gitops.GitError as exc:
        raise WorkerStateError(str(exc)) from None


def _workspaces(agent: _AgentRun) -> dict:
    from evo_agents.worker import gitops

    found = {}
    for item in agent.record.get("repos") or []:
        try:
            workspace = gitops.Workspace.from_record(item)
        except ValueError:
            continue
        found[workspace.repo] = workspace
    return found


def _plan_step(body: dict, key: str) -> dict | None:
    """The step of ``body`` whose id is ``key`` (ids compare as text), or whose order it is."""
    steps = [step for step in body.get("steps") or [] if isinstance(step, dict)]
    for step in steps:
        if str(step.get("id")) == key:
            return step
    if key.isdigit() and 1 <= int(key) <= len(steps):
        return steps[int(key) - 1]
    return None


def _step_repo(body: dict, step: dict | None, workspaces: dict) -> str | None:
    """The repo a step is in: the plan's repo of the step, or the run's only repo."""
    named = step.get("repo") if isinstance(step, dict) else None
    if isinstance(named, str) and named in workspaces:
        return named
    repos = [entry.get("repo") for entry in body.get("repos") or [] if isinstance(entry, dict)]
    if not isinstance(named, str) and len(repos) == 1 and repos[0] in workspaces:
        return repos[0]
    return next(iter(workspaces)) if len(workspaces) == 1 else None


async def _verify(path: Path, commands: list[str]) -> list[dict]:
    """Run each command with /bin/sh in ``path``, every one even after a failure; each one's exit code."""
    results = []
    for command in commands:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await proc.communicate()
        duration_ms = int((time.monotonic() - started) * 1000)
        results.append({"command": command, "exit_code": proc.returncode, "duration_ms": duration_ms})
        print(f"verify: `{command}` exited {proc.returncode} after {duration_ms} ms")
        if proc.returncode != 0:
            tail = output.decode(errors="replace")[-OUTPUT_TAIL:].rstrip()
            if tail:
                print("\n".join(f"  | {line}" for line in tail.splitlines()))
    return results


@_worker_command
def cmd_step(args) -> int:
    agent = _agent_run("step")
    verify = list(args.verify or [])
    if args.status == "done" and not verify:
        raise WorkerStateError(
            "a step is done only with the commands that check it: give --verify COMMAND once for each; the worker "
            "runs each again in the repo's worktree"
        )
    if args.status != "done" and verify:
        raise WorkerStateError("--verify goes with done only: the worker runs the verify commands of a done step")
    if len(verify) > MAX_VERIFY_COMMANDS or any(not c.strip() or len(c) > MAX_VERIFY_CHARS for c in verify):
        raise WorkerStateError(
            f"give 1 to {MAX_VERIFY_COMMANDS} verify commands, each 1 to {MAX_VERIFY_CHARS} characters"
        )
    if args.evidence is not None and (not args.evidence.strip() or len(args.evidence) > MAX_EVIDENCE_CHARS):
        raise WorkerStateError(f"--evidence takes 1 to {MAX_EVIDENCE_CHARS} characters")
    workspaces = _workspaces(agent)
    if args.repo is not None and args.repo not in workspaces:
        raise WorkerStateError(f"run {agent.run_id} works in {', '.join(workspaces) or 'no repo'}, not in {args.repo}")
    from evo_agents.worker import gitops

    async def work(hub) -> int:
        body = (await hub.plan(agent.run_id)).get("body") or {}
        step = _plan_step(body, args.key)
        name = args.repo or _step_repo(body, step, workspaces)
        report: dict = {"status": args.status}
        if name is not None:
            report["repo"] = name
        if args.evidence is not None:
            report["evidence"] = args.evidence
        pushed = None
        if args.status == "done":
            if name is None:
                raise WorkerStateError(f"name the repo of step {args.key} with --repo: one of {', '.join(workspaces)}")
            workspace = workspaces[name]
            results = await _verify(workspace.worktree, verify)
            failed = [item for item in results if item["exit_code"] != 0]
            if failed:
                print(
                    f"Step {args.key} is not done: {len(failed)} verify command(s) exited other than 0. Nothing was "
                    f"committed or pushed, and nothing was reported; fix it and run `evo-agents worker step "
                    f"{args.key} done` again, or hand the step back with pending.",
                    file=sys.stderr,
                )
                return EXIT_FAILED
            title = step.get("title") if isinstance(step, dict) and isinstance(step.get("title"), str) else None
            message = f"run #{agent.run_id} step {args.key}" + (f": {' '.join(title.split())}" if title else "")
            path = workspace.worktree
            if await gitops.commit_all(path, message):
                print(f"Committed what was left in {name} as {message!r}.")
            branch = await gitops.current_branch(path)
            if branch is None:
                raise WorkerStateError(f"HEAD is detached in {path}: the worker does not push a detached HEAD")
            if branch != workspace.local_branch:
                raise WorkerStateError(
                    f"{path} is on {branch}, not {workspace.local_branch}: switch back; the worker pushes only the "
                    "run's branch"
                )
            plan_branch = gitops.plan_branches(body).get(name) if "repos" in body else workspace.plan_branch
            curator = agent.record.get("curator") if isinstance(agent.record.get("curator"), dict) else None
            builder = curator is not None and curator.get("role") == "builder"
            options = []
            if builder and curator.get("forge") == "gitlab":
                from evo_agents.hub.judge import gitlab_push_options

                target = (curator.get("targets") or {}).get(name) or "main"
                options = gitlab_push_options(target, step.get("title") if isinstance(step, dict) else None)
            try:
                pushed = await gitops.push(
                    path,
                    workspace.branch,
                    protected=workspace.protected,
                    kind="curator" if builder else "plan",
                    plan_branch=plan_branch,
                    options=options,
                )
            except gitops.PushRefused as exc:
                raise WorkerStateError(f"{name}: {exc}; step {args.key} is not reported done") from None
            except gitops.GitError as exc:
                raise WorkerStateError(
                    f"git push of {name} to {workspace.branch} failed: {exc}; step {args.key} is not reported done"
                ) from None
            if pushed.default and pushed.changed:
                from evo_agents.worker.hubapi import HubProblem

                try:
                    await hub.notice(agent.run_id, gitops.push_notice(agent.run_id, name, pushed))
                    print(f"Notified the run's owner of the push to {pushed.branch}, a default branch.")
                except HubProblem as exc:  # the push happened: the step is reported all the same
                    print(
                        f"warning: the notice of the push to {pushed.branch} was not sent ({exc}); send it with "
                        "`evo-agents worker notify --kind push_default_branch`",
                        file=sys.stderr,
                    )
            report["verify"] = results
            report["commit_sha"] = pushed.head
        answer = await hub.step(agent.run_id, args.key, report)
        where = f" ({name}@{pushed.head[:12]}, pushed to {pushed.branch})" if pushed is not None else ""
        kept = "" if answer.get("written", True) else ", as the plan had it already"
        print(f"Step {args.key} of plan {answer.get('plan_id')}: {answer.get('status')}{where}{kept}.")
        return 0

    return _with_hub(agent, work)


def _option(text: str) -> dict:
    """``KEY=LABEL[:DESCRIPTION]`` as an option of a decision."""
    key, sep, rest = text.partition("=")
    label, _, description = rest.partition(":")
    key, label, description = key.strip(), label.strip(), description.strip()
    if not sep or not label or not re.fullmatch(runs.OPTION_KEY, key):
        raise WorkerStateError(
            f"--option takes KEY=LABEL[:DESCRIPTION], KEY being letters, digits, _ and - (at most 32), not {text!r}"
        )
    return {"key": key, "label": label, **({"description": description} if description else {})}


@_worker_command
def cmd_ask(args) -> int:
    agent = _agent_run("ask")
    options = [_option(text) for text in args.option]
    keys = [option["key"] for option in options]
    fewest, most = runs.DECISION_OPTIONS
    if not fewest <= len(options) <= most or len(set(keys)) != len(keys):
        raise WorkerStateError(f"a decision offers {fewest} to {most} options, each with a key of its own")
    if args.recommended is not None and args.recommended not in keys:
        raise WorkerStateError(f"--recommended names one of the options: {', '.join(keys)}")
    body: dict = {"category": args.category, "question": args.question, "options": options}
    if args.context_file:
        try:
            text = sys.stdin.read() if args.context_file == "-" else Path(args.context_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise WorkerStateError(f"cannot read {args.context_file}: {exc}") from None
        if len(text.encode()) > runs.MAX_DECISION_CONTEXT_BYTES:
            raise WorkerStateError(f"the context is at most {runs.MAX_DECISION_CONTEXT_BYTES} bytes of UTF-8")
        if text.strip():
            body["context"] = text
    if args.recommended is not None:
        body["recommended"] = args.recommended
    if args.step is not None:
        body["step_key"] = args.step

    async def work(hub) -> dict:
        return await hub.decision(agent.run_id, body)

    answer = _with_hub(agent, work)
    decision_id = answer.get("id") if isinstance(answer, dict) else None
    if not isinstance(decision_id, int):
        raise HubError("the hub did not answer with the decision's id")
    noted = json.dumps({"id": decision_id, "asked_at": datetime.now(UTC).isoformat(timespec="seconds")})
    with open(agent.home.decisions_path(agent.run_id), "a", encoding="utf-8") as handle:  # the daemon waits on it
        handle.write(noted + "\n")
    print(f"Decision #{decision_id} is open: {' '.join(args.question.split())}")
    print(
        "Go on with the work that does not depend on the answer. When none is left, end your turn: the run waits "
        f"for its owner, and the answer comes back in this session as a message naming decision #{decision_id}."
    )
    return 0


@_worker_command
def cmd_notify(args) -> int:
    agent = _agent_run("notify")
    body: dict = {"kind": args.kind, "title": args.title}
    for name in ("body", "repo", "branch"):
        if getattr(args, name) is not None:
            body[name] = getattr(args, name)
    if args.commit:
        body["commits"] = list(dict.fromkeys(args.commit))

    async def work(hub) -> dict:
        return await hub.notice(agent.run_id, body)

    answer = _with_hub(agent, work)
    print(f"Notice #{answer.get('id')} sent to the owner of run #{agent.run_id}: {args.title}")
    return 0


@_worker_command
def cmd_plan(args) -> int:
    agent = _agent_run("plan")

    async def work(hub) -> dict:
        return await hub.plan(agent.run_id)

    view = _with_hub(agent, work)
    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0
    import yaml

    print(
        f"# The plan {view.get('plan_id')} of project {view.get('project')} at revision {view.get('revision')}, as "
        "the hub holds it now."
    )
    print(yaml.safe_dump(view.get("body") or {}, allow_unicode=True, sort_keys=False, width=120), end="")
    return 0


# The commands of a review run's agent


def _read_text(source: str, what: str, limit: int) -> str | None:
    """The text of file ``source`` (``-``: stdin), at most ``limit`` bytes of UTF-8; None when it holds only blanks."""
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkerStateError(f"cannot read {source}: {exc}") from None
    if len(text.encode()) > limit:
        raise WorkerStateError(f"the {what} is at most {limit} bytes of UTF-8")
    return text if text.strip() else None


def _evidence(texts: list[str]) -> list[dict]:
    from evo_agents.hub.review import EvidenceProblem, parse_evidence

    found = []
    for text in texts:
        try:
            found.append(parse_evidence(text))
        except EvidenceProblem as exc:
            raise WorkerStateError(str(exc)) from None
    return found


async def _attest(evidence: list[dict], workspaces: dict) -> None:
    """Check each piece of code evidence against the run's worktree of its repo: the file is there, with the line;
    and add the commit the worktree is at. The hub checks the rest."""
    from evo_agents.worker import gitops

    for item in evidence:
        if item["kind"] != "code":
            continue
        workspace = workspaces.get(item["repo"])
        if workspace is None:
            raise WorkerStateError(
                f"code:{item['repo']}:{item['path']}: run works in {', '.join(workspaces) or 'no repo'}, not in "
                f"{item['repo']}"
            )
        path = workspace.worktree / item["path"]
        if not path.resolve().is_relative_to(workspace.worktree.resolve()) or not path.is_file():
            raise WorkerStateError(
                f"code:{item['repo']}:{item['path']}: no such file in the worktree of {item['repo']}"
            )
        if item.get("line") is not None:
            with open(path, "rb") as handle:
                lines = sum(1 for _ in handle)
            if item["line"] > lines:
                raise WorkerStateError(f"code:{item['repo']}:{item['path']}:{item['line']}: the file has {lines} lines")
        head = await gitops.rev(workspace.worktree, "HEAD")
        if head:
            item["commit"] = head


@_worker_command
def cmd_finding(args) -> int:
    from evo_agents.hub.review import MAX_BODY_BYTES

    agent = _agent_run("finding", ("review",))
    body: dict = {"lens": args.lens, "severity": args.severity, "title": " ".join(args.title.split())}
    if args.body_file:
        text = _read_text(args.body_file, "body", MAX_BODY_BYTES)
        if text is not None:
            body["body"] = text
    body["evidence"] = _evidence(args.evidence)
    workspaces = _workspaces(agent)

    async def work(hub) -> dict:
        await _attest(body["evidence"], workspaces)
        return await hub.finding(agent.run_id, body)

    answer = _with_hub(agent, work)
    print(f"Finding #{answer.get('id')} recorded ({args.lens}, {args.severity}): {body['title']}")
    return 0


def _plan_file(source: str) -> dict:
    import yaml

    from evo_agents.hub.review import MAX_DRAFT_BYTES

    text = _read_text(source, "draft plan", MAX_DRAFT_BYTES) or ""
    try:
        plan = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkerStateError(f"{source} is neither YAML nor JSON: {exc}") from None
    if not isinstance(plan, dict):
        raise WorkerStateError(f"{source} holds no plan: a draft plan is a mapping with id, goal and steps")
    return json.loads(json.dumps(plan, default=str))  # dates as text, as the hub keeps JSON


@_worker_command
def cmd_propose(args) -> int:
    from evo_agents.hub.review import MAX_BODY_BYTES
    from evo_agents.hub.tiers import split_target

    agent = _agent_run("propose", ("review",))
    paths = []
    for text in args.path or []:
        found = split_target(text)
        if found is None:
            raise WorkerStateError(f"--path takes REPO:PATH, a path within a repo, not {text!r}")
        paths.append({"repo": found[0], "path": found[1]})
    evidence = _evidence(args.evidence or [])
    if not evidence and not args.finding:
        raise WorkerStateError("a proposal rests on --finding ID, --evidence SPEC, or both: give at least one")
    body: dict = {
        "lens": args.lens,
        "kind": args.kind,
        "title": " ".join(args.title.split()),
        "paths": paths,
        "finding_ids": list(dict.fromkeys(args.finding or [])),
        "evidence": evidence,
        "plan": _plan_file(args.plan_file),
    }
    if args.summary_file:
        text = _read_text(args.summary_file, "summary", MAX_BODY_BYTES)
        if text is not None:
            body["summary"] = text
    workspaces = _workspaces(agent)

    async def work(hub) -> dict:
        await _attest(body["evidence"], workspaces)
        return await hub.proposal(agent.run_id, body)

    answer = _with_hub(agent, work)
    if answer.get("state") == "dropped":
        print(
            f"Proposal #{answer.get('id')} was dropped: it repeats proposal #{answer.get('duplicate_of')}, which the "
            "owner rejected lately, and its evidence is not twice as large."
        )
        return 0
    print(f"Proposal #{answer.get('id')} recorded: tier {answer.get('tier')}, {answer.get('state')}.")
    for reason in answer.get("tier_reasons") or []:
        print(f"  {reason}")
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

    from evo_agents.worker import service

    service.register(wsub)

    status = wsub.add_parser("status", help="this worker as the hub and this machine see it")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)

    from evo_agents.worker.doctor import add_parser as add_doctor  # `doctor [--json]`

    add_doctor(wsub)

    from evo_agents.worker.selftest import add_parser as add_selftest  # `selftest --runtime NAME`

    add_selftest(wsub)

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

    # The agent of a plan run runs these; outside one they refuse.
    step = wsub.add_parser(
        "step", help="inside a plan run: report a step of the plan; done runs its verify commands again and pushes"
    )
    step.add_argument("key", help="the step's id, or its order")
    step.add_argument("status", choices=AGENT_STEP_STATUSES, help="in_progress, done, or pending to hand it back")
    step.add_argument("--repo", help="the run's repo the step is in (default: the plan's repo of the step)")
    step.add_argument("--evidence", help="what was done and how it was checked; a line per decision taken")
    step.add_argument(
        "--verify",
        action="append",
        metavar="COMMAND",
        help="a command that checks the step, run again in the repo's worktree; repeat it; done needs one",
    )
    step.set_defaults(func=cmd_step)

    ask = wsub.add_parser("ask", help="inside a plan run: ask the run's owner a decision; prints its id")
    ask.add_argument("--category", required=True, choices=runs.DECISION_CATEGORIES, help="what kind of decision")
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
    ask.add_argument("--step", help="the step of the plan it is about")
    ask.set_defaults(func=cmd_ask)

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

    from evo_agents.hub.review import LENSES, SEVERITIES
    from evo_agents.hub.tiers import CHANGE_KINDS

    # The agent of a review run runs these; outside one they refuse.
    finding = wsub.add_parser("finding", help="inside a review run: record a finding with its evidence; prints its id")
    finding.add_argument("--lens", required=True, choices=list(LENSES), help="the lens it was found through")
    finding.add_argument("--title", required=True, help="what was found, in one line")
    finding.add_argument(
        "--evidence",
        action="append",
        required=True,
        metavar="SPEC",
        help="session:ID[:FIELD:INDEX], run:ID:SEQ or code:REPO:PATH[:LINE]; repeat it",
    )
    finding.add_argument("--severity", choices=SEVERITIES, default="medium", help="low, medium (default) or high")
    finding.add_argument("--body-file", help="markdown that explains it, at most 16 KiB (- for stdin)")
    finding.set_defaults(func=cmd_finding)

    propose = wsub.add_parser(
        "propose", help="inside a review run: propose a change with its draft plan; prints its id, tier and state"
    )
    propose.add_argument("--lens", required=True, choices=list(LENSES), help="the lens it comes from")
    propose.add_argument("--kind", required=True, choices=list(CHANGE_KINDS), help="the kind of change")
    propose.add_argument("--title", required=True, help="the change, in one line")
    propose.add_argument("--plan-file", required=True, help="the draft plan, YAML or JSON, in outcome steps")
    propose.add_argument("--path", action="append", metavar="REPO:PATH", help="a file it would edit; repeat it")
    propose.add_argument("--finding", action="append", type=int, metavar="ID", help="a finding it rests on; repeat it")
    propose.add_argument(
        "--evidence", action="append", metavar="SPEC", help="evidence of its own, as finding takes it; repeat it"
    )
    propose.add_argument("--summary-file", help="markdown: the problem, the change, what waiting costs (- for stdin)")
    propose.set_defaults(func=cmd_propose)

    plan = wsub.add_parser("plan", help="inside a plan run: print the run's plan as the hub holds it now")
    from evo_agents.hub.contract import json_option, returns_object
    from evo_agents.hub.plan_cli import PLAN_KEYS

    json_option(plan, returns_object(*PLAN_KEYS, schema="Plan"), help="the hub's answer as JSON")  # seam hub-cli-v1
    plan.set_defaults(func=cmd_plan)
