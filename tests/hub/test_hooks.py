"""The evo-hub plugin: its hooks (``evo-agents hub hook session-start|stop``), its files, and its eval cases.

The hooks never fail a session: exit status 0 whatever happens, one line at most, no token and no memory text in it,
no request when not signed in, no request from Stop when no memory file changed, a hub that hangs costs at most the
run's budget, a hub that does not answer is left alone for a while and the memory files wait for a later Stop, and a
sync another session holds is waited for briefly. SessionStart pulls memories, exports the plan copies of a harness
whose hub.project is set without overwriting a copy edited by hand, and counts the skills to sync; running it twice
changes nothing. Two machines of one person share a memory through Stop then SessionStart against ``hub serve``.

No test touches the real ~/.claude or ~/.evo: every one runs with a home directory of its own and without
CLAUDE_CONFIG_DIR, CLAUDE_PROJECT_DIR or CODEX_HOME. Tests that need the hub skip without EVO_HUB_TEST_DSN."""

import io
import json
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from evo_agents.cli import main
from evo_agents.harness import plan_digest, validate_harness
from evo_agents.hub import client as hub_client
from evo_agents.hub import hooks
from evo_agents.hub import memory as hub_memory
from evo_agents.hub.mcp_tools import TOOLS
from evo_agents.hub.memory import INDEX, MemorySync, slug
from evo_agents.hub.mirror import export, read_plan, render
from evo_agents.schema import errors
from tests.hub import live, pg

ROOT = Path(__file__).parents[2]
PLUGIN = ROOT / "plugins" / "evo-hub"
EVALS = PLUGIN / "evals"
TOOL = "mcp__plugin_evo-hub_evo-hub__"
MARKER = "Wombat-Memory-Text"  # in memory bodies only: must never reach a hook's output or the hub's log
HOST_VARIABLES = ("CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "CODEX_HOME", "EVO_KG_PROJECT", "EVO_KG_HOME")

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


@pytest.fixture(autouse=True)
def own_home(tmp_path, monkeypatch) -> Path:
    """A home directory of this test's own: nothing can reach the real ~/.claude or ~/.evo."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for variable in HOST_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    return home


def sign_in(home: Path, url: str, login: str = "alice", token: str = "evo_test_token_never_printed") -> str:
    directory = home / ".evo" / "hub"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": login}), encoding="utf-8")
    for name in ("token", "config.json"):
        os.chmod(directory / name, 0o600)
    return token


def memory_dir(home: Path, session: Path) -> Path:
    return home / ".claude" / "projects" / slug(session) / "memory"


def typed(kind: str, text: str) -> str:
    return f"---\nname: {kind} note\ndescription: about {kind}\nmetadata:\n  type: {kind}\n---\n{text}\n"


def run_hook(name: str, payload, monkeypatch, capsys) -> tuple[int, str, str]:
    """The hook in this process, with ``payload`` (a dict, or text as is) on stdin."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    code = main(["hub", "hook", name])
    out, err = capsys.readouterr()
    return code, out, err


def context_of(out: str) -> dict:
    """The JSON a SessionStart hook printed: additionalContext, and systemMessage when there is one."""
    lines = out.splitlines()
    assert len(lines) == 1, out
    data = json.loads(lines[0])
    assert set(data) <= {"hookSpecificOutput", "systemMessage"} and "decision" not in data and "continue" not in data
    assert data["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    return {"context": data["hookSpecificOutput"]["additionalContext"], "message": data.get("systemMessage")}


@pytest.fixture
def requests(monkeypatch) -> list:
    """Every request a hook sends, recorded; each still goes out."""
    sent = []
    real = hub_client._send

    def recording(method, url, headers, data, timeout):
        sent.append((method, url))
        return real(method, url, headers, data, timeout)

    monkeypatch.setattr(hub_client, "_send", recording)
    return sent


def closed_port_url() -> str:
    return f"http://127.0.0.1:{pg.free_port()}"


# Signed out, and the cases that must send nothing


def test_not_signed_in_nothing_is_sent_and_session_start_says_how_to_sign_in(own_home, monkeypatch, capsys, requests):
    session = own_home / "work"
    code, out, err = run_hook("session-start", {"cwd": str(session), "source": "startup"}, monkeypatch, capsys)
    assert (code, err) == (0, "")
    told = context_of(out)
    assert told["context"].startswith("evo-hub: not signed in to a hub, so nothing was synced")
    assert "evo-agents hub login --url URL" in told["context"] and told["message"] is None
    assert run_hook("stop", {"cwd": str(session)}, monkeypatch, capsys) == (0, "", "")
    assert requests == [] and not (own_home / ".evo").exists()


def test_stdin_that_is_not_json_or_huge_is_ignored(own_home, monkeypatch, capsys, requests):
    for payload in ("not json", "[1, 2]", "", "{" * (hooks.MAX_PAYLOAD + 10)):
        code, out, _ = run_hook("session-start", payload, monkeypatch, capsys)
        assert code == 0 and "not signed in" in context_of(out)["context"]
        assert run_hook("stop", payload, monkeypatch, capsys) == (0, "", "")
    assert requests == []


def test_stop_sends_nothing_when_no_memory_file_changed(own_home, monkeypatch, capsys, requests):
    url = closed_port_url()
    sign_in(own_home, url)
    session = own_home / "ws" / "demo-harness"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session))
    assert run_hook("stop", {"cwd": "/elsewhere"}, monkeypatch, capsys) == (0, "", "")  # no memory directory yet
    directory = memory_dir(own_home, session)
    directory.mkdir(parents=True)
    (directory / INDEX).write_text("- [Note](note.md)\n", encoding="utf-8")
    (directory / "old.conflict-laptop.md").write_text("a conflict copy, never pushed", encoding="utf-8")
    assert run_hook("stop", {}, monkeypatch, capsys) == (0, "", "")  # MEMORY.md and conflict copies never count
    assert requests == []

    note = directory / "note.md"
    note.write_bytes(typed("project", MARKER).encode())
    state = own_home / ".evo" / "hub" / "memory-state.json"
    entry = {"id": 7, "revision": 1, "sha256": hub_memory._sha(note.read_bytes()), "deleted": False}
    record = {**entry, "scope": "project", "project": "demo", "location": "harness", "type": "project"}
    state.write_text(
        json.dumps({"version": 1, "hubs": {url: {"files": {f"{slug(session)}/note.md": record}}}}), encoding="utf-8"
    )
    assert run_hook("stop", {}, monkeypatch, capsys) == (0, "", "")  # synced as it is: nothing to send
    assert requests == []
    note.write_bytes(typed("project", MARKER + " changed").encode())
    code, out, err = run_hook("stop", {}, monkeypatch, capsys)
    assert (code, out) == (0, "") and len(requests) == 1  # one try, refused: the hub is down
    assert err.startswith("evo-hub: memories not pushed, the hub did not answer") and err.count("\n") == 1
    assert MARKER not in err and "evo_test_token" not in err


def test_pending_counts_changed_and_new_files_only(own_home):
    sync = MemorySync(hub_client.Hub("https://hub.test"), "alice")
    session = own_home / "work"
    assert sync.pending(session) is False
    directory = memory_dir(own_home, session)
    directory.mkdir(parents=True)
    (directory / INDEX).write_text("index", encoding="utf-8")
    (directory / ".hidden.md").write_text("hidden", encoding="utf-8")
    (directory / "x.conflict-host.md").write_text("copy", encoding="utf-8")
    (directory / "big.md").write_bytes(b"x" * (hub_memory.MAX_BODY + 1))
    (directory / "link.md").symlink_to(own_home / "outside.md")
    (own_home / "outside.md").write_text("outside", encoding="utf-8")
    assert sync.pending(session) is False  # none of these is ever pushed
    (directory / "new.md").write_text(typed("user", "new"), encoding="utf-8")
    assert sync.pending(session) is True
    state = hub_memory.State(own_home / ".evo" / "hub" / "memory-state.json", "https://hub.test")
    held = {"id": 1, "revision": 1, "deleted": False, "body": typed("user", "new")}
    state.set(f"{slug(session)}/new.md", {**held, "scope": "personal", "location": "work", "type": "user"})
    (own_home / ".evo" / "hub").mkdir(parents=True)
    state.save()
    assert sync.pending(session) is False
    other = hub_memory.State(own_home / ".evo" / "hub" / "memory-state.json", "https://other.test")
    assert other.get(f"{slug(session)}/new.md") is None  # the state is per hub
    assert MemorySync(hub_client.Hub("https://other.test"), "alice").pending(session) is True


# A hub that is down, hangs, or is busy with another sync


def test_a_hub_that_does_not_answer_is_left_alone_for_a_while(own_home, monkeypatch, capsys, requests):
    url = closed_port_url()
    sign_in(own_home, url)
    session = own_home / "work"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session))
    directory = memory_dir(own_home, session)
    directory.mkdir(parents=True)
    (directory / "note.md").write_text(typed("user", MARKER), encoding="utf-8")
    clock = [1_000_000.0]
    monkeypatch.setattr(hooks, "_now", lambda: clock[0])

    code, out, err = run_hook("stop", {}, monkeypatch, capsys)
    assert (code, out) == (0, "") and "did not answer" in err and len(requests) == 1
    held = json.loads((own_home / ".evo" / "hub" / hooks.STATE_FILE).read_text(encoding="utf-8"))
    assert held == {"version": 1, "down": {url: clock[0]}}
    assert oct(os.stat(own_home / ".evo" / "hub" / hooks.STATE_FILE).st_mode & 0o777) == "0o600"

    clock[0] += hooks.COOLDOWN - 1
    code, out, err = run_hook("stop", {}, monkeypatch, capsys)
    assert (code, out) == (0, "") and len(requests) == 1  # not asked again yet
    assert f"{url} did not answer at" in err and "a later Stop" in err

    clock[0] += 2
    assert run_hook("stop", {}, monkeypatch, capsys)[0] == 0 and len(requests) == 2  # asked again after the cooldown

    # SessionStart always tries once, and says so in its line for the person and the model.
    code, out, err = run_hook("session-start", {}, monkeypatch, capsys)
    told = context_of(out)
    assert (code, err) == (0, "") and len(requests) == 3
    assert told["context"].startswith(f"evo-hub {url}, no hub project here (personal memories): the hub did not answer")
    assert told["message"] == told["context"] and MARKER not in out


def hanging_server() -> socket.socket:
    """A port that takes connections and never answers."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    return server


def test_a_hub_that_hangs_costs_at_most_the_budget(own_home, monkeypatch, capsys):
    server = hanging_server()
    try:
        url = f"http://127.0.0.1:{server.getsockname()[1]}"
        sign_in(own_home, url)
        session = own_home / "work"
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session))
        directory = memory_dir(own_home, session)
        directory.mkdir(parents=True)
        (directory / "note.md").write_text(typed("user", MARKER), encoding="utf-8")
        monkeypatch.setattr(hooks, "REQUEST_TIMEOUT", 60.0)  # the budget, not the request timeout, has to stop it
        monkeypatch.setattr(hooks, "BUDGETS", {"session-start": 1.0, "stop": 1.0})

        started = time.monotonic()
        code, out, err = run_hook("session-start", {}, monkeypatch, capsys)
        assert code == 0 and time.monotonic() - started < 5
        told = context_of(out)
        assert "stopped after 1s with what was done" in told["context"] and told["message"] == told["context"]

        started = time.monotonic()
        code, out, err = run_hook("stop", {}, monkeypatch, capsys)
        assert (code, out) == (0, "") and time.monotonic() - started < 5
        assert err == "evo-hub: memory push stopped after 1s; what is left goes at a later Stop\n"
    finally:
        server.close()
    assert sorted(p.name for p in (own_home / ".evo" / "hub").iterdir()) == ["config.json", "memory.lock", "token"]
    assert MemorySync(hub_client.Hub(url), "alice").pending(session) is True  # it waits for a later Stop


def test_a_sync_another_session_holds_is_waited_for_briefly(own_home, monkeypatch, capsys):
    sign_in(own_home, closed_port_url())
    session = own_home / "work"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session))
    directory = memory_dir(own_home, session)
    directory.mkdir(parents=True)
    (directory / "note.md").write_text(typed("user", "note"), encoding="utf-8")
    monkeypatch.setattr(hooks, "LOCK_WAIT", 0.3)
    with hub_memory.sync_lock(own_home / ".evo" / "hub"):
        started = time.monotonic()
        code, out, err = run_hook("stop", {}, monkeypatch, capsys)
        assert (code, out) == (0, "") and time.monotonic() - started < 3
        assert err.startswith("evo-hub: memories not pushed: another `evo-agents hub memory` run on this machine held")
    code, out, err = run_hook("stop", {}, monkeypatch, capsys)  # released: this run tries the hub
    assert (code, out) == (0, "") and "did not answer" in err


def test_an_unexpected_error_is_named_by_its_type_only(own_home, monkeypatch, capsys):
    sign_in(own_home, closed_port_url())

    def broken(*args, **kwargs):
        raise KeyError(MARKER)

    monkeypatch.setattr(hooks, "_pull_memories", broken)
    code, out, err = run_hook("session-start", {}, monkeypatch, capsys)
    told = context_of(out)
    assert (code, err) == (0, "") and "the SessionStart hook failed: unexpected KeyError" in told["context"]
    assert MARKER not in out
    monkeypatch.setattr(MemorySync, "pending", broken)
    code, out, err = run_hook("stop", {}, monkeypatch, capsys)
    assert (code, out) == (0, "") and err == "evo-hub: the Stop hook failed: unexpected KeyError\n"


def test_a_closed_stdout_does_not_fail_the_hook(own_home, monkeypatch):
    class Closed(io.StringIO):
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "stdout", Closed())
    assert main(["hub", "hook", "session-start"]) == 0


# The plugin's hooks.json, run as Claude Code runs it


def fake_uvx(bin_dir: Path, exit_code: int | None = None) -> Path:
    """``uvx`` that runs this checkout's evo_agents with the arguments after ``evo-agents``, or fails."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "uvx"
    if exit_code is None:
        body = f'while [ "$1" != "evo-agents" ]; do shift; done\nshift\nexec "{sys.executable}" -m evo_agents "$@"\n'
    else:
        body = f"echo 'uvx: something broke' >&2\nexit {exit_code}\n"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


def plugin_hooks() -> dict:
    return json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]


def test_hooks_json_runs_session_start_and_stop_with_more_time_than_their_budget():
    found = plugin_hooks()
    assert sorted(found) == ["SessionStart", "Stop"]
    (start,) = found["SessionStart"]
    assert start["matcher"] == "startup|resume|clear"
    (stop,) = found["Stop"]
    assert "matcher" not in stop
    for group, name in ((start, "session-start"), (stop, "stop")):
        (hook,) = group["hooks"]
        assert hook["type"] == "command" and hook["command"].endswith(f" evo-agents hub hook {name} || true")
        assert hook["timeout"] >= hooks.BUDGETS[name] + 5  # uvx and the interpreter start inside Claude Code's limit
    assert hooks.REQUEST_TIMEOUT < min(hooks.BUDGETS.values())


def test_the_hook_commands_exit_0_with_uvx_missing_failing_or_working(own_home, tmp_path):
    payload = json.dumps({"session_id": "s1", "cwd": str(own_home), "hook_event_name": "SessionStart"})
    commands = {name: group[0]["hooks"][0]["command"] for name, group in plugin_hooks().items()}

    def run(command: str, path: str) -> subprocess.CompletedProcess:
        env = pg.clean_env(HOME=str(own_home), PATH=path)
        for variable in HOST_VARIABLES:
            env.pop(variable, None)
        return subprocess.run(["/bin/sh", "-c", command], input=payload, env=env, capture_output=True, text=True)

    for command in commands.values():
        missing = run(command, str(tmp_path / "empty"))
        assert (missing.returncode, missing.stdout) == (0, "")
        failing = run(command, str(fake_uvx(tmp_path / "failing", exit_code=2).parent))
        assert (failing.returncode, failing.stdout) == (0, "")
    working = str(fake_uvx(tmp_path / "working").parent)
    started = run(commands["SessionStart"], working)
    assert started.returncode == 0 and "not signed in" in context_of(started.stdout)["context"]
    stopped = run(commands["Stop"], working)
    assert (stopped.returncode, stopped.stdout, stopped.stderr) == (0, "", "")


# Plan copies the export leaves alone


class PlansHub:
    """The plan reads of a hub, in memory: what export calls."""

    url = "https://hub.test"

    def __init__(self, project: str = "demo"):
        self.project = project
        self.plans: dict[str, dict] = {}

    def add(self, body: dict, area: str = "active", revision: int = 1) -> None:
        digest = plan_digest(body)
        self.plans[body["id"]] = {"plan_id": body["id"], "area": area, "revision": revision, "digest": digest}
        self.plans[body["id"]]["body"] = body

    def call(self, method, path, body=None):
        assert method == "GET"
        base = f"/v1/projects/{self.project}/plans"
        if path == base:
            return [{k: v for k, v in plan.items() if k != "body"} for plan in self.plans.values()]
        return self.plans[path.removeprefix(base + "/")]


def plan(plan_id: str, status: str = "pending") -> dict:
    return {"id": plan_id, "steps": [{"id": 1, "what": "the one step", "status": status}]}


def make_harness(root: Path, project: str = "demo") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "harness.yaml").write_text(
        f"name: {project}\nworkspace: {root.parent}\nknowledge_file: knowledge.yaml\nhub: {{project: {project}}}\n"
        "repos:\n  - {name: app, path: app}\n",
        encoding="utf-8",
    )
    (root / "knowledge.yaml").write_text(
        f"version: 1\nproject: {project}\npolicy:\n  levels: [public, internal, customer, secret]\n"
        "  sinks:\n    - {id: claude-code@anthropic, kind: agent-session, clearance: {level: internal}}\n"
        "    - {id: evo-hub, kind: hub, clearance: {level: internal}}\n"
        "sources:\n  - {id: harness, connector: harness, label: {level: internal, integrity: U}}\n",
        encoding="utf-8",
    )
    return root


def test_export_for_the_hook_leaves_hand_edits_and_files_it_did_not_write(tmp_path):
    root = make_harness(tmp_path / "demo-harness")
    hub = PlansHub()
    for name in ("fresh", "edited", "foreign", "moved"):
        hub.add(plan(name))
    export(hub, root)
    active = root / "plans" / "active"
    edited, foreign, moved = active / "edited.yaml", active / "foreign.yaml", active / "moved.yaml"
    edited.write_text(edited.read_text(encoding="utf-8").replace("status: pending", "status: done"), encoding="utf-8")
    foreign.write_text(yaml.safe_dump(plan("foreign", "blocked")), encoding="utf-8")  # a draft with no hub key
    moved.write_text(moved.read_text(encoding="utf-8").replace("the one step", "by hand"), encoding="utf-8")
    hub.add({**plan("fresh"), "goal": "changed on the hub"}, revision=2)
    hub.add(plan("edited", "in_progress"), revision=2)
    hub.add(plan("moved", "done"), area="completed", revision=2)
    held = {path.name: path.read_bytes() for path in (edited, foreign, moved)}

    result = export(hub, root, keep_edited=True)
    statuses = {p["plan_id"]: p["status"] for p in result.plans}
    assert statuses == {"edited": "kept", "foreign": "kept", "fresh": "written", "moved": "written"}
    assert {path.name: path.read_bytes() for path in (edited, foreign, moved)} == held  # nobody's work overwritten
    assert read_plan(active / "fresh.yaml").hub["revision"] == 2
    assert result.removed == [] and (root / "plans" / "completed" / "moved.yaml").exists()
    assert any(
        note.startswith("plans/active/edited.yaml was edited outside the hub (digest mismatch)")
        and "`evo-agents hub plan put plans/active/edited.yaml --if-revision 1`" in note
        for note in result.notes
    )
    assert any(
        note.startswith("plans/active/foreign.yaml is not a copy of a plan of hub project demo")
        for note in result.notes
    )
    assert any(note.startswith("plans/active/moved.yaml: plan moved is completed on the hub") for note in result.notes)
    assert result.changed == ["plans/active/fresh.yaml", "plans/completed/moved.yaml"]

    # The CLI's export restores: every file becomes the hub's copy again.
    restored = export(hub, root)
    assert {p["plan_id"]: p["status"] for p in restored.plans} == {
        "edited": "written",
        "foreign": "written",
        "fresh": "unchanged",
        "moved": "unchanged",
    }
    assert restored.removed == ["plans/active/moved.yaml"]
    assert [str(i) for r in validate_harness(root) for i in errors(r.issues)] == []


# The plugin's files and eval cases


def test_the_skill_names_itself_and_the_tools_it_teaches():
    text = (PLUGIN / "skills" / "using-agent-hub" / "SKILL.md").read_text(encoding="utf-8")
    meta = yaml.safe_load(text.split("---")[1])
    assert meta["name"] == "using-agent-hub"
    for word in ("memory_search", "memory_get", "plan_step", "plan_show", "hub.project", "read-only"):
        assert word in meta["description"], word
    table = set(re.findall(r"^\| `([a-z_*]+)` \|", text, re.MULTILINE))
    hub_tools = {tool["name"] for tool in TOOLS if not tool["name"].startswith("kg_")}
    assert table == hub_tools | {"kg_*"}


def test_eval_mocks_carry_the_real_tool_list_and_name_real_tools():
    # The evals answer from mocks; _tools.json gives the mocked tools their real descriptions and schemas.
    saved = EVALS / "mocks" / "evo-hub" / "_tools.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == {"tools": TOOLS}
    names = {tool["name"] for tool in TOOLS}
    mocks = [path for path in EVALS.rglob("mocks/evo-hub/*.md")]
    assert mocks and {path.stem for path in mocks} <= names
    cases = sorted(path.parent.name for path in EVALS.glob("*/case.yaml"))
    assert cases == ["memory-recall", "plan-step-through-hub"]
    for case in cases:
        front = yaml.safe_load((EVALS / case / "prompt.md").read_text(encoding="utf-8").split("---")[1])
        assert front["name"] == case and "Bash" not in front["allowed_tools"]  # a run never reaches a real hub
        tools = [tool for tool in front["allowed_tools"] if tool.startswith("mcp__")]
        assert tools and {tool.removeprefix(TOOL) for tool in tools} <= names
        for grader in (EVALS / case / "graders").glob("*.md"):
            spec = yaml.safe_load(grader.read_text(encoding="utf-8").split("---")[1])
            if str(spec.get("tool", "")).startswith("mcp__"):
                assert spec["tool"].removeprefix(TOOL) in names, grader


def test_the_plan_step_case_starts_from_a_copy_the_hub_wrote(tmp_path):
    case = EVALS / "plan-step-through-hub"
    work = tmp_path / "case"
    work.mkdir()
    subprocess.run(["/bin/sh", str(case / "scaffold.sh")], cwd=work, check=True)
    assert [str(i) for r in validate_harness(work) for i in errors(r.issues)] == []
    copy = (work / "plans" / "active" / "demo.yaml").read_text(encoding="utf-8")
    found = read_plan(work / "plans" / "active" / "demo.yaml")
    assert copy == render(found.body, "demo", 3, plan_digest(found.body)) + ""
    shown = (case / "mocks" / "evo-hub" / "plan_show.md").read_text(encoding="utf-8").split("---\n", 2)[2]
    assert shown == copy  # plan_show answers with the same plan the copy holds
    untouched = yaml.safe_load((case / "graders" / "copy-not-edited.md").read_text(encoding="utf-8").split("---")[1])
    assert re.search(untouched["pattern"], copy)
    assert not re.search(
        untouched["pattern"], copy.replace("    status: pending\n  - id: 3", "    status: done\n  - id: 3")
    )
    marked = yaml.safe_load((case / "graders" / "marked-through-hub.md").read_text(encoding="utf-8").split("---")[1])
    assert re.search(marked["input_match"], json.dumps({"plan_id": "demo", "step": 2, "status": "done"}))
    assert re.search(marked["input_match"], json.dumps({"status": "done", "step": "2", "plan_id": "demo"}))
    assert not re.search(marked["input_match"], json.dumps({"plan_id": "demo", "step": 3, "status": "done"}))
    assert not re.search(marked["input_match"], json.dumps({"plan_id": "demo", "step": 2, "status": "in_progress"}))


def test_the_memory_case_holds_the_answer_only_on_the_hub(tmp_path):
    case = EVALS / "memory-recall"
    work = tmp_path / "case"
    work.mkdir()
    subprocess.run(["/bin/sh", str(case / "scaffold.sh")], cwd=work, check=True)
    on_disk = " ".join(path.read_text(encoding="utf-8") for path in work.rglob("*") if path.is_file())
    assert "DD/MM/YYYY" not in on_disk and "1.250.000,50" not in on_disk
    answer = (case / "mocks" / "evo-hub" / "memory_get.md").read_text(encoding="utf-8")
    for name in ("answer-dates.md", "answer-amounts.md"):
        spec = yaml.safe_load((case / "graders" / name).read_text(encoding="utf-8").split("---")[1])
        assert re.search(spec["pattern"], answer, re.IGNORECASE), name


# Two machines and `hub serve`


SKILL = "---\nname: house-style\ndescription: Use when writing anything for the team\n---\nWrite plainly.\n"


def hook_cli(name: str, home: Path, session: Path, url_check: str | None = None) -> subprocess.CompletedProcess:
    """``evo-agents hub hook NAME`` as Claude Code runs it for a session in ``session``, as the person of ``home``."""
    env = pg.clean_env(HOME=str(home), CLAUDE_PROJECT_DIR=str(session))
    for variable in HOST_VARIABLES:
        if variable != "CLAUDE_PROJECT_DIR":
            env.pop(variable, None)
    event = {"session-start": "SessionStart", "stop": "Stop"}[name]
    payload = {"session_id": "0b5e7a52-4c1e-4b8e-9d55-2f3c1a7e9b10", "cwd": str(session), "hook_event_name": event}
    command = [sys.executable, "-m", "evo_agents", "hub", "hook", name]
    return subprocess.run(command, env=env, input=json.dumps(payload), capture_output=True, text=True, timeout=60)


def evo(args: list[str], home: Path, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = pg.clean_env(HOME=str(home))
    for variable in HOST_VARIABLES:
        env.pop(variable, None)
    command = [sys.executable, "-m", "evo_agents", *args]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120, cwd=cwd or home)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def machine(base: Path, name: str, url: str, login: str, token: str) -> tuple[Path, Path]:
    """A home signed in to the hub, with the demo harness checked out at ~/ws/demo-harness and ~/.claude present."""
    home = base / name
    (home / ".claude").mkdir(parents=True)
    sign_in(home, url, login, token)
    return home, make_harness(home / "ws" / "demo-harness")


@needs_pg
def test_two_machines_share_memories_plans_and_skills_through_the_hooks(hub_db, tmp_path, s3):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN, **s3.env()) as served:
        tokens = {login: live.insert_token(hub_db, login) for login in (live.ADMIN, "alice")}
        admin, admin_root = machine(tmp_path, "admin", served.url, live.ADMIN, tokens[live.ADMIN])
        evo(["hub", "project", "register", str(admin_root)], admin)
        for login in (live.ADMIN, "alice"):
            evo(["hub", "admin", "grant", login, "demo", "--role", "writer", "--max-level", "internal"], admin)
        draft = admin_root / "plans" / "active" / "demo.yaml"
        draft.parent.mkdir(parents=True)
        draft.write_text(yaml.safe_dump({"id": "demo", **plan("demo")}), encoding="utf-8")
        evo(["hub", "plan", "put", "plans/active/demo.yaml"], admin, cwd=admin_root)
        skill = tmp_path / "skills" / "house-style"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SKILL, encoding="utf-8")
        evo(["hub", "skills", "publish", str(skill), "--scope", "global"], admin)

        laptop, laptop_root = machine(tmp_path, "laptop", served.url, "alice", tokens["alice"])
        desktop, desktop_root = machine(tmp_path, "desktop", served.url, "alice", tokens["alice"])
        outputs = []

        # The laptop writes a memory; Stop pushes it.
        note = memory_dir(laptop, laptop_root) / "deploys.md"
        note.parent.mkdir(parents=True)
        note.write_text(typed("project", f"Deploy on Tuesdays. {MARKER}"), encoding="utf-8")
        pushed = hook_cli("stop", laptop, laptop_root)
        outputs.append(pushed)
        assert (pushed.returncode, pushed.stdout, pushed.stderr) == (0, "", "")
        stored = live.sql(hub_db, "SELECT scope, location, name, revision, project_id IS NOT NULL FROM memories")
        assert stored == [("project", "harness", "deploys.md", 1, True)]

        # The desktop's next session pulls it, writes the plan copy, and counts the skill to sync.
        started = hook_cli("session-start", desktop, desktop_root)
        outputs.append(started)
        told = context_of(started.stdout)
        assert (started.returncode, started.stderr, told["message"]) == (0, "", None)
        assert told["context"] == (
            f"evo-hub {served.url}, project demo: 1 memory pulled; 1 plan copy written of 1 plan (no commit); "
            "1 skill to sync (`evo-agents hub skills sync`)."
        )
        assert (memory_dir(desktop, desktop_root) / "deploys.md").read_bytes() == note.read_bytes()
        assert "(deploys.md)" in (memory_dir(desktop, desktop_root) / INDEX).read_text(encoding="utf-8")
        copy = desktop_root / "plans" / "active" / "demo.yaml"
        assert read_plan(copy).hub["revision"] == 1
        assert [str(i) for r in validate_harness(desktop_root) for i in errors(r.issues)] == []
        assert not (desktop / ".claude" / "skills").exists()  # counted, never written

        # Again: nothing to do, nothing written.
        files = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (desktop / ".claude").rglob("*") if p.is_file()}
        files |= {copy: (copy.stat().st_mtime_ns, copy.read_bytes())}
        again = hook_cli("session-start", desktop, desktop_root)
        outputs.append(again)
        assert "0 memories pulled; 0 plan copies written of 1 plan" in context_of(again.stdout)["context"]
        assert {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in files} == files

        # A copy edited by hand stays as it is, and the line says so to the person too.
        edited = copy.read_text(encoding="utf-8").replace("status: pending", "status: done")
        copy.write_text(edited, encoding="utf-8")
        flagged = hook_cli("session-start", desktop, desktop_root)
        outputs.append(flagged)
        told = context_of(flagged.stdout)
        assert "1 plan file edited outside the hub, left as it is" in told["context"]
        assert told["message"] == told["context"] and copy.read_text(encoding="utf-8") == edited

        # Several sessions stop at once after an edit: one revision, no conflict, a state file that reads.
        note.write_text(typed("project", f"Deploy on Wednesdays. {MARKER}"), encoding="utf-8")
        with ThreadPoolExecutor(3) as pool:
            racing = list(pool.map(lambda _: hook_cli("stop", laptop, laptop_root), range(3)))
        outputs += racing
        assert [(r.returncode, r.stdout, r.stderr) for r in racing] == [(0, "", "")] * 3
        assert live.sql(hub_db, "SELECT revision FROM memories") == [(2,)]
        assert sorted(p.name for p in note.parent.iterdir()) == ["deploys.md"]
        state = json.loads((laptop / ".evo" / "hub" / "memory-state.json").read_text(encoding="utf-8"))
        assert state["hubs"][served.url]["files"][f"{slug(laptop_root)}/deploys.md"]["revision"] == 2

        # The desktop's Stop with nothing changed says nothing.
        quiet = hook_cli("stop", desktop, desktop_root)
        outputs.append(quiet)
        assert (quiet.returncode, quiet.stdout, quiet.stderr) == (0, "", "")

        # The desktop changes the memory it holds at revision 1: a conflict, told to the person, both versions kept.
        theirs = memory_dir(desktop, desktop_root) / "deploys.md"
        theirs.write_text(typed("project", f"Deploy on Fridays. {MARKER}"), encoding="utf-8")
        clashed = hook_cli("stop", desktop, desktop_root)
        outputs.append(clashed)
        assert (clashed.returncode, clashed.stderr) == (0, "")
        assert json.loads(clashed.stdout) == {
            "systemMessage": f"evo-hub: 1 memory conflict on {served.url}: the hub's version is kept under the name "
            "and this machine's next to it as a .conflict- copy, to merge and delete"
        }
        assert theirs.read_bytes() == note.read_bytes()
        (copy_of_theirs,) = theirs.parent.glob("deploys.conflict-*.md")
        assert "Fridays" in copy_of_theirs.read_text(encoding="utf-8")
    printed = "".join(r.stdout + r.stderr for r in outputs)
    for secret in (*tokens.values(), MARKER, "Tuesdays", "Wednesdays", "Fridays"):
        assert secret not in printed and secret not in served.log()


@needs_pg
def test_stop_pushes_what_waited_while_the_hub_was_down(hub_db, tmp_path):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN) as served:
        admin = hub_client.Hub(served.url, live.insert_token(hub_db, live.ADMIN))
        admin.call("PUT", "/v1/projects/demo", live_project())
        admin.call("PUT", "/v1/admin/projects/demo/grants/alice", {"role": "writer", "max_level": "internal"})
        home, root = machine(tmp_path, "laptop", served.url, "alice", live.insert_token(hub_db, "alice"))
        note = memory_dir(home, root) / "deploys.md"
        note.parent.mkdir(parents=True)
        note.write_text(typed("project", MARKER), encoding="utf-8")

        config = home / ".evo" / "hub" / "config.json"
        down = closed_port_url()
        config.write_text(json.dumps({"url": down, "login": "alice"}), encoding="utf-8")
        failed = hook_cli("stop", home, root)
        assert (failed.returncode, failed.stdout) == (0, "") and "the hub did not answer" in failed.stderr
        started = hook_cli("session-start", home, root)
        told = context_of(started.stdout)
        assert started.returncode == 0 and told["context"].startswith(f"evo-hub {down}, project demo: the hub did not")
        assert live.sql(hub_db, "SELECT count(*) FROM memories") == [(0,)]

        config.write_text(json.dumps({"url": served.url, "login": "alice"}), encoding="utf-8")
        pushed = hook_cli("stop", home, root)
        assert (pushed.returncode, pushed.stdout, pushed.stderr) == (0, "", "")
        assert live.sql(hub_db, "SELECT name, revision FROM memories") == [("deploys.md", 1)]


def live_project() -> dict:
    """Project demo as `hub project register` sends it for make_harness at ~/ws/demo-harness."""
    return {
        "levels": ["public", "internal", "customer", "secret"],
        "locations": ["any"],
        "default_label": {"level": "internal", "location": "any", "integrity": "U"},
        "sinks": [
            {"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "internal"}},
            {"id": "evo-hub", "kind": "hub", "clearance": {"level": "internal"}},
        ],
        "repos": [{"name": "app", "path": "app"}],
        "harness": {"name": "demo", "workspace": "~/ws", "path": "demo-harness"},
    }
