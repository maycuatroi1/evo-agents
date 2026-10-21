"""Agents that speak the Agent Client Protocol (ACP, https://agentclientprotocol.com, protocol version 1): the runtimes
plugins declare (``evo_agents.plugins.runtimes``), each run through this one adapter with its declaration.

- ``adapter_class(declaration)`` is the adapter of one runtime, ``<plugin>/<name>``: a subclass of ``AcpAdapter``
  that carries the declaration the hub sent, the command of the agent, its models and the variables it needs.
- The agent is the declaration's command: its program found on the agent's PATH (or an absolute path), started
  through the launcher of ``common`` (a session of its own) in the run's worktree with the run's environment. Its
  stdin and stdout are the JSON-RPC 2.0 connection, one message a line (``Connection``); what it writes on stderr is
  kept, the last STDERR_LINES lines, and written to the run's log as an ``output`` event when the agent fails.
- ``initialize`` says protocol version 1 and asks the agent for nothing of the client's: no file system and no
  terminal, so the agent works in the worktree with its own tools. Every permission the agent asks
  (``session/request_permission``) is granted with its first option of kind ``allow_once``, else ``allow_always``, as
  the other runtimes run with the full permissions of the machine's owner; after an interrupt, ``cancelled``. A
  method of the client the agent calls anyway is answered "method not found".
- ``session/new`` in the worktree, with no MCP server. A run that goes on with a session loads it (``session/load``)
  when the agent says it can (``loadSession``), the replay of the session's history left out of the run's events;
  otherwise it starts a new session, and the run's log says so.
- The run's model (else the first model its agent profile allows) goes to the agent through the agent's config option
  of category ``model`` (``session/set_config_option``); an agent that offers none, or refuses the model, fails the
  start before any prompt.
- The prompt is one ``session/prompt``: HEADLESS_NOTE, with the instructions of the run's agent profile after it,
  then the run's prompt, as two text blocks (ACP has no system prompt). A message of the owner during a turn waits
  for the turn to end (ACP takes none mid-turn) and starts the next one in the same session; a turn that ends
  ``end_turn`` with no message waiting is the agent's last. ``interrupt`` sends ``session/cancel``;
  ``stop_at_turn_boundary`` starts no other turn. Whether the turn completed comes from its stop reason, never from an
  exit code: ``end_turn`` completed, any other did not.
- ``session/update`` notifications are already in the shapes of the hub's events (``docs/workers.md``): an
  ``agent_message_chunk``, ``agent_thought_chunk``, ``tool_call``, ``tool_call_update``, ``plan`` or
  ``usage_update`` becomes the event of that kind with the update's other keys as its body; a
  ``user_message_chunk`` (the prompt, which the hub has) is left out, and any other update is kept raw (``output``).
- The run's usage is the session's ``cost`` in USD, when the agent reports one in a ``usage_update``, with
  ``peak_context_tokens``, the largest ``used`` it reported, and ``context_window``, its ``size``. A run the night
  shift queued stops at its time cap (``QueueAdapter``).

A run under an agent profile gets its model and its instructions as said above; a profile that limits tool or skill
fails the run before its agent starts (``profile_refusal``): an ACP agent's tools and skills are its own, which the
protocol gives the client no way to turn off. A variable of the declaration's ``secrets`` that neither the run's
leases nor the worker's environment set fails the run before its agent starts too (``environment_refusal``).

Standard library only.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import shutil
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import ClassVar

from evo_agents import __version__
from evo_agents.plugins.runtimes import AcpRuntime
from evo_agents.worker.adapter import AgentEvent, Detection, Outcome, RunContext
from evo_agents.worker.runtimes import profiles
from evo_agents.worker.runtimes.common import (
    HEADLESS_NOTE,
    INTERRUPT_TIMEOUT,
    AgentFinished,
    QueueAdapter,
    cut,
    json_value,
    larger,
    new_session_argv,
    raw,
    text_block,
    token_sum,
    with_peak,
)

log = logging.getLogger("evo_agents.worker")

PROTOCOL_VERSION = 1
HANDSHAKE_TIMEOUT = 120.0  # seconds for initialize, session/new or /load and the model: an npx agent installs first
MAX_LINE = 16 * 1024 * 1024  # bytes of one message of the agent
STDERR_LINES = 40
STOP_TIMEOUT = 5.0  # seconds the agent has to exit once its stdin closed, before the launcher's group is killed
EVENT_KINDS = ("agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update", "plan", "usage_update")
ALLOW_KINDS = ("allow_once", "allow_always")
METHOD_NOT_FOUND = -32601
CLIENT_INFO = {"name": "evo-agents", "title": "evo-agents worker", "version": __version__}


class AcpError(RuntimeError):
    """The agent answered a request with an error."""

    def __init__(self, method: str, error: object):
        message = error.get("message") if isinstance(error, Mapping) else None
        super().__init__(f"{method}: {message or error}")
        self.method = method
        self.error = error


class AgentGone(RuntimeError):
    """The agent's stdout ended: it exited, or closed it."""


class Connection:
    """JSON-RPC 2.0 with the agent over its stdin and stdout, one message a line: the requests of the client and their
    answers, the notifications of the agent (``on_notification``, called in order as they come) and its requests
    (``on_request``, answered in a task of their own)."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        on_notification: Callable[[str, dict], None],
        on_request: Callable[[str, dict], Awaitable[object]],
    ):
        self._reader = reader
        self._writer = writer
        self._on_notification = on_notification
        self._on_request = on_request
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[str, asyncio.Future]] = {}
        self._tasks: set[asyncio.Task] = set()
        self.gone: str | None = None  # why the connection ended, once it has
        self._read_task = asyncio.create_task(self._read())

    async def request(self, method: str, params: dict, timeout: float | None = None):
        """The result of ``method``; AcpError for the agent's error, AgentGone once the agent ended."""
        if self.gone is not None:
            raise AgentGone(self.gone)
        number = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[number] = (method, future)
        try:
            await self._send({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
            return await asyncio.wait_for(asyncio.shield(future), timeout) if timeout else await future
        finally:
            self._pending.pop(number, None)

    async def notify(self, method: str, params: dict) -> None:
        if self.gone is None:
            await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: dict) -> None:
        try:
            self._writer.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
            await self._writer.drain()
        except (ConnectionError, RuntimeError) as exc:  # the agent closed its stdin, or exited
            self._end(f"the agent's stdin is closed: {type(exc).__name__}")
            raise AgentGone(self.gone) from None

    async def _read(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                self._take(line)
        except (ValueError, asyncio.LimitOverrunError):  # a line past MAX_LINE
            self._end(f"the agent wrote a message of more than {MAX_LINE // (1024 * 1024)} MiB")
            return
        except Exception as exc:
            self._end(f"reading the agent failed: {type(exc).__name__}: {exc}")
            return
        self._end("the agent ended")

    def _take(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            log.warning(
                "the ACP agent wrote a line that is not JSON", extra={"line": cut(line.decode(errors="replace"))}
            )
            return
        if not isinstance(message, dict):
            return
        method = message.get("method")
        if isinstance(method, str):
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if "id" in message:
                task = asyncio.create_task(self._answer(message["id"], method, params))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                self._on_notification(method, params)
            return
        entry = self._pending.get(message.get("id"))
        if entry is None or entry[1].done():
            return
        method, future = entry
        if "error" in message:
            future.set_exception(AcpError(method, message["error"]))
        else:
            future.set_result(message.get("result"))

    async def _answer(self, number, method: str, params: dict) -> None:
        try:
            result = await self._on_request(method, params)
        except LookupError:
            reply = {"jsonrpc": "2.0", "id": number, "error": {"code": METHOD_NOT_FOUND, "message": "method not found"}}
        else:
            reply = {"jsonrpc": "2.0", "id": number, "result": result}
        with contextlib.suppress(AgentGone):
            await self._send(reply)

    def _end(self, why: str) -> None:
        if self.gone is None:
            self.gone = why
        for _, future in list(self._pending.values()):
            if not future.done():
                future.set_exception(AgentGone(self.gone))

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self._writer.close()
        self._read_task.cancel()
        for task in [self._read_task, *self._tasks]:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


def events_of(update: Mapping) -> list[AgentEvent]:
    """The hub's events for one ``session/update`` of the agent."""
    kind = update.get("sessionUpdate")
    if kind == "user_message_chunk":
        return []
    if kind in EVENT_KINDS:
        return [AgentEvent(kind, json_value({key: value for key, value in update.items() if key != "sessionUpdate"}))]
    return [raw(update)]


def model_option(options) -> str | None:
    """The id of the agent's config option of category model, among the ``configOptions`` it sent; None for none."""
    for option in options if isinstance(options, list) else ():
        if isinstance(option, Mapping) and option.get("category") == "model" and isinstance(option.get("id"), str):
            return option["id"]
    return None


def permission_choice(options) -> dict:
    """The outcome of a ``session/request_permission``: its first option that allows once, else always; cancelled when
    it offers neither."""
    listed = [option for option in options if isinstance(option, Mapping)] if isinstance(options, list) else []
    for kind in ALLOW_KINDS:
        for option in listed:
            if option.get("kind") == kind and isinstance(option.get("optionId"), str):
                return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
    return {"outcome": {"outcome": "cancelled"}}


class AcpAdapter(QueueAdapter):
    """An ACP agent; ``adapter_class`` makes the subclass of one runtime, with its declaration."""

    declaration: ClassVar[AcpRuntime]
    interactive = False

    @classmethod
    def detect(cls) -> Detection:
        """Available when the declaration's program is on PATH (or an absolute path to an executable); an ACP agent has
        no version option the protocol names, so none is reported."""
        program = cls.declaration.program
        if shutil.which(program) is None:
            return Detection(
                False, None, f"{program} is not on PATH, for {cls.runtime} of plugin {cls.declaration.plugin}"
            )
        return Detection(True, None, None)

    @classmethod
    def models(cls) -> list[str] | None:
        return list(cls.declaration.models) or None

    @classmethod
    def profile_refusal(cls, run) -> str | None:
        profile = profiles.Profile.of(run)
        refused = profiles.model_refusal(profile, run)
        if refused or profile is None:
            return refused
        limited = [axis for axis in ("tool", "skill") if profile.allowed(axis) is not None]
        if limited:
            return (
                f"the ACP agent of {cls.runtime} keeps its own {' and '.join(axis + 's' for axis in limited)}, which the protocol cannot turn "
                f"off, and agent profile {profile.label} limits them"
            )
        return None

    @classmethod
    def environment_refusal(cls, context: RunContext) -> str | None:
        visible = context.environment.visible(context)
        missing = [name for name in cls.declaration.secrets if not visible.get(name)]
        if not missing:
            return None
        names = ", ".join(missing)
        if context.environment.container:
            return (
                f"the agent of {cls.runtime} needs {names}, which the run's leases do not set, and the agent runs in "
                f"environment {context.environment.name}, a container that gets none of the worker's own environment: "
                "bind a secret of that name to the project"
            )
        return (
            f"the agent of {cls.runtime} needs {names}, which neither the run's leases nor the worker's environment "
            f"set: bind a secret of that name to the project, or set it in the daemon's environment"
        )

    def __init__(self, context: RunContext):
        super().__init__(context)
        self._session: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._conn: Connection | None = None
        self._stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self._stderr_task: asyncio.Task | None = None
        self._dir: Path | None = None
        self._turn: asyncio.Task | None = None
        self._pending: list[str] = []
        self._accepting = False
        self._loading = False  # session/load replays the session's history, which is no event of this run
        self._stop_reason: str | None = None
        self._error: str | None = None
        self._turn_text: list[str] = []
        self._summary: str | None = None
        self._cost: float | None = None
        self._peak: int | None = None
        self._window: int | None = None
        self.agent_info: dict = {}
        self.loaded = False  # the session went on from context.resume_session

    @property
    def session_id(self) -> str | None:
        return self._session

    # Starting

    async def _open(self) -> None:
        declaration = self.declaration
        environment = self.context.environment
        program = environment.program(declaration.program, self.context.env)
        self._dir = Path(tempfile.mkdtemp(prefix="evo-acp-"))
        self.pid_file = self._dir / "pid"
        command = environment.command(program, declaration.command[1:], context=self.context, directory=self._dir)
        self._process = await asyncio.create_subprocess_exec(
            *new_session_argv(self.pid_file, *command),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.context.worktree),
            env=dict(self.context.env),
            limit=MAX_LINE,
        )
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._conn = Connection(self._process.stdout, self._process.stdin, self._notified, self._asked)
        started = await self._call("initialize", self._initialize_params())
        if not isinstance(started, Mapping) or started.get("protocolVersion") != PROTOCOL_VERSION:
            found = started.get("protocolVersion") if isinstance(started, Mapping) else None
            raise RuntimeError(f"the agent speaks ACP protocol version {found}, not {PROTOCOL_VERSION}")
        info = started.get("agentInfo")
        self.agent_info = dict(info) if isinstance(info, Mapping) else {}
        capabilities = started.get("agentCapabilities")
        can_load = isinstance(capabilities, Mapping) and capabilities.get("loadSession") is True
        options = await self._new_or_loaded(can_load)
        model = self._model()
        if model:
            await self._set_model(model, options)
        self._turn = asyncio.create_task(self._prompt(self._first_prompt()))
        self._accepting = True

    def _initialize_params(self) -> dict:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": CLIENT_INFO,
        }

    async def _call(self, method: str, params: dict, timeout: float | None = HANDSHAKE_TIMEOUT):
        try:
            return await self._conn.request(method, params, timeout)
        except TimeoutError:
            raise RuntimeError(f"the agent did not answer {method} within {timeout:g}s") from None

    async def _new_or_loaded(self, can_load: bool):
        """Start the session, or load the one the run goes on with; the config options the agent sent, if any."""
        cwd = str(Path(self.context.worktree).resolve())
        resume = self.context.resume_session
        if resume and can_load:
            self._loading = True
            try:
                found = await self._call("session/load", {"sessionId": resume, "cwd": cwd, "mcpServers": []})
            finally:
                self._loading = False
            self._session, self.loaded = resume, True
            return found.get("configOptions") if isinstance(found, Mapping) else None
        if resume:
            log.info("the ACP agent cannot load a session; starting a new one", extra={"runtime": self.runtime})
        found = await self._call("session/new", {"cwd": cwd, "mcpServers": []})
        session = found.get("sessionId") if isinstance(found, Mapping) else None
        if not isinstance(session, str) or not session:
            raise RuntimeError("the agent answered session/new without a sessionId")
        self._session = session
        return found.get("configOptions")

    def _model(self) -> str | None:
        """The run's model, else the first its agent profile allows; None: the agent's own choice."""
        asked = self.context.run.get("model") if isinstance(self.context.run, Mapping) else None
        if isinstance(asked, str) and asked.strip():
            return asked.strip()
        profile = profiles.Profile.of(self.context.run)
        allow = profile.allowed("model") if profile is not None else None
        return allow[0] if allow else None

    async def _set_model(self, model: str, options) -> None:
        option = model_option(options)
        if option is None:
            raise RuntimeError(
                f"the run asks for model {model}, and the agent offers no config option of category model"
            )
        params = {"sessionId": self._session, "configId": option, "value": model}
        try:
            await self._call("session/set_config_option", params)
        except AcpError as exc:
            raise RuntimeError(f"the agent did not take model {model}: {exc}") from None

    def _first_prompt(self) -> list[dict]:
        note = profiles.with_instructions(HEADLESS_NOTE, profiles.profile_of(self.context))
        return [text_block(note), text_block(self.context.prompt)]

    # The agent's side

    def _notified(self, method: str, params: dict) -> None:
        if method != "session/update" or self._loading:
            return
        update = params.get("update")
        if not isinstance(update, Mapping):
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content")
            if isinstance(content, Mapping) and isinstance(content.get("text"), str):
                self._turn_text.append(content["text"])
        elif kind == "usage_update":
            self._usage(update)
        self.emit(events_of(update))

    def _usage(self, update: Mapping) -> None:
        self._peak = larger(self._peak, token_sum(update.get("used")))
        self._window = token_sum(update.get("size")) or self._window
        cost = update.get("cost")
        if isinstance(cost, Mapping) and cost.get("currency") == "USD":
            amount = cost.get("amount")
            if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount >= 0:
                self._cost = float(amount)

    async def _asked(self, method: str, params: dict):
        if method == "session/request_permission":
            if self.interrupted:
                return {"outcome": {"outcome": "cancelled"}}
            return permission_choice(params.get("options"))
        raise LookupError(method)

    async def _read_stderr(self) -> None:
        stream = self._process.stderr if self._process is not None else None
        if stream is None:
            return
        with contextlib.suppress(Exception):
            while True:
                line = await stream.readline()
                if not line:
                    return
                self._stderr.append(line.decode(errors="replace").rstrip())

    # The turns

    async def _prompt(self, blocks: list[dict]) -> str | None:
        self._turn_text = []
        result = await self._conn.request("session/prompt", {"sessionId": self._session, "prompt": blocks})
        text = "".join(self._turn_text).strip()
        if text:
            self._summary = text
        reason = result.get("stopReason") if isinstance(result, Mapping) else None
        return reason if isinstance(reason, str) else None

    async def _drive(self) -> None:
        while True:
            try:
                self._stop_reason = await self._turn
            except (AcpError, AgentGone, ConnectionError) as exc:
                self._error = str(exc)
                break
            async with self._lock:
                if self._stop_reason == "end_turn" and self._pending and not (self.stopping or self.interrupted):
                    text = "\n\n".join(self._pending)
                    self._pending.clear()
                    self._turn = asyncio.create_task(self._prompt([text_block(text)]))
                    continue
                self._accepting = False
                break
        self._accepting = False
        if self._error is not None:
            await self._exited()
        self.outcome = self._outcome()

    async def _exited(self) -> None:
        """The agent failed: wait a moment for its exit code and its last lines of stderr, which go to the run's log."""
        process = self._process
        if process is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), STOP_TIMEOUT)
            if process.returncode is not None:
                self._error = f"{self._error} (exit status {process.returncode})"
        if self._stderr_task is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._stderr_task), 1.0)
        if self._stderr:
            self.emit([raw({"stderr": list(self._stderr)})])

    def _outcome(self) -> Outcome:
        usage = with_peak(
            {"cost": self._cost} if self._cost is not None else None, self._peak, context_window=self._window
        )
        summary = self._summary
        if self.interrupted:
            return Outcome(False, "interrupted", usage, summary)
        if self._error is not None:
            return Outcome(False, cut(f"{self.runtime}: {self._error}"), usage, summary)
        if self._stop_reason == "end_turn":
            return Outcome(True, None, usage, summary)
        how = self._stop_reason or "without a stop reason"
        return Outcome(False, f"{self.runtime} ended its turn {how}", usage, summary)

    async def send(self, text: str) -> None:
        async with self._lock:
            if not self._accepting:
                raise AgentFinished(f"{self.runtime} has finished its last turn and takes no more messages")
            self._pending.append(text)

    async def _interrupt(self) -> None:
        if self._conn is not None and self._session is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._conn.notify("session/cancel", {"sessionId": self._session}), INTERRUPT_TIMEOUT
                )

    async def _close(self) -> None:
        self._accepting = False
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            with contextlib.suppress(BaseException):
                await self._turn
        if self._conn is not None:
            await self._conn.close()
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), STOP_TIMEOUT)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await self._stderr_task

    def _cleanup(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)


def adapter_class(declaration: AcpRuntime) -> type[AcpAdapter]:
    """The adapter of the runtime ``declaration`` names, carrying the declaration."""
    name = "Acp_" + "".join(ch if ch.isalnum() else "_" for ch in declaration.runtime)
    return type(
        name, (AcpAdapter,), {"declaration": declaration, "runtime": declaration.runtime, "binary": declaration.program}
    )
