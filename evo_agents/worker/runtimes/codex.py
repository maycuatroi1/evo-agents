"""Codex through ``openai-codex``, the Python SDK that drives ``codex app-server`` over JSON-RPC on stdio; nothing
the CLI prints is parsed here.

- The app-server is ``codex`` on PATH (``codex app-server --listen stdio://``), the owner's own install and login,
  started through the launcher of ``common`` (a session of its own).
- A new thread (``thread/start``), or the run's thread when it goes on with one (``thread/resume``), works in the
  worktree with full access: sandbox ``danger-full-access`` (``Sandbox.full_access``) and approval policy
  ``never`` (``ApprovalMode.deny_all``, which asks nobody: with no sandbox, nothing needs approving). Together they
  are what ``--dangerously-bypass-approvals-and-sandbox`` sets. HEADLESS_NOTE goes in as developer instructions.
- The prompt starts a turn (``turn/start``). A message of the owner during the turn steers it (``turn/steer``); one
  that comes when the turn has ended, or that the turn no longer takes, starts a new turn on the same thread. When
  a turn completes and no message waits, the agent is done and the app-server is closed.
- ``interrupt`` interrupts the turn (``turn/interrupt``); ``stop_at_turn_boundary`` lets it complete and starts no
  other.
- Whether the turn completed comes from ``turn/completed`` and its status (``completed``, ``interrupted``,
  ``failed``), never from an exit code.
- The thread's model is the run's ``model``, else ``EVO_WORKER_CODEX_MODEL``, else codex's own choice. For the
  heartbeat, ``models`` reads the owner's codex home (``$CODEX_HOME``, ``~/.codex`` by default): the ``model`` of
  ``config.toml`` and of each of its profiles, then the models codex offers in its picker, from the cache it keeps
  there (``models_cache.json``: each entry's ``slug`` whose ``visibility`` is ``list``). A file that is missing or not
  in that shape adds nothing.

Checked with openai-codex 0.160.0 and codex-cli 0.153.4 as the app-server (``evo-agents worker selftest --runtime
codex``); the SDK bundles a codex binary of its own version, which the adapter does not use.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path

from evo_agents.worker.adapter import AgentEvent, Detection, Outcome, RunContext
from evo_agents.worker.runtimes.common import (
    HEADLESS_NOTE,
    INTERRUPT_TIMEOUT,
    AgentFinished,
    QueueAdapter,
    cut,
    detect_runtime,
    message_chunk,
    new_session_argv,
    plan,
    raw,
    run_setting,
    thought_chunk,
    tool_call,
    tool_update,
    usage_update,
    which,
)

log = logging.getLogger("evo_agents.worker")

MIN_VERSION = "0.153.4"  # codex-cli, run as the app-server
SDK = ("openai-codex", "0.160.0")
APP_SERVER = ("app-server", "--listen", "stdio://")
# Notifications that only repeat, piece by piece, what a completed item or the turn's end says in full, and the
# start of a hook, whose completion says the same and how it ended.
SKIPPED = frozenset(
    {
        "hook/started",
        "turn/started",
        "turn/completed",
        "turn/diff/updated",
        "item/agentMessage/delta",
        "item/plan/delta",
        "item/reasoning/textDelta",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
        "item/commandExecution/outputDelta",
        "item/fileChange/outputDelta",
        "item/fileChange/patchUpdated",
        "item/mcpToolCall/progress",
        "serverRequest/resolved",
    }
)
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")
MODELS_CACHE = "models_cache.json"


def codex_home(env: Mapping[str, str]) -> Path:
    """``$CODEX_HOME``, else ``~/.codex``."""
    value = env.get("CODEX_HOME")
    if value:
        return Path(value).expanduser()
    return Path(env.get("HOME") or Path.home()) / ".codex"


def config_models(text: str) -> list[str]:
    """The ``model`` of a codex ``config.toml``, then the ``model`` of each of its profiles."""
    try:
        data = tomllib.loads(text)
    except ValueError:
        return []
    found = [data.get("model")]
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        found += [profile.get("model") for profile in profiles.values() if isinstance(profile, dict)]
    return [name for name in found if isinstance(name, str)]


def cached_models(text: str) -> list[str]:
    """The slugs codex offers in its model picker, from its ``models_cache.json``."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    entries = data.get("models") if isinstance(data, dict) else None
    return [
        entry["slug"]
        for entry in entries or []
        if isinstance(entry, dict) and entry.get("visibility") == "list" and isinstance(entry.get("slug"), str)
    ]


def home_models(home: Path) -> list[str]:
    """What ``models`` reports, from the codex home ``home``: each model once, the config's first."""
    found: list[str] = []
    for name, read in (("config.toml", config_models), (MODELS_CACHE, cached_models)):
        try:
            text = (home / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found += read(text)
    return list(dict.fromkeys(found))


def wire(payload) -> dict:
    """A notification's params as the app-server sent them (camelCase JSON)."""
    if hasattr(payload, "model_dump"):
        return payload.model_dump(by_alias=True, mode="json", exclude_none=True, warnings=False)
    params = getattr(payload, "params", None)
    return params if isinstance(params, dict) else {}


def snake(usage: dict) -> dict:
    """Token counts as ``codex exec --json`` names them: ``inputTokens`` -> ``input_tokens``."""
    return {_CAMEL.sub("_", key).lower(): value for key, value in usage.items()}


def _status(item: dict) -> str:
    status = str(item.get("status") or "completed")
    exit_code = item.get("exitCode")
    if status in ("failed", "declined") or (isinstance(exit_code, int) and exit_code != 0):
        return "failed"
    return "in_progress" if status == "inProgress" else "completed"


def _started(item: dict) -> list[AgentEvent]:
    kind = item.get("type")
    call_id = str(item.get("id") or "")
    if kind == "commandExecution":
        command = str(item.get("command") or "")
        return [tool_call(call_id, command, "execute", "in_progress", {"command": command, "cwd": item.get("cwd")})]
    if kind == "fileChange":
        paths = [str(change.get("path")) for change in item.get("changes") or [] if isinstance(change, dict)]
        return [tool_call(call_id, "edit " + ", ".join(paths), "edit", "in_progress", {"changes": item.get("changes")})]
    if kind in ("mcpToolCall", "dynamicToolCall"):
        name = "/".join(str(part) for part in (item.get("server") or item.get("namespace"), item.get("tool")) if part)
        return [tool_call(call_id, name or kind, "other", "in_progress", item.get("arguments"))]
    if kind == "webSearch":
        return [tool_call(call_id, f"search {item.get('query') or ''}".strip(), "fetch", "in_progress", item)]
    return []


def _completed(item: dict) -> list[AgentEvent]:
    kind = item.get("type")
    call_id = str(item.get("id") or "")
    if kind == "agentMessage":
        text = item.get("text")
        return [message_chunk(text)] if isinstance(text, str) and text else []
    if kind == "reasoning":
        text = "\n".join(str(part) for part in [*(item.get("summary") or []), *(item.get("content") or [])] if part)
        return [thought_chunk(text)] if text.strip() else []
    if kind == "userMessage":
        return []  # the prompt or the owner's message, which the hub has
    if kind == "commandExecution":
        output = {key: item[key] for key in ("exitCode", "durationMs") if key in item}
        return [tool_update(call_id, _status(item), item.get("aggregatedOutput"), output or None)]
    if kind == "fileChange":
        diffs = "\n".join(
            str(change.get("diff") or "") for change in item.get("changes") or [] if isinstance(change, dict)
        )
        return [tool_update(call_id, _status(item), diffs or None)]
    if kind in ("mcpToolCall", "dynamicToolCall"):
        failed = item.get("error") or item.get("success") is False
        status = "failed" if failed else _status(item)
        return [tool_update(call_id, status, None, item.get("result") or item.get("error") or item.get("contentItems"))]
    if kind == "webSearch":
        return [tool_update(call_id, "completed", None, item.get("results"))]
    return [raw({"method": "item/completed", "item": item})]


def events_of(method: str, params: dict) -> list[AgentEvent]:
    """The hub's events for one notification of the app-server."""
    if method in SKIPPED:
        return []
    if method == "item/started":
        return _started(params.get("item") or {})
    if method == "item/completed":
        return _completed(params.get("item") or {})
    if method == "turn/plan/updated":
        steps = [step for step in params.get("plan") or [] if isinstance(step, dict)]
        return [plan((str(step.get("step") or ""), step.get("status"), "medium") for step in steps)]
    if method == "thread/tokenUsage/updated":
        usage = params.get("tokenUsage") or {}
        return [
            usage_update(
                usage.get("total") or {}, last=usage.get("last"), contextWindow=usage.get("modelContextWindow")
            )
        ]
    return [raw({"method": method, "params": params})]


class CodexAdapter(QueueAdapter):
    runtime = "codex"
    binary = "codex"
    interactive = True

    @classmethod
    def detect(cls) -> Detection:
        return detect_runtime(cls.runtime, cls.binary, MIN_VERSION, packages=(SDK,))

    @classmethod
    def models(cls) -> list[str] | None:
        """The models of the owner's codex config and of codex's model picker; None when it names none."""
        return home_models(codex_home(os.environ)) or None

    @classmethod
    def tui(cls, context: RunContext, session_id: str | None):
        from evo_agents.worker.interactive import CodexTui

        return CodexTui(context, session_id)

    def __init__(self, context: RunContext):
        super().__init__(context)
        self._session = context.resume_session
        self._codex = None
        self._thread = None
        self._turn = None
        self._pending: list[str] = []
        self._accepting = False
        self._status: str | None = None
        self._turn_error: str | None = None
        self._error: str | None = None
        self._usage: dict | None = None
        self._summary: str | None = None
        self._dir: Path | None = None
        self.config = None
        self.thread_options: dict = {}
        self.server_version: str | None = None

    @property
    def session_id(self) -> str | None:
        return self._session

    def make_codex(self, config):
        """The SDK's client; a test hands it one of its own."""
        from openai_codex import AsyncCodex

        return AsyncCodex(config)

    async def _open(self) -> None:
        from openai_codex import ApprovalMode, CodexConfig, Sandbox

        program = which(self.binary, self.context.env)
        self._dir = Path(tempfile.mkdtemp(prefix="evo-codex-"))
        self.pid_file = self._dir / "pid"
        worktree = str(self.context.worktree)
        self.config = CodexConfig(
            codex_bin=program,
            launch_args_override=tuple(new_session_argv(self.pid_file, program, *APP_SERVER)),
            cwd=worktree,
            env=dict(self.context.env),
        )
        self._codex = self.make_codex(self.config)
        await self._codex.__aenter__()  # starts the app-server and initializes the connection
        with contextlib.suppress(Exception):  # "0.153.4 (Mac OS 26.5.2; arm64) ..."
            self.server_version = self._codex.metadata.serverInfo.version.split()[0]
        effort = run_setting(self.context, self.runtime, "effort")
        self.thread_options = {
            "approval_mode": ApprovalMode.deny_all,
            "sandbox": Sandbox.full_access,
            "cwd": worktree,
            "developer_instructions": HEADLESS_NOTE,
            "model": run_setting(self.context, self.runtime, "model"),
            "config": {"model_reasoning_effort": effort} if effort else None,
        }
        if self.context.resume_session:
            self._thread = await self._codex.thread_resume(self.context.resume_session, **self.thread_options)
        else:
            self._thread = await self._codex.thread_start(**self.thread_options)
        self._session = self._thread.id
        self._turn = await self._thread.turn(self.context.prompt)
        self._accepting = True

    async def _drive(self) -> None:
        try:
            while True:
                await self._follow(self._turn)
                async with self._lock:
                    self._turn = None
                    if self._status == "completed" and self._pending and not (self.stopping or self.interrupted):
                        text = "\n\n".join(self._pending)
                        self._pending.clear()
                        self._turn = await self._thread.turn(text)
                        continue
                    self._accepting = False
                    break
        except Exception as exc:  # the app-server died, or the SDK failed
            self._accepting = False
            self._error = f"{type(exc).__name__}: {exc}"
        self.outcome = self._outcome()

    async def _follow(self, turn) -> None:
        self._status = None
        self._turn_error = None
        async for notification in turn.stream():
            params = wire(notification.payload)
            if notification.method == "turn/completed":
                ended = params.get("turn") or {}
                self._status = ended.get("status")
                error = ended.get("error") or {}
                self._turn_error = error.get("message") if isinstance(error, dict) else None
            elif notification.method == "thread/tokenUsage/updated":
                total = (params.get("tokenUsage") or {}).get("total")
                if isinstance(total, dict):
                    self._usage = snake(total)
            elif notification.method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str) and item["text"].strip():
                    self._summary = item["text"].strip()
            self.emit(events_of(notification.method, params))

    def _outcome(self) -> Outcome:
        usage, summary = self._usage, self._summary
        if self.interrupted:
            return Outcome(False, "interrupted", usage, summary)
        if self._error:
            return Outcome(False, cut(f"codex: {self._error}"), usage, summary)
        if self._status == "completed":
            return Outcome(True, None, usage, summary)
        detail = f": {self._turn_error}" if self._turn_error else ""
        return Outcome(False, cut(f"codex ended its turn {self._status or 'without a status'}{detail}"), usage, summary)

    async def send(self, text: str) -> None:
        async with self._lock:
            if not self._accepting:
                raise AgentFinished("codex has finished its last turn and takes no more messages")
            if self._turn is not None:
                try:
                    await self._turn.steer(text)
                    return
                except Exception as exc:  # the turn ended meanwhile: the message starts the next one
                    log.info("codex did not take a message into its turn", extra={"error": type(exc).__name__})
            self._pending.append(text)

    async def _interrupt(self) -> None:
        turn = self._turn
        if turn is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(turn.interrupt(), INTERRUPT_TIMEOUT)

    async def _close(self) -> None:
        self._accepting = False
        if self._codex is not None:
            await self._codex.close()

    def _cleanup(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
