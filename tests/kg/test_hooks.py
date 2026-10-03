import io
import json
import stat
import sys
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.kg import serve, sessions
from evo_agents.kg.build import build_project
from evo_agents.kg.serve import Session
from evo_agents.kg.sync import sync_project
from tests.kg.projects import make_project

SAMPLE = Path(__file__).parent / "fixtures" / "hooks" / "post_tool_kg_search.json"
TOOL = "mcp__plugin_evo-kg_evo-kg__"


def sample() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def run_hook(monkeypatch, capsys, name: str, stdin: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = main(["kg", "hook", name])
    out, err = capsys.readouterr()
    return code, out, err


def post(monkeypatch, capsys, payload) -> tuple[int, str, str]:
    return run_hook(monkeypatch, capsys, "post-tool", json.dumps(payload))


def as_claude_code_sends(name: str, result: dict, session_id: str) -> dict:
    """A PostToolUse payload shaped like the captured sample around a real server result."""
    payload = sample()
    payload["session_id"] = session_id
    payload["tool_name"] = TOOL + name
    payload["tool_response"] = json.dumps(result["structuredContent"], ensure_ascii=False, separators=(",", ":"))
    return payload


@pytest.fixture
def unbound(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for var in ("CLAUDE_PROJECT_DIR", "EVO_KG_PROJECT", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)


def test_post_tool_reads_the_captured_sample(fake_project, kg_env, monkeypatch, capsys, unbound):
    project, _ = fake_project
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project.harness.root))
    payload = sample()
    code, out, err = post(monkeypatch, capsys, payload)
    assert (code, out, err) == (0, "", "")
    record = sessions.read_session(payload["session_id"])
    assert record["level"] == "internal"  # join of internal and public
    assert record["location"] == "any" and record["integrity"] == "U"
    assert record["projects"] == ["proj"]
    assert record["results_read"] == 2
    assert record["session_id"] == payload["session_id"] and record["last_updated"]
    assert sessions.session_path(payload["session_id"]).name == payload["session_id"] + ".json"


def built(tmp_path, home):
    project = make_project(tmp_path, home, level="public", app_level="customer")
    sync_project(project)
    assert build_project(project).ok
    return project


def test_post_tool_joins_across_calls(tmp_path, kg_env, monkeypatch, capsys, unbound):
    project = built(tmp_path, kg_env)
    session = Session(project, "wide")
    sid = "11111111-2222-4333-8444-555555555555"

    plans = session.call("kg_search", {"query": "demo", "kinds": ["Plan"]})
    assert plans["structuredContent"]["label"] == {
        "level": "public",
        "location": "any",
        "integrity": "U",
        "projects": ["demo"],
    }
    assert post(monkeypatch, capsys, as_claude_code_sends("kg_search", plans, sid))[:2] == (0, "")
    first = sessions.read_session(sid)
    assert first["level"] == "public" and first["projects"] == ["demo"]
    assert first["results_read"] == len(plans["structuredContent"]["results"]) > 0

    symbols = session.call("kg_search", {"query": "rank_results"})
    assert post(monkeypatch, capsys, as_claude_code_sends("kg_search", symbols, sid))[:2] == (0, "")
    second = sessions.read_session(sid)
    assert second["level"] == "customer"
    assert second["results_read"] == first["results_read"] + len(symbols["structuredContent"]["results"])

    # A later public read never lowers the label.
    assert post(monkeypatch, capsys, as_claude_code_sends("kg_search", plans, sid))[0] == 0
    assert sessions.read_session(sid)["level"] == "customer"


def test_post_tool_counts_what_a_truncated_result_reveals(tmp_path, kg_env, monkeypatch, capsys, unbound):
    project = built(tmp_path, kg_env)
    session = Session(project, "wide")
    monkeypatch.setattr(serve, "CAP_CHARS", 300)
    # The use case comes from the public harness; the symbol implementing it from the customer repo.
    card = session.call("kg_node", {"id": "usecase:KB-01"})
    data = card["structuredContent"]
    assert data["truncated"] and data["project"] == "demo" and data["label"]["level"] == "customer"
    sid = "truncated-session"
    assert post(monkeypatch, capsys, as_claude_code_sends("kg_node", card, sid))[0] == 0
    record = sessions.read_session(sid)
    assert record["level"] == "customer" and record["results_read"] == 0

    status = session.call("kg_status", {})  # reveals no element: nothing to record
    assert "label" not in status["structuredContent"]
    assert post(monkeypatch, capsys, as_claude_code_sends("kg_status", status, sid))[0] == 0
    assert sessions.read_session(sid) == record


def test_post_tool_unknown_level_ranks_highest(kg_env, monkeypatch, capsys, unbound):
    payload = sample()
    response = json.loads(payload["tool_response"])
    response["results"][0]["label"]["level"] = "restricted"
    payload["tool_response"] = json.dumps(response)
    assert post(monkeypatch, capsys, payload)[0] == 0
    assert post(monkeypatch, capsys, sample())[0] == 0
    record = sessions.read_session(payload["session_id"])
    assert record["level"] == "restricted" and record["results_read"] == 4
    assert record["projects"] == []  # no project in the result and none bound here


@pytest.mark.parametrize(
    "tool_name", ["Grep", "Bash", "mcp__other__kg_search", "mcp__plugin_other_evo-kg__kg_search", None, 3]
)
def test_post_tool_ignores_other_tools(kg_env, monkeypatch, capsys, unbound, tool_name):
    payload = sample()
    payload["tool_name"] = tool_name
    assert post(monkeypatch, capsys, payload) == (0, "", "")
    assert not sessions.sessions_dir().exists()


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "[1, 2]",
        "null",
        '{"tool_name": "mcp__plugin_evo-kg_evo-kg__kg_search"}',
        '{"tool_name": "mcp__plugin_evo-kg_evo-kg__kg_search", "session_id": "s", "tool_response": "{broken"}',
        '{"tool_name": "mcp__plugin_evo-kg_evo-kg__kg_search", "session_id": "s", "tool_response": 7}',
        '{"tool_name": "mcp__plugin_evo-kg_evo-kg__kg_search", "session_id": "s",'
        ' "tool_response": "{\\"results\\": [{\\"id\\": 1, \\"label\\": \\"internal\\"}]}"}',
    ],
)
def test_post_tool_malformed_stdin_exits_zero(kg_env, monkeypatch, capsys, unbound, stdin):
    code, out, _ = run_hook(monkeypatch, capsys, "post-tool", stdin)
    assert (code, out) == (0, "")
    assert not list(sessions.sessions_dir().glob("*.json"))


@pytest.mark.parametrize("shape", ["mcp-result", "content-blocks", "object"])
def test_post_tool_accepts_other_response_shapes(kg_env, monkeypatch, capsys, unbound, shape):
    payload = sample()
    data = json.loads(payload["tool_response"])
    text = json.dumps(data)
    payload["tool_response"] = {
        "mcp-result": {"content": [{"type": "text", "text": "2 result(s)"}], "structuredContent": data},
        "content-blocks": [{"type": "text", "text": "2 result(s)"}, {"type": "text", "text": text}],
        "object": data,
    }[shape]
    assert post(monkeypatch, capsys, payload)[0] == 0
    assert sessions.read_session(payload["session_id"])["results_read"] == 2


def test_post_tool_error_exits_zero_with_a_note(kg_env, monkeypatch, capsys, unbound):
    def boom(payload):
        raise PermissionError("sessions directory is read-only")

    monkeypatch.setattr(sessions, "post_tool", boom)
    code, out, err = post(monkeypatch, capsys, sample())
    assert (code, out) == (0, "")
    assert err.startswith("evo-kg post-tool hook skipped: PermissionError")


def test_post_tool_file_permissions(kg_env, monkeypatch, capsys, unbound):
    folder = sessions.sessions_dir()
    folder.mkdir(parents=True, mode=0o755)
    folder.chmod(0o755)
    payload = sample()
    assert post(monkeypatch, capsys, payload)[0] == 0
    assert post(monkeypatch, capsys, payload)[0] == 0
    path = sessions.session_path(payload["session_id"])
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in folder.iterdir()) == [".lock", path.name]  # no temporary file left behind


@pytest.mark.parametrize(
    "session_id",
    ["../../outside", "/etc/passwd", "a/b", "a\\b", "..", ".hidden", "x" * 300, "phiên-một", "a\nb", "a b"],
)
def test_post_tool_sanitizes_session_id(kg_env, monkeypatch, capsys, unbound, session_id):
    payload = sample()
    payload["session_id"] = session_id
    assert post(monkeypatch, capsys, payload)[0] == 0
    files = list(sessions.sessions_dir().glob("*.json"))
    assert len(files) == 1 and files[0].parent == sessions.sessions_dir()
    name = files[0].stem
    assert name == sessions.safe_session_id(session_id) and len(name) <= 96
    assert not name.startswith(".") and all(c.isascii() and (c.isalnum() or c in "-_") for c in name)
    assert sessions.read_session(session_id)["session_id"] == session_id


def test_post_tool_session_ids_never_share_a_file(kg_env, unbound):
    ids = ["a/b", "a_b", "a.b", "x" * 300, "x" * 301, "0f6e2a4c-1b7d-4e39-9a58-2c3d4e5f6a7b"]
    names = [sessions.safe_session_id(i) for i in ids]
    assert len(set(names)) == len(ids)
    assert names[-1] == ids[-1]  # a UUID is kept as is
    assert sessions.safe_session_id("") is None and sessions.safe_session_id(None) is None
    assert sessions.session_path(42) is None


def test_post_tool_label_shows_in_status_and_session_start(tmp_path, kg_env, monkeypatch, capsys, unbound):
    project = built(tmp_path, kg_env)
    session = Session(project, "wide")
    sid = "22222222-3333-4444-8555-666666666666"
    found = session.call("kg_search", {"query": "rank_results"})
    assert post(monkeypatch, capsys, as_claude_code_sends("kg_search", found, sid))[0] == 0

    before = session.call("kg_status", {})
    assert "session label" not in before["content"][0]["text"]  # the server does not know the session
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    status = session.call("kg_status", {})
    assert "session label customer,U over demo" in status["content"][0]["text"]
    assert status["structuredContent"]["session"]["level"] == "customer"

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project.harness.root))
    stdin = json.dumps({"session_id": sid, "hook_event_name": "SessionStart", "source": "resume"})
    code, out, _ = run_hook(monkeypatch, capsys, "session-start", stdin)
    note = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert code == 0 and "session label customer,U over demo" in note
    fresh = json.dumps({"session_id": "33333333-4444-4555-8666-777777777777", "source": "startup"})
    code, out, _ = run_hook(monkeypatch, capsys, "session-start", fresh)
    assert code == 0 and "session label" not in json.loads(out)["hookSpecificOutput"]["additionalContext"]
