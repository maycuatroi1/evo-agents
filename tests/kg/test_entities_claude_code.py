"""The entities step through Claude Code (``evo_agents.kg.claude_code``): a command of evo-agents runs ``claude``
headless with the model the mapping names, on the subscription login and never an API key; the mapping names an
llm-backend sink that must clear each item; a refused login or a limit stops the step at the item and a later build
goes on from the memo; a machine without ``claude`` defers its items to a run that has it."""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from evo_agents.kg import claude_code, entities
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.build import build_project, unchanged
from tests.kg.test_extract_entities import entity_nodes, setup

MODEL_ID = "claude-haiku-5-5"
API_KEY = "sk-ant-api03-" + "k" * 24  # fictional values: the test checks they never show
OAUTH = "sk-ant-oat01-" + "o" * 24

# A stand-in for Claude Code: it logs its argv and what of the login it saw, and answers as ``claude -p
# --output-format json`` does. Its mode file says how: ok, nocost, login, limit, or limit-after-N.
FAKE_CLAUDE = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
prompt = sys.stdin.read()
with open(os.path.join(here, "calls.jsonl"), "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "api_key": "ANTHROPIC_API_KEY" in os.environ,
                          "oauth": "CLAUDE_CODE_OAUTH_TOKEN" in os.environ, "aliases": '"aliases"' in prompt}}) + "\\n")
calls = sum(1 for _ in open(os.path.join(here, "calls.jsonl")))
mode = open(os.path.join(here, "mode")).read().strip()
if mode.startswith("limit-after-"):
    mode = "ok" if calls <= int(mode.rsplit("-", 1)[1]) else "limit"
if mode == "login":
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                      "result": "Invalid API key " + token + " · Please run /login"}}))
    sys.exit(1)
if mode == "limit":
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                      "result": "Claude AI usage limit reached|1767225600"}}))
    sys.exit(1)
name = "Search" if "Intro" in prompt else "Obsolete"
answer = {{"nodes": [{{"kind": "Concept", "name": name, "evidence": "from the text", "conf": 0.8,
                      "aliases": [name + " engine"]}},
                     {{"kind": "Concept", "name": "Server", "evidence": "x"}}],
          "edges": [{{"src": name, "rel": "depends_on", "dst": "Server"}}]}}
result = {{"type": "result", "subtype": "success", "is_error": False,
          "result": "Here it is:\\n```json\\n" + json.dumps(answer) + "\\n```",
          "usage": {{"input_tokens": 40, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 10,
                    "output_tokens": 20}}}}
if mode != "nocost":
    result["total_cost_usd"] = 0.25
print(json.dumps(result))
"""

COMMAND = [sys.executable, "-m", "evo_agents.kg.claude_code"]


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """The fake claude, named by EVO_KG_CLAUDE; returns (calls, set_mode)."""
    where = tmp_path / "claude-bin"
    where.mkdir()
    script = where / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable))
    script.chmod(0o755)
    (where / "mode").write_text("ok")
    monkeypatch.setenv(claude_code.BINARY_ENV, str(script))
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH)

    def calls() -> list[dict]:
        log = where / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return calls, lambda mode: (where / "mode").write_text(mode)


def mapping(**extra) -> dict:
    return {"model": MODEL_ID, "command": COMMAND, "sink": "llm", **extra}


def run_command(request: dict) -> subprocess.CompletedProcess:
    return subprocess.run(COMMAND, input=json.dumps(request), capture_output=True, text=True, check=False)


REQUEST = {"text": "Intro to search.", "node_kinds": ["Concept"], "relations": ["depends_on"], "model": MODEL_ID}


def test_the_command_runs_claude_headless_with_the_model_and_returns_nodes_edges_and_usage(fake_claude):
    calls, _ = fake_claude
    done = run_command(REQUEST)
    assert done.returncode == 0, done.stdout + done.stderr
    got = json.loads(done.stdout)
    assert [n["name"] for n in got["nodes"]] == ["Search", "Server"] and got["edges"][0]["rel"] == "depends_on"
    assert got["usage"] == {"input_tokens": 100, "output_tokens": 20, "cost": 0.25}
    (call,) = calls()
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == MODEL_ID and "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "json" and argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert call["aliases"], "the prompt asks for the other names of an entity"


def test_claude_runs_on_the_login_never_on_an_api_key_and_no_token_is_printed(fake_claude):
    calls, set_mode = fake_claude
    run_command(REQUEST)
    assert calls()[-1] == {**calls()[-1], "api_key": False, "oauth": True}
    set_mode("login")
    done = run_command(REQUEST)
    assert done.returncode == claude_code.STOP_EXIT and json.loads(done.stdout)["stop"] == "login"
    assert OAUTH not in done.stdout + done.stderr and API_KEY not in done.stdout + done.stderr
    assert "[masked]" in json.loads(done.stdout)["error"]


def test_a_limit_is_a_stop_and_a_reply_without_a_result_is_a_failure(fake_claude):
    _, set_mode = fake_claude
    set_mode("limit")
    done = run_command(REQUEST)
    assert done.returncode == claude_code.STOP_EXIT and json.loads(done.stdout)["stop"] == "rate_limit"

    def broken(*args, **kwargs):
        return subprocess.CompletedProcess(args, 2, stdout="not json", stderr="boom")

    with pytest.raises(claude_code.ClaudeError, match="exited 2") as caught:
        claude_code.answer(REQUEST, runner=broken)
    assert not isinstance(caught.value, claude_code.ClaudeStop)
    out = io.StringIO()
    assert claude_code.main(stdin=io.StringIO("[1]"), stdout=out) == 1 and "not a JSON object" in out.getvalue()


def test_a_mapping_that_runs_claude_code_must_name_a_sink():
    for command in (COMMAND, ["evo-agents", "kg", "claude-entities"], ["/usr/local/bin/claude", "-p"]):
        with pytest.raises(kg_settings.SettingsError, match="name the llm-backend sink"):
            kg_settings.of({"entities": {"model": MODEL_ID, "node_kinds": ["Concept"], "command": command}})
    got = kg_settings.of({"entities": {"model": MODEL_ID, "node_kinds": ["Concept"], "command": COMMAND, "sink": "s"}})
    assert got.entities["sink"] == "s"
    assert not claude_code.uses([sys.executable, "model.py"])


def test_a_build_through_claude_code_states_entities_and_prices_the_tokens_without_a_cost(
    tmp_path, kg_env, fake_claude
):
    calls, set_mode = fake_claude
    set_mode("nocost")
    project, _ = setup(tmp_path, kg_env, extra=mapping(price={"input": 1.0, "output": 5.0}))
    report = build_project(project)
    assert report.ok, report.errors
    cov = report.coverage["entities"]
    assert cov["local"] is False and cov["sink"] == "llm" and cov["model"] == MODEL_ID
    assert cov["calls"] == len(calls()) == 3 and cov["tokens_in"] == 300 and cov["tokens_out"] == 60
    assert cov["cost"] == pytest.approx(300 / 1e6 * 1.0 + 60 / 1e6 * 5.0)
    found = {r[0]: json.loads(r[3]) for r in entity_nodes(project)}
    assert {"entity:Concept:search", "entity:Concept:server"} <= set(found)
    assert {p["model"] for p in found.values()} == {MODEL_ID}
    assert all(not c["api_key"] for c in calls())


def test_a_reported_cost_is_the_cost(tmp_path, kg_env, fake_claude):
    project, _ = setup(tmp_path, kg_env, extra=mapping(price={"input": 100.0, "output": 100.0}))
    assert build_project(project).coverage["entities"]["cost"] == pytest.approx(0.75)


def test_a_sink_that_does_not_clear_the_label_sends_no_text_to_claude(tmp_path, kg_env, fake_claude):
    calls, _ = fake_claude
    project, _ = setup(tmp_path, kg_env, extra=mapping(sink="llm-low"))
    cov = build_project(project).coverage["entities"]
    assert calls() == [] and cov["calls"] == 0 and cov["refused_count"] >= 2
    assert "above the clearance of sink 'llm-low'" in cov["refused"][0]["reason"]
    assert entity_nodes(project) == []


@pytest.mark.parametrize("mode, kind", [("login", "login"), ("limit-after-1", "limit")])
def test_a_refused_login_or_a_limit_stops_at_the_item_and_the_next_build_goes_on(
    tmp_path, kg_env, fake_claude, mode, kind
):
    calls, set_mode = fake_claude
    set_mode(mode)
    project, _ = setup(tmp_path, kg_env, extra=mapping())
    report = build_project(project)
    assert report.ok, report.errors
    cov = report.coverage["entities"]
    answered = 1 if mode.startswith("limit") else 0
    assert len(calls()) == answered + 1, "no item after the stop is sent"
    assert cov["calls"] == answered and cov["skipped"] == 3 - answered and cov["failed"] == 0
    assert "the model stopped the step" in cov["stopped"] and kind in cov["stopped"].lower()
    assert OAUTH not in json.dumps(report.coverage) and API_KEY not in json.dumps(report.coverage)
    assert unchanged(project) is None, "a build that stopped is not the build to skip"
    set_mode("ok")
    after = build_project(project).coverage["entities"]
    assert after["calls"] == 3 - answered and after["cache_hits"] == answered and after["skipped"] == 0
    assert entity_nodes(project)
    audit = (project.root / "audit.jsonl").read_text() if (project.root / "audit.jsonl").exists() else ""
    assert OAUTH not in audit and API_KEY not in audit


def test_without_claude_the_build_defers_its_items_and_takes_the_answers_of_a_run_that_has_it(
    tmp_path, kg_env, fake_claude, monkeypatch
):
    calls, _ = fake_claude
    script = Path(claude_code.binary())
    project, _ = setup(tmp_path, kg_env, extra=mapping())
    monkeypatch.setenv(claude_code.BINARY_ENV, str(tmp_path / "no-claude-here"))
    outbox: list[dict] = []
    report = build_project(project, entity_outbox=outbox)
    cov = report.coverage["entities"]
    assert report.ok and calls() == [] and cov["deferred"] == 3 and cov["skipped"] == 3
    assert "claude is not installed" in cov["stopped"] and entity_nodes(project) == []
    assert {r["model"] for r in outbox} == {MODEL_ID} and all(r["text"] and r["key"] for r in outbox)
    assert all(set(r) == {"key", "item", "text", "node_kinds", "relations", "model"} for r in outbox)

    monkeypatch.setenv(claude_code.BINARY_ENV, str(script))  # the run on the agent's machine, which has claude
    inbox = {r["key"]: claude_code.answer(r) for r in outbox}
    assert len(calls()) == 3
    monkeypatch.setenv(claude_code.BINARY_ENV, str(tmp_path / "no-claude-here"))
    after = build_project(project, entity_inbox=inbox).coverage["entities"]
    assert after["answered_elsewhere"] == 3 and after["calls"] == 0 and after["deferred"] == 0
    assert {"entity:Concept:search", "entity:Concept:server"} <= {r[0] for r in entity_nodes(project)}
    again = build_project(project, force=True, entity_outbox=[]).coverage["entities"]
    assert again["cache_hits"] == 3 and again["deferred"] == 0, "the answers are the build's memo now"


def test_a_command_that_asks_to_stop_halts_the_step(tmp_path, kg_env):
    stop = f'import sys; print(\'{{"stop": "login", "error": "no login"}}\'); sys.exit({entities.STOP_EXIT})'
    project, log = setup(tmp_path, kg_env, extra={"command": [sys.executable, "-c", stop]})
    cov = build_project(project).coverage["entities"]
    assert cov["calls"] == 0 and cov["skipped"] == 3 and cov["failed"] == 0 and "no login" in cov["stopped"]
