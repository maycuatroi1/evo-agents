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
import contextlib
import glob
import json
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

from evo_agents.worker.adapter import AgentEvent, RunContext
from evo_agents.worker.claude_trust import TrustError, trust_folder
from evo_agents.worker.interactive_pty import Attached, Ring
from evo_agents.worker.interactive_tmux import (
    TMUX_SOCKET_VARIABLE,
    Tmux,
    TmuxError,
    default_socket,
    session_name,
    write_script,
)
from evo_agents.worker.interactive_transcript import (
    JsonlFollower,
    claude_config_dir,
    claude_state_path,
    claude_transcript,
    claude_transcript_events,
    codex_home,
    codex_rollout,
    codex_rollout_events,
    follow_jsonl,
    follow_pane,
    terminal_text,
    worktree_folders,
)
from evo_agents.worker.interactive_web import WebTerminal, ws_url
from evo_agents.worker.runtimes import claude_code, codex, common, gateway, hub_tools, opencode, profiles

__all__ = [
    "TMUX_SOCKET_VARIABLE",
    "Attached",
    "ClaudeCodeTui",
    "CodexTui",
    "JsonlFollower",
    "OpencodeTui",
    "OpencodeWatcher",
    "Ring",
    "Tmux",
    "TmuxError",
    "TrustError",
    "Tui",
    "WebTerminal",
    "claude_config_dir",
    "claude_state_path",
    "claude_transcript",
    "claude_transcript_events",
    "codex_home",
    "codex_rollout",
    "codex_rollout_events",
    "default_socket",
    "follow_jsonl",
    "follow_pane",
    "session_name",
    "terminal_text",
    "trust_folder",
    "worktree_folders",
    "write_script",
    "ws_url",
]

HELP_TIMEOUT = 20.0  # seconds for `codex --help`
HOOK_TRUST_FLAG = "--dangerously-bypass-hook-trust"


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
    """``claude --resume ID --dangerously-skip-permissions --remote-control evo-run-N``, shown in the Claude apps; a
    new session gets ``--session-id`` and the prompt, each the run's model and effort. A lease's CLAUDE_CODE_OAUTH_TOKEN
    drops ``--remote-control`` and the daemon's ANTHROPIC_API_KEY (``claude_code.dropped_env``); ``hub_tools.tui_args``."""

    runtime = "claude-code"
    program = "claude"
    drop_env = claude_code.DROPPED_ENV

    def __init__(self, context: RunContext, session_id: str | None):
        super().__init__(context, session_id)
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
        self.state_path = claude_state_path(context.env)
        self.drop_env = claude_code.dropped_env(context)
        self.remote_control = not (claude_code.leased_oauth(context) or gateway.active(context))
        self._transcript: tuple[Path, int] | None = None

    def transcript(self) -> Path | None:
        return claude_transcript(self.context.env, self.session_id)

    async def prepare(self) -> list[str]:
        folders = worktree_folders(self.context.worktree)
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
            value = profiles.session_model(self.context, self.runtime) if key == "model" else self.setting(key)
            if value:
                argv += [flag, value]
        argv += hub_tools.tui_args(self.context, self.runtime)  # the hub's MCP server alone, in a config dir of its own
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
        return codex_rollout(self.context.env, self.session_id) if self.session_id else None

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
        except TimeoutError:
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
        argv += codex.config_args(self.context, effort)  # the effort, and the compaction of the headless thread
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
        folders = set(worktree_folders(self.context.worktree))
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
        env = opencode.compaction_env(self.context.env, self.context.worktree)  # prunes as the headless one does
        return await opencode.start_server(self.binary, str(self.context.worktree), env)

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
