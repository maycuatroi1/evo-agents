"""The verify of a plan run's step, run once by the worker in a process of its own and kept with what it checked.

``evo-agents worker step <id> verify --repo R --verify CMD`` starts the step's verify commands in the repo's worktree
and waits for them a while (``WAIT`` seconds, or ``--wait``); ``evo-agents worker step <id> done`` takes the result of
a verify that passed with the same commands on the same tree, and otherwise starts one, or waits on the one running.
So a long verify runs once for a step, however often the agent asks, and the agent waits on it with a command that
ends after a while (exit status ``EXIT_RUNNING``, 124, while it still runs) instead of a loop of sleeps.

A job is ``runs/<run>/verify/<key>.json``, its output ``<key>.log``; ``key`` is a digest of the repo, the commands and
the tree (``tree_of``: what ``git add -A`` would stage in the worktree, in an index of its own, RUN_COMMIT_EXCLUDES
left out). The job's process (``python -m evo_agents.worker.verify_jobs <record>``) starts in a session of its own,
through a shell that puts it in the background and ends at once (``launch``), so it is nobody's descendant: neither the
end of the agent's turn or of a subagent's, nor the timeout of the tool call that waited on it, stops it. It notes its pid and the pid's start time, and at its end the
state (``passed`` or ``failed``), each command's exit code and the tree it left (``tree_after``, which differs when a
command wrote a file, such as a formatter). A passed job counts for the tree it ran on and for the tree it left, since
``done`` commits what the verify left. A failed or stopped job never counts: asking again runs it again.

A job for another tree of the same repo stops the one running there first (``stop``), as both would share the
worktree, and the end of the run stops whatever still runs (``stop_all``). Records change under ``lock``.

"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from evo_agents.hub.client import write_atomic
from evo_agents.worker import gitops

JOBS_DIR = "verify"  # under the run's directory
WAIT = 540.0  # seconds `worker step verify|done` waits on a job by default: within a tool call of 10 minutes
EXIT_RUNNING = 124  # the exit status of `worker step verify|done` whose wait ran out with the job still running
POLL = 0.5
ALIVE_EVERY = 5.0  # seconds between two looks at whether a job's process is still there
SPAWN_GRACE = 30.0  # seconds a job has to note its pid before it counts as gone
STOP_GRACE = 5.0  # seconds a job has to end on SIGTERM before SIGKILL
LAUNCH_TIMEOUT = 30.0
RUNNING, PASSED, FAILED, STOPPED = "running", "passed", "failed", "stopped"
FILE_MODE = 0o600
DIR_MODE = 0o700


class StillRunning(Exception):
    """The wait on a job ran out while it still runs."""

    def __init__(self, record: dict):
        super().__init__(f"the verify (job {str(record.get('key', '?'))[:12]}) still runs")
        self.record = record


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def key_of(repo: str, tree: str, commands: list[str]) -> str:
    return hashlib.sha256(json.dumps([repo, tree, commands]).encode()).hexdigest()[:32]


async def tree_of(path: Path, scratch: Path) -> str:
    """The tree ``git add -A`` would stage in the worktree at ``path``, leaving out RUN_COMMIT_EXCLUDES, written with a
    copy of its index in ``scratch``, so its own index stays as it is."""
    _, out, _ = await gitops.git(path, "rev-parse", "--git-path", "index")
    index = Path(out.strip())
    index = index if index.is_absolute() else path / index
    scratch.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    copy = scratch / f"index-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        if index.is_file():
            await asyncio.to_thread(shutil.copyfile, index, copy)
        env = {**os.environ, "GIT_INDEX_FILE": str(copy)}
        left_out = [f":(exclude,glob)**/{prefix}**" for prefix in gitops.RUN_COMMIT_EXCLUDES]
        await gitops.git(path, "add", "-A", "--", ".", *left_out, env=env)
        _, out, _ = await gitops.git(path, "write-tree", env=env)
        return out.strip()
    finally:
        copy.unlink(missing_ok=True)


# Records


def read(path: Path) -> dict | None:
    with contextlib.suppress(OSError, ValueError):
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            return record
    return None


def write(path: Path, record: dict) -> None:
    write_atomic(path, json.dumps(record, indent=1, ensure_ascii=False).encode() + b"\n", FILE_MODE)


def records(directory: Path) -> list[tuple[Path, dict]]:
    found = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            record = read(path)
            if record is not None:
                found.append((path, record))
    return found


@contextlib.contextmanager
def lock(directory: Path):
    import fcntl  # POSIX: the worker runs there alone, while the CLI that imports this module runs on Windows too

    directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    with open(directory / "lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def alive(record: dict) -> bool:
    """Whether the process of a running job is still there: its pid started when the job noted; a job that has not
    noted its pid yet only within SPAWN_GRACE of its start."""
    from evo_agents.worker.orphans import process_start

    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return time.time() - float(record.get("created") or 0) < SPAWN_GRACE
    started = record.get("started")
    return started is not None and process_start(pid) == started


def passed(directory: Path, repo: str, tree: str, commands: list[str]) -> dict | None:
    """The latest job that passed with ``commands`` in ``repo`` on ``tree``, or that left ``tree``; None."""
    found = [
        record
        for _, record in records(directory)
        if record.get("state") == PASSED
        and record.get("repo") == repo
        and record.get("commands") == commands
        and tree in (record.get("tree"), record.get("tree_after"))
    ]
    return max(found, key=lambda record: str(record.get("finished_at") or ""), default=None)


def start(
    directory: Path, repo: str, worktree: Path, tree: str, commands: list[str], venv: str | None
) -> tuple[dict, bool]:
    """The job of ``commands`` in ``repo`` on ``tree``: the one that passed or still runs (False), else a new one
    (True), started once the jobs running on another tree of ``repo`` were stopped."""
    key = key_of(repo, tree, commands)
    path = directory / f"{key}.json"
    with lock(directory):
        record = read(path)
        if record is not None and (record.get("state") == PASSED or (record.get("state") == RUNNING and alive(record))):
            return record, False
        for other_path, other in records(directory):
            if other_path != path and other.get("repo") == repo and other.get("state") == RUNNING:
                stop(other_path, other, why=f"the worktree of {repo} changed: a verify of tree {tree[:12]} started")
        record = {
            "key": key,
            "id": secrets.token_hex(8),
            "repo": repo,
            "worktree": str(worktree),
            "tree": tree,
            "commands": commands,
            "venv": venv,
            "state": RUNNING,
            "created": time.time(),
            "started_at": now(),
        }
        write(path, record)
        launch(path, directory / f"{key}.log")
    return record, True


def launch(path: Path, log: Path) -> None:
    """Start the job of record ``path``, its output to ``log``, in a session of its own, through a shell that starts
    it in the background and ends at once, so the job is nobody's child."""
    with open(log, "wb") as out:
        os.chmod(log, FILE_MODE)
        done = subprocess.run(
            ["/bin/sh", "-c", '"$@" &', "sh", sys.executable, "-m", "evo_agents.worker.verify_jobs", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            timeout=LAUNCH_TIMEOUT,
            check=False,
        )
    if done.returncode != 0:
        raise OSError(f"the verify job did not start (exit {done.returncode}); its log is {log}")


def stop(path: Path, record: dict, *, why: str) -> None:
    """Stop a running job's process group (SIGTERM, then SIGKILL after STOP_GRACE) and mark it stopped."""
    pgid = record.get("pgid")
    if isinstance(pgid, int) and pgid > 1 and alive(record):
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_GRACE
        while time.monotonic() < deadline and alive(record):
            time.sleep(0.1)
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
    current = read(path) or record
    if current.get("id") == record.get("id") and current.get("state") == RUNNING:
        write(path, {**current, "state": STOPPED, "error": why, "finished_at": now()})


def stop_all(directory: Path, why: str = "the run ended") -> int:
    """Stop every job still running in ``directory``; how many."""
    if not directory.is_dir():
        return 0
    stopped = 0
    with lock(directory):
        for path, record in records(directory):
            if record.get("state") == RUNNING:
                stop(path, record, why=why)
                stopped += 1
    return stopped


async def wait(path: Path, timeout: float | None) -> dict:
    """The job's record once it no longer runs; StillRunning when ``timeout`` seconds pass first. A job whose
    process is gone without a result is marked stopped."""
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + max(0.0, timeout)
    looked = loop.time()
    while True:
        record = read(path) or {}
        if record.get("state") != RUNNING:
            return record
        if loop.time() - looked >= ALIVE_EVERY:
            looked = loop.time()
            if not await asyncio.to_thread(alive, record):
                with lock(path.parent):
                    stop(path, record, why="the verify's process ended without a result")
                continue
        if deadline is not None and loop.time() >= deadline:
            raise StillRunning(record)
        await asyncio.sleep(POLL)


def log_of(record: dict, directory: Path) -> str:
    with contextlib.suppress(OSError):
        return (directory / f"{record.get('key')}.log").read_text(encoding="utf-8", errors="replace")
    return ""


def took(record: dict) -> str:
    seconds = sum(int(item.get("duration_ms") or 0) for item in record.get("results") or []) // 1000
    return f"{seconds // 60}m{seconds % 60:02d}s"


HOW = {
    "reused": "reused by done, no run again",
    "waited on": "started before done, which waited on it",
    "run": "run by done",
}


def describe(record: dict, how: str) -> str:
    """The evidence line of the verify a step's report carries: which job, on which tree, and ``how`` ``done`` came
    by its result (a key of HOW)."""
    return (
        f"Verify by the worker: job {str(record.get('key'))[:12]} on tree {str(record.get('tree'))[:12]}, started "
        f"{record.get('started_at')}, {took(record)} of commands; {HOW.get(how, how)}."
    )


# The job's own process


async def run(path: Path) -> int:
    """Run the job of record ``path``: note its pid, make the repo's Python environment again when the daemon made
    it, run the commands, and note the result, unless the record names another job by then."""
    from evo_agents.worker import step_verify
    from evo_agents.worker.home import WorkerStateError
    from evo_agents.worker.orphans import process_start

    directory = path.parent
    with lock(directory):
        record = read(path)
        if record is None or record.get("state") != RUNNING:
            return 0
        record.update(pid=os.getpid(), pgid=os.getpgrp(), started=process_start(os.getpid()))
        write(path, record)
    worktree, error, results = Path(record["worktree"]), None, []
    try:
        env = await step_verify.verify_env(record.get("venv"), record["repo"], worktree)
        results = await step_verify.verify(worktree, list(record["commands"]), env)
    except WorkerStateError as exc:
        error = str(exc)
        print(f"error: {error}")
    after = None
    with contextlib.suppress(gitops.GitError, OSError):
        after = await tree_of(worktree, directory)
    ok = error is None and bool(results) and all(item["exit_code"] == 0 for item in results)
    sys.stdout.flush()
    with lock(directory):
        current = read(path)
        if current is None or current.get("id") != record["id"] or current.get("state") != RUNNING:
            return 0
        current.update(
            state=PASSED if ok else FAILED, results=results, error=error, tree_after=after, finished_at=now()
        )
        write(path, current)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m evo_agents.worker.verify_jobs RECORD", file=sys.stderr)
        return 2
    return asyncio.run(run(Path(argv[0])))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
