"""The floor S3 at a push (plan autonomy-levels, criterion a10): the worker never pushes a commit whose message or diff
holds the value of one of the run's leases.

Every push of the worker goes through ``gitops._send``, which calls ``check`` first. It reads the commits the push
would send that the remote does not have yet (``git log -p SOURCE --not --remotes=REMOTE``, at most MAX_COMMITS) and
looks for each lease value in their messages and in the lines their diffs add:

- In the daemon, the values are those its runs hold (``credentials.held_values``).
- In a command of a run's agent (``evo-agents worker step KEY done`` pushes from the agent's own process, which never
  holds a value), it asks the daemon through the run's socket (``{"op": "leaks", "path", "source", "remote"}``), which
  looks with the run's own values and answers the commit alone. A run without leases has no socket: nothing to look
  for.

A commit that holds one is a ``PushRefused`` naming the commit, never the value: rewrite it without the value (and
rotate the credential if it left the machine) before the push. Values shorter than MIN_VALUE_CHARS are not looked for.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Collection
from pathlib import Path

MAX_COMMITS = 500
MIN_VALUE_CHARS = 8
MARK = "\x00evo-commit\x00"  # what git writes for MARK_FORMAT: a NUL never starts a line of a diff
MARK_FORMAT = "%x00evo-commit%x00"
LOG_TIMEOUT = 60.0


def leaked(log: str, values: Collection[str]) -> str | None:
    """The first commit of ``log`` (``git log -p --format=MARK%H%n%B``) whose message or added lines hold one of
    ``values``; None when none does."""
    secrets = [value for value in values if len(value) >= MIN_VALUE_CHARS]
    if not secrets:
        return None
    for chunk in log.split(MARK)[1:]:
        sha, _, rest = chunk.partition("\n")
        kept = "\n".join(line for line in rest.splitlines() if not line.startswith("-"))
        if any(secret in kept for secret in secrets):
            return sha.strip()
    return None


async def leaked_commit(cwd: Path, source: str, remote: str, values: Collection[str]) -> str | None:
    """The first commit ``source`` would send to ``remote`` that holds one of ``values``."""
    if not any(len(value) >= MIN_VALUE_CHARS for value in values):
        return None
    from evo_agents.worker.gitops import git

    args = ["log", "-p", "--no-color", "--no-ext-diff", f"--max-count={MAX_COMMITS}", f"--format={MARK_FORMAT}%H%n%B"]
    code, out, _err = await git(cwd, *args, source, "--not", f"--remotes={remote}", timeout=LOG_TIMEOUT, check=False)
    return leaked(out, values) if code == 0 else None


async def check(cwd: Path, source: str, remote: str) -> None:
    """PushRefused when a commit ``source`` would send to ``remote`` holds the value of one of the run's leases."""
    from evo_agents.worker import credentials
    from evo_agents.worker.gitops import PushRefused

    values = credentials.held_values()
    if values:
        sha = await leaked_commit(cwd, source, remote, values)
    else:
        raw = (os.environ.get("EVO_RUN_ID") or "").strip()
        if not raw.isdigit():
            return
        from evo_agents.worker.home import WorkerHome

        request = {"op": "leaks", "path": str(cwd), "source": source, "remote": remote}
        answer = await asyncio.to_thread(credentials.ask, WorkerHome(), int(raw), request, LOG_TIMEOUT)
        sha = (answer or {}).get("commit")
    if sha:
        raise PushRefused(
            f"commit {str(sha)[:12]} holds the value of one of the run's leases, in its message or its diff: the "
            "worker never pushes it (floor S3). Rewrite the commit without the value, and have the credential rotated "
            "if it left this machine"
        )
