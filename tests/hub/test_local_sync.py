"""The evo-hub plugin puts a local Claude Code session on the hub: its run of kind local, the state it reports, its log
(redacted here), the account of the session, and the plan write that names the run.

The hooks run in this process against a hub that is a function (``FakeHub``, in place of the client's ``_send``) except
in the last test, which runs them against ``hub serve``. No test touches the real ~/.claude or ~/.evo: each runs with
a home directory of its own. The behaviours a hook must keep whatever happens (exit 0, no answer from the hub, the
agent of a worker run) are the plugin's, in tests/hub/test_hooks.py."""

import json
import os
import socket
import time
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.hub import client as hub_client
from evo_agents.hub import hooks, local_account, local_sync, plan_cli
from evo_agents.hub.client import Hub, HubError, Unreachable
from evo_agents.hub.memory import MemorySync
from tests.hub import live, pg

SESSION = "0199a3c1-0000-7000-8000-0000000000c3"
TOKEN = "evo_test_token_never_printed"
SECRET = "sk-ant-api03-" + "A" * 40
URL = "https://hub.test"


@pytest.fixture(autouse=True)
def own_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / ".evo" / "hub").mkdir(parents=True, mode=0o700)
    (home / ".evo" / "hub" / "token").write_text(TOKEN + "\n", encoding="utf-8")
    (home / ".evo" / "hub" / "config.json").write_text(json.dumps({"url": URL, "login": "alice"}), encoding="utf-8")
    for name in ("token", "config.json"):
        os.chmod(home / ".evo" / "hub" / name, 0o600)
    monkeypatch.setenv("HOME", str(home))
    for variable in (
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_PROJECT_DIR",
        local_sync.DISABLE_VARIABLE,
        local_sync.SESSION_VARIABLE,
        local_account.FOLDERS_VARIABLE,
        local_account.EMAIL_VARIABLE,
        local_account.ACCOUNT_VARIABLE,
        local_account.BINARY_VARIABLE,
        hooks.RUN_VARIABLE,
    ):
        monkeypatch.delenv(variable, raising=False)
    return home


class FakeHub:
    """The routes of a local run, in memory, as ``local_runs`` answers them: register, state and events."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.down = False
        self.state = "running"
        self.run_id = 7
        self.registered = False
        self.events: list[dict] = []

    def send(self, method, url, headers, data, timeout):
        path = url.removeprefix(URL)
        body = json.loads(data) if data else None
        self.calls.append((method, path, body))
        if self.down:
            raise Unreachable(f"the hub at {URL} did not answer")
        base = "/v1/projects/demo/local-runs"
        if method == "PUT" and path.startswith(base + "/"):
            status = 200 if self.registered else 201
            self.registered, self.state = True, "running"
            return status, self._json({"id": self.run_id, "state": "running", "last_seq": len(self.events)})
        if method == "POST" and path == f"{base}/{self.run_id}/state":
            self.state = body["state"]
            return 200, self._json({"id": self.run_id, "state": self.state})
        if method == "POST" and path == f"{base}/{self.run_id}/events":
            for event in sorted(body["events"], key=lambda e: e["seq"]):
                if event["seq"] == len(self.events) + 1:
                    self.events.append(event)
            return 200, self._json({"ack_seq": len(self.events), "stored": len(body["events"])})
        return 404, self._json({"message": f"no route {method} {path}"})

    @staticmethod
    def _json(value) -> bytes:
        return json.dumps(value).encode()

    def of(self, method: str, suffix: str) -> list[dict | None]:
        return [body for m, path, body in self.calls if m == method and path.endswith(suffix)]


@pytest.fixture
def fake(monkeypatch) -> FakeHub:
    hub = FakeHub()
    monkeypatch.setattr(hub_client, "_send", hub.send)
    monkeypatch.setattr(MemorySync, "project_of", lambda self, where=None: "demo")
    return hub


def payload(own_home: Path, **extra) -> dict:
    return {"session_id": SESSION, "cwd": str(own_home / "work"), **extra}


def fire(name: str, data: dict, monkeypatch, capsys) -> tuple[int, str, str]:
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    code = main(["hub", "hook", name])
    out, err = capsys.readouterr()
    return code, out, err


def session_file(home: Path) -> dict:
    return json.loads((home / ".evo" / "hub" / "local" / f"{SESSION}.json").read_text(encoding="utf-8"))


def tool_data(own_home: Path, n: int, **extra) -> dict:
    return payload(
        own_home,
        tool_name="Bash",
        tool_use_id=f"toolu_{n}",
        tool_input={"command": f"echo {n}"},
        tool_response={"stdout": f"{n}\n", "stderr": ""},
        **extra,
    )


def no_throttle(monkeypatch) -> None:
    monkeypatch.setattr(local_sync, "FLUSH_EVERY", 0.0)


# Registering, the state, the log


def test_the_first_prompt_registers_the_session_and_reports_it_running(own_home, fake, monkeypatch, capsys):
    folder = own_home / "Application Support" / "profile-work"
    record = folder / local_account.SESSIONS / "acct-1" / "org-1" / "local_abc.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"cliSessionId": SESSION, "account": {"emailAddress": "me@work.test"}}))
    monkeypatch.setenv(local_account.FOLDERS_VARIABLE, f"work={folder}")

    code, out, err = fire("user-prompt", payload(own_home, prompt=f"fix {SECRET} now\nplease"), monkeypatch, capsys)
    assert (code, out, err) == (0, "", "")
    (registered,) = fake.of("PUT", f"/local-runs/{SESSION}")
    assert registered["claude_email"] == "me@work.test" and registered["claude_profile"] == "work"
    assert registered["cwd"] == str(own_home / "work") and registered["machine"]
    assert registered["title"] == "fix sk-ant-*** now please" and SECRET not in json.dumps(fake.calls)
    assert fake.of("POST", "/state") == []  # registering made it running already: nothing to say twice
    assert fake.state == "running"

    fire("stop", payload(own_home), monkeypatch, capsys)
    assert fake.of("POST", "/state") == [{"state": "idle"}] and fake.state == "idle"
    fire("user-prompt", payload(own_home, prompt="next"), monkeypatch, capsys)
    assert fake.state == "running" and len(fake.of("PUT", f"/local-runs/{SESSION}")) == 1  # registered once
    fire("session-end", payload(own_home), monkeypatch, capsys)
    assert fake.state == "ended" and session_file(own_home)["run_id"] == 7


def test_a_session_that_never_gets_a_prompt_event_registers_at_its_first_tool(own_home, fake, monkeypatch, capsys):
    assert fire("local-tool", tool_data(own_home, 1), monkeypatch, capsys) == (0, "", "")
    assert len(fake.of("PUT", f"/local-runs/{SESSION}")) == 1 and fake.state == "running"
    assert [e["kind"] for e in fake.events] == ["tool_call", "tool_call_update"]


def test_the_log_holds_the_agent_s_words_and_the_tools_redacted_on_this_machine(own_home, fake, monkeypatch, capsys):
    no_throttle(monkeypatch)
    transcript = own_home / "transcript.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"I will use key {SECRET} and {TOKEN}"}]},
        },
        {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "a subagent"}]}},
    ]
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    fire("user-prompt", payload(own_home, prompt="go", transcript_path=str(transcript)), monkeypatch, capsys)
    assert fake.events == []  # what the transcript held at the first prompt is not this turn's

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"using {SECRET}"}]}})
            + "\n"
        )
    leaking = tool_data(own_home, 1, transcript_path=str(transcript))
    leaking["tool_input"] = {"command": f"curl -H 'Authorization: Bearer {SECRET}' https://x.test"}
    leaking["tool_response"] = {"stdout": f"API_TOKEN={SECRET}\nok", "stderr": ""}
    fire("local-tool", leaking, monkeypatch, capsys)

    kinds = [e["kind"] for e in fake.events]
    assert kinds == ["agent_message_chunk", "tool_call", "tool_call_update"]
    assert [e["seq"] for e in fake.events] == [1, 2, 3]
    wire = json.dumps(fake.calls)
    assert SECRET not in wire and TOKEN not in wire and "***" in wire
    assert fake.events[0]["body"]["text"] == "using sk-ant-***"
    assert SECRET not in (own_home / ".evo" / "hub" / "local" / f"{SESSION}.json").read_text(encoding="utf-8")


def test_a_tool_hook_asks_the_hub_at_most_once_in_a_while_and_stop_sends_the_rest(own_home, fake, monkeypatch, capsys):
    fire("user-prompt", payload(own_home, prompt="go"), monkeypatch, capsys)
    for n in range(1, 4):
        fire("local-tool", tool_data(own_home, n), monkeypatch, capsys)
    assert len(fake.of("POST", "/events")) == 1  # the first one; the others wait for FLUSH_EVERY
    assert len(session_file(own_home)["events"]) == 4
    fire("stop", payload(own_home), monkeypatch, capsys)
    assert [e["seq"] for e in fake.events] == list(range(1, 7)) and session_file(own_home)["events"] == []
    assert fake.state == "idle"


# Where it does nothing


def test_a_session_outside_a_hub_project_sends_nothing_and_asks_once(own_home, fake, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr(MemorySync, "project_of", lambda self, where=None: asked.append(where) or None)
    for hook, data in (("user-prompt", payload(own_home, prompt="x")), ("local-tool", tool_data(own_home, 1))):
        assert fire(hook, data, monkeypatch, capsys) == (0, "", "")
    fire("stop", payload(own_home), monkeypatch, capsys)
    fire("session-end", payload(own_home), monkeypatch, capsys)
    assert fake.calls == [] and len(asked) == 1


def test_evo_hub_local_sync_zero_turns_it_off_and_leaves_no_file(own_home, fake, monkeypatch, capsys):
    for value in ("0", "off"):
        monkeypatch.setenv(local_sync.DISABLE_VARIABLE, value)
        for hook, data in (("user-prompt", payload(own_home, prompt="x")), ("local-tool", tool_data(own_home, 1))):
            assert fire(hook, data, monkeypatch, capsys) == (0, "", "")
        fire("stop", payload(own_home), monkeypatch, capsys)
        fire("session-end", payload(own_home), monkeypatch, capsys)
    assert fake.calls == [] and not (own_home / ".evo" / "hub" / "local").exists()
    assert local_sync.run_for(URL, "demo") is None


def test_the_agent_of_a_worker_run_registers_no_local_run(own_home, fake, monkeypatch, capsys):
    monkeypatch.setenv(hooks.RUN_VARIABLE, "12")
    for hook, data in (("user-prompt", payload(own_home, prompt="x")), ("local-tool", tool_data(own_home, 1))):
        assert fire(hook, data, monkeypatch, capsys) == (0, "", "")
    fire("stop", payload(own_home), monkeypatch, capsys)
    fire("session-end", payload(own_home), monkeypatch, capsys)
    assert fake.calls == [] and not (own_home / ".evo" / "hub" / "local").exists()


def test_not_signed_in_sends_nothing(own_home, fake, monkeypatch, capsys):
    (own_home / ".evo" / "hub" / "token").unlink()
    assert fire("user-prompt", payload(own_home, prompt="x"), monkeypatch, capsys) == (0, "", "")
    assert fake.calls == []


def test_a_session_end_for_a_session_never_seen_sends_nothing(own_home, fake, monkeypatch, capsys):
    assert fire("session-end", payload(own_home), monkeypatch, capsys) == (0, "", "")
    assert fake.calls == []


# A hub that does not answer, or answers late, or refuses


def test_events_wait_on_disk_while_the_hub_is_down_and_go_in_order_afterwards(own_home, fake, monkeypatch, capsys):
    no_throttle(monkeypatch)
    fire("user-prompt", payload(own_home, prompt="go"), monkeypatch, capsys)
    fake.down = True
    for n in (1, 2):
        assert fire("local-tool", tool_data(own_home, n), monkeypatch, capsys) == (0, "", "")
    assert [e["seq"] for e in session_file(own_home)["events"]] == [1, 2, 3, 4] and fake.events == []
    asked = len(fake.calls)
    fire("local-tool", tool_data(own_home, 3), monkeypatch, capsys)
    assert len(fake.calls) == asked  # a hub that did not answer is left alone for a while
    fake.down = False
    monkeypatch.setattr(local_sync, "COOLDOWN", 0.0)
    fire("local-tool", tool_data(own_home, 4), monkeypatch, capsys)
    assert [e["seq"] for e in fake.events] == list(range(1, 9)) and session_file(own_home)["events"] == []


def test_a_hub_another_hook_found_silent_holds_local_sync_back_for_its_own_short_cooldown(
    own_home, fake, monkeypatch, capsys
):
    no_throttle(monkeypatch)
    fire("user-prompt", payload(own_home, prompt="go"), monkeypatch, capsys)
    now = time.time()
    hooks.HookState(own_home / ".evo" / "hub").mark_down(URL, now - local_sync.COOLDOWN - 1)  # Stop's 120 s still run
    assert hooks.HookState(own_home / ".evo" / "hub").down_since(URL, now) is not None
    fire("local-tool", tool_data(own_home, 1), monkeypatch, capsys)
    assert [e["seq"] for e in fake.events] == [1, 2] and session_file(own_home)["events"] == []


def test_a_hub_that_hangs_costs_a_hook_at_most_its_own_limits(own_home, monkeypatch, capsys):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    try:
        url = f"http://127.0.0.1:{server.getsockname()[1]}"
        (own_home / ".evo" / "hub" / "config.json").write_text(json.dumps({"url": url, "login": "alice"}))
        monkeypatch.setattr(MemorySync, "project_of", lambda self, where=None: "demo")
        monkeypatch.setitem(local_sync.LIMITS, "tool", (0.5, 1.0))
        started = time.monotonic()
        assert fire("local-tool", tool_data(own_home, 1), monkeypatch, capsys) == (0, "", "")
        assert time.monotonic() - started < 5
        assert len(session_file(own_home)["events"]) == 2  # kept for a later hook
    finally:
        server.close()


def test_a_hub_that_refuses_the_session_is_left_alone_for_a_while(own_home, monkeypatch, capsys):
    calls = []

    def refuse(method, url, headers, data, timeout):
        calls.append(method)
        return 403, b'{"message": "another member registered that session"}'

    monkeypatch.setattr(hub_client, "_send", refuse)
    monkeypatch.setattr(MemorySync, "project_of", lambda self, where=None: "demo")
    for n in (1, 2, 3):
        assert fire("local-tool", tool_data(own_home, n), monkeypatch, capsys) == (0, "", "")
    assert calls == ["PUT"]


def test_a_run_the_hub_ended_registers_again_at_the_next_prompt(own_home, fake, monkeypatch, capsys):
    fire("user-prompt", payload(own_home, prompt="go"), monkeypatch, capsys)
    fire("session-end", payload(own_home), monkeypatch, capsys)
    fire("user-prompt", payload(own_home, prompt="again"), monkeypatch, capsys)
    assert fake.state == "running" and len(fake.of("PUT", f"/local-runs/{SESSION}")) == 1  # the file knew it ended


def test_a_state_the_hub_cannot_find_registers_the_session_again(own_home, monkeypatch, capsys):
    hub = FakeHub()
    monkeypatch.setattr(hub_client, "_send", hub.send)
    monkeypatch.setattr(MemorySync, "project_of", lambda self, where=None: "demo")
    fire("user-prompt", payload(own_home, prompt="go"), monkeypatch, capsys)
    hub.run_id = 8  # the hub lost run 7 (a restored backup)
    fire("stop", payload(own_home), monkeypatch, capsys)
    assert session_file(own_home)["run_id"] == 8 and hub.state == "idle"


# The account


def test_the_account_comes_from_a_desktop_record_with_its_profile_else_from_the_cli_configuration(own_home):
    folder = own_home / "data" / "second"
    record = folder / "claude-code-sessions" / "acct" / "org" / "local_x.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"sessionId": SESSION, "owner": {"email": "second@example.com"}}))
    other = folder / "claude-code-sessions" / "acct" / "org" / "local_y.json"
    other.write_text(json.dumps({"sessionId": "someone-else", "owner": {"email": "wrong@example.com"}}))
    env = {local_account.FOLDERS_VARIABLE: f"{own_home / 'data' / 'first'}{os.pathsep}second={folder}"}
    assert local_account.read_account(SESSION, env, own_home) == local_account.Account("second@example.com", "second")

    (own_home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "cli@example.com"}}))
    assert local_account.read_account("unknown-session", env, own_home) == local_account.Account("cli@example.com")
    config = own_home / "profile-b"
    config.mkdir()
    (config / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "b@example.com"}}))
    assert local_account.read_account("s", {"CLAUDE_CONFIG_DIR": str(config)}, own_home).email == "b@example.com"


def test_an_account_that_cannot_be_read_is_unknown_and_blocks_nothing(own_home):
    assert local_account.read_account(SESSION, {}, own_home) == local_account.Account("unknown", None)
    (own_home / ".claude.json").write_text("{not json")
    broken = own_home / "d" / "claude-code-sessions" / "a" / "o" / "local_z.json"
    broken.parent.mkdir(parents=True)
    broken.write_text(f"{SESSION} but not json")
    env = {local_account.FOLDERS_VARIABLE: str(own_home / "d")}
    assert local_account.read_account(SESSION, env, own_home) == local_account.Account("unknown", None)


def _claude_profiles(home: Path, shared: str) -> dict[str, Path]:
    """Three profiles as claude-profiles keeps them, the default one with no folder, each with a copy of the shared
    session record (which holds no email) under its own account."""
    own = home / "Library" / "Application Support" / "Claude"
    company = home / "ClaudeCompany"
    third = home / "Library" / "Application Support" / "tech.omelet.ClaudeProfiles" / "Profiles" / "third"
    profiles = [
        {"color": 0, "id": "default", "name": "Personal", "shareSessions": True},
        {"color": 1, "dataDir": "~/ClaudeCompany", "id": "company", "name": "Company", "shareSessions": True},
        {"color": 2, "dataDir": str(third), "id": "third", "name": "Third", "shareSessions": True},
    ]
    folders = {"Personal": own, "Company": company, "Third": third}
    for name, folder in folders.items():
        record = folder / "claude-code-sessions" / f"acct-{name}" / "org" / "local_s.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"sessionId": "local_s", "cliSessionId": shared}), encoding="utf-8")
        (folder / "claude-code" / "2.1.299").mkdir(parents=True)
    third.parent.parent.joinpath("profiles.json").write_text(json.dumps({"profiles": profiles}), encoding="utf-8")
    return folders


def test_a_desktop_session_is_the_account_desktop_names_in_the_profile_whose_binary_runs_it(own_home):
    folders = _claude_profiles(own_home, SESSION)
    (own_home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "cli@example.com"}}))
    for name, folder in folders.items():
        env = {
            local_account.EMAIL_VARIABLE: f"{name.lower()}@example.com",
            local_account.ACCOUNT_VARIABLE: f"acct-{name}",
            local_account.BINARY_VARIABLE: str(folder / "claude-code" / "2.1.299" / "claude"),
        }
        assert local_account.read_account(SESSION, env, own_home) == local_account.Account(
            f"{name.lower()}@example.com", name
        )
        del env[local_account.BINARY_VARIABLE]  # the account's records are in one folder only
        assert local_account.read_account(SESSION, env, own_home).profile == name
    env = {local_account.EMAIL_VARIABLE: "x@example.com", local_account.ACCOUNT_VARIABLE: "../acct-Company"}
    assert local_account.read_account(SESSION, env, own_home) == local_account.Account("x@example.com", None)


def test_a_binary_left_by_the_session_desktop_was_started_from_names_no_profile(own_home):
    folders = _claude_profiles(own_home, SESSION)
    personal = str(folders["Personal"] / "claude-code" / "2.1.299" / "claude")  # Desktop does not set it then
    env = {
        local_account.EMAIL_VARIABLE: "third@example.com",
        local_account.ACCOUNT_VARIABLE: "acct-Third",
        local_account.BINARY_VARIABLE: personal,
    }
    assert local_account.read_account(SESSION, env, own_home) == local_account.Account("third@example.com", "Third")
    for name in ("Company", "Third"):  # one account in two profiles: the binary picks among those two only
        (folders[name] / "claude-code-sessions" / "acct-Shared").mkdir()
    env[local_account.ACCOUNT_VARIABLE] = "acct-Shared"
    assert local_account.read_account(SESSION, env, own_home).profile is None
    env[local_account.BINARY_VARIABLE] = str(folders["Company"] / "claude-code" / "2.1.299" / "claude")
    assert local_account.read_account(SESSION, env, own_home).profile == "Company"

    env[local_account.ACCOUNT_VARIABLE] = "acct-New"  # a profile with no records yet
    assert local_account.read_account(SESSION, env, own_home).profile is None
    fresh = own_home / "Fresh"
    (fresh / "claude-code" / "2.1.299").mkdir(parents=True)
    env |= {
        local_account.FOLDERS_VARIABLE: f"Fresh={fresh}{os.pathsep}Personal={folders['Personal']}",
        local_account.BINARY_VARIABLE: str(fresh / "claude-code" / "2.1.299" / "claude"),
    }
    assert local_account.read_account(SESSION, env, own_home).profile == "Fresh"


def test_a_desktop_session_filed_under_another_account_moves_at_its_next_prompt(own_home, fake, monkeypatch, capsys):
    _claude_profiles(own_home, SESSION)
    monkeypatch.setenv(local_account.EMAIL_VARIABLE, "third@example.com")
    monkeypatch.setenv(local_account.ACCOUNT_VARIABLE, "acct-Third")
    fire("user-prompt", payload(own_home, prompt="go"), monkeypatch, capsys)
    rec = session_file(own_home)
    assert rec["account"] == {"email": "third@example.com", "profile": "Third"}

    rec["account"] = {"email": "third@example.com", "profile": "Personal"}  # what an earlier evo-ak read
    (own_home / ".evo" / "hub" / "local" / f"{SESSION}.json").write_text(json.dumps(rec), encoding="utf-8")
    fire("stop", payload(own_home), monkeypatch, capsys)
    fire("user-prompt", payload(own_home, prompt="next"), monkeypatch, capsys)
    first, again = fake.of("PUT", f"/local-runs/{SESSION}")
    assert again["claude_profile"] == "Third" and again["title"] == first["title"] == "go"
    assert fake.state == "running" and session_file(own_home)["account"]["profile"] == "Third"
    fire("user-prompt", payload(own_home, prompt="once more"), monkeypatch, capsys)
    assert len(fake.of("PUT", f"/local-runs/{SESSION}")) == 2  # the same account registers nothing more


def test_without_desktop_variables_the_record_names_the_profile_when_only_one_folder_has_it(own_home):
    folders = _claude_profiles(own_home, SESSION)
    for name in ("Company", "Third"):
        (folders[name] / "claude-code-sessions" / f"acct-{name}" / "org" / "local_s.json").unlink()
    assert local_account.read_account(SESSION, {}, own_home) == local_account.Account("unknown", "Personal")


def test_the_profiles_of_claude_profiles_are_read_whatever_key_names_the_folder(own_home):
    data = own_home / "Library" / "Application Support" / "tech.omelet.ClaudeProfiles"
    data.mkdir(parents=True)
    folder = own_home / "elsewhere"
    record = folder / "claude-code-sessions" / "a" / "o" / "local_q.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"cliSessionId": SESSION, "emailAddress": "p@example.com"}))
    (data / "profiles.json").write_text(json.dumps({"profiles": [{"name": "Personal", "dataFolder": str(folder)}]}))
    assert local_account.read_account(SESSION, {}, own_home) == local_account.Account("p@example.com", "Personal")


# The plan and the run


def test_a_plan_patch_names_the_local_run_of_the_session_that_writes(own_home, fake, monkeypatch, capsys):
    fire("user-prompt", payload(own_home, prompt="go"), monkeypatch, capsys)
    assert local_sync.run_for(URL, "demo", {local_sync.SESSION_VARIABLE: SESSION}) == 7
    assert local_sync.run_for(URL, "demo", {}, cwd=str(own_home / "work")) == 7  # no session id: the newest in the cwd
    assert local_sync.run_for(URL, "other", {local_sync.SESSION_VARIABLE: SESSION}) is None
    assert local_sync.run_for(URL, "demo", {local_sync.SESSION_VARIABLE: "unknown-session"}) is None
    assert local_sync.run_for(URL, "demo", {}, cwd=str(own_home / "elsewhere")) is None
    assert local_sync.run_for(URL, "demo", {hooks.RUN_VARIABLE: "3", local_sync.SESSION_VARIABLE: SESSION}) is None

    sent = []

    class Plans:
        url = URL

        def call(self, method, path, body=None):
            sent.append((method, body))
            return {"revision": 4, "body": {"steps": []}}

    monkeypatch.setenv(local_sync.SESSION_VARIABLE, SESSION)
    plan_cli.patch_item(Plans(), "demo", "rollout", "steps", {"status": "done"}, step=1, if_revision=3)
    assert sent == [
        ("PATCH", {"section": "steps", "updates": {"status": "done"}, "step": 1, "if_revision": 3, "run_id": 7})
    ]
    fire("session-end", payload(own_home), monkeypatch, capsys)
    sent.clear()
    plan_cli.patch_item(Plans(), "demo", "rollout", "steps", {"status": "done"}, step=1, if_revision=3)
    assert "run_id" not in sent[0][1]  # an ended session names no run


def test_a_plan_patch_the_hub_refuses_for_its_run_is_made_without_it(monkeypatch, own_home):
    monkeypatch.setattr(local_sync, "run_for", lambda url, project, *a, **k: 7)
    sent = []

    class Plans:
        url = URL

        def call(self, method, path, body=None):
            sent.append(dict(body))
            if "run_id" in body:
                raise HubError("no such run", 404)
            return {"revision": 2}

    assert plan_cli.write_plan(Plans(), "PATCH", "/p", {"step": 1}, "demo") == {"revision": 2}
    assert sent == [{"step": 1, "run_id": 7}, {"step": 1}]
    with pytest.raises(HubError):  # any other refusal is the caller's

        class Refusing(Plans):
            def call(self, method, path, body=None):
                raise HubError("no", 409)

        plan_cli.write_plan(Refusing(), "PATCH", "/p", {"step": 1}, "demo")


# The whole of it against a hub


@pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
def test_a_session_of_a_project_is_a_local_run_with_its_log_state_account_and_plan_step(hub_db, tmp_path, monkeypatch):
    from sqlalchemy import select

    from evo_agents.hub import tables

    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN) as served:
        admin = Hub(served.url, live.insert_token(hub_db, live.ADMIN))
        admin.call("PUT", "/v1/projects/demo", _project())
        admin.call("PUT", "/v1/admin/projects/demo/grants/alice", {"role": "writer", "max_level": "internal"})
        token = live.insert_token(hub_db, "alice")
        home = tmp_path / "home"
        work = home / "work"
        work.mkdir(parents=True)
        directory = home / ".evo" / "hub"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        (directory / "token").write_text(token + "\n", encoding="utf-8")
        (directory / "config.json").write_text(json.dumps({"url": served.url, "login": "alice"}), encoding="utf-8")
        for name in ("token", "config.json"):
            os.chmod(directory / name, 0o600)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(MemorySync, "project_of", lambda self, where=None: "demo")
        no_throttle(monkeypatch)
        alice = Hub(served.url, token)
        alice.call("PUT", "/v1/projects/demo/plans/rollout", {"body": _plan()})

        hook_payload = {"session_id": SESSION, "cwd": str(work)}
        run_hook("user-prompt", {**hook_payload, "prompt": "ship it"})
        run_hook(
            "local-tool",
            {
                **hook_payload,
                "tool_name": "Bash",
                "tool_use_id": "t1",
                "tool_input": {"command": "ls"},
                "tool_response": {"stdout": f"k={SECRET}"},
            },
        )
        runs = tables.runs
        row = live.sql(hub_db, select(runs.c.id, runs.c.kind, runs.c.state, runs.c.cwd).where(runs.c.kind == "local"))
        assert len(row) == 1 and row[0][2] == "running" and row[0][3] == str(work)
        run_id = row[0][0]
        monkeypatch.setenv(local_sync.SESSION_VARIABLE, SESSION)
        plan_cli.patch_item(alice, "demo", "rollout", "steps", {"status": "in_progress"}, step=1)
        run_hook("stop", hook_payload)
        events = alice.call("GET", f"/v1/projects/demo/runs/{run_id}/events")
        assert [e["kind"] for e in events["events"] if e["kind"].startswith("tool")] == [
            "tool_call",
            "tool_call_update",
        ]
        assert SECRET not in json.dumps(events)
        view = alice.call("GET", f"/v1/projects/demo/runs/{run_id}")
        assert (view["state"], view["plan_id"], view["step_key"]) == ("idle", "rollout", "1")
        run_hook("session-end", hook_payload)
        assert alice.call("GET", f"/v1/projects/demo/runs/{run_id}")["state"] == "ended"
        revisions = tables.plan_revisions
        stored = live.sql(hub_db, select(revisions.c.revision, revisions.c.run_id).order_by(revisions.c.revision))
        assert stored[-1][1] == run_id


def run_hook(name: str, data: dict) -> None:
    import io
    import sys

    sys.stdin = io.StringIO(json.dumps(data))
    try:
        assert main(["hub", "hook", name]) == 0
    finally:
        sys.stdin = sys.__stdin__


def _project() -> dict:
    from tests.hub.test_hooks import live_project

    return live_project()


def _plan() -> dict:
    return {
        "id": "rollout",
        "goal": "Ship the rollout.",
        "repos": [{"repo": "app", "branch": "main", "status": "pending"}],
        "steps": [{"id": 1, "title": "Step 1", "repo": "app", "what": "do it", "status": "pending"}],
    }
