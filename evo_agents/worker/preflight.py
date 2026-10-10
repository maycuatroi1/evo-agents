"""What a run checks once it holds its leases, before it fetches anything or starts its agent (``docs/workers.md``,
Preflight and failure causes).

Each check that does not pass is a ``Problem`` with its cause, one of ``evo_agents.hub.runs.PREFLIGHT_CAUSES``, and a
message that names the repo or the program; the run (``evo_agents.worker.run``) fails with all of them, the cause of the
first, before its agent starts, and the hub queues no next attempt of a failed run.

- ``origin``: the project lists no origin for a repo the run needs, as the hub's answer for the run's leases says (a
  repo it names missing with no origin): no credential can be leased for it.
- ``credentials``: git cannot read a repo of the run from its origin, or cannot push to it when the run pushes that
  repo, with what the run holds: its leases, else this machine's own credentials. ``can_reach`` asks the remote
  itself, as cheaply as git can: ``git ls-remote origin HEAD`` to read, ``git push --dry-run`` of the checkout's HEAD to
  a branch no run pushes to push, which sends nothing. Only a refusal for want of a credential (``refused``) fails the
  run; a remote that does not answer, or any other trouble, is the fetch's to report. An origin that is a path on this
  machine takes no credential and is not asked.
- ``missing_tool``: a program a verify command calls (``evo_agents.hub.judge.programs``) is not on the run's PATH. A
  judge run checks the project's hidden checks the same way, and names a hidden check by its place alone, never its
  command or its program.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from evo_agents.hub.credentials import normalize_origin
from evo_agents.hub.judge import programs
from evo_agents.worker import gitops

PROBE_TIMEOUT = 60.0  # seconds git has to answer one question to a remote
PROBE_BRANCH = "refs/heads/evo-run/preflight-{id}"  # where a dry-run push would go: a branch nothing pushes to
# What git prints when the remote refused it for want of a credential, or a credential it does not take: gitops'
# AUTH_FAILURE, SSH without a key the host takes, a token that may not push, and a repo the credential does not see.
REFUSED = re.compile(
    rf"{gitops.AUTH_FAILURE.pattern}|terminal prompts disabled|Permission denied \(publickey|Permission to \S+ denied"
    r"|marked as read only|Repository not found|repository '[^']*' not found|returned error: 403|HTTP 403",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Problem:
    """One check that did not pass: its cause (``runs.PREFLIGHT_CAUSES``) and what it says, naming the repo or the
    program."""

    cause: str
    message: str


def is_local(url: str) -> bool:
    """Whether ``url`` names a repository on this machine (a path, or file://), which takes no credential."""
    return not normalize_origin(url).startswith("https://")


def refused(text: str) -> bool:
    """Whether git's message says the remote refused it for want of a credential."""
    return bool(REFUSED.search(text or ""))


async def can_reach(checkout: Path, run_id: int, *, push: bool, env: Mapping[str, str]) -> tuple[str, str] | None:
    """None when git can read origin from ``checkout`` (and push to it, with ``push``) with ``env``, or when the remote
    gave no clear answer; else (``read`` or ``push``, the end of git's message), the remote having refused it for want
    of a credential."""
    questions = [("read", ("ls-remote", "--quiet", "origin", "HEAD"))]
    if push:
        target = PROBE_BRANCH.format(id=run_id)
        questions.append(("push", ("push", "--dry-run", "--no-verify", "--quiet", "origin", f"HEAD:{target}")))
    for what, question in questions:
        try:
            code, out, err = await gitops.git(checkout, *question, env=env, check=False, timeout=PROBE_TIMEOUT)
        except gitops.GitError:  # no answer in time: the fetch will say what is wrong
            return None
        said = err or out
        if code != 0 and refused(said):
            return what, _tail(said)
    return None


def missing_programs(commands: Iterable[str], path: str | None) -> list[tuple[str, str]]:
    """(program, command) for each program ``commands`` call (``judge.programs``) that is not on ``path`` (the run's
    PATH; the shell's default one when the run has none), in order, each program once; an absolute path counts when it
    is an executable file."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    search = path if path is not None else os.defpath
    for command in commands:
        for program in programs(command):
            if program in seen:
                continue
            seen.add(program)
            if program.startswith("/"):
                present = os.path.isfile(program) and os.access(program, os.X_OK)
            else:
                present = shutil.which(program, path=search) is not None
            if not present:
                found.append((program, command))
    return found


def verify_problems(commands: Iterable[str], path: str | None, *, what: str = "verify command") -> list[Problem]:
    """A ``missing_tool`` problem for each program of ``commands`` not on ``path``, naming the program and the
    command."""
    return [
        Problem(
            "missing_tool",
            f"{what} `{_cut(command, 200)}` calls {program}, which is not on this worker's PATH",
        )
        for program, command in missing_programs(commands, path)
    ]


def hidden_problems(checks: list[str], path: str | None) -> list[Problem]:
    """A ``missing_tool`` problem for each hidden check that calls a program not on ``path``, naming the check by its
    place alone: neither its command nor its program leaves the daemon's memory."""
    return [
        Problem(
            "missing_tool",
            f"hidden check {index} of {len(checks)} calls a program that is not on this worker's PATH",
        )
        for index, check in enumerate(checks, start=1)
        if missing_programs([check], path)
    ]


def summary(problems: list[Problem]) -> str:
    """The error of a run its preflight failed: each problem, the agent not started."""
    said = "; ".join(problem.message for problem in problems)
    return f"preflight: {said}; the agent did not start"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _tail(text: str, limit: int = 300) -> str:
    """git's message on one line, its end when it is long."""
    text = " ".join(text.split())
    return text if len(text) <= limit else "..." + text[-limit:]
