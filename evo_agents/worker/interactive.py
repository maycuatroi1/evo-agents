"""Interactive runs: a person drives a run's agent in its runtime's own terminal UI, in tmux on the worker.

- A takeover (the heartbeat says ``takeover``) lets the headless agent finish its turn (``stop_at_turn_boundary``),
  then the daemon starts the runtime's terminal UI on the same session in the tmux session ``evo-run-N`` and reports
  the run ``interactive``; the lease goes on being extended. A run dispatched in interactive mode, or taken over
  before its agent started, starts there, the UI given the run's prompt. A handback (the heartbeat says ``handback``),
  or the person leaving the UI, closes the tmux session; a new adapter goes on headless in the same session and the
  run is reported ``running`` again.
- ``Tui`` is a runtime's terminal UI. Claude Code: ``claude --resume ID --dangerously-skip-permissions
  --remote-control evo-run-N`` (``--session-id`` and the prompt for a new session), after the worktree is marked
  trusted in Claude Code's ``.claude.json`` (``trust_folder``), since its folder trust dialog comes in every new folder,
  the bypass flag notwithstanding. A run whose lease sets CLAUDE_CODE_OAUTH_TOKEN starts it without
  ``--remote-control``, which a token of ``claude setup-token`` cannot open, and says so in the run's log; its pane
  unsets the daemon's ANTHROPIC_API_KEY then, as the headless agent's launcher does (``claude_code.dropped_env``).
  opencode: ``opencode attach URL --session ID --dir WORKTREE`` on an ``opencode serve`` of its own, which also
  creates a new session and hands it the prompt. Codex: ``codex resume ID
  --dangerously-bypass-approvals-and-sandbox`` (``codex`` with the prompt for a new session), with
  ``--dangerously-bypass-hook-trust`` when its help lists the flag, so it does not stop at "Hooks need review".
- While the person drives, the run's log follows the runtime's own record of the session: Claude Code's transcript
  (``<config>/projects/*/<ID>.jsonl``), Codex's rollout (``$CODEX_HOME/sessions/Y/M/D/rollout-*-<ID>.jsonl``) and
  opencode's event stream. A UI without one (the tests' fake) is logged from its terminal (``tmux pipe-pane``), its
  escape sequences removed.
- ``Tmux`` runs tmux: the default server, or the one of ``$TMUX`` when the daemon runs inside tmux, or
  ``EVO_WORKER_TMUX_SOCKET``. A UI starts through a script of its own (0700, in the run's directory, removed once it
  is read), which sets the agent's environment whatever the tmux server's is. The variables the run's leases add to
  that environment are not in the script: it evaluates ``evo-agents worker env --run N`` instead, which asks the run's
  socket for them (``credentials``), so no lease value is ever written to disk.
- ``WebTerminal`` is the worker's end of the web terminal (``evo_agents.hub.terminal`` holds the frames): when the
  heartbeat says ``terminal_open`` for an interactive run, the daemon opens a websocket to the hub with aiohttp and
  joins it to ``tmux attach`` on a PTY of its own. The last RING_BYTES of what that terminal printed are kept for the
  run and sent first when a browser connects again. A worker whose ``allow_web_terminal`` is off connects only to
  say so in the terminal, then closes.

Interactive mode needs tmux on the daemon's PATH; without it a takeover is noted as unsupported in the run's log and
an interactive run fails before its agent starts. Standard library only, but for aiohttp in the web terminal.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import glob
import json
import logging
import os
import pty
import re
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from pathlib import Path
from typing import ClassVar

from evo_agents import __version__
from evo_agents.hub import terminal as frames
from evo_agents.hub.client import write_atomic
from evo_agents.hub.runs import PROTOCOL_HEADER, PROTOCOL_VERSION
from evo_agents.worker.adapter import AgentEvent, RunContext
from evo_agents.worker.runtimes import claude_code, common, opencode

log = logging.getLogger("evo_agents.worker")

SESSION_NAME = "evo-run-{id}"
TMUX_SOCKET_VARIABLE = "EVO_WORKER_TMUX_SOCKET"  # a tmux server of the worker's own (tests use one)
TMUX_TIMEOUT = 15.0  # seconds for one tmux command
WINDOW = (200, 50)  # columns and rows of a new tmux session, until a client attaches
HELP_TIMEOUT = 20.0  # seconds for `codex --help`
POLL = 0.5  # seconds between looks at a transcript
PANE_POLL = 1.0  # and at the terminal's own output
MAX_READ = 4 * 1024 * 1024  # bytes of a transcript read at once
MAX_LINE = 16 * 1024 * 1024  # a transcript line longer than this is skipped
PANE_EVENT_CHARS = 16 * 1024  # characters of terminal text in one output event
HOOK_TRUST_FLAG = "--dangerously-bypass-hook-trust"
# Variables the UI's script leaves as tmux sets them in the pane.
KEPT_BY_TMUX = frozenset({"TERM", "TMUX", "TMUX_PANE", "COLUMNS", "LINES", "SHLVL", "PWD", "OLDPWD", "_"})
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# Records of Claude Code's transcript that repeat what others say or keep its own state.
CLAUDE_SKIPPED = frozenset(
    {"attachment", "last-prompt", "atis-latch", "queue-operation", "file-history-snapshot", "cost-state", "summary"}
)

RING_BYTES = 256 * 1024  # what the web terminal printed last, sent first to a browser that connects again
DEFAULT_SIZE = (80, 24)  # columns and rows of the PTY until the browser says its size
FIRST_FRAME_WAIT = 0.5  # seconds the bridge waits for the browser's size before it attaches
CONNECT_TIMEOUT = 30.0  # seconds for the websocket's handshake
PING_SECONDS = 30.0  # websocket pings, for the proxies between the worker and the hub
HIGH_WATER = 1024 * 1024  # bytes of output waiting for the websocket before the PTY is no longer read
LOW_WATER = 256 * 1024
MAX_PENDING_INPUT = 1024 * 1024  # bytes typed that the PTY has not taken yet; more is dropped
CLOSE_GRACE = 10.0
USER_AGENT = f"evo-agents-worker/{__version__}"
# Run before tmux attach, on the PTY: a session of its own whose controlling terminal is the PTY, so a resize of the
# PTY reaches tmux as SIGWINCH.
_CONTROLLING_TTY = (
    "import fcntl, os, sys, termios\n"
    "os.setsid()\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "os.execv(sys.argv[1], sys.argv[1:])\n"
)


def session_name(run_id: int) -> str:
    """The tmux session of run ``run_id``: ``evo-run-N``, as the web's Take over dialog prints it."""
    return SESSION_NAME.format(id=int(run_id))


def _exact(name: str) -> str:
    """A target-session that matches ``name`` only (``evo-run-1`` alone would also find ``evo-run-12``)."""
    return f"={name}"


def _pane(name: str) -> str:
    """The active pane of session ``name``, matched exactly."""
    return f"={name}:"


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or os.path.expanduser("~"))


def _folders(path: Path) -> list[str]:
    """``path`` as given and as resolved: a runtime may key a folder by either."""
    names = [str(path)]
    resolved = os.path.realpath(path)
    if resolved not in names:
        names.append(resolved)
    return names


def _cut(text: str, limit: int = 300) -> str:
    return common.cut(text, limit)


# Claude Code's folder trust


def claude_config_dir(env: Mapping[str, str]) -> Path:
    """Where Claude Code keeps its settings, sessions and transcripts: CLAUDE_CONFIG_DIR, else ``~/.claude``."""
    value = env.get("CLAUDE_CONFIG_DIR")
    return Path(value).expanduser() if value else _home(env) / ".claude"


def claude_state_path(env: Mapping[str, str]) -> Path:
    """Claude Code's own state file, which holds ``projects[<folder>].hasTrustDialogAccepted``: ``.claude.json`` in
    CLAUDE_CONFIG_DIR when that is set, else ``~/.claude.json``."""
    value = env.get("CLAUDE_CONFIG_DIR")
    return (Path(value).expanduser() if value else _home(env)) / ".claude.json"


class TrustError(Exception):
    """The state file cannot be read as a JSON object, so it is left as it is."""


def trust_folder(path: Path, folders: Iterable[str]) -> bool:
    """Set ``projects[<folder>].hasTrustDialogAccepted`` to true in Claude Code's state file ``path`` for each of
    ``folders``, keeping every other key; whether the file changed. The file is replaced atomically, with its mode,
    and through a symbolic link to where it points. A file that is not a JSON object raises TrustError untouched."""
    target = Path(os.path.realpath(path))
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        raise TrustError(f"cannot read {path}: {exc}") from None
    try:
        state = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise TrustError(f"{path} is not JSON ({exc}); it is left as it is") from None
    if not isinstance(state, dict):
        raise TrustError(f"{path} is not a JSON object; it is left as it is")
    projects = state.get("projects")
    if projects is None:
        projects = state["projects"] = {}
    if not isinstance(projects, dict):
        raise TrustError(f"projects in {path} is not a JSON object; it is left as it is")
    changed = False
    for folder in folders:
        entry = projects.get(folder)
        if entry is None:
            entry = projects[folder] = {}
        if not isinstance(entry, dict):
            raise TrustError(f"projects[{folder!r}] in {path} is not a JSON object; it is left as it is")
        if entry.get("hasTrustDialogAccepted") is not True:
            entry["hasTrustDialogAccepted"] = True
            changed = True
    if changed:
        try:
            mode = target.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o600
        target.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(state, indent=2, ensure_ascii=False).encode() + b"\n"
        write_atomic(target, data, mode)
    return changed


# tmux


def _socket_of(tmux_variable: str | None) -> str | None:
    """The server socket in ``$TMUX`` (``/private/tmp/tmux-501/default,1234,0``)."""
    if not tmux_variable:
        return None
    return tmux_variable.split(",", 1)[0] or None


def default_socket(env: Mapping[str, str]) -> str:
    """The socket tmux uses without -S or -L: ``$TMUX_TMPDIR/tmux-UID/default``, /tmp by default."""
    return os.path.join(env.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", "default")


class TmuxError(RuntimeError):
    pass


class Tmux:
    """tmux on one server: ``socket``, or the default one. Its commands run without ``$TMUX``, so they reach that
    server from inside another tmux as well."""

    def __init__(self, binary: str | None, socket: str | None, env: Mapping[str, str]):
        self.binary = binary
        self.socket = socket
        self.env = {key: value for key, value in env.items() if key not in ("TMUX", "TMUX_PANE")}

    @classmethod
    def from_env(cls, env: Mapping[str, str], socket: str | None = None) -> Tmux:
        binary = shutil.which("tmux", path=env.get("PATH"))
        return cls(binary, socket or env.get(TMUX_SOCKET_VARIABLE) or _socket_of(env.get("TMUX")), env)

    @property
    def available(self) -> bool:
        return self.binary is not None

    def argv(self, *args: str) -> list[str]:
        if self.binary is None:
            raise TmuxError("tmux is not on PATH")
        return [self.binary, *(("-S", self.socket) if self.socket else ()), *args]

    async def run(self, *args: str, timeout: float = TMUX_TIMEOUT) -> tuple[int, str]:
        """(exit status, what it printed) of one tmux command; 124 when it gave no answer in time."""
        proc = await asyncio.create_subprocess_exec(
            *self.argv(*args),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return 124, f"tmux gave no answer within {timeout:g}s"
        return proc.returncode, out.decode(errors="replace").strip()

    def run_sync(self, *args: str, timeout: float = TMUX_TIMEOUT) -> tuple[int, str]:
        try:
            done = subprocess.run(
                self.argv(*args),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                env=self.env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, f"tmux gave no answer within {timeout:g}s"
        except OSError as exc:
            return 127, str(exc)
        return done.returncode, (done.stdout + done.stderr).strip()

    def version(self) -> str | None:
        if not self.available:
            return None
        code, out = self.run_sync("-V", timeout=5)
        return out.split()[-1] if code == 0 and out else None

    async def start(
        self,
        name: str,
        argv: list[str],
        env: Mapping[str, str],
        *,
        cwd: Path,
        scratch: Path,
        drop: Iterable[str] = (),
        size: tuple[int, int] = WINDOW,
        withheld: Iterable[str] = (),
        env_command: str | None = None,
    ) -> None:
        """A detached session ``name`` whose one pane runs ``argv`` in ``cwd`` with ``env``, but for ``withheld``, which
        ``env_command`` prints (``write_script``). A session of that name left by an earlier daemon is killed first."""
        await self.kill(name)
        script = write_script(scratch / f"{name}.sh", argv, env, cwd, drop, withheld=withheld, env_command=env_command)
        try:
            code, out = await self.run(
                "new-session",
                "-d",
                "-s",
                name,
                "-x",
                str(size[0]),
                "-y",
                str(size[1]),
                "-c",
                str(cwd),
                shlex.quote(str(script)),
            )
        except BaseException:
            script.unlink(missing_ok=True)
            raise
        if code != 0:
            script.unlink(missing_ok=True)
            raise TmuxError(f"tmux new-session failed: {_cut(out)}")

    async def alive(self, name: str) -> bool:
        """Whether session ``name`` is there with a pane still running (a pane kept by remain-on-exit is not). A tmux
        that does not answer in time counts as alive: only a missing session ends the person's turn."""
        code, out = await self.run("list-panes", "-s", "-t", _pane(name), "-F", "#{pane_dead}")
        if code == 124:
            return True
        return code == 0 and any(line.strip() in ("", "0") for line in out.splitlines())

    def has_session(self, name: str) -> bool:
        code, _ = self.run_sync("has-session", "-t", _exact(name))
        return code == 0

    async def kill(self, name: str) -> None:
        if self.available:
            await self.run("kill-session", "-t", _exact(name))

    def kill_sync(self, name: str) -> None:
        if self.available:
            self.run_sync("kill-session", "-t", _exact(name))

    async def pipe(self, name: str, path: Path) -> None:
        """Append what the pane of ``name`` prints to ``path`` (tmux pipe-pane)."""
        path.touch(mode=0o600, exist_ok=True)
        command = f"cat >> {shlex.quote(str(path))}"
        code, out = await self.run("pipe-pane", "-o", "-t", _pane(name), command)
        if code != 0:
            raise TmuxError(f"tmux pipe-pane failed: {_cut(out)}")

    async def socket_path(self, name: str) -> str | None:
        """The path of the server's socket, as tmux names it, for ``evo-agents worker attach``."""
        code, out = await self.run("display-message", "-p", "-t", _pane(name), "#{socket_path}")
        return out if code == 0 and out.startswith("/") else None

    def attach_command(self, name: str, env: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
        """The command that puts the terminal of ``env`` on session ``name``: ``switch-client`` from inside a client of
        the same server, ``attach-session`` otherwise."""
        inside = _socket_of(env.get("TMUX"))
        ours = self.socket or default_socket(env)
        if inside is not None and os.path.realpath(inside) == os.path.realpath(ours):
            return self.argv("switch-client", "-t", _exact(name)), dict(env)
        clean = {key: value for key, value in env.items() if key not in ("TMUX", "TMUX_PANE")}
        return self.argv("attach-session", "-t", _exact(name)), clean


def _locale(env: Mapping[str, str]) -> dict[str, str]:
    """A UTF-8 locale for the UI when the agent's environment names none (a service starts with almost nothing)."""
    if any(env.get(name) for name in ("LC_ALL", "LC_CTYPE", "LANG")):
        return {}
    return {"LANG": "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"}


def write_script(
    path: Path,
    argv: list[str],
    env: Mapping[str, str],
    cwd: Path,
    drop: Iterable[str] = (),
    *,
    withheld: Iterable[str] = (),
    env_command: str | None = None,
) -> Path:
    """The script tmux runs in the pane: it removes itself, enters ``cwd``, exports ``env`` over what the tmux server
    gives a pane (but for what tmux sets per pane), unsets ``drop`` and executes ``argv``. Mode 0700, as the run's
    directory it lies in.

    The variables in ``withheld``, which the run's leases set, are left out of the script: it evaluates the export
    lines ``env_command`` prints instead (``evo-agents worker env --run N``), after the other variables and before the
    unsets, so the values never touch the disk."""
    dropped, hidden = set(drop), set(withheld)
    lines = ["#!/bin/sh", 'rm -f -- "$0"', f"cd -- {shlex.quote(str(cwd))} || exit 1"]
    for key, value in {**env, **_locale(env)}.items():
        if key in KEPT_BY_TMUX or key in dropped or key in hidden or not _NAME.match(key) or "\0" in value:
            continue
        lines.append(f"export {key}={shlex.quote(value)}")
    if env_command:
        lines.append(f'eval "$({env_command})"')
    for key in sorted(dropped):
        if _NAME.match(key):
            lines.append(f"unset {key}")
    lines.append("exec " + " ".join(shlex.quote(part) for part in argv))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(path, 0o700)
    return path


# The session's log while a person drives it


class JsonlFollower:
    """The records a runtime appends to a JSONL file, from ``offset`` on; a line not yet ended waits for its end."""

    def __init__(self, path: Path | None = None, offset: int = 0):
        self.path = path
        self.offset = offset
        self._partial = b""

    def read(self) -> list[dict]:
        if self.path is None:
            return []
        records: list[dict] = []
        while True:
            try:
                with open(self.path, "rb") as handle:
                    size = os.fstat(handle.fileno()).st_size
                    if size < self.offset:  # replaced or cut: start again
                        self.offset, self._partial = 0, b""
                    handle.seek(self.offset)
                    data = handle.read(MAX_READ)
            except OSError:
                return records
            if not data:
                return records
            self.offset += len(data)
            lines = (self._partial + data).split(b"\n")
            self._partial = lines.pop()
            if len(self._partial) > MAX_LINE:
                self._partial = b""
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
            if len(data) < MAX_READ:
                return records


async def follow_jsonl(
    locate: Callable[[], tuple[Path, int] | None],
    events_of: Callable[[dict], list[AgentEvent]],
    stop: asyncio.Event,
    poll: float = POLL,
) -> AsyncIterator[AgentEvent]:
    """The events of the records appended to the file ``locate`` finds (with the offset to start at), until ``stop``
    is set; a last read follows it."""
    follower = JsonlFollower()
    while True:
        stopping = stop.is_set()
        if follower.path is None:
            found = await asyncio.to_thread(locate)
            if found is not None:
                follower.path, follower.offset = found
        for record in await asyncio.to_thread(follower.read):
            try:
                events = events_of(record)
            except Exception:  # a record of a shape not seen before must not end the log
                log.debug("a transcript record was not mapped", exc_info=True)
                continue
            for event in events:
                yield event
        if stopping:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), poll)


_ESCAPES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI: cursor moves, colours, modes
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: titles, hyperlinks
    r"|\x1b[PX^_][^\x1b]*\x1b\\"  # DCS, SOS, PM, APC
    r"|\x1b[()*+][0-9A-Za-z]"  # character sets
    r"|\x1b[@-Z\\-_=>78]"  # two-byte sequences
)
_BLANK_LINES = re.compile(r"\n{3,}")


def terminal_text(text: str) -> str:
    """What a terminal printed, as text: escape sequences and control characters removed, lines ended by newlines."""
    text = _ESCAPES.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or ch.isprintable())
    lines = text.split("\n")
    lines[:-1] = [line.rstrip() for line in lines[:-1]]  # the last line may go on in the next read
    return _BLANK_LINES.sub("\n\n", "\n".join(lines))


def _complete(text: str) -> tuple[str, str]:
    """``text`` cut before an escape sequence that has not ended yet: (what can be cleaned now, what waits)."""
    start = text.rfind("\x1b")
    if start < 0 or len(text) - start > 256:
        return text, ""
    tail = text[start:]
    if _ESCAPES.match(tail) or (len(tail) >= 2 and tail[1] not in "[]PX^_()*+"):
        return text, ""
    return text[:start], tail


async def follow_pane(path: Path, stop: asyncio.Event, poll: float = PANE_POLL) -> AsyncIterator[AgentEvent]:
    """``output`` events ``{"terminal": text}`` of what tmux pipe-pane appends to ``path``, until ``stop`` is set."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    offset = 0
    waiting = ""
    while True:
        stopping = stop.is_set()
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(MAX_READ)
        except OSError:
            data = b""
        offset += len(data)
        text, waiting = _complete(waiting + decoder.decode(data, final=stopping))
        if stopping:
            text, waiting = text + waiting, ""
        cleaned = terminal_text(text)
        for start in range(0, len(cleaned), PANE_EVENT_CHARS):
            piece = cleaned[start : start + PANE_EVENT_CHARS]
            if piece.strip():
                yield AgentEvent("output", {"terminal": piece})
        if stopping and len(data) < MAX_READ:
            return
        if len(data) < MAX_READ:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), poll)


def claude_transcript_events(record: dict) -> list[AgentEvent]:
    """The hub's events for one record of Claude Code's transcript (``<session>.jsonl``): the assistant's text,
    thinking and tool calls, the results of the tools, and what the person typed (``output``)."""
    kind = record.get("type")
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    if kind == "assistant":
        events: list[AgentEvent] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and block.get("text"):
                events.append(common.message_chunk(str(block["text"])))
            elif block_type == "thinking" and str(block.get("thinking") or "").strip():
                events.append(common.thought_chunk(str(block["thinking"])))
            elif block_type in ("tool_use", "server_tool_use"):
                name = str(block.get("name") or "tool")
                tool_input = block.get("input")
                kind_of = claude_code.TOOL_KINDS.get(name, "other")
                events.append(common.tool_call(str(block.get("id") or ""), name, kind_of, "pending", tool_input))
                todos = tool_input.get("todos") if name == "TodoWrite" and isinstance(tool_input, dict) else None
                if isinstance(todos, list):
                    entries = [
                        (str(t.get("content") or ""), t.get("status"), "medium") for t in todos if isinstance(t, dict)
                    ]
                    events.append(common.plan(entries))
        return events
    if kind == "user":
        if record.get("isMeta"):
            return []
        if isinstance(content, str):
            return [common.raw({"type": "user", "text": content})] if content.strip() else []
        events = []
        typed = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                status = "failed" if block.get("is_error") else "completed"
                events.append(
                    common.tool_update(
                        str(block.get("tool_use_id") or ""), status, claude_code._text_of(block.get("content"))
                    )
                )
            elif block.get("type") == "text" and str(block.get("text") or "").strip():
                typed.append(str(block["text"]))
        if typed:
            events.append(common.raw({"type": "user", "text": "\n".join(typed)}))
        return events
    if kind in CLAUDE_SKIPPED:
        return []
    return [common.raw(record)]


CODEX_SHELL_TOOLS = frozenset({"shell", "exec_command", "local_shell", "container.exec", "unified_exec"})


def _codex_text(output) -> str | None:
    if isinstance(output, str):
        return output
    if isinstance(output, dict) and isinstance(output.get("output"), str):
        return output["output"]
    if isinstance(output, list):
        parts = [item.get("text") for item in output if isinstance(item, dict) and isinstance(item.get("text"), str)]
        return "\n".join(parts) if parts else None
    return None


def codex_rollout_events(record: dict) -> list[AgentEvent]:
    """The hub's events for one record of a Codex rollout (``rollout-*-<thread>.jsonl``): the assistant's messages
    and reasoning, the tool calls and their output, token counts, what the person typed (``output``) and an aborted
    turn (``output``). Records that repeat these (``event_msg`` items, agent messages) or hold the thread's settings
    are left out."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return []
    kind, item = record.get("type"), payload.get("type")
    if kind == "response_item":
        if item == "message":
            if payload.get("role") != "assistant":
                return []  # the person's own words come as event_msg user_message; the rest is context
            texts = [
                str(part.get("text"))
                for part in payload.get("content") or []
                if isinstance(part, dict) and part.get("type") == "output_text" and part.get("text")
            ]
            return [common.message_chunk("\n".join(texts))] if texts else []
        if item == "reasoning":
            texts = [
                str(part.get("text"))
                for part in payload.get("summary") or []
                if isinstance(part, dict) and part.get("text")
            ]
            text = "\n".join(texts)
            return [common.thought_chunk(text)] if text.strip() else []
        if item in ("function_call", "custom_tool_call"):
            name = str(payload.get("name") or "tool")
            arguments = payload.get("arguments") if item == "function_call" else {"input": payload.get("input")}
            if isinstance(arguments, str):
                with contextlib.suppress(ValueError):
                    arguments = json.loads(arguments)
            tool_kind = "execute" if name in CODEX_SHELL_TOOLS else "edit" if name == "apply_patch" else "other"
            return [common.tool_call(str(payload.get("call_id") or ""), name, tool_kind, "in_progress", arguments)]
        if item in ("function_call_output", "custom_tool_call_output"):
            call_id = str(payload.get("call_id") or "")
            return [common.tool_update(call_id, "completed", _codex_text(payload.get("output")))]
        if item in ("local_shell_call", "web_search_call"):
            return [common.raw(record)]
        return []
    if kind == "event_msg":
        if item == "user_message":
            text = payload.get("message")
            return [common.raw({"type": "user_message", "message": text})] if isinstance(text, str) else []
        if item == "token_count" and isinstance(payload.get("info"), dict):
            info = payload["info"]
            usage = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
            return [
                common.usage_update(
                    usage, last=info.get("last_token_usage"), contextWindow=info.get("model_context_window")
                )
            ]
        if item == "turn_aborted":
            return [common.raw(record)]
    return []


# The runtimes' terminal UIs


class Tui:
    """A runtime's terminal UI on a run's session, for a person to drive in tmux.

    ``prepare`` readies what the UI needs and returns what it did, as lines for the run's log; ``command`` is the
    UI's argv for tmux session ``name`` and ``environment`` its variables (``drop_env``, the class's or the UI's own,
    unset); ``logs`` follows the session while the person drives it, until ``logs_done`` is set, or is None when the
    runtime keeps no record this daemon reads (the terminal is logged then); ``close`` releases what ``prepare`` took.
    ``session_id`` is the session's id once it is known; it may become known only once the UI has started a new
    session."""

    runtime: ClassVar[str] = ""
    program: ClassVar[str] = ""
    drop_env: tuple[str, ...] = ()

    def __init__(self, context: RunContext, session_id: str | None):
        self.context = context
        self.session_id = session_id
        self.resumed = session_id is not None
        self.logs_done = asyncio.Event()

    @property
    def binary(self) -> str:
        """The runtime's program on the agent's PATH."""
        return common.which(self.program, self.context.env)

    def setting(self, key: str) -> str | None:
        return common.run_setting(self.context, self.runtime, key)

    @property
    def prompt(self) -> str:
        """The run's prompt as an argument: never one an option parser could take for an option."""
        text = self.context.prompt
        return " " + text if text.startswith("-") else text

    async def prepare(self) -> list[str]:
        return []

    def command(self, name: str) -> list[str]:
        raise NotImplementedError

    def environment(self) -> dict[str, str]:
        return {key: value for key, value in self.context.env.items() if key not in self.drop_env}

    def logs(self) -> AsyncIterator[AgentEvent] | None:
        return None

    async def close(self) -> None:
        self.logs_done.set()


class ClaudeCodeTui(Tui):
    """``claude --resume ID --dangerously-skip-permissions --remote-control evo-run-N``, so the session also shows in
    the Claude apps; a new session gets ``--session-id`` and the run's prompt. The run's model and effort go with
    it when the run names them. A run whose lease sets CLAUDE_CODE_OAUTH_TOKEN gets no ``--remote-control``, and
    no ANTHROPIC_API_KEY of the daemon's (``claude_code.dropped_env``)."""

    runtime = "claude-code"
    program = "claude"
    drop_env = claude_code.DROPPED_ENV

    def __init__(self, context: RunContext, session_id: str | None):
        super().__init__(context, session_id)
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
        self.state_path = claude_state_path(context.env)
        self.drop_env = claude_code.dropped_env(context)
        self.remote_control = not claude_code.leased_oauth(context)
        self._transcript: tuple[Path, int] | None = None

    def transcript(self) -> Path | None:
        """``<config>/projects/<the folder, mangled>/<ID>.jsonl``, found by the session's id."""
        pattern = str(claude_config_dir(self.context.env) / "projects" / "*" / f"{glob.escape(self.session_id)}.jsonl")
        found = sorted(glob.glob(pattern))
        return Path(found[0]) if found else None

    async def prepare(self) -> list[str]:
        folders = _folders(self.context.worktree)
        try:
            changed = await asyncio.to_thread(trust_folder, self.state_path, folders)
        except TrustError as exc:
            notes = [f"Claude Code's folder trust was not recorded: {exc}. Its trust dialog waits in the terminal."]
        else:
            done = "Marked" if changed else "Found"
            notes = [
                f"{done} {self.context.worktree} as trusted in {self.state_path}, so Claude Code opens the session "
                "without its folder trust dialog."
            ]
        if not self.remote_control:
            notes.append(claude_code.REMOTE_CONTROL_NOTE)
        if self.resumed:
            path = await asyncio.to_thread(self.transcript)
            if path is not None:
                with contextlib.suppress(OSError):
                    self._transcript = (path, path.stat().st_size)
        return notes

    def command(self, name: str) -> list[str]:
        argv = [self.binary]
        argv += ["--resume", self.session_id] if self.resumed else ["--session-id", self.session_id]
        argv.append("--dangerously-skip-permissions")
        if self.remote_control:
            argv += ["--remote-control", name]
        for key, flag in (("model", "--model"), ("effort", "--effort")):
            value = self.setting(key)
            if value:
                argv += [flag, value]
        if not self.resumed:
            argv.append(self.prompt)
        return argv

    def _locate(self) -> tuple[Path, int] | None:
        if self._transcript is not None:  # the session's, from where the headless agent left it
            return self._transcript
        path = self.transcript()  # a new session's, once the UI has written it
        return (path, 0) if path is not None else None

    def logs(self) -> AsyncIterator[AgentEvent]:
        return follow_jsonl(self._locate, claude_transcript_events, self.logs_done)


def codex_home(env: Mapping[str, str]) -> Path:
    value = env.get("CODEX_HOME")
    return Path(value).expanduser() if value else _home(env) / ".codex"


class CodexTui(Tui):
    """``codex resume ID --dangerously-bypass-approvals-and-sandbox -C WORKTREE``, with
    ``--dangerously-bypass-hook-trust`` when this codex lists it, so the UI does not stop at "Hooks need review";
    a new session is ``codex`` with the same flags and the run's prompt, its id read from the rollout it starts."""

    runtime = "codex"
    program = "codex"

    def __init__(self, context: RunContext, session_id: str | None):
        super().__init__(context, session_id)
        self.hook_flag = False
        self.started_at = time.time()
        self._rollout: tuple[Path, int] | None = None

    def rollout(self) -> Path | None:
        if not self.session_id:
            return None
        sessions = codex_home(self.context.env) / "sessions"
        pattern = str(sessions / "*" / "*" / "*" / f"rollout-*-{glob.escape(self.session_id)}.jsonl")
        found = sorted(glob.glob(pattern))
        return Path(found[-1]) if found else None

    async def _help(self) -> str:
        args = ("resume", "--help") if self.resumed else ("--help",)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=dict(self.context.env),
            )
        except OSError:
            return ""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), HELP_TIMEOUT)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return ""
        return out.decode(errors="replace")

    async def prepare(self) -> list[str]:
        command = "codex resume" if self.resumed else "codex"
        self.hook_flag = HOOK_TRUST_FLAG in await self._help()
        if self.hook_flag:
            notes = [f"{command} gets {HOOK_TRUST_FLAG}, so it does not stop to ask for a review of its hooks."]
        else:
            notes = [f"{command} lists no {HOOK_TRUST_FLAG} here: it may ask in the terminal to review its hooks."]
        if self.resumed:
            path = await asyncio.to_thread(self.rollout)
            if path is not None:
                with contextlib.suppress(OSError):
                    self._rollout = (path, path.stat().st_size)
        self.started_at = time.time()
        return notes

    def command(self, name: str) -> list[str]:
        argv = [self.binary, "resume", self.session_id] if self.resumed else [self.binary]
        argv.append("--dangerously-bypass-approvals-and-sandbox")
        if self.hook_flag:
            argv.append(HOOK_TRUST_FLAG)
        model, effort = self.setting("model"), self.setting("effort")
        if model:
            argv += ["-m", model]
        if effort:
            argv += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
        argv += ["-C", str(self.context.worktree)]
        if not self.resumed:
            argv.append(self.prompt)
        return argv

    def _new_rollout(self) -> tuple[Path, int] | None:
        """The rollout the UI started for a new session: in the folders of the days since the start, written since,
        whose ``session_meta`` names the worktree. Its id becomes the session's."""
        sessions = codex_home(self.context.env) / "sessions"
        since = self.started_at - 5
        days = {time.strftime("%Y/%m/%d", time.localtime(moment)) for moment in (since, time.time())}
        folders = set(_folders(self.context.worktree))
        for day in sorted(days):
            for name in sorted(glob.glob(str(sessions / day / "rollout-*.jsonl"))):
                path = Path(name)
                try:
                    if path.stat().st_mtime < since:
                        continue
                    with open(path, "rb") as handle:
                        first = json.loads(handle.readline() or b"null")
                except (OSError, ValueError):
                    continue
                payload = first.get("payload") if isinstance(first, dict) else None
                if not isinstance(payload, dict) or first.get("type") != "session_meta":
                    continue
                if payload.get("cwd") in folders and isinstance(payload.get("id"), str):
                    self.session_id = payload["id"]
                    return path, 0
        return None

    def _locate(self) -> tuple[Path, int] | None:
        if self._rollout is not None:
            return self._rollout
        if self.resumed:
            path = self.rollout()
            return (path, 0) if path is not None else None
        return self._new_rollout()

    def logs(self) -> AsyncIterator[AgentEvent]:
        return follow_jsonl(self._locate, codex_rollout_events, self.logs_done)


class OpencodeWatcher(opencode.OpencodeAdapter):
    """The opencode adapter's reading of a session's event stream, on a server the terminal UI shares, while a
    person drives the session: it never ends the session, hands it no prompt but the run's first one for a new
    session, and leaves the server running when it stops."""

    def __init__(self, context: RunContext, server: opencode.Server, first_prompt: str | None):
        super().__init__(context)
        self._given = server
        self._first_prompt = first_prompt

    def group_pid(self) -> int | None:
        return None  # the server is the UI's: it outlives this watcher

    async def start_server(self) -> opencode.Server:
        return self._given

    async def _open(self) -> None:
        import aiohttp

        self._server = self._given
        self.server_url = self._server.url
        self._http = aiohttp.ClientSession(auth=aiohttp.BasicAuth(self._server.username, self._server.password))
        if self._session is None:
            self.model = await self._resolve_model()
            title = common.cut(
                f"evo-agents run {self.context.run.get('id')}: {self.context.run.get('title') or ''}", 100
            )
            created = await self._request("POST", "/session", {"title": title})
            self._session = str(created["id"])
        else:
            await self._request("GET", f"/session/{self._session}")
        self._tree.add(self._session)
        self._stream = await self._http.get(
            self._server.url + "/event",
            params=self._params,
            timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
        )
        if self._stream.status != 200:
            raise RuntimeError(f"opencode answered GET /event with {self._stream.status}")
        if self._first_prompt is not None:
            provider, model = self.model
            body = {
                "parts": [{"type": "text", "text": self._first_prompt}],
                "model": {"providerID": provider, "modelID": model},
            }
            if self.variant:
                body["variant"] = self.variant
            await self._request("POST", f"/session/{self._session}/prompt_async", body)

    async def _went_idle(self) -> bool:
        return False  # a person drives the session: going idle does not end it

    async def _close(self) -> None:
        if self._stream is not None:
            self._stream.close()
        if self._http is not None:
            await self._http.close()

    async def halt(self) -> None:
        """Stop reading the session, and end ``events``."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


class OpencodeTui(Tui):
    """``opencode attach URL --session ID --dir WORKTREE`` on an ``opencode serve`` of its own (127.0.0.1, a free port,
    a random password the UI reads from OPENCODE_SERVER_PASSWORD). A new session is created on that server and
    handed the run's prompt before the UI attaches. The log follows the server's event stream, as the headless adapter
    reads it, and permissions are answered as there (once), so the UI runs with the same rights."""

    runtime = "opencode"
    program = "opencode"

    def __init__(self, context: RunContext, session_id: str | None):
        super().__init__(context, session_id)
        self.server: opencode.Server | None = None
        self.watcher: OpencodeWatcher | None = None

    async def start_server(self) -> opencode.Server:
        """``opencode serve`` for the UI; a test hands it a server of its own."""
        return await opencode.start_server(self.binary, str(self.context.worktree), self.context.env)

    async def prepare(self) -> list[str]:
        self.server = await self.start_server()
        self.watcher = OpencodeWatcher(self.context, self.server, None if self.resumed else self.context.prompt)
        await self.watcher.start()
        self.session_id = self.watcher.session_id
        return [f"opencode serve listens on {self.server.url} for the terminal UI."]

    def command(self, name: str) -> list[str]:
        return [
            self.binary,
            "attach",
            self.server.url,
            "--session",
            self.session_id,
            "--dir",
            str(self.context.worktree),
        ]

    def environment(self) -> dict[str, str]:
        env = super().environment()
        env["OPENCODE_SERVER_PASSWORD"] = self.server.password
        env["OPENCODE_SERVER_USERNAME"] = self.server.username
        return env

    def logs(self) -> AsyncIterator[AgentEvent]:
        return self.watcher.events()  # until close() halts the watcher

    async def close(self) -> None:
        await super().close()
        if self.watcher is not None:
            await self.watcher.halt()
        if self.server is not None:
            await self.server.stop()


# The web terminal


def ws_url(hub_url: str, run_id: int) -> str:
    """The worker's end of run ``run_id``'s terminal on the hub at ``hub_url`` (https -> wss, http -> ws)."""
    scheme, _, rest = hub_url.partition("://")
    socket_scheme = {"https": "wss", "http": "ws"}.get(scheme.lower())
    if socket_scheme is None:
        raise ValueError(f"{hub_url} is neither https nor http")
    return f"{socket_scheme}://{rest.rstrip('/')}/v1/worker/runs/{int(run_id)}/terminal"


class Ring:
    """The last ``limit`` bytes of a terminal's output."""

    def __init__(self, limit: int = RING_BYTES):
        self.limit = limit
        self._data = bytearray()
        self.dropped = False  # whether older output fell out

    def add(self, data: bytes) -> None:
        self._data += data
        excess = len(self._data) - self.limit
        if excess > 0:
            del self._data[:excess]
            self.dropped = True

    def __len__(self) -> int:
        return len(self._data)

    def snapshot(self) -> bytes:
        """What is kept, from the start of a line when older output fell out, so the replay starts clean."""
        data = bytes(self._data)
        if self.dropped:
            newline = data.find(b"\n", 0, 4096)
            if newline >= 0:
                data = data[newline + 1 :]
        return data


def _pieces(data: bytes, size: int = frames.MAX_FRAME_BYTES - 1) -> Iterable[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Attached:
    """``tmux attach`` of one session on a PTY of its own: what it prints comes out of ``output`` (None once it has
    ended) and goes to ``ring``; ``write`` types into it and ``resize`` sizes it."""

    def __init__(self, master: int, proc, ring: Ring):
        self.master = master
        self.proc = proc
        self.ring = ring
        self.output: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.queued = 0
        self._reading = False
        self._writing = False
        self._pending = bytearray()
        self._ended = False
        self._loop = asyncio.get_running_loop()

    @classmethod
    async def start(cls, tmux: Tmux, name: str, size: tuple[int, int], ring: Ring) -> Attached:
        master, slave = pty.openpty()
        try:
            _set_size(master, *size)
            env = {**tmux.env, "TERM": "xterm-256color"}
            proc = await asyncio.create_subprocess_exec(
                sys.executable or "python3",
                "-I",
                "-c",
                _CONTROLLING_TTY,
                *tmux.argv("-u", "attach-session", "-t", _exact(name)),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        attached = cls(master, proc, ring)
        attached._read_on()
        return attached

    def _read_on(self) -> None:
        if not self._reading and not self._ended:
            self._loop.add_reader(self.master, self._readable)
            self._reading = True

    def _read_off(self) -> None:
        if self._reading:
            self._loop.remove_reader(self.master)
            self._reading = False

    def _readable(self) -> None:
        try:
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: the other side of the PTY closed, tmux attach has ended
            data = b""
        if not data:
            self._read_off()
            self._ended = True
            self.output.put_nowait(None)
            return
        self.ring.add(data)
        self.queued += len(data)
        self.output.put_nowait(data)
        if self.queued > HIGH_WATER:
            self._read_off()

    def sent(self, count: int) -> None:
        """``count`` bytes of output went to the websocket."""
        self.queued -= count
        if self.queued < LOW_WATER:
            self._read_on()

    def write(self, data: bytes) -> None:
        if self._ended:
            return
        room = MAX_PENDING_INPUT - len(self._pending)
        if len(data) > room:
            log.warning(
                "terminal input dropped: the terminal takes it slower than it comes", extra={"bytes": len(data) - room}
            )
            data = data[: max(room, 0)]
        self._pending += data
        self._flush()

    def _flush(self) -> None:
        while self._pending:
            try:
                written = os.write(self.master, self._pending)
            except BlockingIOError:
                written = 0
            except OSError:
                self._pending.clear()
                break
            if written == 0:
                if not self._writing:
                    self._loop.add_writer(self.master, self._flush)
                    self._writing = True
                return
            del self._pending[:written]
        if self._writing:
            self._loop.remove_writer(self.master)
            self._writing = False

    def resize(self, cols: int, rows: int) -> None:
        with contextlib.suppress(OSError):
            _set_size(self.master, cols, rows)

    async def close(self) -> None:
        self._read_off()
        if self._writing:
            self._loop.remove_writer(self.master)
            self._writing = False
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()  # tmux attach detaches; the session goes on
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                await self.proc.wait()
        with contextlib.suppress(OSError):
            os.close(self.master)


class WebTerminal:
    """The worker's end of one run's web terminal: connections to the hub's relay, one at a time, each joined to
    ``tmux attach`` of the run's session on a PTY; the output ring outlives them, for the next browser."""

    def __init__(self, run_id: int, tmux: Tmux, *, allowed: bool, worker: str, config_path: Path | None = None):
        self.run_id = int(run_id)
        self.name = session_name(run_id)
        self.tmux = tmux
        self.allowed = allowed
        self.worker = worker
        self.config_path = config_path
        self.ring = Ring()
        self.connections = 0
        self._task: asyncio.Task | None = None
        self._closing = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._task is not None and not self._task.done()

    def open(self, http, hub_url: str, token: str) -> bool:
        """Connect to the hub's relay, unless a connection is open already; whether one starts."""
        if self.connected:
            return False
        self._closing = asyncio.Event()
        self._task = asyncio.create_task(self._serve(http, hub_url, token))
        return True

    async def close(self) -> None:
        """End the connection open now, if any (the run left the terminal)."""
        task = self._task
        if task is None or task.done():
            return
        self._closing.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), CLOSE_GRACE)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        except Exception:
            pass

    async def _serve(self, http, hub_url: str, token: str) -> None:
        import aiohttp

        headers = {PROTOCOL_HEADER: PROTOCOL_VERSION, "Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
        try:
            ws = await asyncio.wait_for(
                http.ws_connect(
                    ws_url(hub_url, self.run_id),
                    headers=headers,
                    max_msg_size=frames.MAX_FRAME_BYTES,
                    heartbeat=PING_SECONDS,
                ),
                CONNECT_TIMEOUT,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            log.warning("terminal not connected to the hub", extra={"run_id": self.run_id, "error": type(exc).__name__})
            return
        self.connections += 1
        log.info("terminal connected", extra={"run_id": self.run_id, "allowed": self.allowed})
        try:
            if self.allowed:
                await self._bridge(ws)
            else:
                await self._refuse(ws)
        except Exception:
            log.exception("the web terminal failed", extra={"run_id": self.run_id})
        finally:
            if not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.close(code=frames.CLOSE_NORMAL, message=b"the terminal session ended")
            code = ws.close_code
            log.info("terminal closed", extra={"run_id": self.run_id, "code": code})

    async def _refuse(self, ws) -> None:
        where = f" in {self.config_path}" if self.config_path else ""
        text = (
            f"\r\nWorker {self.worker} does not allow the web terminal: allow_web_terminal is off{where}.\r\n"
            f"On that machine, `evo-agents worker attach {self.run_id}` opens this session.\r\n"
        )
        # The hub sends the browser's size first, when it knows it, and relays only once that is sent: a socket
        # closed before it would take the message along.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ws.receive(), FIRST_FRAME_WAIT)
        if ws.closed:
            return
        await ws.send_bytes(frames.frame(frames.OUTPUT, text.encode()))
        await ws.close(
            code=frames.CLOSE_FORBIDDEN,
            message=frames.close_reason("this worker does not allow the web terminal").encode(),
        )

    async def _bridge(self, ws) -> None:
        import aiohttp

        size = DEFAULT_SIZE
        early: list[bytes] = []
        try:  # the hub sends the browser's size first when it knows it
            first = await asyncio.wait_for(ws.receive(), FIRST_FRAME_WAIT)
        except asyncio.TimeoutError:
            first = None
        if first is not None:
            if first.type != aiohttp.WSMsgType.BINARY:
                self._log_end(ws, first)
                return
            try:
                kind, payload = frames.parse(first.data)
                if kind == frames.RESIZE:
                    size = frames.parse_resize(payload)
                elif kind == frames.INPUT:
                    early.append(payload)
                else:
                    raise frames.FrameError("the hub sends a worker input (0) and resize (2) frames")
            except frames.FrameError as exc:
                await ws.close(code=frames.CLOSE_UNSUPPORTED, message=frames.close_reason(str(exc)).encode())
                return
        replay = self.ring.snapshot()
        for piece in _pieces(replay):
            await ws.send_bytes(frames.frame(frames.OUTPUT, piece))
        attached = await Attached.start(self.tmux, self.name, size, self.ring)
        for data in early:
            attached.write(data)
        sender = asyncio.create_task(self._send(ws, attached))
        receiver = asyncio.create_task(self._receive(ws, attached))
        closing = asyncio.create_task(self._closing.wait())
        try:
            await asyncio.wait({sender, receiver, closing}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sender, receiver, closing):
                task.cancel()
            await asyncio.gather(sender, receiver, closing, return_exceptions=True)
            await attached.close()

    async def _send(self, ws, attached: Attached) -> None:
        while (data := await attached.output.get()) is not None:
            for piece in _pieces(data):
                await ws.send_bytes(frames.frame(frames.OUTPUT, piece))
            attached.sent(len(data))

    async def _receive(self, ws, attached: Attached) -> None:
        import aiohttp

        while True:
            message = await ws.receive()
            if message.type != aiohttp.WSMsgType.BINARY:
                if message.type == aiohttp.WSMsgType.TEXT:
                    reason = frames.close_reason("frames are binary")
                    await ws.close(code=frames.CLOSE_UNSUPPORTED, message=reason.encode())
                self._log_end(ws, message)
                return
            try:
                kind, payload = frames.parse(message.data)
                if kind == frames.INPUT:
                    attached.write(payload)
                elif kind == frames.RESIZE:
                    attached.resize(*frames.parse_resize(payload))
                else:
                    raise frames.FrameError("the hub sends a worker input (0) and resize (2) frames")
            except frames.FrameError as exc:
                await ws.close(code=frames.CLOSE_UNSUPPORTED, message=frames.close_reason(str(exc)).encode())
                return

    def _log_end(self, ws, message) -> None:
        code = ws.close_code if ws.close_code is not None else message.data if isinstance(message.data, int) else None
        if code not in (None, frames.CLOSE_NORMAL):
            reason = message.extra if isinstance(message.extra, str) else None
            log.info("the hub closed the terminal", extra={"run_id": self.run_id, "code": code, "reason": reason})
