"""The worker's PreToolUse hook of waits in a plan run: what the agent of a claude-code plan run may not use to wait.

A plan run waits for a time or an event with ``evo-agents worker wait`` (``evo_agents.hub.server.run_waits``): its turn
ends, the hub parks the run at once when the wait wakes later than ``runs.DECISION_WAIT_SECONDS``, the run holds no
worker slot, and the session goes on, on the same worker, when the wait wakes. A wait inside the session parks
nothing: the adapter keeps the session open while a command the agent started in the background runs, up to the run's
time cap (``runtimes.claude_code``), and a foreground ``sleep`` holds the turn. Run #1537 of evo-agents (2026-11-04)
held a slot of worker #23 for more than seven hours, four to five of them waiting for the CI of a tag, PyPI, builds of
the knowledge graph on the hub and kg_init runs, with Monitor watches, loops that slept and a ``sleep 590``, and the
worker could not self-update while it was held.

``WaitGuard`` refuses, in a plan run only (the only kind the hub takes a wait of):

- the Monitor tool, a watch of a command's output, whatever it watches;
- a Bash command, in the foreground or the background, that sleeps SLEEP_LIMIT seconds or more at once, holds a loop
  (``while``, ``until``, ``for``) that sleeps LOOP_SLEEP seconds or more, or runs a command that blocks until something
  elsewhere ends (WATCHERS: ``gh run watch``, ``gh pr checks --watch``, ``watch``, ``evo-agents hub run logs --follow``).

The refusal tells the agent to wait with ``evo-agents worker wait`` and end its turn. Work on the machine (tests, a
build) still runs in the background as before. The thresholds are a reading of the commands such waits use, not a
boundary: a poll kept in a script, or a sleep under another name, gets past the hook, so the plan run's prompt
(``run_prompts.WAIT_RULE``) and the execute-plan skill say the same.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from evo_agents.worker.command_classes import SHELLS, _shell_script, simple_commands

SLEEP_LIMIT = 120  # seconds one sleep may last before it is a wait
LOOP_SLEEP = 5  # seconds a sleep inside a loop may last before the loop is a poll
LOOPS = frozenset({"while", "until", "for"})
KEYWORDS = frozenset({"do", "then", "else", "elif", "{", "!", "time"})  # words before a command of a compound one
WRAPPERS = frozenset({"timeout", "gtimeout", "nohup", "setsid", "stdbuf"})
DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")
UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
EVO_PROGRAMS = frozenset({"evo-agents", "evo-ak"})
MAX_WHAT = 160
REFUSAL = (
    "This plan run would wait inside its session ({what}). A wait there holds the worker's slot for as long as it "
    "lasts, and the run cannot park while the session waits. Wait with `evo-agents worker wait --reason TEXT --until "
    "+15m --step KEY` instead (--on and --contains wake it sooner on an event of the hub, such as another plan's step "
    "done), then end your turn: the hub parks the run, which holds no worker, and wakes this session on the same "
    "worker when the time comes. Then look once, and wait again if it has not come. Work on this machine, such as "
    "tests or a build, may run in the background; waiting for something elsewhere (the CI of a commit, a pull request "
    "or a tag, a package on an index, a build or a run on the hub, a deploy) may not."
)


def _what(words: list[str]) -> str:
    text = " ".join(words)
    return text if len(text) <= MAX_WHAT else text[: MAX_WHAT - 3] + "..."


def _seconds(words: list[str]) -> float:
    """The seconds ``sleep`` sleeps for its arguments ``words`` (GNU sleep adds them up); 0 for one it cannot read."""
    total = 0.0
    for word in words:
        found = DURATION.match(word)
        if found is None:
            break
        total += float(found.group(1)) * UNIT_SECONDS[found.group(2)]
    return total


def _bare(words: list[str]) -> list[str]:
    """``words`` from the command on: the keywords of a compound command and the wrappers (with their options and a
    duration of ``timeout``) left out."""
    index = 0
    while index < len(words):
        name = PurePosixPath(words[index]).name
        if words[index] in KEYWORDS:
            index += 1
        elif name in WRAPPERS:
            index += 1
            while index < len(words) and (words[index].startswith("-") or DURATION.match(words[index])):
                index += 1
        else:
            break
    return words[index:]


def _watcher(words: list[str]) -> bool:
    """Whether the command ``words`` blocks until something elsewhere ends (the module's WATCHERS)."""
    name = PurePosixPath(words[0]).name
    rest = words[1:]
    if name == "watch":
        return True
    if name == "gh":
        if rest[:2] == ["run", "watch"]:
            return True
        return rest[:2] == ["pr", "checks"] and "--watch" in rest
    if name in EVO_PROGRAMS:
        return rest[:3] == ["hub", "run", "logs"] and any(word in ("-f", "--follow") for word in rest[3:])
    return False


def waiting(command: str, depth: int = 0) -> str | None:
    """What in the Bash ``command`` is a wait (the module's rules), as words of it; None when nothing is."""
    looped = False
    loop_sleep: list[str] | None = None
    for words in simple_commands(command):
        words = _bare(words)
        if not words:
            continue
        name = PurePosixPath(words[0]).name
        if name in SHELLS and depth < 3:
            script = _shell_script(words)
            found = waiting(script, depth + 1) if script else None
            if found:
                return found
            continue
        if name in LOOPS:
            looped = True
        if _watcher(words):
            return _what(words)
        if name == "sleep":
            seconds = _seconds(words[1:])
            if seconds >= SLEEP_LIMIT:
                return _what(words)
            if seconds >= LOOP_SLEEP and loop_sleep is None:
                loop_sleep = words
    if looped and loop_sleep is not None:
        return f"a loop that runs {_what(loop_sleep)}"
    return None


@dataclass
class WaitGuard:
    """The hook of waits of one plan run (module docstring), and how it records a refusal (``record``, a text and a
    body)."""

    record: Callable[[str, dict], None] = field(default=lambda text, body: None)
    run_id: int | None = None

    def refused(self, tool_name: str, tool_input) -> str | None:
        """What the call of ``tool_name`` waits on, when it is a wait the run may not make; None otherwise."""
        given = tool_input if isinstance(tool_input, Mapping) else {}
        if tool_name == "Monitor":
            command = given.get("command")
            return "a Monitor watch" + (f" of {_what([command])}" if isinstance(command, str) and command else "")
        if tool_name == "Bash" and isinstance(given.get("command"), str):
            return waiting(given["command"])
        return None

    def check(self, tool_name: str, tool_input) -> str | None:
        """Why the call is refused, recorded as an event of the run; None when it goes through. A command the guard
        cannot read goes through: the guard is a second layer, not the boundary."""
        try:
            what = self.refused(tool_name, tool_input)
        except Exception:
            return None
        if what is None:
            return None
        self.record(
            f"Refused {tool_name}: the plan run would wait in its session ({what})",
            {"tool": tool_name, "what": what, "verdict": "refused", "run": self.run_id},
        )
        return REFUSAL.format(what=what)


def claude_wait_hook(guard: WaitGuard | None):
    """The PreToolUse callback of the Claude Agent SDK for ``guard``, or None without one."""
    if guard is None:
        return None

    async def wait_guard(data, tool_use_id, context) -> dict:
        why = guard.check(str(data.get("tool_name") or ""), data.get("tool_input"))
        if why is None:
            return {}
        decision = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": why}
        return {"hookSpecificOutput": decision}

    return wait_guard


def for_run(run) -> WaitGuard | None:
    """The hook of waits of a run of the daemon (``runner.context.Run``): a plan run's, None for any other kind."""
    if run.spec.get("kind") != "plan":
        return None
    return WaitGuard(record=lambda text, body: run.note(text, wait=body), run_id=run.id)
