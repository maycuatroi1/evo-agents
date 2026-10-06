"""``evo-agents hub mcp``: MCP over stdio for any runtime, carried to the hub's /mcp over HTTPS.

Every runtime attaches the hub the same way, by starting this command, and the token stays in ~/.evo/hub/token: no
configuration file of a runtime holds an Authorization header. Each line on stdin is one JSON-RPC message. It goes to
the hub as one POST /mcp with the token, the session's project (X-Evo-Project: --project, else the project
``resolve_project`` finds for the working directory) and its sink (X-Evo-Sink: --sink, default claude-code@anthropic),
and the hub's answer comes back as one line on stdout, as the hub wrote it. Inside a run of this machine's worker
(EVO_RUN_ID, which the daemon sets for a run's agent, and the worker's token and configuration in its state directory,
``$EVO_WORKER_HOME`` or ~/.evo/worker), the message goes with the worker token and X-Evo-Run instead, whether or not
~/.evo/hub/token exists, and without X-Evo-Project: the hub binds the session to the run's project and scopes what it
reads and writes to it (``evo_agents.hub.server.mcp``). A notification gets no line. Once the
handshake is done, every message carries the negotiated MCP-Protocol-Version. A message of the 2026-07-28 revision,
which has no handshake and carries its version in ``params._meta``, gets the headers that revision's HTTP binding
asks for instead: MCP-Protocol-Version, Mcp-Method and Mcp-Name.

The endpoint is stateless: each message is a request of its own. The requests go over kept-alive connections
(``evo_agents.hub.client.Connections``), so a tool call pays for no TCP or TLS handshake once one was made, and a hub
that restarted is reached on a new connection by the next message. A refused connection, as while the hub restarts, is
tried RETRIES more times. When the hub gives no MCP answer (it cannot be reached, it refuses the token, or it answers
something else), the proxy answers in its place: initialize, ping and tools/list from the tool list it carries
(``evo_agents.hub.mcp_tools``), so the session still starts and sees its tools; every tool call with a short error
naming the hub's URL; any other request with a JSON-RPC error saying the same. Credentials are read again for every
message, so an ``evo-agents hub login`` made meanwhile counts at once.

Messages are answered concurrently, WORKERS at a time, the handshake first. The proxy writes nothing on this machine,
and never prints the token or what a message holds: stderr gets one line when the hub stops answering and one when it
answers again.

``McpClient`` is the same channel for single tool calls, which ``kg serve --backend hub`` and ``kg query --backend
hub`` use. Standard library only.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from evo_agents import __version__
from evo_agents.hub.client import CONNECTIONS, Credentials, HubError, Unreachable, check_url, load_credentials
from evo_agents.hub.mcp_tools import (
    HANDSHAKE_VERSIONS,
    INSTRUCTIONS,
    PROJECT_HEADER,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    RUN_HEADER,
    SERVER_NAME,
    SINK_HEADER,
    TOOLS,
)
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.worker.home import WorkerHome, WorkerStateError

MCP_PATH = "/mcp"
TIMEOUT = 120.0  # seconds for one message: a tool call may first fetch the project's graph into the hub's cache
RETRIES = 2  # more tries of a message whose connection was refused
RETRY_DELAY = 0.5  # seconds before the first retry, doubling after
WORKERS = 4
PARSE_FAILED = {"code": -32700, "message": "parse error"}  # what kg serve answers to a line that is not JSON
HUB_ERROR = -32000  # JSON-RPC's range for server errors: the hub gave no answer
ENVELOPE_VERSION = "io.modelcontextprotocol/protocolVersion"  # params._meta key of a 2026-07-28 message
NAMED_BY = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}  # the param Mcp-Name mirrors
RUN_VARIABLE = "EVO_RUN_ID"  # set by the worker daemon for the agent of a run (evo_agents.worker.run)
_HEADER_SAFE = re.compile(r"[\x20-\x7e]*")
_SENTINEL = re.compile(r"=\?base64\?.*\?=")


def header_value(value: str) -> str:
    """``value`` as an HTTP header carries it in the 2026-07-28 binding: verbatim when it is printable ASCII without
    edge spaces, else UTF-8 in base64 between ``=?base64?`` and ``?=``."""
    if _HEADER_SAFE.fullmatch(value) and value == value.strip() and not _SENTINEL.fullmatch(value):
        return value
    return f"=?base64?{base64.b64encode(value.encode()).decode()}?="


def routing_headers(message) -> dict:
    """The headers of the 2026-07-28 HTTP binding for a message carrying that revision's envelope in params._meta;
    empty for any other message."""
    params = message.get("params") if isinstance(message, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    version = meta.get(ENVELOPE_VERSION) if isinstance(meta, dict) else None
    method = message.get("method") if isinstance(message, dict) else None
    if not isinstance(version, str) or not isinstance(method, str):
        return {}
    headers = {PROTOCOL_HEADER: header_value(version), "Mcp-Method": header_value(method)}
    name = params.get(NAMED_BY.get(method, ""))
    if isinstance(name, str):
        headers["Mcp-Name"] = header_value(name)
    return headers


class McpClient:
    """JSON-RPC messages to the /mcp of the hub at ``url``, one POST each, as the holder of ``token``, over the
    process's kept-alive connections (``connections``). With ``run``, ``token`` is a worker's, for the agent of that
    run: the messages carry X-Evo-Run and no X-Evo-Project, since the hub binds the session to the run's project."""

    def __init__(
        self,
        url: str,
        token: str,
        project: str | None,
        sink: str,
        *,
        run: int | None = None,
        timeout: float = TIMEOUT,
        sleep=time.sleep,
        connections=CONNECTIONS,
    ):
        self.url = check_url(url)
        self.token = token
        self.project = project
        self.sink = sink
        self.run = run
        self.timeout = timeout
        self.sleep = sleep
        self.connections = connections

    def __repr__(self) -> str:  # the token stays out of tracebacks
        run = "" if self.run is None else f", run={self.run}"
        return f"McpClient({self.url!r}, project={self.project!r}, sink={self.sink!r}{run})"

    def post(self, message, protocol: str | None = None):
        """The hub's answer to ``message``: a JSON-RPC response, or None when it has none (a notification). Raises
        HubError when the hub gives no MCP answer: Unreachable when no answer came at all."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"evo-agents/{__version__}",
            "Authorization": f"Bearer {self.token}",
            SINK_HEADER: self.sink,
        }
        if self.run is not None:
            headers[RUN_HEADER] = str(self.run)
        elif self.project:
            headers[PROJECT_HEADER] = self.project
        if protocol:
            headers[PROTOCOL_HEADER] = protocol
        headers.update(routing_headers(message))
        data = json.dumps(message, ensure_ascii=False).encode()
        for attempt in range(RETRIES + 1):
            try:
                status, raw = self.connections.send("POST", self.url + MCP_PATH, headers, data, self.timeout)
                break
            except Unreachable as exc:
                if not exc.refused or attempt == RETRIES:  # a request that may have been sent is never sent twice
                    raise
                self.sleep(RETRY_DELAY * 2**attempt)
        return self._answer(status, raw)

    def _answer(self, status: int, raw: bytes):
        try:
            payload = json.loads(raw) if raw.strip() else None
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0" and ({"result", "error"} & payload.keys()):
            return payload  # an MCP answer, whatever the status: the SDK sends its own errors with 4xx
        if status == 202 and payload is None:
            return None
        if 300 <= status < 400:
            raise HubError(f"the hub at {self.url} answered {status} with a redirect, which is not followed", status)
        message = payload.get("message") if isinstance(payload, dict) else None
        code = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), str) else None
        what = f": {message}" if isinstance(message, str) and message else ""
        raise HubError(f"the hub at {self.url} answered HTTP {status}{what}", status, code)

    def call_tool(self, name: str, arguments: dict) -> dict:
        """The result of one tools/call. Raises HubError when the hub gives no result."""
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        answer = self.post(message, PROTOCOL_VERSION)
        result = answer.get("result") if isinstance(answer, dict) else None
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            error = answer.get("error") if isinstance(answer, dict) else None
            reason = error.get("message") if isinstance(error, dict) else "no tool result"
            raise HubError(f"the hub at {self.url} did not answer {name} with a tool result: {reason}")
        return result


def current_run() -> int | None:
    """The run whose agent this process serves (EVO_RUN_ID, a positive integer), or None."""
    value = os.environ.get(RUN_VARIABLE, "").strip()
    return int(value) if value.isascii() and value.isdigit() and int(value) >= 1 else None


def session_credentials() -> Credentials:
    """What a message goes with: inside a run (``current_run``) of this machine's worker, the worker's token for that
    run and the hub the worker joined; else, and when the worker's state holds no token, the machine token of
    ``evo-agents hub login``. Read again for every message, never kept."""
    run = current_run()
    if run is not None:
        home = WorkerHome()
        try:
            config, token = home.load_config(), home.load_token()
        except WorkerStateError:
            pass  # not a worker here: the machine's own credentials
        else:
            return Credentials(config.url, config.owner or config.name, token, run)
    return load_credentials()


def hub_project(project: str | None) -> str | None:
    """The hub project of the session: ``project`` (a name or a harness path) as ``resolve_project`` reads it, else
    the one the working directory belongs to; a name no project here has is taken as the hub's. None when there is
    none."""
    from evo_agents.kg.project import ProjectError, resolve_project

    try:
        return resolve_project(project).name
    except ProjectError:
        return project if project and "/" not in project and project not in (".", "..") else None


class Proxy:
    """The stdio side of ``evo-agents hub mcp`` (see the module)."""

    def __init__(
        self,
        project: str | None,
        sink: str = AGENT_SINK,
        *,
        credentials=session_credentials,
        timeout: float = TIMEOUT,
        workers: int = WORKERS,
        err=None,
        sleep=time.sleep,
        connections=CONNECTIONS,
    ):
        self.project = project
        self.sink = sink
        self.credentials = credentials
        self.timeout = timeout
        self.sleep = sleep
        self.connections = connections
        self.workers = workers
        self.err = err or sys.stderr
        self.protocol: str | None = None  # negotiated by the handshake
        self._out = None
        self._lock = threading.Lock()
        self._failing: str | None = None  # the hub's failure last reported on stderr

    def run(self, stdin, stdout) -> int:
        """Answer the messages of ``stdin`` (bytes, one per line) on ``stdout`` (bytes) until stdin ends."""
        self._out = stdout
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="hub-mcp") as pool:
            for raw in stdin:
                if not raw.strip():
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    self._write({"jsonrpc": "2.0", "id": None, "error": PARSE_FAILED})
                    continue
                if isinstance(message, dict) and message.get("method") == "initialize":
                    self.handle(message)  # the version it settles goes with every message after it
                else:
                    pool.submit(self.handle, message)
        return 0

    def handle(self, message) -> None:
        try:
            reply = self.answer(message)
        except Exception as exc:  # one message never stops the proxy
            reply = self._standing_in(message, f"evo-agents hub mcp failed on this message ({type(exc).__name__})")
        if reply is not None:
            self._write(reply)

    def answer(self, message):
        """The line to write for ``message``: the hub's answer, or the proxy's when the hub gives none."""
        try:
            credentials = self.credentials()
            client = McpClient(
                credentials.url,
                credentials.token,
                self.project,
                self.sink,
                run=credentials.run,
                timeout=self.timeout,
                sleep=self.sleep,
                connections=self.connections,
            )
            reply = client.post(message, self.protocol)
        except HubError as exc:
            self._report(str(exc))
            return self._standing_in(message, str(exc))
        self._report(None, client.url)
        if not isinstance(message, dict):
            return reply  # not a message MCP knows, such as a batch: the hub's refusal goes back as it is
        if "id" not in message:
            return None  # a notification
        if isinstance(reply, dict) and reply.get("id") is None:
            reply = {**reply, "id": message["id"]}  # the SDK refuses some messages before reading their id
        if message.get("method") == "initialize" and isinstance(reply, dict):
            version = (reply.get("result") or {}).get("protocolVersion")
            if isinstance(version, str):
                self.protocol = version
        return reply

    def _standing_in(self, message, reason: str):
        """The proxy's own answer to ``message`` while the hub gives none."""
        if not isinstance(message, dict) or "id" not in message:
            return None
        method = message.get("method")
        if method == "initialize":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            asked = params.get("protocolVersion")
            version = asked if asked in HANDSHAKE_VERSIONS else PROTOCOL_VERSION
            self.protocol = self.protocol or version
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": f"error: {reason}"}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": HUB_ERROR, "message": reason}}
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}

    def _write(self, reply) -> None:
        line = json.dumps(reply, ensure_ascii=False).encode() + b"\n"
        with self._lock:
            self._out.write(line)
            self._out.flush()

    def _report(self, failure: str | None, url: str | None = None) -> None:
        """One stderr line when the hub stops answering, or answers again; nothing while that does not change."""
        with self._lock:
            if failure == self._failing:
                return
            was, self._failing = self._failing, failure
        if failure is not None:
            print(f"evo-agents hub mcp: {failure}; tool calls return this error until it is fixed", file=self.err)
        elif was is not None:
            print(f"evo-agents hub mcp: the hub at {url} answers again", file=self.err)
        self.err.flush()


def cmd_mcp(args) -> int:
    project = hub_project(args.project)
    if project is None and current_run() is None:  # a run's agent gets the run's project from the hub
        print(
            "evo-agents hub mcp: no project for this directory: the kg_* tools need --project, the other tools "
            "a project argument",
            file=sys.stderr,
        )
    return Proxy(project, args.sink).run(sys.stdin.buffer, sys.stdout.buffer)


def register_mcp(hsub) -> None:
    mcp = hsub.add_parser(
        "mcp",
        help="MCP server over stdio for any runtime, answered by the hub's /mcp with this machine's token",
    )
    mcp.add_argument("--project", help="project name or harness path (default: the project of the working directory)")
    mcp.add_argument("--sink", default=AGENT_SINK, help=f"sink whose clearance applies (default: {AGENT_SINK})")
    mcp.set_defaults(func=cmd_mcp)
