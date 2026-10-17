"""The questions of a local session reach the hub as decisions and its answers reach the session: the PreToolUse hook
puts the AskUserQuestion dialog to the hub without waiting, PostToolUse reports the answer given in the dialog (the
first answer wins, a 409 is left alone), and UserPromptSubmit gives the agent what the member answered on the hub after
the session stopped waiting. The hub is a function, as in test_local_sync.py."""

import json

from evo_agents.hub import client as hub_client
from evo_agents.hub import local_sync
from evo_agents.hub.client import Unreachable
from tests.hub import test_local_sync as base_tests
from tests.hub.test_local_sync import SECRET, URL, FakeHub, fire, payload, session_file

own_home = base_tests.own_home  # the fixture of that module: each test has a home of its own

BASE = "/v1/projects/demo/local-runs/7"

DIALOG = {
    "questions": [
        {
            "question": f"Which database? {SECRET}",
            "header": "DB",
            "multiSelect": False,
            "options": [
                {"label": "Postgres", "description": "the default"},
                {"label": "SQLite", "description": "small"},
            ],
        },
        {"question": "Run the migration?", "options": [{"label": "Yes"}, {"label": "No"}]},
        {"question": "One option only", "options": [{"label": "Alone"}]},
    ]
}


class AskHub(FakeHub):
    def __init__(self):
        super().__init__()
        self.decisions = 100
        self.answered: dict[int, int] = {}  # decision id -> status the machine's report gets
        self.inbox: list[dict] = []

    def send(self, method, url, headers, data, timeout):
        path = url.removeprefix(URL)
        body = json.loads(data) if data else None
        if method == "POST" and path == f"{BASE}/decisions":
            self.calls.append((method, path, body))
            self.decisions += 1
            self.state = "waiting"
            return 201, self._json({"id": self.decisions})
        if method == "POST" and path.startswith(f"{BASE}/decisions/") and path.endswith("/answered"):
            self.calls.append((method, path, body))
            number = int(path.split("/")[-2])
            status = self.answered.get(number, 200)
            return status, self._json({"id": number, "message": "answered first"})
        if method == "POST" and path == f"{BASE}/inbox":
            self.calls.append((method, path, body))
            ack = (body or {}).get("ack") or 0
            self.inbox = [m for m in self.inbox if m["id"] > ack]
            return 200, self._json({"messages": self.inbox})
        return super().send(method, url, headers, data, timeout)


def ask_hub(monkeypatch) -> AskHub:
    from evo_agents.hub.memory import MemorySync

    hub = AskHub()
    monkeypatch.setattr(hub_client, "_send", hub.send)
    monkeypatch.setattr(MemorySync, "project_of", lambda self, where=None: "demo")
    return hub


def dialog(own_home, **extra) -> dict:
    return payload(own_home, tool_name="AskUserQuestion", tool_use_id="toolu_ask", tool_input=DIALOG, **extra)


def test_a_dialog_becomes_decisions_with_keyed_options_redacted_and_does_not_wait(own_home, monkeypatch, capsys):
    hub = ask_hub(monkeypatch)
    assert fire("local-ask", dialog(own_home), monkeypatch, capsys) == (0, "", "")
    sent = hub.of("POST", "/decisions")
    assert len(sent) == 2  # the question with one option cannot be a decision
    first = sent[0]
    assert first["category"] == "question" and "sk-ant-***" in first["question"] and SECRET not in json.dumps(hub.calls)
    assert first["options"] == [
        {"key": "1", "label": "Postgres", "description": "the default"},
        {"key": "2", "label": "SQLite", "description": "small"},
    ]
    assert first["context"] == "DB"
    assert [e["decision"] for e in session_file(own_home)["asks"]["toolu_ask"]] == [101, 102]
    assert hub.state == "waiting"


def test_the_answer_given_in_the_dialog_is_reported_to_its_decisions(own_home, monkeypatch, capsys):
    hub = ask_hub(monkeypatch)
    fire("local-ask", dialog(own_home), monkeypatch, capsys)
    answers = {f"Which database? {SECRET}": "SQLite", "Run the migration?": "later, after the release"}
    code, out, err = fire("local-tool", dialog(own_home, tool_response={"answers": answers}), monkeypatch, capsys)
    assert (code, out, err) == (0, "", "")
    reported = [(p, b) for m, p, b in hub.calls if p.endswith("/answered")]
    assert reported == [
        (f"{BASE}/decisions/101/answered", {"option": "2"}),
        (f"{BASE}/decisions/102/answered", {"text": "later, after the release"}),
    ]
    assert "asks" not in session_file(own_home) or session_file(own_home)["asks"] == {}
    assert hub.state == "running"


def test_a_dialog_the_hub_answered_first_gets_a_409_that_is_left_alone(own_home, monkeypatch, capsys):
    hub = ask_hub(monkeypatch)
    hub.answered = {101: 409, 102: 409}
    fire("local-ask", dialog(own_home), monkeypatch, capsys)
    assert fire("local-tool", dialog(own_home, tool_response={}), monkeypatch, capsys) == (0, "", "")
    assert session_file(own_home).get("refused_at") is None
    assert len([1 for m, p, b in hub.calls if p.endswith("/answered")]) == 2
    assert [b for m, p, b in hub.calls if p.endswith("/answered")][0] == {"text": "answered in the session"}


def test_an_answer_that_came_late_reaches_the_agent_at_the_next_message_and_once(own_home, monkeypatch, capsys):
    hub = ask_hub(monkeypatch)
    hub.inbox = [{"id": 5, "text": "Decision 101: option 1 (Postgres)", "sent_by": "alice", "decision_id": 101}]
    code, out, err = fire("user-prompt", payload(own_home, prompt="go on"), monkeypatch, capsys)
    assert (code, err) == (0, "")
    said = json.loads(out)["hookSpecificOutput"]
    assert said["hookEventName"] == "UserPromptSubmit"
    assert "Decision 101: option 1 (Postgres)" in said["additionalContext"] and "alice" in said["additionalContext"]
    fire("stop", payload(own_home), monkeypatch, capsys)
    code, out, err = fire("user-prompt", payload(own_home, prompt="and again"), monkeypatch, capsys)
    assert (code, out) == (0, "")  # the second message acknowledged it, and nothing is repeated
    assert {"ack": 5} in hub.of("POST", "/inbox")


def test_no_answers_means_no_output_and_a_silent_hub_changes_nothing(own_home, monkeypatch, capsys):
    hub = ask_hub(monkeypatch)
    assert fire("user-prompt", payload(own_home, prompt="hi"), monkeypatch, capsys) == (0, "", "")
    hub.down = True
    assert fire("local-ask", dialog(own_home), monkeypatch, capsys) == (0, "", "")
    assert fire("local-tool", dialog(own_home, tool_response={}), monkeypatch, capsys) == (0, "", "")


def test_sync_off_and_a_worker_run_ask_nothing(own_home, monkeypatch, capsys):
    hub = ask_hub(monkeypatch)
    monkeypatch.setenv(local_sync.DISABLE_VARIABLE, "0")
    assert fire("local-ask", dialog(own_home), monkeypatch, capsys) == (0, "", "")
    monkeypatch.delenv(local_sync.DISABLE_VARIABLE)
    monkeypatch.setenv("EVO_RUN_ID", "9")
    assert fire("local-ask", dialog(own_home), monkeypatch, capsys) == (0, "", "")
    assert hub.calls == []


def test_unreachable_is_not_a_failure(own_home, monkeypatch, capsys):
    def down(*args, **kwargs):
        raise Unreachable("down")

    monkeypatch.setattr(hub_client, "_send", down)
    assert fire("local-ask", dialog(own_home), monkeypatch, capsys) == (0, "", "")
