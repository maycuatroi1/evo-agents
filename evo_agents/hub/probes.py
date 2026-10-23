"""A probe of a Curator's change: a task in one repo, from a base commit, that a session got wrong before the change,
and a one-line verify that exits 0 when an agent given the task does not make that mistake again (plan
correction-recurrence).

A proposal whose evidence cites a correction of the session figures (``ledger.evidence_metrics`` names
``corrections``) and that changes a file a probe arm can place (``probe_path``) carries one probe, or the hub refuses
it; accepting the proposal attaches its probe to the change. A review run may add one to a merged change that has none,
and the project's admins add, edit and retire them. The verify is the answer key: its admins alone read it.

Nothing here touches the database: the shape of a probe, read the same way by the hub and by the worker.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from math import comb
from pathlib import PurePosixPath

from evo_agents.hub import replay
from evo_agents.hub.verify_words import programs, shell_problem

STATES = ("active", "retired")
SOURCES = ("proposal", "review", "admin")  # who wrote the probe: the proposal it came with, a review run, an admin
MAX_TASK_BYTES = 16 * 1024  # the task text handed the agent of a probe run, as a replay task's ``what``
MAX_VERIFY_CHARS = replay.MAX_VERIFY_CHARS  # one shell line, as a step's verify
MAX_NOTE_CHARS = 2000
MAX_REPO_CHARS = 200
SETTINGS_FILE = ".claude/settings.json"  # the repo's own Claude Code settings, at its root
HOOKS_DIR = ".claude/hooks"  # the repo's own hooks, at its root
# probes.k, the arms each probe runs per model; probes.models, at most this many models
DEFAULT_K = 3
MAX_K = 10
MAX_MODELS = 10
MAX_WEEK_BUDGET_USD = 7000.0  # probes.budget_usd, per ISO week: seven nights of curator.MAX_NIGHT_BUDGET_USD

# A probe run (run kind ``probe``): one probe measured once on one runtime and model of the charter in an ISO week, k
# new sessions with the change's rule on (arm ``on``: the harness files of the default branch's tip) and k with it off
# (arm ``off``: that tip less the change's diff). One row of curator_probe_runs per probe, week, runtime and model.
ARMS = ("on", "off")
RUN_STATES = ("queued", "running", "done", "stale", "failed", "unmeasured")
# queued: to run (again) this week; running: its run holds it; done: every session reported; stale: the change's diff
# no longer comes off the tip, with why; failed: its runs ended short PROBE_ATTEMPTS times; unmeasured: the week's
# money ran out before it
PROBE_ATTEMPTS = 2  # the probe runs of one probe, model and week, before it fails
MAX_REASON_CHARS = 2000
MAX_COMMANDS = 200  # of the trace file: the commands the agent ran, each at most MAX_COMMAND_CHARS
MAX_COMMAND_CHARS = 2000
MAX_FILES = 500  # of the trace file: the files the agent changed
PROBE_KEY_HEADER = "X-Evo-Probe-Key"  # a probe run's own key, which its claim hands the daemon
TRACE_ENV = "EVO_PROBE_TRACE"  # names the trace file to the probe's verify: {"commands": [...], "files": [...]}
CLAUDE_CODE = "claude-code"  # the runtime whose arms carry the repo's .claude/settings.json and .claude/hooks/

# The weekly report of the probes: the status of one probe on one runtime and model in an ISO week. holds: arm on did
# not fail once; recurring: arm on failed at least once; not_reproduced: arm off did not fail once either, so the probe
# did not reproduce the mistake and says nothing of the rule; stale: the change's diff no longer comes off the tip;
# unmeasured: no result of both arms this week (the money ran out, the runs ended short, or it has not run yet).
STATUSES = ("holds", "recurring", "not_reproduced", "stale", "unmeasured")
FLAGGED = ("recurring", "not_reproduced", "stale", "unmeasured")  # the statuses a report points out
MAX_REPORT_WEEKS = 52


def week_of(day: date) -> date:
    """The Monday of the ISO week of ``day``: what a probe run's week is named by."""
    return day - timedelta(days=day.weekday())


def placed_paths(paths: Iterable[str], runtime: str) -> list[str]:
    """The paths among ``paths`` the arms of a probe run on ``runtime`` place: a harness file always, the repo's
    .claude/settings.json and .claude/hooks/ on claude-code alone."""
    return [path for path in probe_paths(paths) if replay.harness_path(path) or runtime == CLAUDE_CODE]


def probe_prompt(task: str, *, folder: str, harness_folder: str | None = None) -> str:
    """The prompt of a session of a probe run: the probe's task alone, after the line that says where its repo is, and
    where the files of the change's repo are when that is another repo (``harness_folder``); the same in both arms."""
    where = f"The repository is in the folder {folder}/ of your working directory."
    if harness_folder:
        where += f" {harness_folder}/ holds files of another repository of the project that this work follows."
    return f"{where}\n\n{task}"


def probe_path(path: str) -> bool:
    """Whether a probe arm can place ``path`` (within its repo): a harness file (``replay.harness_path``: AGENTS.md or
    CLAUDE.md anywhere, a file of a skill), the repo's ``.claude/settings.json``, or a file under its ``.claude/hooks/``.
    """
    if not isinstance(path, str) or not path:
        return False
    parts = PurePosixPath(path).parts
    if not parts or ".." in parts or parts[0] == "/":
        return False
    if replay.harness_path(path):
        return True
    joined = "/".join(parts)
    return joined == SETTINGS_FILE or (joined.startswith(HOOKS_DIR + "/") and len(parts) > 2)


def probe_paths(paths: Iterable[str]) -> list[str]:
    """The paths among ``paths`` a probe arm can place, each once, sorted."""
    return sorted({path for path in paths if isinstance(path, str) and probe_path(path)})


def verify_problem(verify: str) -> str | None:
    """Why ``verify`` cannot be a probe's verify, or None: one shell line of at most MAX_VERIFY_CHARS characters that
    the shell reads (``shell_problem``) and whose first program is a real command (``programs``: not a builtin such as
    ``true`` or ``exit 0`` alone, which would pass every arm). The phrase goes after "the probe's verify"."""
    if not isinstance(verify, str) or not verify.strip():
        return "is empty"
    if "\n" in verify or "\r" in verify:
        return "is more than one line: a probe's verify is one shell line"
    if len(verify) > MAX_VERIFY_CHARS:
        return f"has {len(verify)} characters, more than {MAX_VERIFY_CHARS}"
    why = shell_problem(verify)
    if why is not None:
        return why
    if not programs(verify):
        return "runs no program: its first program must be a real command, not a builtin such as `true` or `exit 0`"
    return None


def arm_counts(sessions: Iterable[dict], arm: str) -> tuple[int, int]:
    """(sessions, failed sessions) of ``arm`` among the sessions a probe run reported."""
    found = [item for item in sessions if isinstance(item, dict) and item.get("arm") == arm]
    return len(found), sum(not item.get("passed") for item in found)


def recurrence(runs: int, failed: int, k: int) -> float | None:
    """1 - pass^k of an arm whose ``runs`` sessions failed ``failed`` times: the chance that k sessions in a row do not
    all pass, by the unbiased estimate of tau-bench (C(c, k) / C(n, k) for c passes in n sessions); with fewer than k
    sessions, 1 - (c / n)^k. With n = k it is 1 as soon as one session failed, else 0. None without a session."""
    if runs <= 0:
        return None
    passed = runs - failed
    rate = comb(passed, k) / comb(runs, k) if runs >= k else (passed / runs) ** k
    return round(1 - rate, 6)


def status_of(state: str | None, on_failed: int, off_runs: int, off_failed: int) -> str:
    """The status a probe run's row of ``state`` (None: the week has none) gives its probe, runtime and model: a row
    done is recurring when arm on failed, not_reproduced when arm off did not fail either, holds otherwise; a stale row
    is stale; any other, or none, is unmeasured."""
    if state == "stale":
        return "stale"
    if state != "done":
        return "unmeasured"
    if on_failed:
        return "recurring"
    if off_runs == 0 or off_failed == 0:
        return "not_reproduced"
    return "holds"
