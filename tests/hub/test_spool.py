"""The PostToolUse hook of the evo-hub plugin (``evo-agents hub hook post-tool-use``, ``evo_agents.hub.spool``).

A result of Bash, Read, Grep or an MCP tool longer than the threshold is written whole to ~/.evo/tool-output (the
directories 0700, the file 0600) and the model gets its head, its tail and the file's path, in the shape the tool
returns, which Claude Code needs. Everything else passes unchanged: a short result, a failed one, an image, a Read of a
range, a call that reads a spool file, Codex's payloads, a hook that runs out of time or meets an error. The hook exits
0 whatever happens, prints the replacement or nothing, never names the middle of a result on stderr, talks to no hub,
and deletes spool files older than EVO_SPOOL_DAYS.

The payloads follow what Claude Code 2.1.294 handed a hook on 2026-10-12 (the shapes are in spool's docstring). Every
test runs with a home directory of its own."""

import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.hub import spool

MIDDLE = "Wombat-Middle-Marker"  # in the middle of a long output only: must never reach the hook's output
SESSION = "bbedc7e6-0ef8-453f-ae51-64f3fdc18f19"
CALL = "toolu_01ABCdef234"
VARIABLES = (spool.THRESHOLD_VARIABLE, spool.DAYS_VARIABLE, "CLAUDE_PROJECT_DIR", "EVO_RUN_ID")


@pytest.fixture(autouse=True)
def own_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    return home


def long_text(size: int = 50_000, line: str = "row {:05d} " + "b" * 40) -> str:
    """``size`` characters of numbered lines, with MIDDLE once in the middle."""
    lines, used, number = [], 0, 0
    while used <= size:
        text = MIDDLE if number == 450 else line.format(number)
        lines.append(text)
        used += len(text) + 1
        number += 1
    return "\n".join(lines)[:size]


def payload(tool: str, response, tool_input: dict | None = None, **extra) -> dict:
    return {
        "session_id": SESSION,
        "transcript_path": f"/home/someone/.claude/projects/x/{SESSION}.jsonl",
        "cwd": "/home/someone/work",
        "permission_mode": "bypassPermissions",
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": tool_input if tool_input is not None else {},
        "tool_response": response,
        "tool_use_id": CALL,
        **extra,
    }


def bash_response(stdout: str, stderr: str = "", **extra) -> dict:
    return {
        "stdout": stdout,
        "stderr": stderr,
        "interrupted": False,
        "isImage": False,
        "noOutputExpected": False,
        **extra,
    }


def run_hook(data, monkeypatch, capsys) -> tuple[int, str, str]:
    text = data if isinstance(data, str) else json.dumps(data)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    code = main(["hub", "hook", "post-tool-use"])
    out, err = capsys.readouterr()
    return code, out, err


def replaced(out: str):
    answer = json.loads(out)
    specific = answer["hookSpecificOutput"]
    assert specific["hookEventName"] == "PostToolUse" and set(answer) == {"hookSpecificOutput"}
    return specific["updatedToolOutput"]


def spool_file(home: Path) -> Path:
    return home / ".evo" / "tool-output" / SESSION / f"{CALL}.txt"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


posix_modes = pytest.mark.skipif(os.name == "nt", reason="Windows keeps no POSIX mode bits")


# Bash


def test_a_short_result_passes_unchanged_and_writes_nothing(own_home, monkeypatch, capsys):
    code, out, err = run_hook(payload("Bash", bash_response("x" * spool.THRESHOLD)), monkeypatch, capsys)
    assert (code, out, err) == (0, "", "")
    assert not (own_home / ".evo").exists()


@posix_modes
def test_a_long_bash_result_goes_to_a_private_file_and_the_model_gets_head_tail_size_and_path(
    own_home, monkeypatch, capsys
):
    stdout = long_text(20_000)
    previous = os.umask(0)  # the modes are exact whatever the umask
    try:
        code, out, err = run_hook(payload("Bash", bash_response(stdout)), monkeypatch, capsys)
    finally:
        os.umask(previous)
    assert (code, err) == (0, "")
    output = replaced(out)
    path = spool_file(own_home)
    assert set(output) == {"stdout", "stderr", "interrupted", "isImage", "noOutputExpected"}
    assert output["stdout"].startswith(stdout[:1000]) and output["stdout"].endswith(stdout[-1000:])
    assert (
        "[evo-hub: this output has 20,000 characters and only its start and end are shown here; all of it is in "
        in output["stdout"]
    )
    assert str(path) in output["stdout"] and "kept 7 days" in output["stdout"]
    assert len(output["stdout"]) < spool.HEAD + spool.TAIL + 400 < spool.THRESHOLD
    assert MIDDLE not in out
    assert path.read_text(encoding="utf-8") == stdout, "the file holds the whole output"
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700 and mode(path.parent.parent) == 0o700


def test_claude_codes_own_saved_copy_of_a_very_long_output_fills_the_spool_file(
    own_home, tmp_path, monkeypatch, capsys
):
    whole = long_text(50_001)
    saved = tmp_path / "tool-results" / "bwp1unkq4.txt"
    saved.parent.mkdir()
    saved.write_bytes(whole.encode("utf-8"))  # the output's own bytes: write_text would make each \n \r\n on Windows
    response = bash_response(whole[:30_000], persistedOutputPath=str(saved), persistedOutputSize=50_001)
    code, out, err = run_hook(payload("Bash", response), monkeypatch, capsys)
    output = replaced(out)
    assert "persistedOutputPath" not in output and "persistedOutputSize" not in output, "else Claude Code shows its own"
    assert output["stdout"].endswith(whole[-1000:]), "the tail of the whole output, not of the first 30,000"
    assert "this output has 50,001 characters" in output["stdout"]
    assert spool_file(own_home).read_text(encoding="utf-8") == whole

    missing = bash_response(whole[:30_000], persistedOutputPath=str(tmp_path / "gone.txt"), persistedOutputSize=50_001)
    assert run_hook(payload("Bash", missing), monkeypatch, capsys) == (0, "", ""), "Claude Code's preview stays"


def test_a_successful_commands_stderr_stays_whole_when_short_and_goes_to_the_file_with_stdout(
    own_home, monkeypatch, capsys
):
    stdout = long_text(15_000)
    warning = "warning: something to know\n"
    output = replaced(run_hook(payload("Bash", bash_response(stdout, warning)), monkeypatch, capsys)[1])
    assert output["stderr"] == warning
    assert spool_file(own_home).read_text(encoding="utf-8") == f"{stdout}\n--- stderr ---\n{warning}"

    noisy = "e" * 3_000 + "\nthe error at the end\n"
    output = replaced(run_hook(payload("Bash", bash_response("ok\n", noisy * 4)), monkeypatch, capsys)[1])
    assert output["stderr"].endswith("the error at the end\n") and spool.STDERR_NOTE in output["stderr"]
    assert output["stdout"].startswith("ok\n") and "only its start and end are shown here" in output["stdout"]


# Read, Grep and MCP tools


def test_a_long_read_keeps_whole_first_lines_and_says_where_to_read_on(own_home, monkeypatch, capsys):
    content = long_text(22_861)
    lines = content.split("\n")
    file = {"filePath": "big.txt", "content": content, "numLines": len(lines), "startLine": 1, "totalLines": 901}
    response = {"type": "text", "file": {**file, "truncatedByTokenCap": True}}
    output = replaced(run_hook(payload("Read", response, {"file_path": "/w/big.txt"}), monkeypatch, capsys)[1])
    assert output["type"] == "text" and output["file"]["truncatedByTokenCap"] is True
    shown = output["file"]["content"].split("\n")
    kept = len(shown) - 1
    assert shown[:-1] == lines[:kept], "whole lines, numbered from startLine as before"
    assert output["file"]["numLines"] == len(shown) and output["file"]["totalLines"] == 901
    assert shown[-1].startswith(f"[evo-hub: the read returned {len(lines):,} lines (22,861 characters) and only ")
    assert f"only lines 1 to {kept} are shown here: Read the file with offset {kept + 1} and a limit" in shown[-1]
    assert str(spool_file(own_home)) in shown[-1]
    assert spool_file(own_home).read_text(encoding="utf-8") == content

    minified = {"type": "text", "file": {**file, "content": "{" + "x" * 30_000 + "}", "startLine": 5, "numLines": 1}}
    output = replaced(run_hook(payload("Read", minified, {"file_path": "/w/min.json"}), monkeypatch, capsys)[1])
    first, note = output["file"]["content"].split("\n")
    assert first == "{" + "x" * (spool.HEAD - 1)
    assert "only lines 5 to 5 are shown here, the last of them cut: Read the file with offset 5 and" in note

    ranged = payload("Read", response, {"file_path": "/w/big.txt", "offset": 300, "limit": 400})
    assert run_hook(ranged, monkeypatch, capsys) == (0, "", ""), "a range asked for on purpose"
    image = {"type": "image", "file": {"base64": "x" * 50_000, "type": "image/png"}}
    assert run_hook(payload("Read", image, {"file_path": "/w/a.png"}), monkeypatch, capsys) == (0, "", "")


def test_long_grep_results_keep_their_shape(own_home, monkeypatch, capsys):
    content = long_text(15_386, "src/mod{0:05d}.py:1:def needle(): return 0  # needle marker")
    response = {"mode": "content", "numFiles": 0, "filenames": [], "content": content, "numLines": 250}
    output = replaced(run_hook(payload("Grep", response, {"pattern": "needle"}), monkeypatch, capsys)[1])
    assert {**output, "content": None} == {**response, "content": None}
    assert output["content"].startswith(content[:500]) and "only its start and end are shown here" in output["content"]

    names = [f"src/very/long/path/to/module_{i:04d}.py" for i in range(400)]
    response = {"mode": "files_with_matches", "numFiles": 400, "filenames": names}
    output = replaced(run_hook(payload("Grep", response, {"pattern": "x"}), monkeypatch, capsys)[1])
    assert output["numFiles"] == 400 and output["filenames"][0] == names[0] and output["filenames"][-1] == names[-1]
    assert any("only its start and end are shown here" in name for name in output["filenames"])
    assert spool_file(own_home).read_text(encoding="utf-8") == "\n".join(names)


def test_a_long_mcp_result_becomes_one_text_block_and_keeps_its_other_blocks(own_home, monkeypatch, capsys):
    text = long_text(50_000, "line {:05d} " + "m" * 40)
    image = {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}
    response = [{"type": "text", "text": text[:25_000]}, image, {"type": "text", "text": text[25_000:]}]
    tool = "mcp__bigout__big_text"
    output = replaced(run_hook(payload(tool, response, mcp_server={"name": "bigout"}), monkeypatch, capsys)[1])
    assert len(output) == 2 and output[1] == image and output[0]["type"] == "text"
    assert output[0]["text"].startswith(text[:1000]) and output[0]["text"].endswith(text[-1000:])
    assert spool_file(own_home).read_text(encoding="utf-8") == text[:25_000] + "\n" + text[25_000:]

    as_string = replaced(run_hook(payload(tool, text), monkeypatch, capsys)[1])
    assert isinstance(as_string, str) and "this output has 50,000 characters" in as_string


# What passes unchanged


@pytest.mark.parametrize(
    "data",
    [
        payload("Write", {"type": "create", "filePath": "/w/a", "content": "x" * 50_000}),
        payload("WebFetch", {"result": "x" * 50_000}),
        payload("Bash", "x" * 50_000),  # not Claude Code's shape
        payload("Bash", bash_response("x" * 50_000, isImage=True)),
        payload("Grep", {"mode": "count", "numFiles": 3}),
        payload("mcp__a__b", {"content": [{"type": "text", "text": "x" * 50_000}]}),
        payload("mcp__a__b", [{"type": "image", "data": "x" * 50_000}]),
        payload("Bash", bash_response("x" * 50_000), hook_event_name="PostToolUseFailure"),
        payload("Bash", bash_response("x" * 50_000), hook_event_name="SessionStart"),
        payload("Bash", "x" * 50_000, turn_id="019a", model="gpt-5.5"),  # Codex
        payload("mcp__a__b", [{"type": "text", "text": "x" * 50_000}], turn_id="019a"),  # Codex
        payload("Bash", bash_response("x" * 50_000), {"command": "sed -n 1,400p ~/.evo/tool-output/s/c.txt"}),
        payload(
            "Read", {"type": "text", "file": {"content": "x" * 50_000}}, {"file_path": "/h/.evo/tool-output/a.txt"}
        ),
        "not json",
        "[1, 2]",
        "",
    ],
    ids=lambda data: (
        data.get("tool_name", "") + ":" + data.get("hook_event_name", "") if isinstance(data, dict) else ""
    ),
)
def test_anything_else_passes_unchanged(data, own_home, monkeypatch, capsys):
    assert run_hook(data, monkeypatch, capsys) == (0, "", "")
    assert not (own_home / ".evo" / "tool-output").exists()


def test_the_threshold_comes_from_the_environment_and_0_or_off_turns_the_hook_off(own_home, monkeypatch, capsys):
    data = payload("Bash", bash_response(long_text(3_000)))
    assert run_hook(data, monkeypatch, capsys) == (0, "", ""), "under the default"
    monkeypatch.setenv(spool.THRESHOLD_VARIABLE, "2000")
    output = replaced(run_hook(data, monkeypatch, capsys)[1])
    assert len(output["stdout"]) < 2_000 + 400, "head and tail shrink with a small threshold"
    for off in ("0", "off", "OFF"):
        monkeypatch.setenv(spool.THRESHOLD_VARIABLE, off)
        assert run_hook(payload("Bash", bash_response(long_text())), monkeypatch, capsys) == (0, "", "")
    monkeypatch.setenv(spool.THRESHOLD_VARIABLE, "ten thousand")
    assert spool.threshold(os.environ) == spool.THRESHOLD
    monkeypatch.setenv(spool.DAYS_VARIABLE, "2.5")
    monkeypatch.delenv(spool.THRESHOLD_VARIABLE)
    assert (
        "kept 2.5 days"
        in replaced(run_hook(payload("Bash", bash_response(long_text())), monkeypatch, capsys)[1])["stdout"]
    )
    for wrong in ("-1", "0", "soon"):
        assert spool.keep_days({spool.DAYS_VARIABLE: wrong}) == spool.DAYS


# The hook never fails the session


def test_the_hook_exits_0_and_names_only_the_type_when_it_cannot_write(own_home, monkeypatch, capsys):
    (own_home / ".evo").write_text("a file where the directory should be", encoding="utf-8")
    code, out, err = run_hook(payload("Bash", bash_response(long_text())), monkeypatch, capsys)
    assert (code, out) == (0, "")
    assert err.strip().startswith("evo-hub: tool output left as it was: unexpected ") and len(err.splitlines()) == 1
    assert MIDDLE not in err and "row 0" not in err


def test_the_hook_stops_within_its_budget_and_leaves_the_result(monkeypatch, capsys):
    # SIGALRM cuts one long blocking call short. Windows has none: hooks.budget's timer interrupts the main thread,
    # which sees it when the call it is in returns (Claude Code's hook timeout bounds a call that never does), so
    # there the slow hook is many short calls.
    step = 5.0 if hasattr(signal, "setitimer") else 0.05

    def slow(*args, **kwargs):
        for _ in range(round(5 / step)):
            time.sleep(step)

    monkeypatch.setattr(spool, "BUDGET", 0.2)
    monkeypatch.setattr(spool, "post_tool_use", slow)
    started = time.monotonic()
    code, out, err = run_hook(payload("Bash", bash_response(long_text())), monkeypatch, capsys)
    assert time.monotonic() - started < 3
    assert (code, out) == (0, "") and "out of time after 0.2s" in err


def test_the_hook_talks_to_no_hub_even_when_signed_in(own_home, monkeypatch, capsys):
    hub = own_home / ".evo" / "hub"
    hub.mkdir(parents=True)
    (hub / "token").write_text("evo_test_token_never_printed\n", encoding="utf-8")
    (hub / "config.json").write_text(json.dumps({"url": "https://hub.test", "login": "alice"}), encoding="utf-8")
    connections = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: connections.append(address))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: connections.append(args))
    code, out, err = run_hook(payload("Bash", bash_response(long_text())), monkeypatch, capsys)
    assert (code, err) == (0, "") and replaced(out)
    assert connections == []
    assert "evo_test_token_never_printed" not in out


# Old spool files


def test_old_spool_files_are_deleted_at_most_once_an_hour(own_home, monkeypatch, capsys):
    root = own_home / ".evo" / "tool-output"
    old, fresh = root / "old-session" / "toolu_old.txt", root / "fresh-session" / "toolu_fresh.txt"
    for path in (old, fresh):
        path.parent.mkdir(parents=True)
        path.write_text("output", encoding="utf-8")
    eight_days = time.time() - 8 * 86400
    os.utime(old, (eight_days, eight_days))
    run_hook(payload("Bash", bash_response(long_text())), monkeypatch, capsys)
    assert not old.exists() and not old.parent.exists(), "the file and the session directory it left empty"
    assert fresh.exists() and spool_file(own_home).exists()

    old.parent.mkdir()
    old.write_text("output", encoding="utf-8")
    os.utime(old, (eight_days, eight_days))
    assert spool.prune(root, 7 * 86400, time.time()) == 0, "pruned less than an hour ago"
    assert spool.prune(root, 7 * 86400, time.time() + spool.PRUNE_EVERY + 1) == 1
    assert not old.exists()


def test_nothing_is_pruned_when_nothing_is_spooled(own_home, monkeypatch, capsys):
    old = own_home / ".evo" / "tool-output" / "old-session" / "toolu_old.txt"
    old.parent.mkdir(parents=True)
    old.write_text("output", encoding="utf-8")
    eight_days = time.time() - 8 * 86400
    os.utime(old, (eight_days, eight_days))
    run_hook(payload("Bash", bash_response("short")), monkeypatch, capsys)
    assert old.exists(), "a short result costs no directory walk"


# The plugin's hooks.json, run as Claude Code runs it


def test_the_plugin_command_spools_through_uvx_and_exits_0_without_it(own_home, tmp_path):
    from tests.hub import pg
    from tests.hub.test_hooks import SH, fake_uvx, plugin_hooks

    group = plugin_hooks()["PostToolUse"][0]  # the second group is the local-tool hook: tests/hub/test_local_sync.py
    assert group["matcher"] == "Bash|Read|Grep|mcp__.*"
    (hook,) = group["hooks"]
    assert hook["command"].endswith(" evo-agents hub hook post-tool-use || true")
    assert hook["timeout"] >= spool.BUDGET + 5  # uvx and the interpreter start inside Claude Code's limit
    data = json.dumps(payload("Bash", bash_response(long_text())))

    def run(path: str) -> subprocess.CompletedProcess:
        env = pg.clean_env(HOME=str(own_home), PATH=path)
        for variable in VARIABLES:
            env.pop(variable, None)
        return subprocess.run([SH, "-c", hook["command"]], input=data, env=env, capture_output=True, text=True)

    missing = run(str(tmp_path / "empty"))
    assert (missing.returncode, missing.stdout) == (0, "")
    failing = run(str(fake_uvx(tmp_path / "failing", exit_code=2).parent))
    assert (failing.returncode, failing.stdout) == (0, "")
    working = run(str(fake_uvx(tmp_path / "working").parent))
    assert (working.returncode, working.stderr) == (0, "")
    assert "only its start and end are shown here" in replaced(working.stdout)["stdout"]
    assert spool_file(own_home).read_text(encoding="utf-8") == json.loads(data)["tool_response"]["stdout"]
