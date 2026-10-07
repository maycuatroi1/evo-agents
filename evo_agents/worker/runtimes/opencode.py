"""opencode through the HTTP API and the event stream of ``opencode serve``, called with aiohttp; nothing ``opencode
run`` prints is parsed here.

- For each run the adapter starts ``opencode serve --hostname 127.0.0.1 --port <a free port>`` in the worktree, in a
  session of its own, with stdin ``/dev/null`` (``opencode run`` waits for stdin's end when it is a pipe) and a random
  ``OPENCODE_SERVER_PASSWORD``, and reads the address from its ``listening on`` line. The server's output carries
  other lines too (a Warp plugin writes escape sequences), so the line is found by a pattern. The server is stopped
  when the agent has ended: SIGTERM to its process group, SIGKILL 5 seconds later.
- ``POST /session`` creates the session with the rules ``opencode run`` gives its own: no question to a person, no
  plan mode. ``GET /event`` streams the events, ``POST /session/:id/prompt_async`` hands over the prompt and each
  message of the owner, always with the model named (``providerID``, ``modelID``), and ``POST
  /session/:id/abort`` interrupts. A message sent while the session is busy is taken at the next step boundary.
- Permissions: every ``permission.asked`` of the session, or of a session it started, is answered ``once``
  (``POST /permission/:id/reply``), which is what ``opencode run --auto`` does ("auto-approve permissions that are
  not explicitly denied"): a rule of the owner's config that denies something still denies it.
- The model is the run's ``model``, else ``EVO_WORKER_OPENCODE_MODEL``, else the ``model`` of the owner's opencode
  config, as ``provider/model``, split at its first ``/`` into the ``providerID`` and ``modelID`` of each prompt; a
  model the server does not list fails the start with that reason, since opencode would only answer with an unknown
  error. The run's ``effort`` goes as the model ``variant``. For the heartbeat, ``models`` lists what ``opencode
  models`` prints: ``provider/model``, one a line, for the providers the owner set up.
- The turn is over when the session goes idle (``session.status`` ``idle``) and every prompt sent has reached the
  session; ``session.error`` (``MessageAbortedError`` after an abort, a provider error) fails it. The exit code of
  nothing is read.

Checked with opencode 1.18.34 (``evo-agents worker selftest --runtime opencode``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import signal
import socket
from collections.abc import AsyncIterator, Mapping

from evo_agents.worker.adapter import AgentEvent, Detection, Outcome, RunContext, command_output
from evo_agents.worker.runtimes.common import (
    HEADLESS_NOTE,
    INTERRUPT_TIMEOUT,
    AgentFinished,
    QueueAdapter,
    add_numbers,
    cut,
    detect_runtime,
    kill_group,
    message_chunk,
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

MIN_VERSION = "1.18.34"
AIOHTTP = ("aiohttp", "3.9")
LISTENING = re.compile(rb"listening on (https?://[^\s\x07\x1b]+)[\r\n]")
# What a terminal would read as control rather than text (CSI, OSC and two-character escapes): a plugin of the owner's
# may write some into what opencode prints.
ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
MODEL_LINE = re.compile(r"^[^\s/]+/\S+$")  # provider/model, as `opencode models` prints each
START_TIMEOUT = 60.0  # seconds for `opencode serve` to listen
STOP_GRACE = 5.0  # seconds between SIGTERM and SIGKILL to the server's process group
REQUEST_TIMEOUT = 60.0
OUTPUT_TAIL = 64 * 1024
# The rules `opencode run` creates its sessions with: nobody answers a question, and plan mode is not for a run.
RUN_RULES = [
    {"permission": "question", "action": "deny", "pattern": "*"},
    {"permission": "plan_enter", "action": "deny", "pattern": "*"},
    {"permission": "plan_exit", "action": "deny", "pattern": "*"},
]
# Events of the session that repeat what message.part.updated, message.updated and session.status say. The
# session.next.* family carries the same content again, piece by piece.
IGNORED = frozenset({"session.updated", "session.diff", "session.idle", "message.part.delta", "message.updated"})
TOOL_KINDS = {
    "read": "read",
    "edit": "edit",
    "write": "edit",
    "patch": "edit",
    "multiedit": "edit",
    "apply_patch": "edit",
    "bash": "execute",
    "grep": "search",
    "glob": "search",
    "list": "search",
    "codesearch": "search",
    "webfetch": "fetch",
    "websearch": "fetch",
    "task": "think",
    "todowrite": "think",
    "todoread": "think",
}


def parse_models(output: str | None) -> list[str] | None:
    """The ``provider/model`` lines of what ``opencode models`` printed, escapes left out, in order."""
    if not output:
        return None
    lines = (ESCAPES.sub("", line).strip() for line in output.splitlines())
    return [line for line in lines if MODEL_LINE.match(line)] or None


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def error_text(error) -> str:
    """``{"name": "MessageAbortedError", "data": {"message": "Aborted"}}`` -> ``MessageAbortedError: Aborted``."""
    if not isinstance(error, Mapping):
        return str(error)
    data = error.get("data")
    message = data.get("message") if isinstance(data, Mapping) else None
    name = str(error.get("name") or "error")
    return f"{name}: {message}" if message else name


async def sse_events(content) -> AsyncIterator[dict]:
    """The JSON object of each event of a text/event-stream (aiohttp's ``response.content``), without a limit on the
    length of a line, since a tool's output can make one long."""
    buffer = bytearray()
    data: list[bytes] = []
    async for chunk in content.iter_any():
        buffer.extend(chunk)
        while (end := buffer.find(b"\n")) >= 0:
            line = bytes(buffer[:end]).rstrip(b"\r")
            del buffer[: end + 1]
            if line.startswith(b"data:"):
                data.append(line[6:] if line.startswith(b"data: ") else line[5:])
            elif not line and data:
                text, data = b"\n".join(data), []
                try:
                    event = json.loads(text)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event


class Server:
    """A running ``opencode serve``: its address, the credentials of its Basic auth and its process."""

    def __init__(self, url: str, password: str, username: str = "opencode", process=None, drain=None):
        self.url = url.rstrip("/")
        self.password = password
        self.username = username
        self.process = process
        self._drain = drain

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    async def stop(self) -> None:
        if self.process is not None and self.process.returncode is None:
            kill_group(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), STOP_GRACE)
            except TimeoutError:
                kill_group(self.process.pid, signal.SIGKILL)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.process.wait(), STOP_GRACE)
        if self._drain is not None:
            self._drain.cancel()


async def start_server(binary: str, cwd: str, env: Mapping[str, str]) -> Server:
    """``opencode serve`` on 127.0.0.1 at a free port, with a random password, once it listens."""
    password = secrets.token_urlsafe(32)
    port = free_port()
    process = await asyncio.create_subprocess_exec(
        binary,
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        str(port),
        cwd=cwd,
        env={**env, "OPENCODE_SERVER_PASSWORD": password},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    seen = bytearray()

    async def listening() -> str:
        while True:
            chunk = await process.stdout.read(4096)
            if not chunk:
                await process.wait()
                raise RuntimeError(f"opencode serve exited {process.returncode} before it listened")
            seen.extend(chunk)
            del seen[:-OUTPUT_TAIL]
            found = LISTENING.search(seen)
            if found:
                return found.group(1).decode()

    try:
        url = await asyncio.wait_for(listening(), START_TIMEOUT)
    except BaseException as exc:
        kill_group(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), STOP_GRACE)
        tail = cut(seen[-300:].decode(errors="replace"), 300)
        if isinstance(exc, asyncio.TimeoutError):
            raise RuntimeError(f"opencode serve did not listen within {START_TIMEOUT:g}s: {tail}") from None
        if isinstance(exc, RuntimeError):
            raise RuntimeError(f"{exc}: {tail}") from None
        raise

    async def drain() -> None:  # the server goes on writing; a full pipe would stop it
        while await process.stdout.read(65536):
            pass

    username = env.get("OPENCODE_SERVER_USERNAME") or "opencode"
    return Server(url, password, username, process, asyncio.create_task(drain()))


class OpencodeAdapter(QueueAdapter):
    runtime = "opencode"
    binary = "opencode"
    interactive = True

    @classmethod
    def detect(cls) -> Detection:
        return detect_runtime(cls.runtime, cls.binary, MIN_VERSION, packages=(AIOHTTP,))

    @classmethod
    def models(cls) -> list[str] | None:
        """``provider/model`` for each line of ``opencode models``; None when it fails or lists none."""
        return parse_models(command_output(cls.binary, "models"))

    @classmethod
    def tui(cls, context: RunContext, session_id: str | None):
        from evo_agents.worker.interactive import OpencodeTui

        return OpencodeTui(context, session_id)

    def __init__(self, context: RunContext):
        super().__init__(context)
        self._session = context.resume_session
        self._server: Server | None = None
        self._http = None
        self._stream = None
        self._params = {"directory": str(context.worktree)}
        self._tree: set[str] = set()  # the session and the sessions it started
        self._user_messages: set[str] = set()
        self._posted = 0
        self._accepting = False
        self._idle = False
        self._tools: set[str] = set()
        self._error: str | None = None
        self._usage: dict = {}
        self._cost = 0.0
        self._summary: str | None = None
        self.model: tuple[str, str] | None = None
        self.variant = run_setting(context, self.runtime, "effort")
        self.server_url: str | None = None

    @property
    def session_id(self) -> str | None:
        return self._session

    def group_pid(self) -> int | None:
        return self._server.pid if self._server is not None else None

    async def start_server(self) -> Server:
        """``opencode serve`` for this run; a test hands it a server of its own."""
        return await start_server(which(self.binary, self.context.env), str(self.context.worktree), self.context.env)

    # HTTP

    async def _request(self, method: str, path: str, body=None):
        import aiohttp

        async with self._http.request(
            method,
            self._server.url + path,
            params=self._params,
            json=body,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as response:
            text = await response.text()
            if response.status >= 300:
                raise RuntimeError(f"opencode answered {method} {path} with {response.status}: {cut(text, 300)}")
            return json.loads(text) if text.strip() else None

    async def _resolve_model(self) -> tuple[str, str]:
        name = run_setting(self.context, self.runtime, "model")
        hint = "set EVO_WORKER_OPENCODE_MODEL to provider/model (the selftest takes --model)"
        if self._run_model():
            hint = "dispatch it with a model as `opencode models` lists it, provider/model"
        if not name:
            config = await self._request("GET", "/config") or {}
            name = config.get("model") if isinstance(config.get("model"), str) else None
        if not name:
            raise RuntimeError(f"opencode has no model to use: {hint}")
        provider, _, model = name.partition("/")
        listing = await self._request("GET", "/config/providers") or {}
        models = {
            str(entry.get("id")): set(entry.get("models") or {})
            for entry in listing.get("providers") or []
            if isinstance(entry, dict)
        }
        if model not in models.get(provider, set()):
            raise RuntimeError(f"opencode lists no model {name}: {hint}")
        return provider, model

    def _run_model(self) -> bool:
        """Whether the model is the run's own, which the dispatch named."""
        value = self.context.run.get("model") if isinstance(self.context.run, Mapping) else None
        return isinstance(value, str) and bool(value.strip())

    async def _prompt(self, text: str) -> None:
        provider, model = self.model
        body = {
            "parts": [{"type": "text", "text": text}],
            "model": {"providerID": provider, "modelID": model},
            "system": HEADLESS_NOTE,
        }
        if self.variant:
            body["variant"] = self.variant
        await self._request("POST", f"/session/{self._session}/prompt_async", body)
        self._posted += 1

    # The run

    async def _open(self) -> None:
        import aiohttp

        self._server = await self.start_server()
        self.server_url = self._server.url
        self._http = aiohttp.ClientSession(auth=aiohttp.BasicAuth(self._server.username, self._server.password))
        self.model = await self._resolve_model()
        if self.context.resume_session:
            await self._request("GET", f"/session/{self.context.resume_session}")
        else:
            title = cut(f"evo-agents run {self.context.run.get('id')}: {self.context.run.get('title') or ''}", 100)
            created = await self._request("POST", "/session", {"title": title, "permission": RUN_RULES})
            self._session = str(created["id"])
        self._tree.add(self._session)
        self._stream = await self._http.get(
            self._server.url + "/event",
            params=self._params,
            timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
        )
        if self._stream.status != 200:
            raise RuntimeError(f"opencode answered GET /event with {self._stream.status}")
        self._accepting = True
        await self._prompt(self.context.prompt)

    async def _drive(self) -> None:
        try:
            async for event in sse_events(self._stream.content):
                if await self._handle(event):
                    break
        except Exception as exc:
            self._error = self._error or f"the event stream failed: {type(exc).__name__}: {exc}"
        self._accepting = False
        if not self._idle and not self._error:
            self._error = "the server's event stream ended before the session went idle"
        self.outcome = self._outcome()

    async def _handle(self, event: dict) -> bool:
        """Map one event of the stream; whether the agent is done."""
        kind = str(event.get("type") or "")
        props = event.get("properties")
        if not isinstance(props, dict):
            return False
        if kind == "session.created":
            info = props.get("info") or {}
            if info.get("parentID") in self._tree and info.get("id"):
                self._tree.add(str(info["id"]))
            return False
        session = props.get("sessionID") or (props.get("part") or {}).get("sessionID")
        if session not in self._tree:
            return False
        if kind == "permission.asked":
            await self._approve(props)
            return False
        if session != self._session:
            return False
        if kind == "message.updated":
            self._message(props.get("info") or {})
        elif kind == "message.part.updated":
            self.emit(self._part(props.get("part") or {}))
        elif kind == "session.status":
            status = (props.get("status") or {}).get("type")
            if status == "idle":
                return await self._went_idle()
            if status == "retry":
                self.emit([raw(event)])
        elif kind == "session.error":
            self._error = error_text(props.get("error"))
            self.emit([raw(event)])
        elif kind == "todo.updated":
            todos = [todo for todo in props.get("todos") or [] if isinstance(todo, dict)]
            self.emit([plan((str(t.get("content") or ""), t.get("status"), t.get("priority")) for t in todos)])
        elif kind not in IGNORED and not kind.startswith("session.next."):
            self.emit([raw(event)])
        return False

    async def _went_idle(self) -> bool:
        async with self._lock:
            done = self.interrupted or self.stopping or bool(self._error) or len(self._user_messages) >= self._posted
            if done:
                self._idle = True
                self._accepting = False
            return done

    def _message(self, info: dict) -> None:
        if info.get("role") == "user" and info.get("id"):
            self._user_messages.add(str(info["id"]))
        elif info.get("role") == "assistant" and info.get("error"):
            self._error = error_text(info["error"])

    def _part(self, part: dict) -> list[AgentEvent]:
        kind = part.get("type")
        if part.get("messageID") in self._user_messages:
            return []  # the prompt's own text
        if kind in ("text", "reasoning"):
            text = part.get("text") or ""
            if (
                not (part.get("time") or {}).get("end")
                or not text.strip()
                or part.get("synthetic")
                or part.get("ignored")
            ):
                return []  # still streaming (message.part.delta), or not shown to the model's reader
            if kind == "reasoning":
                return [thought_chunk(text)]
            self._summary = text.strip()
            return [message_chunk(text)]
        if kind == "tool":
            return self._tool(part)
        if kind == "step-finish":
            tokens = part.get("tokens") or {}
            flat = dict(tokens)
            cache = flat.pop("cache", None)
            if isinstance(cache, dict):
                flat.update({f"cache_{key}": value for key, value in cache.items()})
            add_numbers(self._usage, flat)
            cost = part.get("cost") if isinstance(part.get("cost"), (int, float)) else None
            self._cost += cost or 0
            return [usage_update(tokens, cost, reason=part.get("reason"))]
        if kind == "step-start":
            return []
        return [raw({"type": "message.part.updated", "part": part})]

    def _tool(self, part: dict) -> list[AgentEvent]:
        state = part.get("state") or {}
        status = state.get("status")
        call_id = str(part.get("callID") or part.get("id") or "")
        name = str(part.get("tool") or "tool")
        events = []
        if status in ("running", "completed", "error") and call_id not in self._tools:
            self._tools.add(call_id)
            title = str(state.get("title") or name)
            events.append(tool_call(call_id, title, TOOL_KINDS.get(name, "other"), "in_progress", state.get("input")))
        if status == "completed":
            metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            exit_code = metadata.get("exit")
            failed = isinstance(exit_code, int) and exit_code != 0
            raw_output = {"exit": exit_code} if "exit" in metadata else None
            events.append(tool_update(call_id, "failed" if failed else "completed", state.get("output"), raw_output))
        elif status == "error":
            events.append(tool_update(call_id, "failed", state.get("error")))
        return events

    async def _approve(self, request: dict) -> None:
        try:
            await self._request("POST", f"/permission/{request.get('id')}/reply", {"reply": "once"})
        except Exception as exc:
            log.warning("a permission of opencode was not answered", extra={"error": str(exc)})
            return
        self.emit([raw({"type": "permission.asked", "properties": request, "reply": "once"})])

    def _outcome(self) -> Outcome:
        usage = {**self._usage, "cost": self._cost} if self._usage else None
        if self.interrupted:
            return Outcome(False, "interrupted", usage, self._summary)
        if self._error:
            return Outcome(False, cut(f"opencode: {self._error}"), usage, self._summary)
        return Outcome(True, None, usage, self._summary)

    async def send(self, text: str) -> None:
        async with self._lock:
            if not self._accepting:
                raise AgentFinished("opencode has gone idle and takes no more messages")
            await self._prompt(text)

    async def _interrupt(self) -> None:
        if self._http is not None and self._session:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._request("POST", f"/session/{self._session}/abort"), INTERRUPT_TIMEOUT)

    async def _close(self) -> None:
        self._accepting = False
        if self._stream is not None:
            self._stream.close()
        if self._http is not None:
            await self._http.close()
        if self._server is not None:
            await self._server.stop()
