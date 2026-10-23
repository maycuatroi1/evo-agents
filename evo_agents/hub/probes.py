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
