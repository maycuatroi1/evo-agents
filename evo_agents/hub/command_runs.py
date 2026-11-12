"""Command runs (plan hub-cron-jobs, criterion a5): a run of kind ``command`` (``runs.RUN_KINDS``) in which the worker
runs one shell command line of a cron job, with no agent and no model. Pure functions over JSON values, standard library
only, so the api and the worker daemon share them.

The hub queues a command run for the cron job's owner (``evo_agents.hub.server.command_dispatch.queue_command_run``): the command line in
``runs.request``, the repo the job chose, if any, as the run's one entry of ``runs.repos``, and a timeout of
DEFAULT_TIMEOUT_MINUTES, at most MAX_TIMEOUT_MINUTES. A worker of the owner, or one shared with them in the project,
whose daemon says it runs command runs (its heartbeat's ``run_kinds``) claims it; an older daemon never does.

The daemon takes the leases every run of the owner gets for that repo (K7 of the run's principal narrowing them), none
of a runtime's model accounts, makes a new worktree of the repo detached at origin's default branch (or an empty
directory of the run when the job chose no repo), and runs the line there with the machine's shell (``SHELL``, else
/bin/sh) as the daemon's user. Its stdout and stderr go to the run's log, line by line; exit 0 ends the run done, any
other code failed with cause ``command_failed``, and its timeout stops the command and fails the run. The daemon
commits and pushes nothing of what the command left.
"""

from __future__ import annotations

KIND = "command"
DEFAULT_TIMEOUT_MINUTES = 30
MIN_TIMEOUT_MINUTES = 5  # runs_timeout_s_check: no run is shorter than 300 seconds
MAX_TIMEOUT_MINUTES = 240
MAX_COMMAND_BYTES = 16 * 1024  # the command line as UTF-8, as runs.request holds it (author.MAX_REQUEST_BYTES)
MAX_ATTEMPTS = 1  # a command the hub lost is not run again: it may have done its work, and the next fire comes
FAILURE_CAUSE = "command_failed"  # runs.FAILURE_CAUSES: the command exited other than 0
MAX_LINE_CHARS = 4000  # one line of output in the run's log; a longer one is cut
MAX_OUTPUT_LINES = 10_000  # lines of output the run's log takes; the rest is counted, not logged


def command_problem(command: object) -> str | None:
    """Why ``command`` is not a command line a run can hold; None when it is one."""
    if not isinstance(command, str) or not command.strip():
        return "the command is empty"
    if "\x00" in command:
        return "the command holds a NUL character"
    size = len(command.encode("utf-8"))
    if size > MAX_COMMAND_BYTES:
        return f"the command is {size} bytes; at most {MAX_COMMAND_BYTES} are taken"
    return None


def timeout_problem(minutes: object) -> str | None:
    """Why ``minutes`` is not a timeout of a command run; None when it is one."""
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return "the timeout is a whole number of minutes"
    if not MIN_TIMEOUT_MINUTES <= minutes <= MAX_TIMEOUT_MINUTES:
        return f"the timeout is {minutes} minutes; it is {MIN_TIMEOUT_MINUTES} to {MAX_TIMEOUT_MINUTES}"
    return None


def title_of(job: str, command: str) -> str:
    """The run's title: the cron job's name and the start of its command line."""
    line = " ".join(command.split())
    return f"cron {job}: {line[:80]}{'...' if len(line) > 80 else ''}"
