"""The exit status of evo-agents when a command fails (``evo_agents.hub.client.exit_status``, docs/hub.md "Exit
status"): 2 for a wrong command line, as argparse exits, with the words evo-cli reads to tell an evo-agents too old for
the command it ran; 69 when the hub or the network could not be reached; 75 when the hub answered 5xx or 429; 1 for any
other failure. A hub on 127.0.0.1 answers each status the test sets; no Postgres."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.hub import client
from evo_agents.hub.client import (
    EXIT_FAILED,
    EXIT_TEMPFAIL,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    HubError,
    NotSignedIn,
    Unreachable,
    exit_status,
)
from tests.hub import pg


class _Answers(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802 (http.server's name)
        body = json.dumps({"error": "test", "message": f"the test hub answers {self.status}"}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def hub_answering():
    """A hub on 127.0.0.1 that answers every GET with the status set on the returned handler class."""
    handler = type("Answers", (_Answers,), {})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", handler
    server.shutdown()
    server.server_close()


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for variable in ("EVO_RUN_ID", "EVO_WORKER_HOME"):
        monkeypatch.delenv(variable, raising=False)
    return tmp_path


def signed_in(home: Path, url: str) -> None:
    directory = home / ".evo" / "hub"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / "token").write_text("evo_test_token_never_printed\n", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": "alice"}), encoding="utf-8")


@pytest.mark.parametrize(
    "problem, status",
    [
        (Unreachable("cannot reach http://127.0.0.1:9: Connection refused", refused=True), EXIT_UNAVAILABLE),
        (Unreachable("cannot reach https://hub.test: no answer within 30s"), EXIT_UNAVAILABLE),
        (HubError("the hub answered HTTP 502", 502), EXIT_TEMPFAIL),
        (HubError("the hub answered HTTP 503", 503), EXIT_TEMPFAIL),
        (HubError("too many requests", 429), EXIT_TEMPFAIL),
        (HubError("no such plan", 404), EXIT_FAILED),
        (HubError("a conflict", 409), EXIT_FAILED),
        (HubError("not signed in", 401), EXIT_FAILED),
        (NotSignedIn("not signed in to a hub"), EXIT_FAILED),
        (HubError("which project? pass --project"), EXIT_FAILED),
    ],
)
def test_exit_status_of_each_failure(problem, status):
    assert exit_status(problem) == status
    assert (EXIT_FAILED, EXIT_USAGE, EXIT_UNAVAILABLE, EXIT_TEMPFAIL) == (1, 2, 69, 75)


def test_exit_status_of_a_hub_command_says_why_it_failed(home, hub_answering, capsys):
    url, handler = hub_answering
    signed_in(home, url)
    for answered, status in ((503, EXIT_TEMPFAIL), (502, EXIT_TEMPFAIL), (429, EXIT_TEMPFAIL), (404, EXIT_FAILED)):
        handler.status = answered
        assert main(["hub", "whoami"]) == status, answered
        out, err = capsys.readouterr()
        assert out == "" and err.startswith("error: ") and err.count("\n") == 1
        assert "evo_test_token" not in err

    signed_in(home, f"http://127.0.0.1:{pg.free_port()}")  # nothing listens there
    assert main(["hub", "whoami"]) == EXIT_UNAVAILABLE
    assert "cannot reach" in capsys.readouterr().err

    (home / ".evo" / "hub" / "token").unlink()
    assert main(["hub", "whoami"]) == EXIT_FAILED  # not signed in: evo-cli reads "hub login" in it
    assert "hub login" in capsys.readouterr().err


def test_exit_status_of_a_wrong_command_line_is_2_with_the_words_evo_cli_reads(home, capsys):
    """evo-cli (evo_cli/commands/harness/_hub.py) reads status 2 with "invalid choice" or "unrecognized arguments" as an
    evo-agents too old for the command it ran: both stay argparse's."""
    for argv, words in (
        (["hub", "no-such-command"], "invalid choice"),
        (["hub", "whoami", "--no-such-option"], "unrecognized arguments"),
    ):
        with pytest.raises(SystemExit) as stopped:
            main(argv)
        assert stopped.value.code == EXIT_USAGE
        assert words in capsys.readouterr().err


def test_exit_status_of_the_hub_client_is_read_from_the_error_the_request_raised():
    """A request the network refuses raises ``Unreachable``, and a 5xx a HubError with its status: the two the exit
    status reads."""
    hub = client.Hub(f"http://127.0.0.1:{pg.free_port()}", "evo_test_token")
    with pytest.raises(Unreachable) as refused:
        hub.call("GET", "/v1/auth/whoami")
    assert exit_status(refused.value) == EXIT_UNAVAILABLE
