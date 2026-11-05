"""The broad test runs of a plan run: what the agent of a claude-code plan run may not run as a test while it works.

The step's verify runs once through the worker (``evo-agents worker step KEY verify``), the Judge of the merge check
runs the verify of every done step again on the pull request merged with its base, and the full CI runs on the release
tag. Run #1642 of evo-agents (2026-11-05) still spent about 115 of its 173 minutes in tests: a selection of 19 hub test
files took 26 minutes, four commands were killed at their timeout with no result, and run #1675 ran the whole suite
for 28 minutes to guess what two hidden checks wanted.

``broad`` reads a Bash command and names the first pytest run in it that is broad: one that names no test file or node
(the whole suite, or the suite less what ``--ignore`` leaves out), a directory or a glob, more than MAX_FILES files, or
one ``timeout`` lets run longer than MAX_SECONDS. ``--collect-only``, ``--last-failed`` and the like name nothing and
pass. A word that is a shell variable is not read. As ``wait_guard``, this is a second layer, not a boundary: a suite
run from a script gets past it, so the plan run's prompt (``record_rule``) says the same.

Standard library only.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from evo_agents.worker.command_classes import SHELLS, shell_script, simple_commands

MAX_FILES = 10  # test files one pytest command may name
MAX_SECONDS = 600  # what a `timeout` around a pytest command may allow
TIMEOUTS = frozenset({"timeout", "gtimeout"})
DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")
UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
REDIRECT = re.compile(r"^(\d*|&)(>>?|<)$")  # a redirection whose target is the next word
REDIRECTED = re.compile(r"^(\d*|&)(>>?|<)\S")  # one with its target in the same word (2>/dev/null)
# Options of pytest (and its plugins this repo uses) whose value is the next word when not given with "="
VALUED = frozenset(
    {
        "-k",
        "-m",
        "-n",
        "-p",
        "-c",
        "-o",
        "-W",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "--rootdir",
        "--maxfail",
        "--durations",
        "--timeout",
        "--basetemp",
        "--confcutdir",
        "--tb",
        "--dist",
        "--numprocesses",
        "--junitxml",
        "--import-mode",
        "--log-level",
        "--color",
        "--cov",
        "--cov-report",
    }
)
# Options with which pytest runs no broad selection although it names none
NARROW = frozenset(
    {"--collect-only", "--co", "--last-failed", "--lf", "--version", "-V", "-h", "--help", "--fixtures", "--markers"}
)
MAX_WHAT = 160
REFUSAL = (
    "This plan run would run a broad test selection ({what}). Test what you changed by file or node id (at most "
    f"{MAX_FILES} files a command, no directory or glob, a `timeout` of at most {MAX_SECONDS} s), and run the step's "
    "verify once with `evo-agents worker step KEY verify`. The Judge of the merge check runs the verify of every done "
    "step again on the pull request merged with its base, and the full CI runs on the release tag: a broad run here "
    "only holds the worker. A failure of the merge check says how its checks ended; read that rather than running "
    "the suite to guess."
)


def _what(words: list[str]) -> str:
    text = " ".join(words)
    return text if len(text) <= MAX_WHAT else text[: MAX_WHAT - 3] + "..."


def _seconds(word: str) -> float | None:
    found = DURATION.match(word)
    return float(found.group(1)) * UNIT_SECONDS[found.group(2)] if found else None


def _pytest_at(words: list[str]) -> int | None:
    """The index of the word ``pytest`` that starts the arguments of a pytest run in ``words``: the program itself
    (``pytest``, ``.venv/bin/pytest``, ``uv run pytest``) or the module of ``python -m pytest``; None without one."""
    for index, word in enumerate(words):
        name = PurePosixPath(word).name
        if name in ("pytest", "py.test") and (index == 0 or words[index - 1] in ("-m", "run", "exec") or "/" in word):
            return index
    return None


def _allowed_seconds(words: list[str]) -> float | None:
    """The seconds a ``timeout`` before the pytest run in ``words`` allows it; None without one."""
    for index, word in enumerate(words):
        if PurePosixPath(word).name not in TIMEOUTS:
            continue
        for later in words[index + 1 :]:
            if later.startswith("-"):
                continue
            return _seconds(later)
    return None


def _selection(args: list[str]) -> tuple[list[str], bool]:
    """(the test paths and nodes ``args`` of pytest name, whether an option there narrows a run that names none)."""
    named: list[str] = []
    narrow = False
    skip = False
    for word in args:
        if skip:
            skip = False
            continue
        if word in NARROW:
            narrow = True
        elif word in VALUED or REDIRECT.match(word):
            skip = True
        elif REDIRECTED.match(word):
            continue
        elif not word.startswith("-"):
            named.append(word)
    return named, narrow


def _broad_run(words: list[str]) -> str | None:
    """What makes the pytest run in the simple command ``words`` broad; None when it has none or it is narrow."""
    at = _pytest_at(words)
    if at is None:
        return None
    found = _broad_selection(words[at + 1 :])
    if found is not None:
        return f"{found}: {_what(words)}"
    allowed = _allowed_seconds(words[:at])
    if allowed is not None and allowed > MAX_SECONDS:
        return f"a timeout of {allowed:.0f} s: {_what(words)}"
    return None


def _broad_selection(args: list[str]) -> str | None:
    """What makes the selection of the pytest arguments ``args`` broad; None when it names few files or nodes, or what
    it names cannot be read."""
    named, narrow = _selection(args)
    if not named:
        return None if narrow else "the whole suite"
    if any("$" in word for word in named):
        return None
    files = set()
    for word in named:
        path = word.split("::", 1)[0]
        if any(mark in path for mark in "*?["):
            return f"the glob {path}"
        if not path.endswith(".py"):
            return f"the directory {path}"
        files.add(path)
    return f"{len(files)} test files" if len(files) > MAX_FILES else None


def broad(command: str, depth: int = 0) -> str | None:
    """What in the Bash ``command`` is a broad test run (the module's rules); None when nothing is."""
    for words in simple_commands(command):
        if not words:
            continue
        if PurePosixPath(words[0]).name in SHELLS and depth < 3:
            script = shell_script(words)
            found = broad(script, depth + 1) if script else None
            if found:
                return found
            continue
        found = _broad_run(words)
        if found:
            return found
    return None
