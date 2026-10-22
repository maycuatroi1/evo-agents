"""The verify, commit and push stages of ``evo-agents worker step`` (``cli.cmd_step``).

``agent`` is the command's ``cli._AgentRun``: the worker's home, its config and the run's record. A step's verify runs
as a job of the worker (``verify_jobs``): ``verify`` starts it and waits a while, ``done`` reuses the job that passed
with the same commands on the tree the worktree holds now, and otherwise starts it or waits on the one running. Once it
passed, ``done`` commits what is left and pushes the run's branch; a default branch the remote moved on gets the run's
commits on top of its tip, and the verify runs again there, as a job of the replayed tree.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

from evo_agents.worker import verify_jobs
from evo_agents.worker.home import WorkerStateError

OUTPUT_TAIL = 4000  # characters of a failed verify command's output printed
EXIT_FAILED = 1


async def verify_env(venv: object, name: str, path: Path) -> dict[str, str] | None:
    """The environment of the verify of a step in repo ``name``, whose worktree is ``path``: when the daemon made the
    repo's Python environment (``evo_agents.worker.venvs``; ``venv`` is its bin directory), it is made again, so what
    the step changed in pyproject.toml is in it, and its bin directory goes first on PATH; else None, the agent's own
    environment."""
    from evo_agents.worker import venvs

    if not isinstance(venv, str) or not venv:
        return None
    env = venvs.uv_env(os.environ)
    try:
        want = venvs.wanted(path)
    except ValueError as exc:
        raise WorkerStateError(f"{name}: {exc}") from None
    uv = shutil.which("uv", path=env.get("PATH"))
    if want is not None and uv is None:
        print(f"warning: uv is not on PATH: the Python environment of {name} is not made again", file=sys.stderr)
    elif want is not None:
        command, code, output = await venvs.make(path, want, uv, env)
        if code != 0:
            tail = output.rstrip()[-OUTPUT_TAIL:]
            raise WorkerStateError(
                f"`{venvs.shown(command)}` in {name} exited {code}, so the verify did not run:\n{tail}"
            )
        said = ", ".join(f"`{venvs.shown(command)}`" for command in want.commands())
        print(f"Made the Python environment of {name} again: {said}.")
    env["PATH"] = venvs.front(env.get("PATH"), [venv])
    return env


async def verify(path: Path, commands: list[str], env: dict[str, str] | None = None) -> list[dict]:
    """Run each command with /bin/sh in ``path``, in ``env`` (this process's when None), every one even after a
    failure; each one's exit code."""
    results = []
    for command in commands:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        output, _ = await proc.communicate()
        duration_ms = int((time.monotonic() - started) * 1000)
        results.append({"command": command, "exit_code": proc.returncode, "duration_ms": duration_ms})
        print(f"verify: `{command}` exited {proc.returncode} after {duration_ms} ms", flush=True)
        if proc.returncode != 0:
            tail = output.decode(errors="replace")[-OUTPUT_TAIL:].rstrip()
            if tail:
                print("\n".join(f"  | {line}" for line in tail.splitlines()), flush=True)
    return results


# The verify as a job of the worker


def jobs_dir(agent) -> Path:
    return agent.home.run_dir(agent.run_id) / verify_jobs.JOBS_DIR


def left(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


async def checked(agent, name: str, path: Path, commands: list[str], wait: float | None) -> tuple[dict, str]:
    """The job of ``commands`` in repo ``name`` on the tree its worktree ``path`` holds now, once it ended, and how
    it came: ``reused`` (it had passed already), ``waited on`` (an earlier command started it) or ``run``.
    StillRunning when ``wait`` seconds pass first; the job goes on."""
    directory = jobs_dir(agent)
    tree = await verify_jobs.tree_of(path, directory)
    found = verify_jobs.passed(directory, name, tree, commands)
    if found is not None:
        for item in found.get("results") or []:
            print(f"verify: `{item['command']}` exited {item['exit_code']} after {item.get('duration_ms')} ms")
        print(f"(reused: job {found['key'][:12]} passed on tree {tree[:12]} at {found.get('finished_at')})")
        return found, "reused"
    made = agent.record.get("venvs")
    venv = made.get(name) if isinstance(made, dict) else None
    try:
        record, started = await asyncio.to_thread(verify_jobs.start, directory, name, path, tree, commands, venv)
    except OSError as exc:
        raise WorkerStateError(f"the verify of {name} did not start: {exc}") from None
    if record.get("state") == verify_jobs.PASSED:  # it passed between the look and the lock
        return record, "reused"
    what = "started" if started else f"running since {record.get('started_at')}"
    print(f"The verify of {name} on tree {tree[:12]} runs as job {record['key'][:12]} of the worker ({what}).")
    done = await verify_jobs.wait(directory / f"{record['key']}.json", wait)
    print(verify_jobs.log_of(done, directory), end="", flush=True)
    return done, "run" if started else "waited on"


def failures(record: dict) -> str | None:
    """Why the job did not pass, in one line; None when it passed."""
    if record.get("state") == verify_jobs.PASSED:
        return None
    failed = [item for item in record.get("results") or [] if item.get("exit_code") != 0]
    shown = "; ".join(f"`{item['command']}` exited {item['exit_code']}" for item in failed)
    return shown or str(record.get("error") or f"the verify ended {record.get('state') or 'without a result'}")


def still_running(command: str, record: dict) -> int:
    print(
        f"The verify (job {str(record.get('key'))[:12]}, started {record.get('started_at')}) still runs, on its own: "
        f"ending this command or your turn does not stop it. Run the same `{command}` again to wait on it; it picks "
        "the job up as long as the worktree stays as it is. Nothing was committed, pushed or reported.",
        file=sys.stderr,
    )
    return verify_jobs.EXIT_RUNNING


async def verify_step(agent, key: str, name: str, path: Path, commands: list[str], wait: float | None) -> int:
    """``worker step KEY verify``: run the step's verify as a job of the worker and wait on it; 0 when it passed."""
    command = f"evo-agents worker step {key} verify"
    try:
        record, how = await checked(agent, name, path, commands, wait)
    except verify_jobs.StillRunning as exc:
        return still_running(command, exc.record)
    why = failures(record)
    if why is not None:
        print(f"The verify of step {key} failed ({why}); fix it and verify again.", file=sys.stderr)
        return EXIT_FAILED
    print(
        f"The verify of step {key} passed ({how}, job {record['key'][:12]}). `evo-agents worker step {key} done` with "
        "the same --verify commands takes this result while the worktree stays as it is, and runs nothing again."
    )
    return 0


def with_line(evidence: str | None, line: str, limit: int) -> str:
    """The step's evidence with ``line`` last, within ``limit`` characters."""
    if not evidence:
        return line[:limit]
    room = limit - len(line) - 4
    return (evidence if len(evidence) <= room else evidence[: max(0, room)] + "...") + "\n" + line


async def done_step(hub, agent, key: str, step, body: dict, name: str, workspace, commands, wait):
    """``worker step KEY done``: the step's verify passed (reused, waited on or run now), what is left committed and
    the run's branch pushed; the push (``gitops.Pushed``), the verify results the report carries and the evidence line
    that names their job. The exit status instead when the step is not done."""
    from evo_agents.worker import gitops

    deadline = None if wait is None else time.monotonic() + wait
    command = f"evo-agents worker step {key} done"
    try:
        record, how = await checked(agent, name, workspace.worktree, commands, wait)
    except verify_jobs.StillRunning as exc:
        return still_running(command, exc.record)
    why = failures(record)
    if why is not None:
        print(
            f"Step {key} is not done: {why}. Nothing was committed or pushed, and nothing was reported; fix it and run "
            f"`{command}` again, or hand the step back with pending.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    title = step.get("title") if isinstance(step, dict) and isinstance(step.get("title"), str) else None
    message = f"run #{agent.run_id} step {key}" + (f": {' '.join(title.split())}" if title else "")
    path = workspace.worktree
    if await gitops.commit_all(path, message):
        print(f"Committed what was left in {name} as {message!r}.")
    branch = await gitops.current_branch(path)
    if branch is None:
        raise WorkerStateError(f"HEAD is detached in {path}: the worker does not push a detached HEAD")
    if branch != workspace.local_branch:
        raise WorkerStateError(
            f"{path} is on {branch}, not {workspace.local_branch}: switch back; the worker pushes only the run's branch"
        )
    try:
        pushed, again = await push_step(hub, agent, key, name, workspace, body, title, commands, deadline)
    except verify_jobs.StillRunning as exc:
        print(
            f"{workspace.branch} of {name} moved on at origin: the worktree holds the run's commits on top of it now, "
            "and their verify there goes on.",
            file=sys.stderr,
        )
        return still_running(command, exc.record)
    if again is not None:
        record, how = again
    return pushed, record.get("results") or [], verify_jobs.describe(record, how)


async def push_step(hub, agent, key: str, name: str, workspace, body: dict, title, commands, deadline):
    """Push the run's branch of ``name`` for ``worker step KEY done``; the push, and the verify job of the replayed
    tree with how it came when the run's commits went on top of a default branch the remote moved on (else None).
    StillRunning when that verify runs past ``deadline``."""
    from evo_agents.hub import runs
    from evo_agents.worker import gitops

    path = workspace.worktree
    plan_branch = gitops.plan_branches(body).get(name) if "repos" in body else workspace.plan_branch
    curator = agent.record.get("curator") if isinstance(agent.record.get("curator"), dict) else None
    if curator is not None and curator.get("role") == "builder":
        # A Builder of the Curator holds no credential: the daemon pushes the run's branch for it.
        from evo_agents.worker import credential_commands

        answer = await asyncio.to_thread(credential_commands.ask_push, agent.home, agent.run_id, name, title)
        if answer.get("error") or not isinstance(answer.get("head"), str):
            why = answer.get("error") or "the daemon did not say what it pushed"
            raise WorkerStateError(f"{name}: {why}; step {key} is not reported done")
        pushed = gitops.Pushed(
            str(answer.get("branch") or workspace.branch),
            answer["head"],
            bool(answer.get("default")),
            bool(answer.get("changed")),
            tuple(answer.get("commits") or ()),
        )
        return pushed, None
    again: list = []

    async def verify_again(head: str) -> str | None:
        """The step's verify in the worktree at the commits replayed on top of the remote's tip; how it failed."""
        print(
            f"{workspace.branch} of {name} moved on at origin: the run's commits were put on top of it, and the verify "
            f"of step {key} runs again at {head[:12]}."
        )
        try:
            again[:] = await checked(agent, name, path, commands, left(deadline))
        except WorkerStateError as exc:
            return str(exc)
        return failures(again[0])

    replay = gitops.Replay(
        scratch=path.parent / runs.RESULT_DIR / f"replay-{path.name}",
        side_branch=gitops.SIDE_BRANCH.format(id=agent.run_id),
        pushed=agent.home.pushed(agent.run_id, name, workspace.branch),
        verify=verify_again,
    )
    try:
        pushed = await gitops.push(
            path, workspace.branch, protected=workspace.protected, kind="plan", plan_branch=plan_branch, replay=replay
        )
    except gitops.PushRefused as exc:
        raise WorkerStateError(f"{name}: {exc}; step {key} is not reported done") from None
    except gitops.PushConflict as exc:
        await push_conflict(hub, agent, name, exc)
        raise WorkerStateError(
            f"{name}: {exc}. Step {key} is not reported done, and run {agent.run_id} ends failed (push_conflict) once "
            "this turn ends: end your turn now"
        ) from None
    except gitops.GitError as exc:
        raise WorkerStateError(
            f"git push of {name} to {workspace.branch} failed: {exc}; step {key} is not reported done"
        ) from None
    if pushed.default:
        agent.home.note_push(agent.run_id, name, pushed.branch, pushed.head)
    if pushed.onto is None:
        return pushed, None
    print(
        f"{pushed.branch} of {name} on origin had moved on to {pushed.onto[:12]}: the run's commits since then are on "
        f"top of it at {pushed.head[:12]}, and their verify passed there."
    )
    return pushed, (tuple(again) if again else None)


async def push_conflict(hub, agent, name: str, conflict) -> None:
    """A push of ``worker step`` went to the run's side branch (``gitops.PushConflict``): tell the run's owner, and
    leave the daemon the word to fail the run once the agent's turn ends."""
    from evo_agents.worker import gitops
    from evo_agents.worker.hubapi import HubProblem

    agent.home.save_conflict(
        agent.run_id,
        {
            "error": f"{name}: {conflict}",
            "repo": name,
            "branch": conflict.branch,
            "side_branch": conflict.side_branch,
            "head": conflict.head,
        },
    )
    try:
        await hub.notice(agent.run_id, gitops.conflict_notice(agent.run_id, name, conflict))
        print(f"Notified the run's owner that the commits of {name} are on {conflict.side_branch}.", file=sys.stderr)
    except HubProblem as exc:
        print(f"warning: the notice of {conflict.side_branch} was not sent ({exc})", file=sys.stderr)
