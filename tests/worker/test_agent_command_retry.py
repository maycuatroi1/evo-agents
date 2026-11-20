"""The commands of a run's agent in chats (``evo_agents.worker.chat_cli.call``) send a call again while the hub does
not take it: run #8743 of evo-agents (2026-11-16) forwarded an answer while the hub deployed, got the proxy's HTTP 500
and gave up, so the asking agent posted the answer as a file by hand (problem P-24)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("aiohttp")

from evo_agents.hub.client import HubError  # noqa: E402
from evo_agents.worker import agent_ask_cli, chat_cli  # noqa: E402
from evo_agents.worker.home import WorkerConfig  # noqa: E402


class Deploying:
    """A hub behind its proxy while it deploys: the first ``down`` calls get the proxy's 500 (no JSON of the hub), then
    the hub answers the forward."""

    def __init__(self, down: int):
        self.down = down
        self.paths: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.paths.append(self.path)
                if len(outer.paths) <= outer.down:
                    self._send(500, b"Internal Server Error", "text/plain")
                else:
                    self._send(201, json.dumps({"message_id": 13240, "refusal": None}).encode(), "application/json")

            def _send(self, status: int, body: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Deploying:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


def agent_run(url: str) -> chat_cli.AgentRun:
    config = WorkerConfig(url=url, worker_id=48, name="agent-ea-lead", projects=["evo-agents"])
    return chat_cli.AgentRun(8743, "chat", config, "evw_" + "f" * 43)


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(chat_cli, "RETRY_FIRST_DELAY", 0.01, raising=False)


def test_a_forward_that_hits_a_deploy_is_sent_again_and_lands(quick, capsys):
    with Deploying(down=2) as hub:
        answer = chat_cli.call(agent_run(hub.url), agent_ask_cli.FORWARD_ROUTE, {"run_id": 8741})
    assert answer == {"message_id": 13240, "refusal": None}
    assert hub.paths == ["/v1/worker/runs/8743/agent/forwards"] * 3
    tries = capsys.readouterr().err.splitlines()
    assert len(tries) == 2 and all("answered HTTP 500" in line and "trying again" in line for line in tries)


def test_a_hub_down_past_the_outage_bound_fails_the_command(quick, monkeypatch):
    monkeypatch.setattr(chat_cli, "OUTAGE_SECONDS", 0.2)
    with Deploying(down=10_000) as hub:
        with pytest.raises(HubError, match="answered HTTP 500"):
            chat_cli.call(agent_run(hub.url), agent_ask_cli.FORWARD_ROUTE, {"run_id": 8741})
    assert 2 <= len(hub.paths) < 100
