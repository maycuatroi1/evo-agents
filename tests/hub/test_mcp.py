"""MCP on the hub (POST /mcp, ``evo_agents.hub.server.mcp``) and the stdio proxy ``evo-agents hub mcp``
(``evo_agents.hub.mcp_proxy``).

Checked here, as step 8 asks: tools/list names exactly the 21 tools, the kg_* ones with the schemas of ``kg serve``;
kg_* answer as ``kg serve`` does on the same store (the graph a machine built is the artifact the hub answers from); a
memory alice writes with memory_write reaches bob's memory_search only when the read rule lets it through; POST /mcp
without the trailing slash answers 200, never a 307; a request without a Bearer token is 401; a Host outside the
allow-list is refused; and ``printf '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\\n' | evo-agents hub mcp`` prints
the 21 tools against ``hub serve``. Also the failure paths around them: a web session cookie, a revoked token, a
foreign Origin, malformed session headers, an oversized body and Postgres away at the gate; an undeclared sink, a
missing project, a reader writing, a stale revision and bad arguments at the tools; and for the proxy, a hub that
does not answer, refuses the token, restarts, or answers too slowly, and credentials that are missing. Neither the
logs nor stderr ever carry a token or what a call held, and nothing is written on the machine.

The proxy tests run against a fake hub (``http.server`` in this process) and need no Postgres; the others run the app
in this process (TestClient), and the CLI ones run ``hub serve`` as a process of its own.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.hub import mcp_tools
from evo_agents.hub.client import Credentials, HubError, NotSignedIn, Unreachable
from evo_agents.hub.mcp_proxy import McpClient, Proxy, hub_project
from evo_agents.kg import serve
from tests.hub import live, pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

PROJECT = "alpha"
AGENT = "claude-code@anthropic"
LEVELS = ["public", "internal", "customer", "secret"]
SINKS = [
    {"id": AGENT, "kind": "agent-session", "clearance": {"level": "customer"}},
    {"id": "low", "kind": "agent-session", "clearance": {"level": "public"}},
    {"id": "hub", "kind": "hub", "clearance": {"level": "customer"}},
]
REGISTRATION = {
    "levels": LEVELS,
    "locations": ["any"],
    "default_label": {"level": "internal"},
    "sinks": SINKS,
    "repos": [{"name": "app", "path": "app", "origin": "https://git.example.org/alpha/app.git"}],  # runs need it
    "harness": {"name": PROJECT, "workspace": "~/ws", "path": "alpha-harness"},
}
MEMBERS = {
    "alice": ("writer", "internal"),
    "bob": ("reader", "internal"),
    "carol": ("reader", "public"),
    "dora": ("reader", "customer"),
}
KNOWLEDGE = """\
version: 1
project: alpha
policy:
  levels: [public, internal, customer, secret]
  sinks:
    - {{id: claude-code@anthropic, kind: agent-session, clearance: {{level: customer}}}}
    - {{id: low, kind: agent-session, clearance: {{level: public}}}}
    - {{id: hub, kind: hub, clearance: {{level: customer}}}}
identifiers:
  - {{kind: Requirement, pattern: "KB-[0-9]+"}}
sources:
  - id: docs
    connector: "python:tests.kg.fakes:run"
    config: {{file: "{docs}"}}
    label: {{level: internal, integrity: U}}
  - id: deals
    connector: "python:tests.kg.fakes:run"
    config: {{file: "{deals}"}}
    label: {{level: customer, integrity: U}}
"""
SECRET = "Okapi-Marmoset-Memory-Text"  # in memory bodies and plan evidence only: never in a log line or on stderr
TOOLS_LIST = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
HUB_TOOL_NAMES = [
    "memory_search",
    "memory_get",
    "memory_write",
    "plan_list",
    "plan_show",
    "plan_step",
    "skill_list",
    "hub_projects",
    "run_tool_stats",
    "curator_figures",
    "digest_list",
    "digest_show",
    "run_events",
    "decision_list",
]


def doc(key: str, body: str) -> dict:
    return {"key": key, "text": f"# {key}\n\n{body}\n\n## Details of {key}\n\nSee KB-01 in {key}.\n", "rev": "1"}


# The tool list


def test_the_endpoint_has_twenty_one_tools_and_the_kg_ones_are_those_of_kg_serve():
    names = [tool["name"] for tool in mcp_tools.TOOLS]
    assert len(names) == len(set(names)) == 21
    assert names == [tool["name"] for tool in serve.TOOLS] + HUB_TOOL_NAMES
    assert mcp_tools.TOOLS[:7] == serve.TOOLS  # same objects: names and schemas cannot drift
    for tool in mcp_tools.HUB_TOOLS + mcp_tools.REVIEW_TOOLS:
        assert tool["inputSchema"]["type"] == "object" and tool["inputSchema"]["additionalProperties"] is False
        assert tool["description"] and tool["name"] in mcp_tools.SCHEMAS


def test_names_follow_the_hub_s_patterns():
    pytest.importorskip("fastapi")
    from evo_agents.hub.server.admin import PROJECT_NAME
    from evo_agents.hub.server.plans import PLAN_ID

    assert mcp_tools.NAME_PATTERN == PROJECT_NAME == PLAN_ID


# The proxy, against a fake hub


class FakeHub:
    """A hub on a local port whose /mcp answers with ``reply(message, headers) -> (status, body)``; the requests it
    received, with their headers. With ``keep_alive`` it speaks HTTP/1.1 and keeps connections open, as uvicorn does;
    ``connections`` counts those it accepted. ``reply`` may return CLOSE_IDLE with its answer to close the connection
    once the answer is sent, without saying so, as a server does when a connection was idle too long, or HANG_UP alone
    to close it without answering."""

    def __init__(self, reply, port: int | None = None, delay: float = 0.0, keep_alive: bool = False):
        self.reply = reply
        self.delay = delay
        self.requests: list[tuple[dict, object]] = []
        self.connections = 0
        self.sockets: list[socket.socket] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if keep_alive else "HTTP/1.0"

            def setup(self):
                fake.connections += 1
                fake.sockets.append(self.request)
                super().setup()

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                message = json.loads(raw)
                fake.requests.append(({k.lower(): v for k, v in self.headers.items()}, message))
                time.sleep(fake.delay)
                answer = fake.reply(message, self.headers)
                if answer is HANG_UP:
                    self.close_connection = True
                    return
                close = len(answer) == 3 and answer[2] is CLOSE_IDLE
                status, body = answer[:2]
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                if close:
                    self.close_connection = True

            def log_message(self, *args):
                pass

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # a client that gave up on a slow answer closed the connection: expected here

        self.server = Server(("127.0.0.1", port or 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        """Stop as a process does: the open connections go with it."""
        self.server.shutdown()
        self.server.server_close()
        for sock in self.sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()


HANG_UP = object()
CLOSE_IDLE = object()


def mcp_reply(message, headers):
    """What the SDK answers: a result per request, 202 and no body for a notification."""
    if "id" not in message:
        return 202, None
    method = message["method"]
    if method == "initialize":
        return 200, {"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2025-06-18"}}
    if method == "tools/call":
        result = {"content": [{"type": "text", "text": f"called {message['params']['name']}"}], "isError": False}
        return 200, {"jsonrpc": "2.0", "id": message["id"], "result": result}
    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": [{"name": "from-the-hub"}]}}
    return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "Method not found"}}


TOKEN = "evh_" + "T" * 43
HELLO = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}


def signed_in(url: str):
    return lambda: Credentials(url, "alice", TOKEN)


def run_proxy(proxy: Proxy, *messages) -> list[dict]:
    lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
    out = io.BytesIO()
    assert proxy.run(io.BytesIO("".join(f"{line}\n" for line in lines).encode()), out) == 0
    return [json.loads(line) for line in out.getvalue().decode().splitlines()]


def by_id(replies: list[dict]) -> dict:
    return {reply["id"]: reply for reply in replies}


def request(id_, method, params=None) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, **({"params": params} if params is not None else {})}


def call(id_, tool, arguments=None) -> dict:
    return request(id_, "tools/call", {"name": tool, "arguments": arguments or {}})


def test_the_proxy_streams_every_message_to_the_hub_with_the_session_headers():
    hub = FakeHub(mcp_reply)
    err = io.StringIO()
    try:
        proxy = Proxy("alpha", "low", credentials=signed_in(hub.url), err=err)
        replies = run_proxy(
            proxy,
            request(1, "initialize", {"protocolVersion": "2025-06-18"}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            request(2, "tools/list"),
            call(3, "kg_status"),
            request(4, "nope"),
            "{not json",
        )
    finally:
        hub.stop()
    assert len(replies) == 5  # the notification gets no line
    answered = by_id(replies)
    assert answered[1]["result"] == {"protocolVersion": "2025-06-18"}
    assert answered[2]["result"] == {"tools": [{"name": "from-the-hub"}]}  # as the hub wrote it
    assert answered[3]["result"]["content"][0]["text"] == "called kg_status"
    assert answered[4]["error"]["code"] == -32601  # the hub's refusal, given the id it could not read
    assert answered[None]["error"] == {"code": -32700, "message": "parse error"}
    assert len(hub.requests) == 5
    for headers, message in hub.requests:
        assert headers["authorization"] == f"Bearer {TOKEN}"
        assert headers["x-evo-project"] == "alpha" and headers["x-evo-sink"] == "low"
        assert headers["content-type"] == "application/json" and "application/json" in headers["accept"]
        if message.get("method") != "initialize":  # sent after the handshake settled it
            assert headers["mcp-protocol-version"] == "2025-06-18"
        else:
            assert "mcp-protocol-version" not in headers
    assert err.getvalue() == ""


MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}


def test_a_message_of_the_2026_revision_gets_the_headers_of_its_http_binding():
    hub = FakeHub(mcp_reply)
    try:
        message = {**call(1, "kg_search", {"query": "x"}), "params": {"name": "kg_search", "_meta": MODERN}}
        run_proxy(
            Proxy("alpha", credentials=signed_in(hub.url), err=io.StringIO()),
            request(1, "initialize", HELLO),
            message,
            {**call(2, "naïve tool"), "params": {"name": "naïve tool", "_meta": MODERN}},
            call(3, "kg_status"),
        )
    finally:
        hub.stop()
    sent = {m.get("id"): h for h, m in hub.requests}
    assert sent[1]["mcp-protocol-version"] == "2026-07-28" and sent[1]["mcp-method"] == "tools/call"
    assert sent[1]["mcp-name"] == "kg_search"
    assert sent[2]["mcp-name"] == "=?base64?bmHDr3ZlIHRvb2w=?="  # not ASCII: base64, as the binding encodes it
    assert sent[3]["mcp-protocol-version"] == "2025-06-18" and "mcp-method" not in sent[3]  # a handshake-era call


def test_while_the_hub_does_not_answer_the_proxy_answers_for_it_and_tools_name_the_hub():
    port = pg.free_port()
    url = f"http://127.0.0.1:{port}"
    err = io.StringIO()
    proxy = Proxy("alpha", credentials=signed_in(url), err=err, sleep=lambda seconds: None)
    replies = by_id(
        run_proxy(
            proxy,
            request(1, "initialize", {"protocolVersion": "2025-06-18"}),
            request(2, "tools/list"),
            request(3, "ping"),
            call(4, "memory_search", {"query": SECRET}),
            request(5, "resources/list"),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
    )
    assert replies[1]["result"]["serverInfo"]["name"] == "evo-hub"
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert [t["name"] for t in replies[2]["result"]["tools"]] == [t["name"] for t in mcp_tools.TOOLS]
    assert replies[3]["result"] == {}
    failed = replies[4]["result"]
    assert failed["isError"] and url in failed["content"][0]["text"] and len(failed["content"][0]["text"]) < 200
    assert replies[5]["error"]["code"] == -32000 and url in replies[5]["error"]["message"]
    assert len(replies) == 5
    report = err.getvalue()
    assert report.count("\n") == 1 and url in report  # one line for the outage, not one per message
    assert TOKEN not in report and SECRET not in report


def test_a_hub_that_restarts_is_reached_again_and_says_so_once():
    port = pg.free_port()
    url = f"http://127.0.0.1:{port}"
    err = io.StringIO()
    proxy = Proxy("alpha", credentials=signed_in(url), err=err, sleep=lambda seconds: None)
    down = run_proxy(proxy, call(1, "kg_status"))
    assert down[0]["result"]["isError"]
    hub = FakeHub(mcp_reply, port=port)
    try:
        up = run_proxy(proxy, call(2, "kg_status"))
    finally:
        hub.stop()
    assert up[0]["result"]["content"][0]["text"] == "called kg_status"
    lines = err.getvalue().splitlines()
    assert len(lines) == 2 and lines[1] == f"evo-agents hub mcp: the hub at {url} answers again"


def test_a_refused_connection_is_tried_again_and_a_request_that_may_have_been_sent_never_twice():
    port = pg.free_port()
    started = []

    def start_meanwhile(seconds):  # the hub comes up while the client waits to retry
        started.append(FakeHub(mcp_reply, port=port))

    client = McpClient(f"http://127.0.0.1:{port}", TOKEN, "alpha", AGENT, sleep=start_meanwhile)
    try:
        assert client.call_tool("kg_status", {})["content"][0]["text"] == "called kg_status"
        assert len(started) == 1 and len(started[0].requests) == 1
    finally:
        for hub in started:
            hub.stop()

    slow = FakeHub(mcp_reply, delay=1.0)
    try:
        client = McpClient(slow.url, TOKEN, "alpha", AGENT, timeout=0.2, sleep=lambda s: pytest.fail("retried"))
        with pytest.raises(Unreachable) as caught:
            client.call_tool("memory_write", {"name": "x.md", "body": SECRET})
        assert not caught.value.refused and slow.url in str(caught.value)
        time.sleep(1.2)
        assert len(slow.requests) == 1
    finally:
        slow.stop()

    never = McpClient(f"http://127.0.0.1:{pg.free_port()}", TOKEN, None, AGENT, sleep=lambda s: None)
    with pytest.raises(Unreachable) as caught:
        never.post(request(1, "ping"))
    assert caught.value.refused


def test_the_proxy_sends_every_message_over_one_kept_alive_connection():
    from evo_agents.hub.client import Connections

    hub = FakeHub(mcp_reply, keep_alive=True)
    connections = Connections()
    try:
        client = McpClient(hub.url, TOKEN, "alpha", AGENT, connections=connections)
        for _ in range(20):
            assert client.call_tool("kg_search", {"query": "x"})["content"][0]["text"] == "called kg_search"
        assert hub.connections == 1 and connections.opened == 1 and len(hub.requests) == 20
        proxy = Proxy("alpha", credentials=signed_in(hub.url), err=io.StringIO(), workers=1, connections=connections)
        replies = run_proxy(proxy, request(1, "initialize", HELLO), *(call(i, "kg_status") for i in range(2, 12)))
        assert len(replies) == 11 and all("result" in reply for reply in replies)
        assert hub.connections == 1 and len(hub.requests) == 31  # a proxy carries on over the same connection
        for headers, _ in hub.requests:
            assert headers["authorization"] == f"Bearer {TOKEN}" and headers["x-evo-project"] == "alpha"
    finally:
        connections.close()
        hub.stop()


def wait_for_the_close(connections, timeout: float = 10.0) -> None:
    """Wait until every connection idle in ``connections`` shows that the hub closed it, as ``Connections`` checks
    before it reuses one. The close of a loopback peer reaches this side a moment after the hub's close() returned (on
    macOS the kernel hands it over asynchronously, later still on a busy runner), and a request sent in between leaves
    on a connection the hub already closed, which the client must fail rather than send twice."""
    from evo_agents.hub.client import _dropped

    deadline = time.monotonic() + timeout
    while True:
        with connections._lock:
            idle = [conn.sock for held in connections._idle.values() for _, conn in held]
        assert idle, "no kept-alive connection to wait on"
        if all(_dropped(sock) for sock in idle):
            return
        assert time.monotonic() < deadline, f"the hub's close did not reach the client within {timeout:g}s"
        time.sleep(0.01)


def test_a_kept_alive_connection_the_hub_closed_is_replaced_and_no_request_goes_twice():
    from evo_agents.hub.client import Connections

    sent = []

    def close_after_the_second(message, headers):  # then the server closes the connection, as when it was idle
        sent.append(message["id"])
        status, body = mcp_reply(message, headers)
        return (status, body, CLOSE_IDLE) if len(sent) == 2 else (status, body)

    hub = FakeHub(close_after_the_second, keep_alive=True)
    connections = Connections()
    try:
        client = McpClient(hub.url, TOKEN, "alpha", AGENT, connections=connections)
        for id_ in range(1, 6):
            client.post(call(id_, "kg_status"), "2025-06-18")
            if id_ == 2:
                wait_for_the_close(connections)  # the server's close reaches this side
        assert sent == [1, 2, 3, 4, 5]  # every call answered, none sent twice
        assert hub.connections == 2 and connections.opened == 2
    finally:
        hub.stop()

    # the hub restarts on the same port: once its close reaches this side, the next call reaches it on a new connection
    wait_for_the_close(connections)
    restarted = FakeHub(mcp_reply, port=int(hub.url.rsplit(":", 1)[1]), keep_alive=True)
    try:
        assert client.call_tool("kg_status", {})["content"][0]["text"] == "called kg_status"
        assert restarted.connections == 1 and connections.opened == 3
    finally:
        connections.close()
        restarted.stop()

    # a connection the hub drops once it has the request is not sent again: the request may have been carried out
    answered = []

    def hang_up_on_the_second(message, headers):
        answered.append(message["id"])
        return HANG_UP if len(answered) == 2 else mcp_reply(message, headers)

    dropping = FakeHub(hang_up_on_the_second, keep_alive=True)
    try:
        client = McpClient(dropping.url, TOKEN, "alpha", AGENT, connections=Connections(), sleep=lambda s: None)
        client.call_tool("kg_status", {})
        with pytest.raises(Unreachable) as caught:
            client.call_tool("memory_write", {"name": "x.md", "body": SECRET})
        assert not caught.value.refused and dropping.url in str(caught.value)
        assert answered == [1, 1] and len(dropping.requests) == 2
    finally:
        dropping.stop()


def test_a_connection_idle_too_long_is_not_reused():
    from evo_agents.hub.client import Connections

    now = [0.0]
    hub = FakeHub(mcp_reply, keep_alive=True)
    connections = Connections(idle_timeout=30.0, clock=lambda: now[0])
    try:
        client = McpClient(hub.url, TOKEN, "alpha", AGENT, connections=connections)
        client.call_tool("kg_status", {})
        now[0] = 29.0
        client.call_tool("kg_status", {})
        assert connections.opened == 1
        now[0] = 60.0
        client.call_tool("kg_status", {})
        assert connections.opened == 2 and hub.connections == 2
    finally:
        connections.close()
        hub.stop()


def test_refusals_of_the_hub_become_tool_errors_naming_it():
    def refuse(message, headers):
        body = {
            "error": "unauthorized",
            "message": "the token is revoked, expired or unknown: run `evo-agents hub login`",
        }
        return 401, body

    hub = FakeHub(refuse)
    try:
        replies = by_id(
            run_proxy(Proxy("alpha", credentials=signed_in(hub.url), err=io.StringIO()), call(1, "kg_search"))
        )
        client = McpClient(hub.url, TOKEN, "alpha", AGENT)
        with pytest.raises(HubError) as caught:
            client.call_tool("kg_search", {"query": "x"})
    finally:
        hub.stop()
    text = replies[1]["result"]["content"][0]["text"]
    assert text == (
        f"error: the hub at {hub.url} answered HTTP 401: the token is revoked, expired or unknown: run "
        "`evo-agents hub login`"
    )
    assert caught.value.status == 401 and caught.value.code == "unauthorized"


def test_without_credentials_the_session_starts_and_every_tool_says_how_to_sign_in():
    def nobody():
        raise NotSignedIn("not signed in to a hub: run `evo-agents hub login --url URL`")

    replies = by_id(
        run_proxy(
            Proxy(None, credentials=nobody, err=io.StringIO()),
            request(1, "initialize", {"protocolVersion": "2099-01-01"}),
            call(2, "plan_list"),
        )
    )
    assert replies[1]["result"]["protocolVersion"] == mcp_tools.PROTOCOL_VERSION  # one it does not know: the latest
    assert "evo-agents hub login" in replies[2]["result"]["content"][0]["text"]


def test_the_session_project_comes_from_the_flag_or_the_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("EVO_KG_PROJECT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("EVO_KG_HOME", str(tmp_path / "kg-home"))
    harness = tmp_path / "alpha-harness"
    (harness / "src").mkdir(parents=True)
    (harness / "harness.yaml").write_text("name: alpha\nrepos: []\nknowledge_file: knowledge.yaml\n")
    (harness / "knowledge.yaml").write_text(
        "version: 1\nproject: alpha\npolicy:\n  levels: [public, internal]\n  sinks: []\nsources: []\n"
    )
    monkeypatch.chdir(harness / "src")
    assert hub_project(None) == "alpha"
    assert hub_project(str(harness)) == "alpha"
    assert hub_project("beta") == "beta"  # a project only the hub knows, by name
    monkeypatch.chdir(tmp_path)
    assert hub_project(None) is None and hub_project("./nowhere") is None


# The endpoint, in this process


@pytest.fixture
def hub(hub_db, tmp_path, monkeypatch):
    """The app on ``hub_db`` behind https://hub.test, with project alpha registered, its members and their Bearer
    headers, and a graph of alpha built on a machine (``laptop``), installed as the latest successful build."""
    from fastapi.testclient import TestClient

    from evo_agents.hub.config import HubConfig
    from evo_agents.hub.server.app import create_app

    config = HubConfig(
        dsn=hub_db.dsn,
        data_dir=tmp_path / "cache",
        pool_min_size=1,
        pool_max_size=4,
        pool_timeout=3.0,
        admins=frozenset({live.ADMIN}),
        public_url="https://hub.test",
    )
    app = create_app(config)
    with TestClient(app, base_url="https://hub.test") as client:
        admin = live.bearer(live.insert_token(hub_db, live.ADMIN))
        assert client.put(f"/v1/projects/{PROJECT}", json=REGISTRATION, headers=admin).status_code == 200
        members = {"admin": admin, "eve": live.bearer(live.insert_token(hub_db, "eve"))}
        for login, (role, level) in MEMBERS.items():
            grant = {"role": role, "max_level": level}
            response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=admin)
            assert response.status_code == 200, response.text
            members[login] = live.bearer(live.insert_token(hub_db, login))
        laptop = build_graph_on_a_machine(tmp_path / "laptop", monkeypatch)
        install_graph(hub_db, tmp_path / "cache", laptop)
        yield SimpleNamespace(client=client, db=hub_db, app=app, laptop=laptop, **members)


def build_graph_on_a_machine(root: Path, monkeypatch) -> SimpleNamespace:
    from evo_agents.hub.kg_build import build_graph
    from evo_agents.kg.project import load_project_at
    from evo_agents.kg.sync import sync_project

    harness, home = root / "alpha-harness", root / "kg-home"
    harness.mkdir(parents=True)
    files = {source: root / f"{source}.json" for source in ("docs", "deals")}
    files["docs"].write_text(json.dumps({"items": [doc("guide", "How the app works."), doc("setup", "Install it.")]}))
    files["deals"].write_text(json.dumps({"items": [doc("acme", "The Acme deal.")]}))
    (harness / "harness.yaml").write_text("name: alpha\nrepos: []\nknowledge_file: knowledge.yaml\n")
    (harness / "knowledge.yaml").write_text(KNOWLEDGE.format(docs=files["docs"], deals=files["deals"]))
    monkeypatch.setenv("EVO_KG_HOME", str(home))
    project = load_project_at(harness, home)
    assert all(result.ok for result in sync_project(project, ["docs", "deals"]))
    path, report = build_graph(project)
    data = path.read_bytes()
    return SimpleNamespace(
        project=project, path=path, data=data, sha256=hashlib.sha256(data).hexdigest(), report=report
    )


def install_graph(db, cache: Path, laptop) -> None:
    """The laptop's graph as the hub's latest successful build of alpha, in the api's cache already."""
    target = cache / "kg" / "graphs" / PROJECT / f"{laptop.sha256}.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(laptop.data)
    from sqlalchemy import func, insert, literal, select

    from evo_agents.hub import tables

    report, projects = laptop.report, tables.projects
    build = select(
        projects.c.id,
        literal("succeeded"),
        literal(laptop.sha256),
        literal(len(laptop.data)),
        literal(report.content_hash),
        literal(report.nodes),
        literal(report.edges),
        func.now(),
        func.now(),
    ).where(projects.c.name == PROJECT)
    columns = ["project_id", "status", "artifact_sha256", "artifact_size", "content_hash", "nodes", "edges"]
    live.sql(db, insert(tables.kg_builds).from_select([*columns, "started_at", "finished_at"], build))


MCP_HEADERS = {"Accept": "application/json", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"}


def rpc(hub, member: str, message: dict, *, project=PROJECT, sink=None, path="/mcp", headers=None):
    sent = {**getattr(hub, member), **MCP_HEADERS, **(headers or {})}
    if project:
        sent["X-Evo-Project"] = project
    if sink:
        sent["X-Evo-Sink"] = sink
    return hub.client.post(path, json=message, headers=sent, follow_redirects=False)


def tool(hub, member: str, name: str, arguments: dict | None = None, **options) -> dict:
    response = rpc(hub, member, call(7, name, arguments), **options)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["id"] == 7, answer
    return answer["result"]


def ok(hub, member: str, name: str, arguments: dict | None = None, **options) -> dict:
    result = tool(hub, member, name, arguments, **options)
    assert not result.get("isError"), result
    return result


def failed(hub, member: str, name: str, arguments: dict | None = None, **options) -> str:
    result = tool(hub, member, name, arguments, **options)
    assert result["isError"] is True, result
    return result["content"][0]["text"]


@needs_pg
def test_tools_list_names_the_twenty_one_tools_and_initialize_names_the_hub(hub):
    listed = rpc(hub, "alice", request(1, "tools/list"))
    assert listed.status_code == 200 and listed.headers["content-type"].startswith("application/json")
    tools = listed.json()["result"]["tools"]
    assert len(tools) == 21
    assert [t["name"] for t in tools] == [t["name"] for t in mcp_tools.TOOLS]
    assert {t["name"]: t["inputSchema"] for t in tools[:7]} == {t["name"]: t["inputSchema"] for t in serve.TOOLS}
    assert all(t["description"] == m["description"] for t, m in zip(tools, mcp_tools.TOOLS, strict=True))
    hello = rpc(hub, "alice", request(2, "initialize", HELLO))
    result = hello.json()["result"]
    assert result["serverInfo"]["name"] == "evo-hub" and result["protocolVersion"] == "2025-06-18"
    assert "memory_search" in result["instructions"] and result["capabilities"]["tools"]
    assert rpc(hub, "alice", request(3, "ping")).json()["result"] == {}


@needs_pg
def test_post_mcp_without_the_slash_answers_in_place_and_other_paths_and_methods_are_refused(hub):
    for path in ("/mcp", "/mcp/"):
        response = rpc(hub, "alice", request(1, "tools/list"), path=path)
        assert response.status_code == 200, (path, response.status_code, response.headers.get("location"))
        assert len(response.json()["result"]["tools"]) == 21
    got = hub.client.get("/mcp", headers=hub.alice, follow_redirects=False)
    assert got.status_code == 405 and got.headers["allow"] == "POST" and got.json()["error"] == "method_not_allowed"
    assert hub.client.delete("/mcp/", headers=hub.alice).status_code == 405
    other = rpc(hub, "alice", request(1, "tools/list"), path="/mcp/other")
    assert other.status_code == 404 and other.json()["error"] == "not_found" and other.json()["request_id"]


@needs_pg
def test_mcp_needs_a_live_machine_token_as_bearer(hub):
    message = request(1, "tools/list")
    body = json.dumps(message)
    bare = hub.client.post("/mcp", content=body, headers=MCP_HEADERS)
    assert bare.status_code == 401 and bare.headers["www-authenticate"].startswith("Bearer")
    assert bare.json()["error"] == "unauthorized" and "evo-agents hub login" in bare.json()["message"]
    session = live.insert_token(hub.db, "alice", kind="web")
    cookie = hub.client.post("/mcp", content=body, headers={**MCP_HEADERS, "Cookie": f"evo_hub_session={session}"})
    assert cookie.status_code == 401  # the web session is for the web, never for MCP
    as_bearer = hub.client.post("/mcp", content=body, headers={**MCP_HEADERS, "Authorization": f"Bearer {session}"})
    assert as_bearer.status_code == 401
    basic = hub.client.post("/mcp", content=body, headers={**MCP_HEADERS, "Authorization": "Basic YWxpY2U6eA=="})
    assert basic.status_code == 401
    from sqlalchemy import func, update

    from evo_agents.hub import tables

    token = hub.alice["Authorization"].removeprefix("Bearer ")
    tokens = tables.tokens
    revoked = update(tokens).values(revoked_at=func.now())
    live.sql(hub.db, revoked.where(tokens.c.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    assert rpc(hub, "alice", message).status_code == 401
    assert rpc(hub, "bob", message).status_code == 200


@needs_pg
def test_a_host_or_origin_outside_the_allow_list_is_refused_and_malformed_headers_are_400(hub):
    message = request(1, "tools/list")
    foreign = rpc(hub, "alice", message, headers={"Host": "evil.test"})
    assert foreign.status_code == 421 and foreign.json()["error"] == "misdirected"
    for host in ("hub.test", "hub.test:443", "127.0.0.1:8123", "localhost:9000"):
        assert rpc(hub, "alice", message, headers={"Host": host}).status_code == 200, host
    assert rpc(hub, "alice", message, headers={"Host": "hub.test.evil.test"}).status_code == 421
    origin = rpc(hub, "alice", message, headers={"Origin": "https://evil.test"})
    assert origin.status_code == 403 and origin.json()["error"] == "forbidden"
    assert rpc(hub, "alice", message, headers={"Origin": "https://hub.test"}).status_code == 200
    assert rpc(hub, "alice", message, project="Not A Project").status_code == 400
    assert rpc(hub, "alice", message, sink="s" * 101).status_code == 400
    huge = {**MCP_HEADERS, **hub.alice}
    too_large = hub.client.post("/mcp", content=b" " * (4 * 1024 * 1024 + 1), headers=huge)
    assert too_large.status_code == 413 and too_large.json()["error"] == "too_large"

    from evo_agents.hub.server.mcp import allowed as host_allowed
    from evo_agents.hub.server.mcp import transport_security

    security = transport_security(hub.app.state.config)
    assert host_allowed("hub.test", security.allowed_hosts) and not host_allowed("evil.test", security.allowed_hosts)


@needs_pg
def test_a_request_of_the_2026_revision_reaches_the_tools_too(hub):
    from evo_agents.hub.mcp_proxy import routing_headers

    message = {**call(9, "hub_projects"), "params": {"name": "hub_projects", "arguments": {}, "_meta": MODERN}}
    response = rpc(hub, "alice", message, headers=routing_headers(message))
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["content"][0]["text"].startswith("alpha (this session): role writer")
    listed = {**request(10, "tools/list"), "params": {"_meta": MODERN}}
    tools = rpc(hub, "alice", listed, headers=routing_headers(listed)).json()["result"]["tools"]
    assert [t["name"] for t in tools] == [t["name"] for t in mcp_tools.TOOLS]


@needs_pg
def test_postgres_away_at_the_gate_is_503_and_back_again_200(hub):
    pg.set_reachable(hub.db, False)
    try:
        down = rpc(hub, "alice", request(1, "tools/list"))
    finally:
        pg.set_reachable(hub.db, True)
    assert down.status_code == 503 and down.json()["error"] == "unavailable"
    for _ in range(20):  # the pool reconnects on its own
        if rpc(hub, "alice", request(2, "tools/list")).status_code == 200:
            break
        time.sleep(0.25)
    else:
        pytest.fail("/mcp did not recover after Postgres came back")


# kg_*


def plain(result: dict) -> dict:
    """A tool result as JSON carries it, its isError flag spelled out: MCP adds false where kg serve leaves it out."""
    data = json.loads(json.dumps(result))
    data["isError"] = bool(data.get("isError"))
    return data


@needs_pg
def test_kg_tools_answer_as_kg_serve_does_on_the_same_store(hub):
    from evo_agents.kg.serve import Session

    local = Session(hub.laptop.project, AGENT)  # the machine's store: the file the hub answers from

    def same(name: str, arguments: dict) -> dict:
        here = plain(local.call(name, dict(arguments)))
        there = plain(tool(hub, "dora", name, arguments))  # dora's grant reaches customer, as the sink does
        assert there == here, name
        return there

    found = same("kg_search", {"query": "guide"})
    first = found["structuredContent"]["results"][0]["id"]
    assert found["structuredContent"]["project"] == PROJECT
    same("kg_search", {"query": "acme", "limit": 5})
    same("kg_node", {"id": first})
    same("kg_context", {"ids": [first]})
    same("kg_context", {"query": "guide", "hops": 1})
    same("kg_path", {"from": first})
    same("kg_impact", {"ids": [first]})
    same("kg_node", {"id": "no-such-node"})  # an error, the same one
    same("kg_search", {"query": "x", "unknown": 1})  # bad arguments, the same message
    status = ok(hub, "dora", "kg_status")["structuredContent"]
    report = hub.laptop.report
    assert status["backend"] == "hub"
    assert (status["graph"]["nodes"], status["graph"]["edges"]) == (report.nodes, report.edges)
    assert status["hub_build"]["content_hash"] == report.content_hash


@needs_pg
def test_kg_tools_read_through_the_grant_and_the_session_sink(hub):
    def names(member: str, query: str, **options) -> list[str]:
        result = ok(hub, member, "kg_search", {"query": query}, **options)
        return [node["name"] for node in result["structuredContent"]["results"]]

    assert "acme" in names("dora", "acme")
    assert "acme" not in names("alice", "acme") and "guide" in names("alice", "guide")  # alice reaches internal
    assert names("carol", "guide") == []  # carol reaches public: nothing, exactly as if there were nothing
    assert names("dora", "guide", sink="low") == []  # the low sink clears public
    undeclared = failed(hub, "dora", "kg_search", {"query": "guide"}, sink="codex@openai")
    assert "not declared" in undeclared
    assert "has no project" in failed(hub, "dora", "kg_search", {"query": "guide"}, project=None)
    assert "no project alpha that you can see" in failed(hub, "eve", "kg_search", {"query": "guide"})
    assert "needs a grant" in failed(hub, "admin", "kg_search", {"query": "guide"})
    assert failed(hub, "dora", "kg_reveal") == "error: unknown tool 'kg_reveal'"


@needs_pg
def test_results_over_cap_chars_are_cut_and_kg_more_continues_them_for_their_owner(hub, monkeypatch):
    from evo_agents.kg.serve import Session

    monkeypatch.setattr(serve, "CAP_CHARS", 300)
    local = Session(hub.laptop.project, AGENT)
    here = local.call("kg_context", {"query": "guide", "budget_tokens": 4000})
    there = ok(hub, "dora", "kg_context", {"query": "guide", "budget_tokens": 4000})
    handle = there["structuredContent"]["handle"]
    assert there["structuredContent"]["truncated"] and here["structuredContent"]["truncated"]
    assert there["content"][0]["text"] == here["content"][0]["text"].replace(
        here["structuredContent"]["handle"], handle
    )
    assert "unknown or expired handle" in failed(hub, "alice", "kg_more", {"handle": handle})  # not alice's

    def whole(first: dict, more) -> str:
        text, handle = first["content"][0]["text"], first["structuredContent"]["handle"]
        parts = [text[: text.index("\n[truncated:")]]
        while True:
            part = more({"handle": handle})
            body = part["content"][0]["text"]
            parts.append(body.split("\n[more:")[0])
            if not part["structuredContent"]["remaining"]:
                return "".join(parts)

    assert whole(there, lambda args: ok(hub, "dora", "kg_more", args)) == whole(
        here, lambda a: local.call("kg_more", a)
    )
    assert "unknown or expired handle" in failed(hub, "dora", "kg_more", {"handle": handle})  # read to the end

    # the hub's own tools go through the same envelope
    long_body = "---\nname: Long\ndescription: a long one\nmetadata:\n  type: project\n---\n" + "word " * 400
    written = ok(hub, "alice", "memory_write", {"name": "long.md", "body": long_body})
    memory_id = written["structuredContent"]["memory"]["id"]
    cut = ok(hub, "bob", "memory_get", {"id": memory_id})
    assert cut["structuredContent"]["truncated"] and len(cut["content"][0]["text"]) < 400
    text = cut["content"][0]["text"]
    rest = whole(cut, lambda args: ok(hub, "bob", "kg_more", args))
    assert rest.startswith(text[: text.index("\n[truncated:")]) and rest.count("word") == 400


# Memories


@needs_pg
def test_memory_write_from_one_token_reaches_memory_search_of_another_only_as_the_rule_allows(hub):
    def write(member: str, name: str, kind: str, **extra) -> dict:
        body = f"---\nname: {name}\ndescription: about kangaroos\nmetadata:\n  type: {kind}\n---\nkangaroo {SECRET}\n"
        return ok(hub, member, "memory_write", {"name": name, "body": body, **extra})

    shared = write("alice", "shared.md", "project")
    assert "Nothing was written on this machine" in shared["content"][0]["text"]
    memory = shared["structuredContent"]["memory"]
    assert (memory["scope"], memory["project"], memory["location"], memory["type"]) == (
        "project",
        PROJECT,
        "harness",
        "project",
    )
    assert memory["label"]["level"] == "internal" and "body" not in memory
    write("alice", "private.md", "user")
    write("alice", "deal.md", "reference", label={"level": "customer"})
    write("alice", "personal.md", "project", scope="personal", location="notes")

    def found(member: str, **options) -> list[str]:
        result = ok(hub, member, "memory_search", {"query": "kangaroo"}, **options)
        return sorted(item["name"] for item in result["structuredContent"]["results"])

    assert found("alice") == ["private.md", "shared.md"]  # her own user memory; the customer one is above her grant
    assert found("bob") == ["shared.md"]  # not alice's user memory, not the customer one
    assert found("dora") == ["deal.md", "shared.md"]
    assert found("carol") == []  # her grant reaches public
    assert found("bob", sink="low") == []  # the low sink clears public
    assert "not declared" in failed(hub, "bob", "memory_search", {"query": "kangaroo"}, sink="codex@openai")
    assert "no project alpha that you can see" in failed(hub, "eve", "memory_search", {"query": "kangaroo"})
    personal = ok(hub, "alice", "memory_search", {"query": "kangaroo", "scope": "personal"})
    assert [item["name"] for item in personal["structuredContent"]["results"]] == ["personal.md"]
    assert (
        ok(hub, "bob", "memory_search", {"query": "kangaroo", "scope": "personal"})["structuredContent"]["results"]
        == []
    )
    hit = ok(hub, "bob", "memory_search", {"query": "kangaroo"})
    assert "[" in hit["content"][0]["text"] and "about kangaroos" in hit["content"][0]["text"]

    got = ok(hub, "bob", "memory_get", {"id": memory["id"]})
    assert got["content"][0]["text"].endswith(f"kangaroo {SECRET}\n")
    from sqlalchemy import select

    from evo_agents.hub import tables

    memories = tables.memories
    private_id = live.sql(hub.db, select(memories.c.id).where(memories.c.name == "private.md"))[0][0]
    hidden = failed(hub, "bob", "memory_get", {"id": private_id})
    missing = failed(hub, "bob", "memory_get", {"id": 999999})
    assert hidden.replace(str(private_id), "N") == missing.replace("999999", "N")  # as if it did not exist


@needs_pg
def test_memory_write_refuses_what_the_write_rule_and_revisions_refuse_and_writes_nothing(hub):
    from sqlalchemy import func, select

    from evo_agents.hub import tables

    revisions = select(func.count()).select_from(tables.memory_revisions)
    before = live.sql(hub.db, revisions)[0][0]
    body = "---\nname: n\n---\nsome text\n"
    assert "writer role" in failed(hub, "bob", "memory_write", {"name": "n.md", "body": body})
    above = failed(hub, "alice", "memory_write", {"name": "n.md", "body": body, "label": {"level": "secret"}})
    assert "not cleared by hub sink" in above
    assert "ending in .md" in failed(hub, "alice", "memory_write", {"name": "notes.txt", "body": body})
    assert "has no project" in failed(hub, "alice", "memory_write", {"name": "n.md", "body": body}, project=None)
    assert "needs its location" in failed(
        hub, "alice", "memory_write", {"name": "n.md", "body": body, "scope": "personal"}
    )
    assert "bad arguments for memory_write" in failed(hub, "alice", "memory_write", {"name": "n.md"})
    assert "unknown key" in failed(hub, "alice", "memory_write", {"name": "n.md", "body": body, "path": "/etc"})
    assert live.sql(hub.db, revisions)[0][0] == before

    first = ok(hub, "alice", "memory_write", {"name": "n.md", "body": body})["structuredContent"]["memory"]
    again = failed(hub, "alice", "memory_write", {"name": "n.md", "body": body + "more\n"})
    assert "exists on the hub at revision 1" in again and "The hub holds revision 1" in again
    stale = failed(hub, "alice", "memory_write", {"name": "n.md", "body": body + "x\n", "if_revision": 7})
    assert "not 7" in stale
    changed = ok(hub, "alice", "memory_write", {"name": "n.md", "body": body + "x\n", "if_revision": 1})
    assert changed["structuredContent"]["memory"]["revision"] == 2 == first["revision"] + 1
    same = ok(hub, "alice", "memory_write", {"name": "n.md", "body": body + "x\n"})
    assert "unchanged" in same["content"][0]["text"]


# Plans


def plan_body(plan_id: str = "rollout") -> dict:
    return {
        "id": plan_id,
        "goal": "Ship the rollout.",
        "repos": [{"repo": "app", "branch": "main", "status": "pending"}],
        "steps": [
            {"id": n, "title": f"Step {n}", "repo": "app", "what": f"do part {n}", "status": "pending"}
            for n in range(1, 4)
        ],
    }


@needs_pg
def test_plan_tools_list_show_and_mark_steps_on_the_hub(hub, monkeypatch):
    put = hub.client.put(f"/v1/projects/{PROJECT}/plans/rollout", json={"body": plan_body()}, headers=hub.alice)
    assert put.status_code in (200, 201), put.text

    listed = ok(hub, "bob", "plan_list")
    assert listed["content"][0]["text"] == "rollout (active, revision 1): -; 0/3 done"
    shown = ok(hub, "bob", "plan_show", {"plan_id": "rollout"})
    assert shown["content"][0]["text"].startswith("# Mirror of evo-agents hub plan rollout, revision 1.")
    assert shown["structuredContent"]["body"]["id"] == "rollout"
    step = ok(hub, "bob", "plan_show", {"plan_id": "rollout", "step": 2})
    assert step["structuredContent"]["step"]["title"] == "Step 2" and "what: do part 2" in step["content"][0]["text"]
    assert "no step" in failed(hub, "bob", "plan_show", {"plan_id": "rollout", "step": 9}).lower()
    assert "no plan nope" in failed(hub, "bob", "plan_show", {"plan_id": "nope"})

    marked = ok(hub, "alice", "plan_step", {"plan_id": "rollout", "step": 1, "status": "done", "evidence": SECRET})
    assert "is now done on the hub: revision 2" in marked["content"][0]["text"]
    assert "Nothing was written on this machine" in marked["content"][0]["text"]
    assert marked["structuredContent"]["step"]["evidence"] == SECRET and marked["structuredContent"]["step"]["done_at"]
    again = ok(hub, "alice", "plan_step", {"plan_id": "rollout", "step": 1, "status": "done", "evidence": SECRET})
    assert "was already done" in again["content"][0]["text"] and not again["structuredContent"]["changed"]
    from sqlalchemy import select

    from evo_agents.hub import tables

    trail, projects = tables.audit, tables.projects
    audit = live.sql(
        hub.db,
        select(trail.c.action, trail.c.target, projects.c.name)
        .join_from(trail, projects, projects.c.id == trail.c.project_id, isouter=True)
        .where(trail.c.action == "plan.patch"),
    )
    assert audit == [("plan.patch", f"{PROJECT}/rollout@2", PROJECT)]  # filed under the plan's project

    assert "writer role" in failed(hub, "bob", "plan_step", {"plan_id": "rollout", "step": 2, "status": "done"})
    stale = failed(hub, "alice", "plan_step", {"plan_id": "rollout", "step": 2, "status": "done", "if_revision": 1})
    assert "revision 2 on the hub, not 1" in stale
    assert "done_at goes with" in failed(
        hub, "alice", "plan_step", {"plan_id": "rollout", "step": 2, "status": "blocked", "done_at": "2026-10-04"}
    )
    assert "bad arguments" in failed(hub, "alice", "plan_step", {"plan_id": "rollout", "step": 2, "status": "gone"})

    # another write lands between the read and the patch: retried on the new revision, nothing lost
    from evo_agents.hub.server import mcp as hub_mcp

    real_show, reads = hub_mcp.plans.show, []

    async def stale_first(request, project, plan_id, user, sink):
        found = await real_show(request, project, plan_id, user, sink)
        reads.append(found["revision"])
        if len(reads) == 1:  # another writer, between this read and the patch
            other = hub_mcp.plans.PlanPatch(
                section="steps", step=3, updates={"note": "by another writer"}, if_revision=2
            )
            written = await hub_mcp.plans.patch(request, project, plan_id, other, user)
            assert written.revision == 3
        return found

    monkeypatch.setattr(hub_mcp.plans, "show", stale_first)
    retried = ok(hub, "alice", "plan_step", {"plan_id": "rollout", "step": 2, "status": "in_progress"})
    assert reads == [2, 3] and retried["structuredContent"]["revision"] == 4
    body = hub.client.get(f"/v1/projects/{PROJECT}/plans/rollout", headers=hub.alice).json()["body"]
    assert body["steps"][1]["status"] == "in_progress" and body["steps"][2]["note"] == "by another writer"


# Skills, projects, arguments


@needs_pg
def test_skill_list_and_hub_projects_show_what_the_caller_may_see(hub):
    projects = ok(hub, "alice", "hub_projects")
    assert projects["content"][0]["text"].startswith("alpha (this session): role writer, max level internal; sinks ")
    (alpha,) = projects["structuredContent"]["projects"]
    assert alpha["repos"] == ["app"] and [s["id"] for s in alpha["sinks"]] == sorted(s["id"] for s in SINKS)
    assert ok(hub, "eve", "hub_projects")["content"][0]["text"] == "you hold no grant on a project of this hub"
    assert ok(hub, "alice", "skill_list")["structuredContent"]["skills"] == []
    assert "for its members" in failed(hub, "eve", "skill_list", {"project": PROJECT})
    assert "bad arguments for hub_projects" in failed(hub, "alice", "hub_projects", {"x": 1})
    assert "pattern" in failed(hub, "alice", "plan_list", {"project": "Not/A/Name"})


@needs_pg
def test_no_log_line_carries_a_token_arguments_or_results(hub, caplog):
    caplog.set_level(logging.DEBUG)
    body = f"---\nname: s\n---\n{SECRET}\n"
    ok(hub, "alice", "memory_write", {"name": "s.md", "body": body})
    ok(hub, "bob", "memory_search", {"query": SECRET})
    failed(hub, "bob", "memory_write", {"name": "s.md", "body": body})
    ok(hub, "dora", "kg_search", {"query": "guide"})
    lines = [record.getMessage() + json.dumps(record.__dict__, default=str) for record in caplog.records]
    tools = [r for r in caplog.records if r.getMessage() == "mcp tool"]
    assert [(r.tool, r.login, r.error) for r in tools] == [
        ("memory_write", "alice", False),
        ("memory_search", "bob", False),
        ("memory_write", "bob", True),
        ("kg_search", "dora", False),
    ]
    tokens = [hub.alice, hub.bob, hub.dora]
    for line in lines:
        assert SECRET not in line and "guide" not in line
        for headers in tokens:
            assert headers["Authorization"].removeprefix("Bearer ") not in line


# The agent of a run: its worker's token and X-Evo-Run

WORKER_PROTOCOL = {"X-Evo-Worker-Protocol": "1"}
LISTING = request(1, "tools/list")


def grant(hub, login: str, role: str, max_level: str, project: str = PROJECT) -> None:
    body = {"role": role, "max_level": max_level}
    response = hub.client.put(f"/v1/admin/projects/{project}/grants/{login}", json=body, headers=hub.admin)
    assert response.status_code == 200, response.text


def register_beta(hub) -> None:
    """Project beta, alpha's twin, which no member holds a grant on yet."""
    beta = {**REGISTRATION, "harness": {"name": "beta", "workspace": "~/ws", "path": "beta-harness"}}
    assert hub.client.put("/v1/projects/beta", json=beta, headers=hub.admin).status_code == 200


def push_plan(hub) -> None:
    pushed = hub.client.put(f"/v1/projects/{PROJECT}/plans/rollout", json={"body": plan_body()}, headers=hub.alice)
    assert pushed.status_code in (200, 201), pushed.text


def worker_of(hub, member: str, name: str) -> SimpleNamespace:
    """A worker of ``member`` serving alpha, registered with the member's machine token, after a first heartbeat that
    reports Claude Code and a checkout of app."""
    host = {"hostname": f"{name}.local", "os": "darwin", "arch": "arm64", "agent_version": "0.4.0"}
    body = {"name": name, "projects": [PROJECT], "slots": 4, **host}
    response = hub.client.post("/v1/workers", json=body, headers=getattr(hub, member))
    assert response.status_code == 201, response.text
    token = response.json()["token"]
    worker = SimpleNamespace(
        id=response.json()["worker"]["id"], token=token, headers={**live.bearer(token), **WORKER_PROTOCOL}
    )
    beat = {
        "runtimes": {"claude-code": {"available": True, "version": "2.1.289"}},
        "checkouts": {f"{PROJECT}/app": {"path": "/src/app", "branch": "main"}},
        "free_slots": 4,
    }
    assert hub.client.post("/v1/worker/heartbeat", json=beat, headers=worker.headers).status_code == 200
    return worker


def run_on(hub, member: str, worker, step: int) -> int:
    """The run of ``step`` of rollout that ``member`` dispatched, approval auto, and ``worker`` claimed."""
    body = {"plan_id": "rollout", "steps": [step], "approval": "auto"}
    response = hub.client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=getattr(hub, member))
    assert response.status_code == 201, response.text
    run_id = response.json()[0]["id"]
    claimed = hub.client.post("/v1/worker/claim", json={"wait_s": 0}, headers=worker.headers)
    assert claimed.status_code == 200 and claimed.json()["run"]["id"] == run_id, claimed.text
    return run_id


def report(hub, worker, run_id: int, state: str, **body):
    sent = {"state": state, **body}
    return hub.client.post(f"/v1/worker/runs/{run_id}/state", json=sent, headers=worker.headers)


def agent(hub, worker, run_id, message: dict, *, project=None, headers=None):
    """``message`` from the agent of run ``run_id`` on ``worker``, as `evo-agents hub mcp` sends it inside a run: the
    worker's token and X-Evo-Run (none when ``run_id`` is None), and no X-Evo-Project unless ``project``."""
    sent = {**live.bearer(worker.token), **MCP_HEADERS, **(headers or {})}
    if run_id is not None:
        sent["X-Evo-Run"] = str(run_id)
    if project is not None:
        sent["X-Evo-Project"] = project
    return hub.client.post("/mcp", json=message, headers=sent, follow_redirects=False)


def agent_tool(hub, worker, run_id: int, name: str, arguments: dict | None = None) -> dict:
    response = agent(hub, worker, run_id, call(7, name, arguments))
    assert response.status_code == 200, response.text
    return response.json()["result"]


def agent_principal(hub, worker, run_id: int):
    """The principal the gate gives the agent of run ``run_id`` on ``worker``."""
    from dataclasses import replace

    from evo_agents.hub.server.mcp import run_scope
    from evo_agents.hub.server.security import WORKER, authenticate

    async def find():
        engine = hub.app.state.engine
        user = await authenticate(engine, worker.token, WORKER, hub.app.state.config)
        async with engine.begin() as conn:
            return replace(user, scope=await run_scope(conn, user, run_id))

    return hub.client.portal.call(find)


@needs_pg
def test_a_worker_token_opens_mcp_only_with_x_evo_run_naming_a_run_its_worker_holds(hub):
    from evo_agents.hub.server.mcp import NOT_HELD

    push_plan(hub)
    grant(hub, "eve", "writer", "internal")
    mine, spare = worker_of(hub, "alice", "mac-mini"), worker_of(hub, "alice", "laptop")
    theirs = worker_of(hub, "eve", "eve-box")
    run_id = run_on(hub, "alice", mine, 1)

    opened = agent(hub, mine, run_id, LISTING)
    assert opened.status_code == 200 and len(opened.json()["result"]["tools"]) == 21

    bare = agent(hub, mine, None, LISTING)  # a worker token without X-Evo-Run
    assert bare.status_code == 403 and bare.json()["error"] == "forbidden" and "X-Evo-Run" in bare.json()["message"]
    for value in ("abc", "0", "-3", "1" * 19):
        malformed = agent(hub, mine, None, LISTING, headers={"X-Evo-Run": value})
        assert malformed.status_code == 400, (value, malformed.text)
    machine = rpc(hub, "alice", LISTING, headers={"X-Evo-Run": str(run_id)})
    assert machine.status_code == 400 and "worker token" in machine.json()["message"]

    # The run of another worker, of the same owner or another member's, and a run that does not exist: one 403.
    for worker in (spare, theirs):
        refused = agent(hub, worker, run_id, LISTING)
        assert refused.status_code == 403 and refused.json()["message"] == NOT_HELD.format(run=run_id), refused.text
    assert agent(hub, mine, 999_999, LISTING).json()["message"] == NOT_HELD.format(run=999_999)
    unknown = agent(hub, SimpleNamespace(token="evw_" + "x" * 43), run_id, LISTING)
    assert unknown.status_code == 401 and "evo-agents worker join" in unknown.json()["message"]
    # /v1 outside the worker's routes still refuses the worker token, with or without the header.
    assert (
        hub.client.get("/v1/projects", headers={**live.bearer(mine.token), "X-Evo-Run": str(run_id)}).status_code == 403
    )
    assert agent(hub, mine, run_id, LISTING).status_code == 200


@needs_pg
def test_a_run_scope_binds_the_session_to_the_run_s_project_and_every_tool_to_it(hub, caplog):
    push_plan(hub)
    register_beta(hub)
    grant(hub, "alice", "writer", "internal", project="beta")
    worker = worker_of(hub, "alice", "mac-mini")
    run_id = run_on(hub, "alice", worker, 1)

    other = agent(hub, worker, run_id, LISTING, project="beta")
    assert other.status_code == 403 and other.json()["error"] == "forbidden", other.text
    assert f"run {run_id} is of project {PROJECT}" in other.json()["message"]
    assert agent(hub, worker, run_id, LISTING, project=PROJECT).status_code == 200
    projects = agent_tool(hub, worker, run_id, "hub_projects")  # without X-Evo-Project: the run's project
    assert projects["structuredContent"]["session_project"] == PROJECT
    assert [p["name"] for p in projects["structuredContent"]["projects"]] == [PROJECT]
    assert [p["name"] for p in ok(hub, "alice", "hub_projects")["structuredContent"]["projects"]] == [PROJECT, "beta"]

    # alice herself writes into beta and keeps a personal memory; her run's agent reaches neither.
    body = f"---\nname: n\n---\n{SECRET}\n"
    ok(hub, "alice", "memory_write", {"project": "beta", "name": "beta.md", "body": body})
    diary = ok(hub, "alice", "memory_write", {"scope": "personal", "location": "notes", "name": "d.md", "body": body})
    for name, arguments in (
        ("plan_list", {"project": "beta"}),
        ("plan_show", {"project": "beta", "plan_id": "rollout"}),
        ("memory_search", {"project": "beta", "query": SECRET}),
        ("memory_write", {"project": "beta", "name": "agent.md", "body": body}),
    ):
        result = agent_tool(hub, worker, run_id, name, arguments)
        assert result["isError"] and "no project beta that you can see" in result["content"][0]["text"], name
    assert "for its members" in agent_tool(hub, worker, run_id, "skill_list", {"project": "beta"})["content"][0]["text"]
    personal = {"scope": "personal", "location": "notes", "name": "agent-diary.md", "body": body}
    refused = agent_tool(hub, worker, run_id, "memory_write", personal)
    assert refused["isError"] and "a personal memory is its owner's" in refused["content"][0]["text"]
    mine = agent_tool(hub, worker, run_id, "memory_search", {"query": SECRET, "scope": "personal"})
    assert mine["structuredContent"]["results"] == []
    hidden = agent_tool(hub, worker, run_id, "memory_get", {"id": diary["structuredContent"]["memory"]["id"]})
    assert hidden["isError"] and "no memory" in hidden["content"][0]["text"]

    # In alpha it reads and writes as alice would, and the audit trail names the worker's token.
    caplog.set_level(logging.INFO)
    caplog.clear()
    written = agent_tool(hub, worker, run_id, "memory_write", {"name": "agent.md", "body": body})
    assert not written.get("isError"), written
    found = agent_tool(hub, worker, run_id, "memory_search", {"query": SECRET})["structuredContent"]["results"]
    assert [m["name"] for m in found] == ["agent.md"]
    marked = agent_tool(hub, worker, run_id, "plan_step", {"plan_id": "rollout", "step": 3, "status": "in_progress"})
    assert not marked.get("isError"), marked
    assert not agent_tool(hub, worker, run_id, "kg_search", {"query": "guide"}).get("isError")
    from sqlalchemy import select

    from evo_agents.hub import tables

    audit, tokens = tables.audit, tables.tokens
    kinds = live.sql(
        hub.db,
        select(audit.c.action, tokens.c.kind)
        .join_from(audit, tokens, tokens.c.id == audit.c.token_id)
        .where(audit.c.action.in_(("memory.put", "plan.patch")))
        .order_by(audit.c.id),
    )
    assert kinds[-2:] == [("memory.put", "worker"), ("plan.patch", "worker")]
    calls = [(r.tool, r.login, r.run_id) for r in caplog.records if r.getMessage() == "mcp tool"]
    assert calls[0] == ("memory_write", "alice", run_id)


@needs_pg
def test_a_run_scope_is_never_a_hub_admin_and_at_most_a_writer(hub):
    """The run's owner is a hub admin with the admin role on alpha; its agent is neither, and whatever needs either is
    refused to it."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from evo_agents.hub.server import projects, skills

    push_plan(hub)
    register_beta(hub)  # where the hub admin holds no grant
    grant(hub, live.ADMIN, "admin", "internal")
    worker = worker_of(hub, "admin", "admin-box")
    run_id = run_on(hub, "admin", worker, 2)

    own = ok(hub, "admin", "hub_projects")["content"][0]["text"].splitlines()
    assert own[0].startswith("alpha (this session): role admin, max level internal;")
    assert own[1].startswith("beta: no grant (hub admin);")
    shown = agent_tool(hub, worker, run_id, "hub_projects")
    assert shown["content"][0]["text"].startswith("alpha (this session): role writer, max level internal;")
    assert [p["role"] for p in shown["structuredContent"]["projects"]] == ["writer"]
    # What a hub admin reaches without a grant answers its agent as a project that does not exist.
    assert "needs a grant on it" in failed(hub, "admin", "plan_list", {"project": "beta"})
    beta = agent_tool(hub, worker, run_id, "plan_list", {"project": "beta"})
    assert beta["isError"] and "no project beta that you can see" in beta["content"][0]["text"]

    # The hub's own checks of the admin role and of a hub admin refuse the agent's principal.
    principal = agent_principal(hub, worker, run_id)
    assert (principal.admin, principal.scope.role, principal.scope.max_level) == (False, "writer", "internal")
    request = Request(
        {"type": "http", "app": hub.app, "method": "PUT", "path": "/", "headers": [], "query_string": b""}
    )
    body = projects.Registration(**REGISTRATION)
    with pytest.raises(HTTPException) as caught:
        hub.client.portal.call(projects.register, request, body, PROJECT, principal)
    assert caught.value.status_code == 403 and "admin role on project alpha" in caught.value.detail

    async def publish_global():
        async with hub.app.state.engine.begin() as conn:
            return await skills._writable(conn, principal, None)

    with pytest.raises(HTTPException) as caught:
        hub.client.portal.call(publish_global)
    assert caught.value.status_code == 403 and "hub admin" in caught.value.detail
    assert hub.client.put(f"/v1/projects/{PROJECT}", json=REGISTRATION, headers=hub.admin).status_code == 200


@needs_pg
def test_a_run_scope_holds_while_the_run_is_held_and_ends_with_it(hub):
    from evo_agents.hub.server.mcp import NOT_HELD

    push_plan(hub)
    worker = worker_of(hub, "alice", "mac-mini")
    run_id = run_on(hub, "alice", worker, 1)

    def status() -> int:
        return agent(hub, worker, run_id, LISTING).status_code

    assert status() == 200  # leased
    for state in ("running", "verifying"):
        assert report(hub, worker, run_id, state).status_code == 200, state
        assert status() == 200, state
    # waiting is a held state; review and parked are not. A run of one step reaches neither so: set in place.
    from sqlalchemy import func, update

    from evo_agents.hub import tables

    runs = tables.runs
    for state, opens in (("waiting", True), ("review", False), ("parked", False), ("verifying", True)):
        since = {
            "waiting_since": func.now() if state == "waiting" else None,
            "parked_at": func.now() if state == "parked" else None,
        }
        live.sql(hub.db, update(runs).values(state=state, **since).where(runs.c.id == run_id))
        assert (status() == 200) is opens, state

    passed = [{"command": "pytest -q", "exit_code": 0}]
    done = report(hub, worker, run_id, "done", verify=passed, commit_sha="a" * 40)
    assert done.status_code == 200, done.text
    after = agent(hub, worker, run_id, LISTING)
    assert after.status_code == 403 and after.json()["message"] == NOT_HELD.format(run=run_id)

    failing = run_on(hub, "alice", worker, 2)
    assert agent(hub, worker, failing, LISTING).status_code == 200
    assert report(hub, worker, failing, "failed", error="the agent gave up").status_code == 200
    assert agent(hub, worker, failing, LISTING).status_code == 403

    revoked = run_on(hub, "alice", worker, 3)
    assert agent(hub, worker, revoked, LISTING).status_code == 200
    assert hub.client.post(f"/v1/workers/{worker.id}/revoke", headers=hub.alice).status_code == 200
    assert agent(hub, worker, revoked, LISTING).status_code == 401  # the worker's token went with it


def test_inside_a_run_the_proxy_sends_the_worker_token_and_x_evo_run_instead_of_the_machine_one(tmp_path, monkeypatch):
    from evo_agents.worker.home import WorkerConfig, WorkerHome

    fake = FakeHub(mcp_reply)
    worker_token = "evw_" + "W" * 43
    try:
        monkeypatch.setenv("HOME", str(sign_in(tmp_path / "home", fake.url, "alice", TOKEN)))
        state = WorkerHome(tmp_path / "worker")
        config = WorkerConfig(url=fake.url, worker_id=3, name="mac-mini", projects=["alpha"], owner="alice")
        state.save(config, worker_token)
        monkeypatch.setenv("EVO_WORKER_HOME", str(state.root))

        def sent(run: str | None) -> dict:
            if run is None:
                monkeypatch.delenv("EVO_RUN_ID", raising=False)
            else:
                monkeypatch.setenv("EVO_RUN_ID", run)
            fake.requests.clear()
            (reply,) = run_proxy(Proxy("alpha", err=io.StringIO()), call(1, "plan_list"))
            assert reply["result"]["content"][0]["text"] == "called plan_list"
            ((headers, _),) = fake.requests
            return headers

        inside = sent("42")
        assert inside["authorization"] == f"Bearer {worker_token}" and inside["x-evo-run"] == "42"
        assert "x-evo-project" not in inside  # the hub binds the session to the run's project
        for outside in (sent(None), sent("0"), sent("not-a-run")):
            assert outside["authorization"] == f"Bearer {TOKEN}" and "x-evo-run" not in outside
            assert outside["x-evo-project"] == "alpha"
        state.token_path.unlink()  # a run's variables on a machine that is no worker: its own machine token
        fallback = sent("42")
        assert fallback["authorization"] == f"Bearer {TOKEN}" and "x-evo-run" not in fallback
    finally:
        fake.stop()


# The CLI against `hub serve`


def sign_in(home: Path, url: str, login: str, token: str) -> Path:
    directory = home / ".evo" / "hub"
    directory.mkdir(parents=True)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": login}), encoding="utf-8")
    return home


def hub_mcp(env: dict, stdin: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "evo_agents", "hub", "mcp", *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=cwd,
    )


@needs_pg
def test_printf_tools_list_into_evo_agents_hub_mcp_prints_the_twenty_one_tools(hub_db, tmp_path):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN) as served:
        token = live.insert_token(hub_db, "alice")
        home = sign_in(tmp_path / "home", served.url, "alice", token)
        env = pg.clean_env(HOME=str(home), EVO_KG_HOME=str(tmp_path / "kg-home"))
        for name in ("EVO_KG_PROJECT", "CLAUDE_PROJECT_DIR"):
            env.pop(name, None)
        listed = hub_mcp(env, TOOLS_LIST, cwd=tmp_path)
        assert listed.returncode == 0, listed.stderr
        (line,) = listed.stdout.splitlines()
        answer = json.loads(line)
        assert answer["id"] == 1 and len(answer["result"]["tools"]) == 21
        assert [t["name"] for t in answer["result"]["tools"]] == [t["name"] for t in mcp_tools.TOOLS]
        assert "the hub at" not in listed.stderr  # the hub answered: no outage was reported
        assert '"path": "/mcp"' in served.log() and '"status": 200' in served.log()

        # a whole session: the handshake, a notification, a refusal, a write that stays on the hub
        admin = live.bearer(live.insert_token(hub_db, live.ADMIN))
        session = [
            request(1, "initialize", HELLO),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            call(2, "hub_projects"),
            call(3, "memory_write", {"name": "cli.md", "body": f"---\nname: cli\n---\n{SECRET}\n"}),
            call(4, "kg_status"),
            request(5, "no/such/method"),
        ]

        def put(path, body):
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                served.url + path, data=data, method="PUT", headers={**admin, "Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                assert response.status in (200, 201)

        put(f"/v1/projects/{PROJECT}", REGISTRATION)
        put(f"/v1/admin/projects/{PROJECT}/grants/alice", {"role": "writer", "max_level": "internal"})
        ran = hub_mcp(env, "".join(json.dumps(m) + "\n" for m in session), "--project", PROJECT, cwd=tmp_path)
        assert ran.returncode == 0, ran.stderr
        replies = by_id([json.loads(line) for line in ran.stdout.splitlines()])
        assert sorted(replies) == [1, 2, 3, 4, 5]
        assert replies[1]["result"]["serverInfo"]["name"] == "evo-hub"
        assert replies[2]["result"]["structuredContent"]["session_project"] == PROJECT
        assert "Nothing was written on this machine" in replies[3]["result"]["content"][0]["text"]
        assert "has no graph on the hub yet" in replies[4]["result"]["content"][0]["text"]
        assert replies[5]["error"]["code"] == -32601
        assert not (home / ".claude").exists()  # memory_write wrote nothing here
        assert sorted(p.name for p in (home / ".evo" / "hub").iterdir()) == ["config.json", "token"]
        from sqlalchemy import select

        from evo_agents.hub import tables

        assert live.sql(hub_db, select(tables.memories.c.name)) == [("cli.md",)]

        served.proc.terminate()
        served.proc.wait(timeout=30)
        down = hub_mcp(env, "".join(json.dumps(m) + "\n" for m in session[:3]), "--project", PROJECT, cwd=tmp_path)
    assert down.returncode == 0, down.stderr
    replies = by_id([json.loads(line) for line in down.stdout.splitlines()])
    assert replies[1]["result"]["serverInfo"]["name"] == "evo-hub"  # answered by the proxy
    text = replies[2]["result"]["content"][0]["text"]
    assert replies[2]["result"]["isError"] and served.url in text
    assert down.stderr.count("evo-agents hub mcp:") == 1 and served.url in down.stderr
    for output in (listed.stdout + listed.stderr, ran.stdout + ran.stderr, down.stdout + down.stderr, served.log()):
        assert token not in output
    assert SECRET not in served.log() and SECRET not in ran.stderr
