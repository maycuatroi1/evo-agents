"""Interactive runs on the worker (``evo_agents.worker.interactive``): takeover, handback, the tmux session and the web
terminal.

The checks step 10 of the worker-fleet plan names, with real tmux (skipped when the machine has none) and the fake
runtime of ``tests.worker.fake_adapter``, whose terminal UI is ``tests/worker/fake_tui.py``: a takeover makes the tmux
session ``evo-run-N`` and moves the run to interactive, and a handback goes on headless in the same session; the PTY
bridge carries bytes both ways through the websocket of a hub under test; a worker that does not allow the terminal
refuses it; a fake ``~/.claude.json`` gets the trust key and keeps every other key; a browser that connects again
gets the output kept in the ring first. Around them: interactive mode from the start, leaving the UI, the UI's
script, the command lines of the three runtimes' UIs, and their transcripts in the hub's event kinds.

Nothing here writes the real ``~/.claude.json`` or ``~/.codex``: every HOME is a temporary one, and no real runtime or
Remote Control is started (step 21 checks those by hand).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from evo_agents.hub import terminal as frames
from evo_agents.worker import interactive
from evo_agents.worker.adapter import RunContext
from evo_agents.worker.runtimes import opencode
from tests.hub import pg

TMUX = shutil.which("tmux")
WORKER_EXTRA = importlib.util.find_spec("aiohttp") is not None  # the daemon (run, hubapi) needs it to load
HUB_SKIP = pg.SKIP_REASON if not pg.DSN else "the daemon needs the worker extra, evo-ak[worker]"
needs_tmux = pytest.mark.skipif(TMUX is None, reason="tmux is not on PATH")
needs_hub = pytest.mark.skipif(not (pg.DSN and WORKER_EXTRA), reason=HUB_SKIP)
HUB = "https://hub.test"  # the hub's public URL: the browser's Origin
WS_WAIT = 30.0

if pg.DSN and WORKER_EXTRA:
    from evo_agents.hub.server.security import SESSION_COOKIE, WEB, csrf_token, hash_token
    from evo_agents.worker.run import HANDBACK_PROMPT
    from tests.hub import live
    from tests.hub.test_runs import OWNER, PROJECT
    from tests.worker.test_daemon import WORKER, finished_cleanly, make_stack, wait_until  # noqa: F401 (a fixture)


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def tmux_socket():
    """A tmux server of the test's own, killed at its end. A unix socket's path must stay short, hence /tmp."""
    directory = tempfile.mkdtemp(prefix="evo-tmux-", dir="/tmp")
    socket = os.path.join(directory, "s")
    yield socket
    if TMUX:
        subprocess.run([TMUX, "-S", socket, "kill-server"], capture_output=True, timeout=30)
    shutil.rmtree(directory, ignore_errors=True)


def tmux(socket: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([TMUX, "-S", socket, *args], capture_output=True, text=True, timeout=30)


def screen(socket: str, name: str) -> str:
    return tmux(socket, "capture-pane", "-p", "-t", f"={name}:").stdout


def clients(socket: str) -> list[str]:
    return tmux(socket, "list-clients", "-F", "#{client_width}x#{client_height}").stdout.split()


# The pieces, without a hub


def test_claude_codes_trust_is_recorded_for_the_worktree_and_every_other_key_is_kept(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    worktree = tmp_path / "worktrees" / "evo-agents-7"
    worktree.mkdir(parents=True)
    link = tmp_path / "wt-link"
    link.symlink_to(worktree)
    state = {
        "numStartups": 41,
        "oauthAccount": {"emailAddress": "owner@example.org"},
        "bypassPermissionsModeAccepted": True,
        "projects": {"/somewhere/else": {"hasTrustDialogAccepted": False, "allowedTools": ["Bash"]}},
    }
    path = home / ".claude.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    path.chmod(0o600)
    assert interactive.claude_state_path({"HOME": str(home)}) == path

    assert interactive.trust_folder(path, interactive._folders(link)) is True
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["projects"].pop(str(link)) == {"hasTrustDialogAccepted": True}
    assert written["projects"].pop(os.path.realpath(worktree)) == {"hasTrustDialogAccepted": True}
    assert written == state, "every other key, and the other projects, stay as they were"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.read_bytes()
    assert interactive.trust_folder(path, interactive._folders(link)) is False
    assert path.read_bytes() == before, "a folder trusted already leaves the file alone"

    # An entry the folder has keeps its keys.
    assert interactive.trust_folder(path, ["/somewhere/else"]) is True
    entry = json.loads(path.read_text(encoding="utf-8"))["projects"]["/somewhere/else"]
    assert entry == {"hasTrustDialogAccepted": True, "allowedTools": ["Bash"]}

    # CLAUDE_CONFIG_DIR moves the file; a state file that is a link is changed where it points.
    config_dir = tmp_path / "claude-config"
    assert interactive.claude_state_path({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(config_dir)}) == (
        config_dir / ".claude.json"
    )
    target = tmp_path / "dotfiles" / "claude.json"
    target.parent.mkdir()
    target.write_text('{"theme": "dark"}', encoding="utf-8")
    linked = tmp_path / "other-home" / ".claude.json"
    linked.parent.mkdir()
    linked.symlink_to(target)
    assert interactive.trust_folder(linked, ["/w"]) is True
    assert linked.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "theme": "dark",
        "projects": {"/w": {"hasTrustDialogAccepted": True}},
    }

    # A file that is not a JSON object is left as it is.
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(interactive.TrustError):
        interactive.trust_folder(broken, ["/w"])
    assert broken.read_text(encoding="utf-8") == "{not json"


def _fake_bin(directory: Path, *, hook_flag: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("claude", "opencode"):
        _script(directory / name, "exit 3")
    flag = " '      --dangerously-bypass-hook-trust'" if hook_flag else ""
    # printf is the shell's own: the PATH of these tests holds the fakes alone
    _script(directory / "codex", f"printf '%s\\n' 'Usage: codex resume [OPTIONS] [SESSION_ID] [PROMPT]'{flag}")
    return directory


def _tui_context(tmp_path: Path, *, prompt: str = "Do the step.", **run) -> RunContext:
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    env = {"HOME": str(tmp_path / "home"), "PATH": str(tmp_path / "bin"), "CLAUDECODE": "1"}
    return RunContext(run={"id": 7, "title": "the step", **run}, worktree=worktree, prompt=prompt, env=env)


def test_the_terminal_uis_go_on_with_the_session_with_full_permissions(tmp_path):
    bin_dir = _fake_bin(tmp_path / "bin")
    (tmp_path / "home").mkdir()
    context = _tui_context(tmp_path)
    worktree = str(context.worktree)

    claude = interactive.ClaudeCodeTui(context, "sid-1")
    notes = asyncio.run(claude.prepare())
    assert notes[0].startswith(f"Marked {worktree} as trusted in {tmp_path / 'home' / '.claude.json'}")
    trusted = json.loads((tmp_path / "home" / ".claude.json").read_text(encoding="utf-8"))
    assert trusted["projects"][worktree] == {"hasTrustDialogAccepted": True}
    assert claude.command("evo-run-7") == [
        str(bin_dir / "claude"),
        "--resume",
        "sid-1",
        "--dangerously-skip-permissions",
        "--remote-control",
        "evo-run-7",
    ]
    assert "CLAUDECODE" not in claude.environment(), "the UI is no child of a Claude Code session"
    fresh = interactive.ClaudeCodeTui(_tui_context(tmp_path, prompt="-starts like an option", model="opus"), None)
    assert fresh.command("evo-run-7") == [
        str(bin_dir / "claude"),
        "--session-id",
        fresh.session_id,
        "--dangerously-skip-permissions",
        "--remote-control",
        "evo-run-7",
        "--model",
        "opus",
        " -starts like an option",
    ]

    codex = interactive.CodexTui(context, "thread-1")
    notes = asyncio.run(codex.prepare())
    assert codex.hook_flag and "gets --dangerously-bypass-hook-trust" in notes[0]
    assert codex.command("evo-run-7") == [
        str(bin_dir / "codex"),
        "resume",
        "thread-1",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "-C",
        worktree,
    ]
    _fake_bin(bin_dir, hook_flag=False)
    plain = interactive.CodexTui(_tui_context(tmp_path, effort="low"), None)
    notes = asyncio.run(plain.prepare())
    assert not plain.hook_flag and "lists no --dangerously-bypass-hook-trust" in notes[0]
    assert plain.command("evo-run-7") == [
        str(bin_dir / "codex"),
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        'model_reasoning_effort="low"',
        "-C",
        worktree,
        "Do the step.",
    ]

    oc = interactive.OpencodeTui(context, "ses_1")
    oc.server = opencode.Server("http://127.0.0.1:4567", "the-password")
    assert oc.command("evo-run-7") == [
        str(bin_dir / "opencode"),
        "attach",
        "http://127.0.0.1:4567",
        "--session",
        "ses_1",
        "--dir",
        worktree,
    ]
    env = oc.environment()
    assert env["OPENCODE_SERVER_PASSWORD"] == "the-password" and env["OPENCODE_SERVER_USERNAME"] == "opencode"


def test_the_opencode_ui_shares_a_server_of_its_own_whose_events_the_run_logs(tmp_path):
    from tests.worker.test_runtimes import MODEL, OC_SESSION, PASSWORD, FakeOpencode, needs, opencode_samples

    needs("aiohttp")
    _fake_bin(tmp_path / "bin")
    context = _tui_context(tmp_path, prompt="the run's prompt", model=MODEL)

    async def samples(server, body) -> None:
        server.emit(*opencode_samples())

    async def go():
        server = FakeOpencode(samples)
        await server.start()

        class Under(interactive.OpencodeTui):
            async def start_server(self):
                return opencode.Server(server.url, PASSWORD)

        tui = Under(context, None)
        notes = await tui.prepare()
        events = []

        async def read() -> None:
            async for event in tui.logs():
                events.append(event)

        reader = asyncio.create_task(read())
        deadline = time.monotonic() + 10
        while "agent_message_chunk" not in [event.kind for event in events]:
            assert time.monotonic() < deadline, "the session's events never came"
            await asyncio.sleep(0.02)
        await tui.close()
        await asyncio.wait_for(reader, 5)
        await server.stop()
        return tui, server, notes, events

    tui, server, notes, events = asyncio.run(go())
    worktree = str(context.worktree)
    assert notes == [f"opencode serve listens on {server.url} for the terminal UI."]
    assert tui.session_id == OC_SESSION
    # A person drives this session: no rules against questions, no note that nobody watches.
    assert server.requests[0] == ("create", {"directory": worktree}, {"title": "evo-agents run 7: the step"})
    assert server.requests[1] == (
        "prompt",
        OC_SESSION,
        {
            "parts": [{"type": "text", "text": "the run's prompt"}],
            "model": {"providerID": "zai-coding-plan", "modelID": "glm-5.3-flash"},
        },
    )
    assert ("reply", "per_1", {"reply": "once"}) in server.requests, "permissions are answered as the run's were"
    kinds = [event.kind for event in events]
    assert "tool_call" in kinds and "agent_message_chunk" in kinds and "usage_update" in kinds


def test_the_transcripts_of_claude_code_and_codex_become_the_hubs_events():
    claude = [
        {"type": "file-history-snapshot", "snapshot": {}},
        {"type": "user", "message": {"role": "user", "content": "now fix the docs too"}},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<local-command-caveat>"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "Docs first."},
                    {"type": "text", "text": "On it."},
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "TodoWrite",
                        "input": {"todos": [{"content": "docs", "status": "in_progress"}]},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "a\nb"}]},
        },
        {"type": "cost-state", "totalCostUSD": 1.2},
    ]
    events = [event for record in claude for event in interactive.claude_transcript_events(record)]
    assert [event.kind for event in events] == [
        "output",
        "agent_thought_chunk",
        "agent_message_chunk",
        "tool_call",
        "tool_call",
        "plan",
        "tool_call_update",
    ]
    assert events[0].body == {"raw": {"type": "user", "text": "now fix the docs too"}}
    assert events[3].body["kind"] == "execute" and events[3].body["rawInput"] == {"command": "ls"}
    assert events[5].body["entries"] == [{"content": "docs", "status": "in_progress", "priority": "medium"}]
    assert events[6].body["content"][0]["content"]["text"] == "a\nb"

    codex = [
        {"type": "session_meta", "payload": {"id": "t-1", "cwd": "/w", "base_instructions": "long"}},
        {"type": "turn_context", "payload": {"cwd": "/w"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "also the README"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": []}},
        {
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Hm."}]},
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "exec_command", "arguments": '{"cmd": "ls"}', "call_id": "c1"},
        },
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "README"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin"}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]},
        },
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Done."}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 9}, "last_token_usage": {}},
            },
        },
        {"type": "event_msg", "payload": {"type": "turn_aborted", "reason": "interrupted"}},
    ]
    events = [event for record in codex for event in interactive.codex_rollout_events(record)]
    assert [event.kind for event in events] == [
        "output",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
        "tool_call",
        "agent_message_chunk",
        "usage_update",
        "output",
    ]
    assert events[0].body == {"raw": {"type": "user_message", "message": "also the README"}}
    assert events[2].body["kind"] == "execute" and events[2].body["rawInput"] == {"cmd": "ls"}
    assert events[4].body["kind"] == "edit"
    assert events[6].body["usage"] == {"input_tokens": 9}


def test_a_transcript_is_followed_from_where_the_headless_agent_left_it(tmp_path):
    path = tmp_path / "sid.jsonl"

    def said(text: str) -> str:
        return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"

    path.write_text(said("headless"), encoding="utf-8")
    offset = path.stat().st_size

    async def go() -> list[str]:
        stop = asyncio.Event()
        found = []

        async def read() -> None:
            locate = lambda: (path, offset)  # noqa: E731
            async for event in interactive.follow_jsonl(locate, interactive.claude_transcript_events, stop, poll=0.02):
                found.append(event.body["content"]["text"])

        reader = asyncio.create_task(read())
        await asyncio.sleep(0.1)
        line = said("half a line, then the rest")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(said("typed in the terminal") + line[:20])
        await asyncio.sleep(0.1)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line[20:])
        stop.set()
        await asyncio.wait_for(reader, 5)
        return found

    assert asyncio.run(go()) == ["typed in the terminal", "half a line, then the rest"]


def test_what_a_terminal_prints_becomes_text_and_the_ring_keeps_the_end_of_it(tmp_path):
    printed = "\x1b[?1049h\x1b[1;32mgreen\x1b[0m line\r\n\x1b]0;title\x07next\x1b(B line\r\n\r\n\r\n\r\nlast\x07"
    assert interactive.terminal_text(printed) == "green line\nnext line\n\nlast"

    whole = "café \x1b[31mred\x1b[0m done\n".encode()
    escape = whole.index(b"\x1b[31m")
    # A character cut in two, then an escape sequence cut in two, as tmux pipe-pane may write them.
    parts = [whole[:4], whole[4 : escape + 3], whole[escape + 3 :]]
    pane = tmp_path / "terminal.log"
    pane.write_bytes(parts[0])

    async def go() -> list[str]:
        stop = asyncio.Event()
        texts = []

        async def read() -> None:
            async for event in interactive.follow_pane(pane, stop, poll=0.02):
                texts.append(event.body["terminal"])

        reader = asyncio.create_task(read())
        for part in parts[1:]:
            await asyncio.sleep(0.1)
            with open(pane, "ab") as handle:
                handle.write(part)
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(reader, 5)
        return texts

    texts = asyncio.run(go())
    assert "".join(texts) == "café red done\n" and len(texts) == 3, texts

    ring = interactive.Ring(limit=16)
    ring.add(b"0123456789")
    assert ring.snapshot() == b"0123456789"
    ring.add(b"abc\ndefghij")
    assert len(ring) == 16 and ring.dropped
    assert ring.snapshot() == b"defghij", "a replay starts at a line once older output fell out"


def test_codex_finds_the_rollout_a_new_session_started_in_the_worktree(tmp_path):
    codex_home = tmp_path / "codex"
    context = _tui_context(tmp_path)
    context.env["CODEX_HOME"] = str(codex_home)
    tui = interactive.CodexTui(context, None)
    tui.started_at = time.time()
    day = codex_home / "sessions" / time.strftime("%Y/%m/%d")
    day.mkdir(parents=True)
    meta = {"type": "session_meta", "payload": {"id": "elsewhere", "cwd": "/somewhere/else"}}
    (day / "rollout-2026-10-05T10-00-00-elsewhere.jsonl").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    assert tui._locate() is None
    thread = "01a10b1f-ad53-7e41-8a28-30e04b1e06fe"
    mine = day / f"rollout-2026-10-05T10-00-01-{thread}.jsonl"
    meta = {"type": "session_meta", "payload": {"id": thread, "cwd": str(context.worktree)}}
    mine.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    assert tui._locate() == (mine, 0) and tui.session_id == thread
    assert interactive.CodexTui(context, thread).rollout() == mine


@needs_tmux
def test_tmux_runs_the_ui_through_a_script_that_sets_the_agents_environment(tmp_path, tmux_socket):
    tmuxed = interactive.Tmux.from_env({"PATH": os.environ["PATH"], interactive.TMUX_SOCKET_VARIABLE: tmux_socket})
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    scratch = tmp_path / "runs" / "7"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "EVO_RUN_ID": "7",
        "TERM": "dumb",
        "CLAUDECODE": "1",
        "QUOTED": 'it\'s "quoted" $HOME',
    }
    show = (
        "import os, time\n"
        "print('cwd', os.getcwd())\n"
        "print('run', os.environ['EVO_RUN_ID'], 'cc', os.environ.get('CLAUDECODE'))\n"
        "print('quoted', os.environ['QUOTED'])\n"
        "print('term', os.environ.get('TERM'))\n"
        "time.sleep(60)\n"
    )
    asyncio.run(
        tmuxed.start(
            "evo-run-7", [sys.executable, "-c", show], env, cwd=worktree, scratch=scratch, drop=("CLAUDECODE",)
        )
    )
    text = ""
    deadline = time.monotonic() + 20
    while "term" not in text:
        assert time.monotonic() < deadline, f"the UI printed nothing:\n{text}"
        time.sleep(0.05)
        text = screen(tmux_socket, "evo-run-7")
    assert f"cwd {os.path.realpath(worktree)}" in text
    assert "run 7 cc None" in text
    assert 'quoted it\'s "quoted" $HOME' in text
    assert "term dumb" not in text, "TERM is tmux's own in the pane"
    assert not list(scratch.glob("*.sh")), "the script removes itself once it runs"
    assert asyncio.run(tmuxed.alive("evo-run-7"))
    assert not asyncio.run(tmuxed.alive("evo-run-")), "sessions are matched by their whole name"
    socket = asyncio.run(tmuxed.socket_path("evo-run-7"))
    assert os.path.realpath(socket) == os.path.realpath(tmux_socket)
    argv, attach_env = tmuxed.attach_command("evo-run-7", {"TMUX": f"{tmux_socket},1,0", "PATH": "/bin"})
    assert argv[-3:] == ["switch-client", "-t", "=evo-run-7"], "inside a client of the same server, switch to it"
    argv, attach_env = tmuxed.attach_command("evo-run-7", {"TMUX": "/tmp/another/server,1,0", "PATH": "/bin"})
    assert argv[-3:] == ["attach-session", "-t", "=evo-run-7"] and "TMUX" not in attach_env
    asyncio.run(tmuxed.kill("evo-run-7"))
    assert not asyncio.run(tmuxed.alive("evo-run-7"))


def test_the_panes_script_evaluates_the_runs_leases_and_never_holds_their_values(tmp_path):
    token = "sk-lease-" + "0123456789abcdef" * 2
    printer = _script(tmp_path / "print-env", f"echo \"export OPENAI_API_KEY='{token}'\"")  # stands for `worker env`
    env = {"PATH": os.environ["PATH"], "EVO_RUN_ID": "7", "OPENAI_API_KEY": token, "GIT_CONFIG_COUNT": "1"}
    script = interactive.write_script(
        tmp_path / "runs" / "7" / "evo-run-7.sh",
        ["/bin/sh", "-c", 'echo "key=$OPENAI_API_KEY count=${GIT_CONFIG_COUNT:-none} run=$EVO_RUN_ID"'],
        env,
        tmp_path,
        ("CLAUDECODE",),
        withheld={"OPENAI_API_KEY", "GIT_CONFIG_COUNT"},
        env_command=str(printer),
    )
    text = script.read_text(encoding="utf-8")
    assert token not in text, "a lease's value is never written into the pane's script"
    assert "export OPENAI_API_KEY" not in text and "export GIT_CONFIG_COUNT" not in text
    lines = text.splitlines()
    evaluated = lines.index(f'eval "$({printer})"')
    assert lines.index("export EVO_RUN_ID=7") < evaluated < lines.index("unset CLAUDECODE"), (
        "after exports, before unsets"
    )
    done = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True, timeout=30)
    assert done.stdout.strip() == f"key={token} count=none run=7", done.stderr
    assert not script.exists(), "the script removes itself once it runs"
    without = interactive.write_script(tmp_path / "plain.sh", ["true"], env, tmp_path)
    assert 'eval "$(' not in without.read_text(encoding="utf-8"), "a run without leases evaluates nothing"


# Against a hub, with the daemon


@pytest.fixture
def interactive_stack(request, tmux_socket):
    """``make_stack`` of the daemon's tests with web sign-in on the hub, tmux on the daemon's PATH and a tmux server
    of the test's own, and the worker registered (allowing the web terminal unless told otherwise)."""
    if not (pg.DSN and WORKER_EXTRA):
        pytest.skip(HUB_SKIP)
    make = request.getfixturevalue("make_stack")
    github = request.getfixturevalue("github")

    def started(*, allow_web_terminal: bool = True):
        stack = make(github_client_secret=github.client_secret, public_url=HUB)
        os.symlink(TMUX, stack.tmp / "bin" / "tmux")
        stack.env[interactive.TMUX_SOCKET_VARIABLE] = tmux_socket
        stack.starts_path = stack.tmp / "starts.jsonl"
        stack.env["EVO_FAKE_STARTS"] = str(stack.starts_path)
        args = ["register", "--name", WORKER, "--project", PROJECT]
        if allow_web_terminal:
            args.append("--allow-web-terminal")
        registered = stack.cli(*args)
        assert registered.returncode == 0, registered.stderr
        stack.worker_token = (stack.state / "token").read_text(encoding="utf-8").strip()
        return stack

    return started


def _after_handback() -> list[dict]:
    return [{"write": {"f.txt": "after the handback\n"}}, {"result": {"verify_commands": ["test -f f.txt"]}}]


def _starts(stack) -> list[dict]:
    return [json.loads(line) for line in stack.starts_path.read_text(encoding="utf-8").splitlines()]


def _moves(stack, run_id: int) -> list[str]:
    return [event["body"]["to"] for event in stack.events(run_id) if event["kind"] == "state"]


def _notes(stack, run_id: int) -> list[str]:
    return [event["body"].get("text", "") for event in stack.events(run_id) if event["kind"] == "system"]


def _on_screen(socket: str, name: str, text: str, stack) -> str:
    found = {}

    def seen():
        found["screen"] = screen(socket, name)
        return text in found["screen"]

    wait_until(seen, f"{text!r} in tmux session {name}", explain=lambda: f"{found}\n{stack.daemon_output()[-4000:]}")
    return found["screen"]


class Browser:
    """The browser's end of a run's terminal: a websocket to the hub with the owner's web session, the hello first."""

    def __init__(self, stack, run_id: int, token: str, csrf: str):
        self.url = stack.url.replace("http://", "ws://") + f"/v1/projects/{PROJECT}/runs/{run_id}/terminal"
        self.token = token
        self.csrf = csrf
        self.output = bytearray()
        self.close_code = None

    async def __aenter__(self):
        import aiohttp

        self.http = aiohttp.ClientSession()
        headers = {"Origin": HUB, "Cookie": f"{SESSION_COOKIE}={self.token}"}
        self.ws = await self.http.ws_connect(self.url, headers=headers)
        await self.ws.send_str(json.dumps({"csrf": self.csrf, "cols": 100, "rows": 30}))
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()
        await self.http.close()

    async def read_until(self, needle: bytes, timeout: float = WS_WAIT) -> bytes:
        import aiohttp

        deadline = time.monotonic() + timeout
        while needle not in self.output:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"{needle!r} never came; the terminal printed:\n{bytes(self.output[-3000:])!r}"
            try:
                message = await asyncio.wait_for(self.ws.receive(), remaining)
            except asyncio.TimeoutError:
                continue
            if message.type == aiohttp.WSMsgType.BINARY:
                kind, payload = frames.parse(message.data)
                assert kind == frames.OUTPUT
                self.output += payload
            else:
                self.close_code = self.ws.close_code
                raise AssertionError(
                    f"the terminal closed ({self.close_code}, {message.extra}) before {needle!r}:\n"
                    f"{bytes(self.output[-3000:])!r}"
                )
        return bytes(self.output)

    async def closed(self, timeout: float = WS_WAIT) -> int | None:
        """Wait for the hub to close the socket; its code."""
        import aiohttp

        deadline = time.monotonic() + timeout
        while True:
            message = await asyncio.wait_for(self.ws.receive(), max(0.1, deadline - time.monotonic()))
            if message.type == aiohttp.WSMsgType.BINARY:
                self.output += frames.parse(message.data)[1]
                continue
            self.close_code = self.ws.close_code
            return self.close_code

    async def type(self, data: bytes) -> None:
        await self.ws.send_bytes(frames.frame(frames.INPUT, data))

    async def resize(self, cols: int, rows: int) -> None:
        await self.ws.send_bytes(frames.resize(cols, rows))


def _web_session(stack, hub_db) -> tuple[str, str]:
    token = live.insert_token(hub_db, OWNER, WEB)
    return token, csrf_token(stack.server.config.session_secret, hash_token(token))


async def _until(predicate, what: str, timeout: float = WS_WAIT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.05)


@needs_hub
@needs_tmux
def test_a_takeover_opens_the_session_in_tmux_and_a_handback_goes_on_headless_in_the_same_session(
    interactive_stack, tmux_socket
):
    stack = interactive_stack()
    started, never = stack.tmp / "started", stack.tmp / "never"
    stack.scenarios({"2": [{"touch": str(started)}, {"wait_for": str(never)}], "2/resume": _after_handback()})
    proc = stack.start_daemon()
    run_id = stack.dispatch([2])[0]["id"]
    wait_until(started.exists, "the agent to start", explain=stack.daemon_output)
    session = stack.wait_state(run_id, "running")["session_id"]
    assert session

    taken = stack.client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/takeover", headers=stack.owner)
    assert taken.status_code == 200, taken.text
    run = stack.wait_state(run_id, "interactive")
    name = f"evo-run-{run_id}"
    assert run["session_id"] == session
    assert tmux(tmux_socket, "list-sessions", "-F", "#{session_name}").stdout.split() == [name]
    shown = _on_screen(tmux_socket, name, "fake tui: session", stack)
    assert f"fake tui: session {session} in {name}" in shown, "the UI goes on with the headless agent's session"
    record = json.loads((stack.state / "runs" / str(run_id) / "run.json").read_text(encoding="utf-8"))
    assert record["tmux_session"] == name
    assert os.path.realpath(record["tmux_socket"]) == os.path.realpath(tmux_socket)
    assert not list((stack.state / "runs" / str(run_id)).glob("*.sh")), "the UI's script removed itself"

    # What the terminal prints reaches the run's log (the fake UI keeps no transcript).
    tmux(tmux_socket, "send-keys", "-t", f"={name}:", "typed in tmux", "Enter")

    def logged() -> bool:
        outputs = [event["body"].get("terminal", "") for event in stack.events(run_id) if event["kind"] == "output"]
        return any("echo: typed in tmux" in text for text in outputs)

    wait_until(logged, "the terminal's text in the run's log", explain=stack.daemon_output)
    missing = stack.cli("attach", str(run_id + 1000))
    assert missing.returncode == 1 and f"tmux session evo-run-{run_id + 1000} is not there" in missing.stderr

    handed = stack.client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/handback", headers=stack.owner)
    assert handed.status_code == 200, handed.text
    run = stack.wait_state(run_id, "done", "failed")
    assert run["state"] == "done", (run["error"], stack.daemon_output()[-6000:])
    assert run["session_id"] == session
    assert tmux(tmux_socket, "has-session", "-t", f"={name}").returncode != 0, "the handback closed the UI"
    starts = _starts(stack)
    assert [(start["session"], start["resume"]) for start in starts] == [(session, None), (session, session)]
    assert starts[1]["prompt"] == HANDBACK_PROMPT
    assert _moves(stack, run_id) == ["leased", "running", "interactive", "running", "verifying", "done"]
    notes = _notes(stack, run_id)
    assert any(note.startswith("The owner asked for a takeover: the agent ends its turn") for note in notes)
    assert any(f"tmux session {name}" in note and f"evo-agents worker attach {run_id}" in note for note in notes)
    assert any(note.startswith("The owner handed the run back") for note in notes)
    finished_cleanly(stack, proc)


@needs_hub
@needs_tmux
def test_an_interactive_run_starts_in_tmux_on_its_prompt_and_leaving_the_ui_hands_it_back(
    interactive_stack, tmux_socket
):
    stack = interactive_stack()
    stack.scenarios({"2/resume": _after_handback()})
    proc = stack.start_daemon()
    run_id = stack.dispatch([2], mode="interactive")[0]["id"]
    run = stack.wait_state(run_id, "interactive")
    name = f"evo-run-{run_id}"
    session = run["session_id"]
    assert session
    shown = _on_screen(tmux_socket, name, "fake tui: prompt", stack)
    assert f"fake tui: session {session} in {name}" in shown
    assert "fake tui: prompt You are running step 2 of the plan" in shown, "the UI starts on the run's prompt"

    tmux(tmux_socket, "send-keys", "-t", f"={name}:", "exit", "Enter")
    run = stack.wait_state(run_id, "done", "failed")
    assert run["state"] == "done", (run["error"], stack.daemon_output()[-6000:])
    assert [(start["session"], start["resume"]) for start in _starts(stack)] == [(session, session)]
    assert _moves(stack, run_id) == ["leased", "interactive", "running", "verifying", "done"]
    assert any(note.startswith("The terminal UI ended") for note in _notes(stack, run_id))
    finished_cleanly(stack, proc)


@needs_hub
@needs_tmux
def test_the_web_terminal_carries_bytes_both_ways_and_replays_its_ring_to_a_browser_that_comes_back(
    interactive_stack, tmux_socket, hub_db
):
    stack = interactive_stack()
    stack.scenarios({"2/resume": _after_handback()})
    proc = stack.start_daemon()
    run_id = stack.dispatch([2], mode="interactive")[0]["id"]
    stack.wait_state(run_id, "interactive")
    name = f"evo-run-{run_id}"
    _on_screen(tmux_socket, name, "fake tui: session", stack)
    token, csrf = _web_session(stack, hub_db)

    async def first_visit() -> None:
        async with Browser(stack, run_id, token, csrf) as browser:
            await browser.read_until(b"fake tui: session")
            await browser.type(b"hello from the web\r")
            await browser.read_until(b"echo: hello from the web")
            await _until(lambda: clients(tmux_socket) == ["100x30"], "tmux to take the browser's size")
            await browser.resize(90, 28)
            await _until(lambda: clients(tmux_socket) == ["90x28"], "tmux to take the new size")
            await browser.type(b"clear\r")
            await browser.read_until(b"fake tui: cleared")

    asyncio.run(first_visit())
    wait_until(
        lambda: clients(tmux_socket) == [], "the web terminal's tmux client to detach", explain=stack.daemon_output
    )
    assert "echo: hello from the web" not in screen(tmux_socket, name), "tmux alone would not show it again"

    async def second_visit() -> bytes:
        async with Browser(stack, run_id, token, csrf) as browser:
            replayed = await browser.read_until(b"echo: hello from the web")
            await browser.type(b"once more\r")
            await browser.read_until(b"echo: once more")
            return replayed

    replayed = asyncio.run(second_visit())
    assert replayed.index(b"echo: hello from the web") < replayed.index(b"fake tui: cleared")
    assert stack.run(run_id)["state"] == "interactive", "the terminal does not hand the run back"

    tmux(tmux_socket, "send-keys", "-t", f"={name}:", "exit", "Enter")
    run = stack.wait_state(run_id, "done", "failed")
    assert run["state"] == "done", (run["error"], stack.daemon_output()[-6000:])
    finished_cleanly(stack, proc)


@needs_hub
@needs_tmux
def test_a_worker_that_does_not_allow_the_web_terminal_says_so_and_attaches_nothing(
    interactive_stack, tmux_socket, hub_db
):
    stack = interactive_stack()  # the hub has the worker allowing it; its owner turned it off on the machine
    config_path = stack.state / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["allow_web_terminal"] is True
    config["allow_web_terminal"] = False
    config_path.write_text(json.dumps(config), encoding="utf-8")
    stack.scenarios({"2/resume": _after_handback()})
    proc = stack.start_daemon()
    run_id = stack.dispatch([2], mode="interactive")[0]["id"]
    stack.wait_state(run_id, "interactive")
    name = f"evo-run-{run_id}"
    _on_screen(tmux_socket, name, "fake tui: session", stack)
    token, csrf = _web_session(stack, hub_db)

    async def visit() -> tuple[bytes, int | None]:
        async with Browser(stack, run_id, token, csrf) as browser:
            said = await browser.read_until(b"does not allow the web terminal")
            return said, await browser.closed()

    said, code = asyncio.run(visit())
    assert f"evo-agents worker attach {run_id}".encode() in said
    assert code is not None, "the terminal closes"
    assert clients(tmux_socket) == [], "nothing was attached to the session"
    assert b"fake tui" not in said, "not a byte of the session reached the browser"
    wait_until(
        lambda: any("this worker does not allow it" in note for note in _notes(stack, run_id)),
        "the refusal in the run's log",
        explain=stack.daemon_output,
    )

    tmux(tmux_socket, "send-keys", "-t", f"={name}:", "exit", "Enter")
    run = stack.wait_state(run_id, "done", "failed")
    assert run["state"] == "done", (run["error"], stack.daemon_output()[-6000:])
    finished_cleanly(stack, proc)
