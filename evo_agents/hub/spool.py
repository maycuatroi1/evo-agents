"""``evo-agents hub hook post-tool-use``: the PostToolUse hook of the evo-hub plugin, which keeps a long tool result
out of the model's context.

A successful result of Bash, Grep, WebFetch or an MCP tool longer than the threshold (THRESHOLD characters, or
EVO_SPOOL_THRESHOLD; 0 or ``off`` turns the hook off) is written whole to ``~/.evo/tool-output/<session>/<call>.txt``,
the directories mode 0700 and the file 0600. In its place the model gets, through ``updatedToolOutput``, the first
HEAD and the last TAIL characters around one line that names the size and the file. A Read without an offset or a
limit has a threshold of its own (READ_THRESHOLD, or EVO_SPOOL_READ_THRESHOLD; 0 or ``off`` leaves every Read whole)
and keeps its first READ_HEAD characters of whole lines. The tools of evo-hub and evo-kg (SELF_LIMITED) pass whole:
they cut their own text at 25,000 characters and give the rest through kg_more, and Claude Code itself saves an MCP
text over 50,000 characters to a file. So does a Bash call that prints a plan's handoff (HANDOFF_COMMAND), which has
a budget of its own and is never to be cut.

The numbers come from the Claude Code transcripts of one laptop from 2026-10-13 07:41 to 2026-10-18 (669 transcripts,
353 of them subagents, 50,255 tool calls; the scripts of the harness's ``research_notes/Rà soát quản lý context/``):

- Bash: 38,087 results, 95th percentile 10,729 characters. Spooled at 10,000, they saved 67% of their characters after
  the agent's reads of the spool file or reruns within 10 calls are taken off; even the results of 10,000 to 15,000
  saved 38%. The threshold, HEAD and TAIL stay.
- Read without offset or limit: 914 reads, median 4,528 characters, 90th percentile 24,307, 95th 32,311. Of the 287
  cut at 10,000 with a head of 1,500, 260 (91%) were read again, 95% of those from the offset the note named and 59%
  to the file's end, whatever the file's size: the cut saved 8% of their characters and cost 338 extra model calls.
  No head under a file's size changes that much, since most reads go on to the end; what saves calls is to leave
  more reads whole. At 40,000 only 36 of the 914 reads are cut (they hold 21% of the characters of all reads), each
  keeping its first 20,000 characters, so a cut always takes off at least as much as it keeps. Replayed on the same
  calls, that makes 74 extra model calls instead of 338.
- WebFetch: 861 results, median 1,382, 95th percentile 23,858; the 58 over 10,000 hold 59% of WebFetch's characters,
  and none was spooled before, since WebFetch was not in the matcher.
- evo-hub and evo-kg: 440 results, 95th percentile 23,514; cutting them at 10,000 hid what kg_more and plan_handoff
  are there to give whole.

What Claude Code 2.1.294 hands the hook, and what it takes back (tried on 2026-10-12 with a hook that logged its input
and one that replaced the output; ``updatedToolOutput`` must keep the tool's shape, else Claude Code ignores it):

- Bash: ``{stdout, stderr, interrupted, isImage, noOutputExpected}``. Past 30,000 characters Claude Code saves the
  output in its session directory itself, adds ``persistedOutputPath`` and ``persistedOutputSize``, and stdout holds
  the first 30,000: the spool file is filled from Claude Code's file, and the replacement leaves both keys out, else
  Claude Code shows its own preview. A successful command's stderr up to STDERR_KEEP characters stays whole.
- Read: ``{type: "text", file: {filePath, content, numLines, startLine, totalLines, ...}}``. Claude Code numbers the
  lines of ``content`` from ``startLine``, so the replacement keeps whole first lines and ends with the note line, which
  names the line to Read on from; a tail would carry wrong numbers, and file text given as ``additionalContext`` would
  reach the model with the authority of a system reminder. A Read with ``offset`` or ``limit`` asked for that range and
  passes unchanged.
- Grep: ``{mode, numFiles, filenames, content, numLines, ...}``. Claude Code leaves Grep out of the default tool set on
  macOS and Linux, where searches run as Bash's grep, which the Bash case covers.
- WebFetch: ``{bytes, code, codeText, result, durationMs, url}`` (Claude Code 2.1.286 to 2.1.299 transcripts); the
  model reads ``result``, which becomes the head and tail, the other keys staying.
- An MCP tool: the list of the result's content blocks, given without a check of its shape; the text blocks become one,
  the others stay.

Anything else passes unchanged: another shape, an image, a call that names the spool directory (the agent reading a
spool file), a tool of evo-hub or evo-kg, a Bash call that prints a plan's handoff, and a failed call, which comes as
PostToolUseFailure and keeps Claude Code's own excerpt of up to about 10,000 characters of its head and tail, so the
agent sees why it failed. So does a payload from Codex (it has a ``turn_id``): Codex 0.153 ignores
``updatedToolOutput``, ``decision: "block"`` turns a successful command into "Script failed", and its hook gets the
output already cut to Codex's own limit, about 10,000 tokens.

Like the other hooks of the plugin this one never fails the session: exit status 0, at most BUDGET seconds (hooks.json
gives Claude Code's limit), and it prints the replacement or nothing. A failure is one line on stderr naming the type
of the exception, never the output. It talks to no hub: a spool file never leaves this machine. Files older than
EVO_SPOOL_DAYS days (DAYS by default) are deleted by the hook, at most once every PRUNE_EVERY seconds.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from evo_agents.hub.hooks import OutOfTime, budget, read_payload

THRESHOLD = 10_000  # characters of a result kept in context; a longer one goes to a file
THRESHOLD_VARIABLE = "EVO_SPOOL_THRESHOLD"
READ_THRESHOLD = 40_000  # characters of a Read without offset or limit kept whole
READ_THRESHOLD_VARIABLE = "EVO_SPOOL_READ_THRESHOLD"
READ_HEAD = 20_000  # characters of whole first lines a longer Read keeps, at most half its threshold
DAYS = 7.0  # days a spool file is kept
DAYS_VARIABLE = "EVO_SPOOL_DAYS"
HEAD = 1_500  # characters of a spooled result's start the model still gets
TAIL = 1_500  # and of its end
STDERR_KEEP = 2_000  # characters of a successful command's stderr kept whole
BUDGET = 5.0  # seconds for the whole hook
MAX_PAYLOAD = 32 << 20  # characters of the JSON Claude Code writes to the hook's stdin
MAX_COPY = 64 << 20  # bytes of Claude Code's own saved output copied into a spool file
PRUNE_EVERY = 3600.0  # seconds between two clean-ups of old spool files
DIRECTORY = "tool-output"  # under ~/.evo
DIR_MODE = 0o700
FILE_MODE = 0o600
STAMP = ".pruned"  # its mtime says when the last clean-up ran
NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")  # a session id or tool_use_id that may name a file
SAVED_KEYS = ("persistedOutputPath", "persistedOutputSize")  # Claude Code's own saved copy of a long Bash output
# The tools of evo-hub and evo-kg, as a plugin (plugin_<plugin>_<server>) or a server of their own name: they cut their
# own text at CAP_CHARS of ``evo_agents.kg.serve`` and give the rest through kg_more.
SELF_LIMITED = ("mcp__plugin_evo-hub_evo-hub__", "mcp__plugin_evo-kg_evo-kg__", "mcp__evo-hub__", "mcp__evo-kg__")
HANDOFF_COMMAND = re.compile(r"\bhub\s+plan\s+handoff\b")  # `evo-agents hub plan handoff`: a budget of its own
STDERR_NOTE = "[evo-hub: stderr cut here; all of it follows the stdout in the file the stdout names]"


def spool_root() -> Path:
    return Path.home() / ".evo" / DIRECTORY


def threshold(env: Mapping[str, str], variable: str = THRESHOLD_VARIABLE, default: int = THRESHOLD) -> int | None:
    """The threshold in characters, or None when ``variable`` (EVO_SPOOL_THRESHOLD) turns the hook off; a value that
    is not a whole number counts as unset."""
    value = env.get(variable, "").strip().lower()
    if value in ("0", "off"):
        return None
    return int(value) if value.isdigit() else default


def read_threshold(env: Mapping[str, str]) -> int | None:
    """The threshold of a Read without offset or limit, or None when EVO_SPOOL_READ_THRESHOLD leaves every Read
    whole."""
    return threshold(env, READ_THRESHOLD_VARIABLE, READ_THRESHOLD)


def keep_days(env: Mapping[str, str]) -> float:
    try:
        days = float(env.get(DAYS_VARIABLE, "") or DAYS)
    except ValueError:
        return DAYS
    return days if days > 0 else DAYS


# The view the model gets


def excerpt(text: str, note: str, head: int, tail: int) -> str:
    """The first ``head`` and last ``tail`` characters of ``text`` around ``note``, cut at a line end when one is
    near, so no line shows half."""
    if len(text) <= head + tail:
        return f"{text}\n\n{note}"
    start, end = text[:head], text[-tail:]
    cut = start.rfind("\n")
    if cut >= head // 2:
        start = start[:cut]
    cut = end.find("\n")
    if 0 <= cut < tail // 2:
        end = end[cut + 1 :]
    return f"{start}\n\n{note}\n\n{end}"


def head_lines(lines: list[str], head: int) -> list[str]:
    """The first of ``lines`` that fit in ``head`` characters, at least the first, cut to ``head``."""
    kept, used = [], 0
    for line in lines:
        if kept and used + len(line) + 1 > head:
            break
        kept.append(line[:head])
        used += len(line) + 1
    return kept


@dataclass
class Long:
    """One result the hook may spool: the whole text for the file, its size as the tool gave it, and ``render``,
    which builds the tool's output for the model from the note and the head and tail sizes."""

    text: str
    size: int
    render: Callable[[str, int, int], object]
    lines: int | None = None  # for a Read: how many lines the read returned
    first_line: int = 1


# Each tool's output


def _string(value) -> str:
    return value if isinstance(value, str) else ""


def _saved_copy(response: dict) -> str | None:
    """The whole output of a long command from the file Claude Code saved it in; None when that cannot be read."""
    path = response.get("persistedOutputPath")
    if not isinstance(path, str) or not os.path.isabs(path):
        return None
    try:
        with open(path, "rb") as handle:
            return handle.read(MAX_COPY).decode("utf-8", errors="replace")
    except OSError:
        return None


def bash(response, tool_input) -> Long | None:
    if not isinstance(response, dict) or not isinstance(response.get("stdout"), str) or response.get("isImage"):
        return None
    stdout, stderr = response["stdout"], _string(response.get("stderr"))
    if response.get("persistedOutputPath") is not None:
        stdout = _saved_copy(response)
        if stdout is None:  # Claude Code's own preview and path stay
            return None
    kept = {key: value for key, value in response.items() if key not in SAVED_KEYS}

    def render(note: str, head: int, tail: int) -> dict:
        errors = stderr if len(stderr) <= STDERR_KEEP else excerpt(stderr, STDERR_NOTE, head // 2, tail)
        return {**kept, "stdout": excerpt(stdout, note, head, tail), "stderr": errors}

    text = stdout + (f"\n--- stderr ---\n{stderr}" if stderr else "")
    return Long(text, len(stdout) + len(stderr), render)


def read(response, tool_input) -> Long | None:
    file = response.get("file") if isinstance(response, dict) and response.get("type") == "text" else None
    if not isinstance(file, dict) or not isinstance(file.get("content"), str):
        return None
    if isinstance(tool_input, dict) and (tool_input.get("offset") is not None or tool_input.get("limit") is not None):
        return None  # a range asked for on purpose
    lines = file["content"].split("\n")
    first = file.get("startLine") if isinstance(file.get("startLine"), int) else 1

    def render(note: str, head: int, tail: int) -> dict:
        kept = head_lines(lines, head)
        return {**response, "file": {**file, "content": "\n".join([*kept, note]), "numLines": len(kept) + 1}}

    return Long(file["content"], len(file["content"]), render, lines=len(lines), first_line=first)


def grep(response, tool_input) -> Long | None:
    if not isinstance(response, dict):
        return None
    content, names = response.get("content"), response.get("filenames")
    if isinstance(content, str) and content:
        return Long(content, len(content), lambda note, h, t: {**response, "content": excerpt(content, note, h, t)})
    if isinstance(names, list) and names and all(isinstance(name, str) for name in names):
        text = "\n".join(names)

        def render(note: str, head: int, tail: int) -> dict:
            return {**response, "filenames": excerpt(text, note, head, tail).split("\n")}

        return Long(text, len(text), render)
    return None


def web_fetch(response, tool_input) -> Long | None:
    if not isinstance(response, dict) or not isinstance(response.get("result"), str):
        return None
    text = response["result"]
    return Long(text, len(text), lambda note, h, t: {**response, "result": excerpt(text, note, h, t)})


def mcp(response, tool_input) -> Long | None:
    if isinstance(response, str):
        return Long(response, len(response), lambda note, h, t: excerpt(response, note, h, t))
    if not isinstance(response, list) or not all(isinstance(block, dict) for block in response):
        return None
    texts = [block for block in response if block.get("type") == "text" and isinstance(block.get("text"), str)]
    if not texts:
        return None
    text = "\n".join(block["text"] for block in texts)
    others = [block for block in response if block not in texts]

    def render(note: str, head: int, tail: int) -> list:
        return [{"type": "text", "text": excerpt(text, note, head, tail)}, *others]

    return Long(text, len(text), render)


HANDLERS: dict[str, Callable[..., Long | None]] = {"Bash": bash, "Read": read, "Grep": grep, "WebFetch": web_fetch}


def handler(tool_name) -> Callable[..., Long | None] | None:
    """How to read a tool's output; None for a tool the hook leaves alone, those of evo-hub and evo-kg among them."""
    if not isinstance(tool_name, str) or tool_name.startswith(SELF_LIMITED):
        return None
    return mcp if tool_name.startswith("mcp__") else HANDLERS.get(tool_name)


# The spool file


def _name(value, fallback: str) -> str:
    return value if isinstance(value, str) and NAME.fullmatch(value) else fallback


def write_spool(root: Path, session: str, call: str, text: str) -> Path:
    """Write ``text`` to ``root/session/call.txt``, the directories 0700 and the file 0600 whatever the umask."""
    directory = root / session
    for path in (root, directory):
        path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        os.chmod(path, DIR_MODE)
    target = directory / f"{call}.txt"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8", errors="replace", newline="") as handle:
        handle.write(text)
    os.chmod(target, FILE_MODE)
    return target


def prune(root: Path, keep_seconds: float, now: float) -> int:
    """Delete the spool files older than ``keep_seconds`` and the session directories left empty, at most once every
    PRUNE_EVERY seconds; how many files went."""
    stamp = root / STAMP
    with contextlib.suppress(OSError):
        if 0 <= now - stamp.stat().st_mtime < PRUNE_EVERY:
            return 0
    with contextlib.suppress(OSError):
        os.close(os.open(stamp, os.O_WRONLY | os.O_CREAT, FILE_MODE))
        os.utime(stamp, (now, now))
    removed = 0
    for directory in _entries(root):
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in _entries(directory):
            with contextlib.suppress(OSError):
                if path.is_file() and not path.is_symlink() and now - path.stat().st_mtime > keep_seconds:
                    path.unlink()
                    removed += 1
        with contextlib.suppress(OSError):
            directory.rmdir()  # only when empty
    return removed


def _entries(directory: Path) -> list[Path]:
    try:
        return list(directory.iterdir())
    except OSError:
        return []


# The hook


def note(long: Long, path: Path, head: int, days: float) -> str:
    """The line that tells the model what was left out and where the whole result is."""
    kept = f"kept {days:g} days"
    if long.lines is None:
        return (
            f"[evo-hub: this output has {long.size:,} characters and only its start and end are shown here; all of it "
            f"is in {path} ({kept}). Read it there, with offset and limit or grep, rather than running the call again.]"
        )
    lines = long.text.split("\n")
    shown = head_lines(lines, head)
    last = long.first_line + len(shown) - 1
    cut = len(shown[-1]) < len(lines[len(shown) - 1])  # a line longer than the head
    return (
        f"[evo-hub: the read returned {long.lines:,} lines ({long.size:,} characters) and only lines "
        f"{long.first_line:,} to {last:,} are shown here{', the last of them cut' if cut else ''}. If you need the "
        f"rest, Read the file with offset {last if cut else last + 1} and a limit (a Read with an offset or a limit "
        f"is never cut), or see the whole read in {path} ({kept}).]"
    )


def prints_handoff(tool_name, tool_input) -> bool:
    """Whether the call is a Bash command that prints a plan's handoff, which the hub sized to a budget and which a
    step's agent must read whole."""
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return tool_name == "Bash" and isinstance(command, str) and HANDOFF_COMMAND.search(command) is not None


def mentions_spool(tool_input) -> bool:
    """Whether the call names the spool directory, as when the agent reads a spool file."""
    try:
        text = json.dumps(tool_input, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return f".evo/{DIRECTORY}" in text.replace("\\\\", "/")


def post_tool_use(payload: dict, env: Mapping[str, str], now: float) -> dict | None:
    """The hook's answer to one PostToolUse payload, after writing its spool file; None leaves the result as it is."""
    if payload.get("hook_event_name") != "PostToolUse" or "turn_id" in payload:  # Codex sends a turn_id
        return None
    name, given = payload.get("tool_name"), payload.get("tool_input")
    limit, take = threshold(env), handler(name)
    if limit is None or take is None or mentions_spool(given) or prints_handoff(name, given):
        return None
    if take is read:
        limit = read_threshold(env)
        if limit is None:
            return None
        head = tail = min(READ_HEAD, limit // 2)
    else:
        head, tail = min(HEAD, limit // 3), min(TAIL, limit // 3)
    long = take(payload.get("tool_response"), given)
    if long is None or long.size <= limit:
        return None
    session = _name(payload.get("session_id"), "session")
    path = write_spool(spool_root(), session, _name(payload.get("tool_use_id"), str(time.time_ns())), long.text)
    output = long.render(note(long, path, head, keep_days(env)), head, tail)
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": output}}


def _say(stream, text: str) -> None:
    with contextlib.suppress(OSError, ValueError):  # a closed stdout or stderr must not fail the hook either
        print(text, file=stream, flush=True)


def cmd_hook_post_tool_use(args) -> int:
    """Print the replacement of a long result, then clean up old spool files; a failure before the replacement is
    printed leaves the result as it was and says why on stderr, by the exception's type alone."""
    env, now, answer = os.environ, time.time(), None
    try:
        with budget(BUDGET):
            answer = post_tool_use(read_payload(limit=MAX_PAYLOAD), env, now)
            if answer is not None:
                _say(sys.stdout, json.dumps(answer, ensure_ascii=False))
                prune(spool_root(), keep_days(env) * 86400, now)
    except OutOfTime:
        if answer is None:
            _say(sys.stderr, f"evo-hub: tool output left as it was, out of time after {BUDGET:g}s")
    except BaseException as exc:  # a hook never fails the session
        if answer is None:
            _say(sys.stderr, f"evo-hub: tool output left as it was: unexpected {type(exc).__name__}")
    return 0
