"""The transcripts of sessions on this machine (``evo_agents.hub.transcripts``): where they are found, how a part is
read and redacted, and what the hooks queue and start.

Step 2 of plan history-driven-curator, criterion a1 on the machine's side: every transcript of a Claude Code or Codex
session, its subagents' too, whatever the number of messages, compressed rollouts included; a part holds whole lines
only, redacted with ``redact_data`` and numbered as the file is; the same lines make the same blob; and a hook only
writes the queue and starts a sync it does not wait for. No database: ``test_session_transcripts`` sends them."""

import gzip
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evo_agents.hub import transcripts
from evo_agents.hub.memory import slug
from evo_agents.hub.transcripts import CLAUDE, CODEX, MAIN, OMITTED, Found, Queue, State, chain, read_part
from tests.hub.test_digest import SESSION, answered, at, said, session_lines, tool_use
from tests.hub.test_redact import ANTHROPIC_KEY, HUB_TOKEN

CODEX_SESSION = "01a12c1b-f58c-7672-acfc-cb7f35d3469a"
CODEX_THREAD = "01a12c1c-0000-7000-8000-000000000001"


@pytest.fixture(autouse=True)
def own_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "EVO_HUB_HOME", transcripts.DISABLE_VARIABLE, "EVO_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    return home


def lines_of(path: Path, lines: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((json.dumps(line) if isinstance(line, dict) else line) + "\n" for line in lines))
    return path


def claude_transcript(home: Path, cwd: str, session: str = SESSION, lines: list | None = None) -> Path:
    return lines_of(home / ".claude" / "projects" / slug(cwd) / f"{session}.jsonl", lines or session_lines())


def rollout(path: Path, session: str = CODEX_SESSION, thread: str | None = None, cwd: str = "/work/proj") -> Path:
    meta = {"timestamp": at(0), "type": "session_meta", "payload": {"session_id": session, "id": thread or session}}
    meta["payload"]["cwd"] = cwd
    turn = {"timestamp": at(1), "type": "turn_context", "payload": {"model": "gpt-6", "cwd": cwd}}
    return lines_of(path, [meta, turn])


def found_at(path: Path, file: str = MAIN, session: str = SESSION) -> Found:
    stat = path.stat()
    return Found(path, session, file, CLAUDE, slug("/work/app"), None, stat.st_mtime, stat.st_size)


def unpacked(part) -> bytes:
    return gzip.decompress(part.path.read_bytes() if part.path else b"")


# Finding transcripts


def test_claude_code_transcripts_and_their_subagents_are_found_under_every_projects_directory(own_home):
    main = claude_transcript(own_home, "/work/app")
    sub = lines_of(main.parent / SESSION / "subagents" / "agent-a1b2.jsonl", [said("sub", 1)])
    deep = lines_of(main.parent / SESSION / "subagents" / "workflows" / "wf_1-2" / "agent-c3.jsonl", [said("x", 2)])
    lines_of(main.parent / SESSION / "subagents" / "agent-a1b2.meta.json", [{"not": "a transcript"}])
    lines_of(main.parent / "memory" / "notes.jsonl", [{"not": "a session"}])
    other = lines_of(
        own_home / ".claude-m1" / "projects" / slug("/work/other") / f"{SESSION[:-1]}9.jsonl", [said("o", 1)]
    )

    found = {item.key: item for item in transcripts.find_all()}
    assert sorted(found) == sorted(
        [
            f"{SESSION}/main",
            f"{SESSION}/subagents/agent-a1b2.jsonl",
            f"{SESSION}/subagents/workflows/wf_1-2/agent-c3.jsonl",
            f"{SESSION[:-1]}9/main",
        ]
    )
    assert found[f"{SESSION}/main"].path == main and found[f"{SESSION}/main"].slug == slug("/work/app")
    assert found[f"{SESSION}/subagents/agent-a1b2.jsonl"].path == sub
    assert found[f"{SESSION}/subagents/workflows/wf_1-2/agent-c3.jsonl"].path == deep
    assert found[f"{SESSION[:-1]}9/main"].path == other and found[f"{SESSION[:-1]}9/main"].runtime == CLAUDE

    os.utime(sub, (1_000_000, 1_000_000))
    recent = {item.key for item in transcripts.find_all(since=2_000_000)}
    assert f"{SESSION}/subagents/agent-a1b2.jsonl" not in recent and f"{SESSION}/main" in recent


def test_a_codex_rollout_names_its_session_and_a_subagent_s_thread_is_a_file_of_that_session(own_home):
    base = own_home / ".codex" / "sessions" / "2026" / "10" / "19"
    rollout(base / "rollout-2026-10-19T08-00-00-a.jsonl")
    rollout(base / "rollout-2026-10-19T08-05-00-b.jsonl", thread=CODEX_THREAD)
    lines_of(base / "rollout-2026-10-19T08-06-00-c.jsonl", [{"type": "not a meta"}])
    found = {item.key: item for item in transcripts.find_all()}
    assert sorted(found) == [f"{CODEX_SESSION}/main", f"{CODEX_SESSION}/subagents/{CODEX_THREAD}.jsonl"]
    main = found[f"{CODEX_SESSION}/main"]
    assert (main.runtime, main.cwd, main.slug) == (CODEX, "/work/proj", slug("/work/proj"))


def test_a_rollout_without_a_thread_id_is_the_session_s_own(own_home):
    path = own_home / ".codex" / "sessions" / "rollout-x.jsonl"
    lines_of(
        path, [{"timestamp": at(0), "type": "session_meta", "payload": {"session_id": CODEX_SESSION, "cwd": "/w"}}]
    )
    (item,) = transcripts.find_all()
    assert (item.session_id, item.file) == (CODEX_SESSION, MAIN)


def zstd_available() -> bool:
    return transcripts._zstd_module() is not None or shutil.which("zstd") is not None


@pytest.mark.skipif(not shutil.which("zstd"), reason="the zstd command makes the compressed rollout")
def test_a_compressed_rollout_is_read_and_its_plain_copy_wins(own_home):
    base = own_home / ".codex" / "sessions" / "2025" / "12" / "19"
    plain = rollout(base / "rollout-2025-12-19T20-33-40-z.jsonl")
    subprocess.run(["zstd", "-q", "--rm", str(plain)], check=True)
    packed = plain.with_name(plain.name + ".zst")
    (item,) = transcripts.find_all()
    assert item.path == packed and item.compressed
    assert transcripts.source_size(item) == len(rollout(own_home / "copy.jsonl").read_bytes())
    part = read_part(item, 0, 1, [])
    assert part.lines == 2 and part.cwd == "/work/proj"

    shutil.copy(own_home / "copy.jsonl", base / "rollout-2025-12-19T20-33-40-z.jsonl")
    (again,) = transcripts.find_all()
    assert again.path.name.endswith(".jsonl") and not again.compressed


def test_without_a_zstd_decoder_a_compressed_rollout_is_unreadable_and_said_so(own_home, monkeypatch):
    path = own_home / "rollout-x.jsonl.zst"
    path.write_bytes(b"\x28\xb5\x2f\xfd")
    monkeypatch.setattr(transcripts, "_zstd_module", lambda: None)
    monkeypatch.setattr(transcripts.shutil, "which", lambda name: None)
    with pytest.raises(transcripts.Unreadable, match="no zstd decoder"):
        with transcripts.open_source(path):
            pass
    assert transcripts.find_all() == []  # skipped when found, never a failure


def test_the_transcript_a_hook_names_brings_its_subagents(own_home):
    main = claude_transcript(own_home, "/work/app")
    lines_of(main.parent / SESSION / "subagents" / "agent-1.jsonl", [said("s", 1)])
    assert [item.file for item in transcripts.given(main)] == [MAIN, "subagents/agent-1.jsonl"]
    codex = rollout(own_home / "elsewhere" / "rollout-1.jsonl")
    assert [(item.runtime, item.session_id) for item in transcripts.given(codex)] == [(CODEX, CODEX_SESSION)]
    assert transcripts.given(own_home / "missing.jsonl") == []


# Parts


def test_a_part_holds_whole_lines_redacted_numbered_as_the_file_and_the_same_lines_make_the_same_blob(own_home):
    secret = said(f"use {ANTHROPIC_KEY} and my token {HUB_TOKEN}", 3)
    path = lines_of(own_home / "t.jsonl", [said("plain", 1), {**secret, "cwd": "/work/app"}, "not json " + HUB_TOKEN])
    with open(path, "ab") as handle:
        handle.write(b'{"type": "user", "unfinished')  # still being written: waits for the next part
    item = found_at(path)
    part = read_part(item, 0, 1, [HUB_TOKEN], into=own_home / "parts")
    assert transcripts.source_size(item) == part.end, "the unfinished line is not the hub's to hold yet"
    held = unpacked(part).decode()
    assert part.lines == 3 and part.first_line == 1 and part.end == path.read_bytes().index(b'{"type": "user", "unf')
    assert ANTHROPIC_KEY not in held and HUB_TOKEN not in held and held.count("***") >= 3
    plain = path.read_text().splitlines(keepends=True)[0]
    assert held.splitlines(keepends=True)[0] == plain, "a line redaction leaves alone keeps its bytes"
    assert held.splitlines()[2].startswith("not json") and part.cwd == "/work/app"
    assert (part.started_at, part.ended_at) == ("2026-10-08T01:00:01+00:00", "2026-10-08T01:00:03+00:00")
    again = read_part(item, 0, 1, [HUB_TOKEN])
    assert (again.sha256, again.size, again.path) == (part.sha256, part.size, None)

    with open(path, "ab") as handle:
        handle.write(b'"}\n')
    rest = read_part(found_at(path), part.end, part.first_line + part.lines, [HUB_TOKEN])
    assert (rest.first_line, rest.lines, rest.end) == (4, 1, path.stat().st_size)
    assert read_part(found_at(path), rest.end, 5, []) is None


def test_a_part_stops_at_its_limit_on_a_line_boundary_and_a_huge_line_becomes_a_note(own_home, monkeypatch):
    path = lines_of(own_home / "t.jsonl", [said("a" * 100, 1), said("b" * 100, 2), said("c" * 100, 3)])
    first = read_part(found_at(path), 0, 1, [], limit=250)  # the second line passes it, the third waits
    assert first.lines == 2 and path.read_bytes()[: first.end].count(b"\n") == 2
    monkeypatch.setattr(transcripts, "MAX_LINE_BYTES", 50)
    huge = read_part(found_at(path), 0, 1, [], into=own_home / "parts")
    notes = [json.loads(line) for line in unpacked(huge).decode().splitlines()]
    assert notes[0] == {"type": OMITTED, "bytes": len(path.read_text().splitlines(keepends=True)[0])}
    assert huge.lines == 3 and huge.end == path.stat().st_size


def test_the_chain_of_a_transcript_is_each_hash_after_the_one_before():
    first = chain(None, "a" * 64)
    assert first == chain("", "a" * 64) != chain(first, "b" * 64)
    assert len(chain(first, "b" * 64)) == 64


# What the hooks do


def signed_in(home: Path, url: str = "https://hub.example.org") -> None:
    directory = home / ".evo" / "hub"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / "token").write_text("evh_" + "t" * 43 + "\n")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": "alice"}))
    for name in ("token", "config.json"):
        os.chmod(directory / name, 0o600)


@pytest.fixture
def spawned(monkeypatch) -> list:
    started: list = []
    monkeypatch.setattr(transcripts, "spawn_sync", lambda base, wait=transcripts.SETTLE: started.append(wait) or True)
    return started


def test_stop_queues_the_session_and_starts_a_sync_and_nothing_without_sign_in_switch_or_transcript(
    own_home, monkeypatch, spawned
):
    transcript = str(claude_transcript(own_home, "/work/app"))
    payload = {"session_id": SESSION, "transcript_path": transcript, "cwd": "/work/app"}
    transcripts.on_turn(payload, now=100.0)
    assert spawned == [] and not (own_home / ".evo").exists(), "not signed in: nothing written"

    signed_in(own_home)
    for bad in ({"session_id": "../x", "transcript_path": transcript}, {"session_id": SESSION, "transcript_path": "t"}):
        transcripts.on_turn(bad)
    monkeypatch.setenv(transcripts.DISABLE_VARIABLE, "off")
    transcripts.on_turn(payload)
    monkeypatch.setenv(transcripts.DISABLE_VARIABLE, "1")
    monkeypatch.setenv("EVO_RUN_ID", "7")
    transcripts.on_turn(payload)
    assert spawned == [] and not (own_home / ".evo" / "hub" / "transcripts").exists()

    monkeypatch.delenv("EVO_RUN_ID")
    transcripts.on_turn(payload, now=100.0)
    transcripts.on_turn(payload, ended=True, now=200.0)
    transcripts.on_turn(payload, now=300.0)
    assert spawned == [transcripts.SETTLE] * 3
    queued = Queue(transcripts.directory()).entries()
    assert queued == {SESSION: {"transcript": transcript, "ended": True, "at": 300.0}}


def test_session_start_starts_a_sync_for_a_queue_or_a_stale_sync_and_writes_nothing_on_a_new_machine(own_home, spawned):
    signed_in(own_home)
    transcripts.on_session_start(now=10_000.0)
    assert spawned == [] and not (own_home / ".evo" / "hub" / "transcripts").exists()
    state = State(transcripts.directory(), "https://hub.example.org")
    state.mark_synced(10_000.0)
    state.save()
    transcripts.on_session_start(now=10_000.0 + transcripts.SWEEP_EVERY - 1)
    assert spawned == []
    transcripts.on_session_start(now=10_000.0 + transcripts.SWEEP_EVERY)
    Queue(transcripts.directory()).add(SESSION, "/t.jsonl", False, 1.0)
    transcripts.on_session_start(now=10_001.0)
    assert spawned == [transcripts.SETTLE, transcripts.SETTLE]


def test_a_queue_entry_named_again_while_a_sync_took_it_stays(own_home):
    queue = Queue(transcripts.directory())
    queue.add(SESSION, "/a.jsonl", False, 1.0)
    taken = queue.entries()
    queue.add(SESSION, "/a.jsonl", False, 2.0)
    queue.add(CODEX_SESSION, "/b.jsonl", True, 3.0)
    queue.drop(taken)
    assert sorted(queue.entries()) == sorted([SESSION, CODEX_SESSION])
    queue.drop(queue.entries())
    assert queue.entries() == {}


def test_a_sync_is_started_detached_only_when_none_holds_the_lock(own_home, monkeypatch):
    started: list = []

    class Popen:
        def __init__(self, command, **options):
            started.append((command, options))

    monkeypatch.setattr(transcripts.subprocess, "Popen", Popen)
    base = transcripts.directory()
    assert transcripts.spawn_sync(base, wait=3) is True
    ((command, options),) = started
    assert command == [sys.executable, "-m", "evo_agents", "hub", "sessions", "sync", "--wait", "3"]
    assert options["stdout"] is subprocess.DEVNULL and options["stderr"] is subprocess.DEVNULL
    assert options["stdin"] is subprocess.DEVNULL and options["cwd"] == str(base)
    assert options.get("start_new_session") or options.get("creationflags")
    with transcripts.sync_lock(base) as held:
        assert held is True
        assert transcripts.spawn_sync(base) is False
    assert len(started) == 1


def test_the_sync_log_keeps_one_line_per_run_and_its_newer_half(own_home, monkeypatch):
    base = transcripts.directory()
    monkeypatch.setattr(transcripts, "MAX_LOG_BYTES", 400)
    for n in range(40):
        transcripts.log_line(base, f"run {n}\nwith a break")
    text = (base / transcripts.LOG_FILE).read_text()
    assert len(text.encode()) <= 400 and text.endswith("run 39 with a break\n") and "run 0 " not in text
    assert all(line.count(" run ") == 1 for line in text.splitlines())


def test_a_secret_inside_a_tool_input_is_redacted_and_the_line_stays_json(own_home):
    command = f"export ANTHROPIC_API_KEY={ANTHROPIC_KEY}"
    path = lines_of(own_home / "t.jsonl", [answered("msg_1", 1, tool_use("toolu_1", "Bash", command=command))])
    part = read_part(found_at(path), 0, 1, [], into=own_home / "parts")
    text = unpacked(part).decode()
    assert ANTHROPIC_KEY not in text
    assert json.loads(text)["message"]["content"][0]["input"]["command"] == "export ANTHROPIC_API_KEY=***"
