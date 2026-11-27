"""Which process the kernel's OOM killer takes first in a run: a command of the agent, never the agent's CLI.

Every process of a worker's unit starts with the unit's ``oom_score_adj`` (systemd's ``OOMScoreAdjust``, 200 in the unit
the worker installs), so when the machine runs out of memory the kernel kills the largest one. In a run whose agent works
with subagents that is the agent's CLI: run #12899 of evo-agents lost its session twice in 16 minutes (2026-11-27,
``exit code -9``) while ten subagents ran tests at once, and every subagent's work with it; the run then failed.

So the shells of the agent's commands raise their own score (``SCORE``) before they run, and what they start inherits
it: Claude Code runs each Bash call as ``/bin/bash -c``, and a non-interactive bash reads the file ``BASH_ENV`` names
first (``SCRIPT``). The kernel then kills a test or a build, whose shell tells the agent so, before the CLI. A score
already above ``SCORE`` stays; a ``BASH_ENV`` the daemon's environment set is read after it (``PREVIOUS``). Only
where ``/proc/self/oom_score_adj`` exists (Linux): elsewhere the agent's environment is left as it is.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

SCORE = 700  # out of 1000: above the CLI's (the unit's) score, below what a process may still raise itself to
ADJ = Path("/proc/self/oom_score_adj")
VARIABLE = "BASH_ENV"
PREVIOUS = "EVO_PREVIOUS_BASH_ENV"  # the BASH_ENV of the daemon's environment, which SCRIPT reads after its own line
FILE = Path("oom") / "bash_env.sh"
SCRIPT = f"""# evo-agents worker (evo_agents/worker/oom.py): a command of the run's agent, and what it starts, goes before the
# agent's CLI when the machine runs out of memory.
if [ -w /proc/self/oom_score_adj ] && read -r evo_adj < /proc/self/oom_score_adj && [ "$evo_adj" -lt {SCORE} ]; then
    echo {SCORE} > /proc/self/oom_score_adj 2>/dev/null
fi
unset evo_adj
if [ -n "${{{PREVIOUS}:-}}" ] && [ -r "${PREVIOUS}" ]; then . "${PREVIOUS}"; fi
"""


def script(root: Path) -> Path:
    """The file of ``SCRIPT`` under the worker's ``root``, written when missing or stale."""
    path = Path(root) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != SCRIPT:
        path.write_text(SCRIPT, encoding="utf-8")
    return path


def agent_env(env: Mapping[str, str], root: Path, *, adj: Path = ADJ) -> dict[str, str]:
    """What a run's agent gets after ``env`` so that its commands go first: ``BASH_ENV`` naming ``SCRIPT``, and the
    ``BASH_ENV`` ``env`` had as ``PREVIOUS``; nothing on a machine without ``adj``."""
    if not adj.exists():
        return {}
    path = script(root)
    found = {VARIABLE: str(path)}
    previous = env.get(VARIABLE)
    if previous and previous != str(path):
        found[PREVIOUS] = previous
    return found
