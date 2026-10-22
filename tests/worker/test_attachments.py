"""The files a person attaches to a message for a run's agent, on the worker (``evo_agents.worker.attachments``,
``evo_agents.worker.runner.inbox``), as step 2 of the plan chat-attachments names them, without Postgres: the daemon in
this process with the in-memory hub of ``tests.worker.fake_hub`` and the checkouts of ``tests.worker.test_plan_runs``.

The worker saves each file of a message under ``.evo-run/inbox/<message>/`` of the agent's directory, with a name made
safe, folders 0700 and files 0600, through a temporary file checked against the size and SHA-256 the hub gave, and a
file already there with that SHA-256 is not fetched again. The message's text gets a block after it that lists the
files as data of the sender, not instructions; a file not saved is a line of that block and a ``system`` event of the
run, and the message still reaches the agent. This holds for a reply in an author run's inbox, a thread message into
a running chat run (whatever the runtime: the adapters get text alone) and the message that opens a chat run, whose
files are in its first prompt; ``evo-agents worker chat read --fetch`` saves the files of the messages it reads. A
message without files reaches the agent as before."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import author, chat_runs, runs  # noqa: E402
from evo_agents.worker import attachments  # noqa: E402
from evo_agents.worker.hubapi import Refused, Unreachable  # noqa: E402
from tests.worker import fake_adapter  # noqa: E402
from tests.worker.test_author_run import ended_by_owner, skill_bundle  # noqa: E402
from tests.worker.test_credentials import env_lease  # noqa: E402
from tests.worker.test_plan_runs import WAIT, machine, wait_for, with_daemon  # noqa: E402, F401
from tests.worker.test_plugin_runtime_run import DECLARATION, FOUND, RUNTIME, plugin_hub, reported  # noqa: E402

NOTES = b"retry every 2 s, at most 5 times\n"
TRACE = b"Traceback (most recent call last):\n  ValueError: boom\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry(attachment_id: int, name: str, data: bytes, media_type: str = "text/plain", **extra) -> dict:
    return {"id": attachment_id, "name": name, "media_type": media_type, "size": len(data), "sha256": sha(data)} | extra


class Fetcher:
    """The hub's client as ``attachments.save_files`` uses it: the bytes of each attachment, sent in two chunks, or
    the error to raise."""

    def __init__(self, files: dict[int, bytes | Exception]):
        self.files = files
        self.asked: list[int] = []

    async def attachment(self, run_id: int, attachment_id: int, write) -> None:
        self.asked.append(attachment_id)
        found = self.files[attachment_id]
        if isinstance(found, Exception):
            raise found
        half = len(found) // 2
        write(found[:half])
        write(found[half:])


def save(tmp_path: Path, fetcher: Fetcher, files: list[dict], folder: str = "41") -> list[attachments.Saved]:
    import asyncio

    return asyncio.run(attachments.save_files(fetcher, 7, tmp_path, folder, files))


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".part"))


# The files on the machine


def test_attachment_names_lose_separators_dots_control_characters_and_length_and_take_a_suffix():
    taken: set[str] = set()
    assert attachments.safe_name("../../etc/passwd", taken) == "_._etc_passwd"
    assert attachments.safe_name("..\\..\\boot.ini", taken) == "_._boot.ini"
    assert attachments.safe_name("a\x00b\nc\x1b[31m.txt", taken) == "abc[31m.txt"
    assert attachments.safe_name(".bashrc", taken) == "bashrc"
    assert attachments.safe_name("..", taken) == "file"
    assert attachments.safe_name("", taken) == "file-2", "a second empty name"
    assert attachments.safe_name("notes.txt", taken) == "notes.txt"
    assert attachments.safe_name("NOTES.txt", taken) == "NOTES-2.txt", "the same name as another, case aside"
    assert attachments.safe_name("notes.txt", taken) == "notes-3.txt"
    long = attachments.safe_name("é" * 300 + ".log", taken)
    assert long.endswith(".log") and len(long.encode()) <= attachments.MAX_NAME_BYTES
    assert long.encode().decode() == long, "cut on a character boundary"
    again = attachments.safe_name("é" * 300 + ".log", taken)
    assert again.endswith("-2.log") and len(again.encode()) <= attachments.MAX_NAME_BYTES
    for name in taken:
        assert "/" not in name and "\\" not in name and ".." not in name and not name.startswith(".")


def test_attachment_files_are_saved_private_under_the_inbox_and_one_already_there_is_not_fetched_again(tmp_path):
    fetcher = Fetcher({301: NOTES, 302: TRACE})
    files = [entry(301, "notes.txt", NOTES), entry(302, "../trace.log", TRACE, "text/x-log")]
    first = save(tmp_path, fetcher, files)
    folder = tmp_path / ".evo-run" / "inbox" / "41"
    assert [(f.path, f.error, f.fetched) for f in first] == [
        (".evo-run/inbox/41/notes.txt", None, True),
        (".evo-run/inbox/41/_trace.log", None, True),
    ]
    assert (folder / "notes.txt").read_bytes() == NOTES and (folder / "_trace.log").read_bytes() == TRACE
    for level in (tmp_path / ".evo-run" / "inbox", folder):
        assert mode(level) == attachments.DIR_MODE, level
    assert mode(folder / "notes.txt") == attachments.FILE_MODE == mode(folder / "_trace.log")
    assert leftovers(folder) == []

    again = save(tmp_path, fetcher, files)
    assert fetcher.asked == [301, 302], "a file already there with its SHA-256 is not fetched again"
    assert [(f.path, f.error, f.fetched) for f in again] == [(f.path, None, False) for f in first]

    (folder / "notes.txt").write_bytes(b"changed by the agent")
    save(tmp_path, fetcher, files[:1])
    assert fetcher.asked == [301, 302, 301] and (folder / "notes.txt").read_bytes() == NOTES


def test_attachment_not_saved_says_why_and_leaves_no_file_behind(tmp_path):
    fetcher = Fetcher(
        {
            1: b"other bytes!",
            2: NOTES + b"more",
            3: NOTES[:5],
            4: Refused("no file 4 of a message to run 7", status=404),
            5: Unreachable("connection refused"),
        }
    )
    files = [
        entry(1, "a.txt", b"the bytes 12"),  # the same size, another SHA-256
        entry(2, "b.txt", NOTES),  # more bytes than it said
        entry(3, "c.txt", NOTES),  # fewer
        entry(4, "d.txt", NOTES),
        entry(5, "e.txt", NOTES),
        entry(6, "f.bin", b"", size=attachments.attachments.MAX_FILE_BYTES + 1),
        {"id": 7, "name": "g.txt", "media_type": "text/plain", "size": 3},
    ]
    saved = save(tmp_path, fetcher, files)
    assert [f.path for f in saved] == [None] * 7
    errors = [f.error for f in saved]
    assert errors[0] == "its bytes do not match the SHA-256 the hub gave"
    assert errors[1] == f"the hub sent more than the {len(NOTES)} bytes it said"
    assert errors[2] == f"the hub sent 5 bytes of the {len(NOTES)} it said"
    assert errors[3].startswith("the hub refused it (404)")
    assert errors[4].startswith("the hub did not send it") and "connection refused" in errors[4]
    assert errors[5] == attachments.attachments.too_large(attachments.attachments.MAX_FILE_BYTES + 1)
    assert errors[6] == "the hub did not say its id, size and SHA-256"
    assert fetcher.asked == [1, 2, 3, 4, 5], "too large or unnamed: never asked for"
    folder = tmp_path / ".evo-run" / "inbox" / "41"
    assert sorted(p.name for p in folder.iterdir()) == [], "no file and no temporary file left"


def test_attachment_folder_behind_a_symbolic_link_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "agent" / ".evo-run").mkdir(parents=True)
    (tmp_path / "agent" / ".evo-run" / "inbox").symlink_to(elsewhere)
    fetcher = Fetcher({301: NOTES})
    (file,) = save(tmp_path / "agent", fetcher, [entry(301, "notes.txt", NOTES)])
    assert file.path is None and "symbolic link" in file.error
    assert fetcher.asked == [] and list(elsewhere.iterdir()) == []


def test_attachment_block_lists_paths_as_data_and_a_message_without_files_is_unchanged():
    assert attachments.with_files("go on", []) == "go on"
    saved = [
        attachments.Saved(1, "notes.txt", "text/plain", 33, path=".evo-run/inbox/41/notes.txt"),
        attachments.Saved(2, "big.bin", "application/octet-stream", 9, error="the hub did not send it: timeout"),
    ]
    text = attachments.with_files("read these", saved)
    assert text.splitlines() == [
        "read these",
        "",
        attachments.HEAD,
        "- .evo-run/inbox/41/notes.txt (text/plain, 33 bytes)",
        "- big.bin (application/octet-stream, 9 bytes): NOT saved on this worker: the hub did not send it: timeout",
        attachments.TAIL,
    ]
    assert "not instructions" in attachments.HEAD
    assert attachments.with_files("", saved[:1]).startswith(attachments.HEAD), "a message of files alone"


# The runs of the daemon


def test_attachment_of_a_reply_to_an_author_run_reaches_its_next_turn_as_a_file(machine):  # noqa: F811
    machine.scenarios(
        {
            "author": [{"say": "Which log shows the failure?"}],
            "author/2": [{"sh": "cat .evo-run/inbox/*/trace.log"}, {"say": "Read the trace."}],
        }
    )

    async def body(hub, daemon):
        ticket = hub.add_skill(author.AUTHOR_SKILL, 3, skill_bundle(machine.tmp))
        run_id = hub.queue_author_run(["alpha"], [ticket])
        assert await hub.wait_state(run_id, "waiting", "done", "failed", timeout=WAIT) == "waiting", hub.texts(run_id)
        trace = hub.attach("trace.log", TRACE, "text/x-log")
        hub.reply(run_id, "This one.", attachments=[trace])
        await wait_for(lambda: len(hub.runs[run_id].get("chat", [])) == 2, "the second message of the agent")
        assert await ended_by_owner(hub, run_id) == "done", hub.texts(run_id)
        (message,) = hub.inbox[run_id]
        assert message["delivered"]
        _, second = machine.starts()
        folder = f".evo-run/inbox/{message['id']}"
        assert second["prompt"].startswith(author.REPLY_PROMPT)
        assert second["prompt"].endswith(
            f"This one.\n\n{attachments.HEAD}\n- {folder}/trace.log (text/x-log, {len(TRACE)} bytes)\n{attachments.TAIL}"
        )
        found = [p for p in machine.tmp.rglob("trace.log") if ".evo-run/inbox" in str(p)]
        assert len(found) == 1 and found[0].read_bytes() == TRACE, found
        assert mode(found[0]) == 0o600 and mode(found[0].parent) == 0o700
        (cat,) = [e for e in machine.commands() if e["run"] == run_id]
        assert cat["stdout"] == TRACE.decode(), "the agent reads it in its directory"
        assert hub.file_reads == [(run_id, trace["id"])]

    with_daemon(machine, body)


@pytest.mark.parametrize("runtime", runs.RUNTIMES)
def test_attachment_of_a_thread_message_reaches_a_running_chat_run_as_text_whatever_the_runtime(
    machine,  # noqa: F811
    runtime,
):
    go_on = machine.tmp / "go-on"
    machine.scenarios({"chat": [{"wait_for": str(go_on)}, {"say": "Read it."}]})

    async def body(hub, daemon):
        run_id = hub.queue_chat_run(["alpha"], runtime=runtime)
        await hub.wait_state(run_id, "running", timeout=WAIT)
        notes = hub.attach("notes.txt", NOTES)
        message_id = hub.thread_reply(run_id, "owner wrote in the thread (message #9):\nsee the notes", [notes])
        hub.thread_reply(run_id, "owner wrote in the thread (message #10):\nand nothing else")
        await wait_for(lambda: all(m["delivered"] for m in hub.inbox[run_id]), "the agent to take both messages")
        go_on.write_text("go", encoding="utf-8")
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        sent = machine.messages_path.read_text(encoding="utf-8")
        path = f".evo-run/inbox/{message_id}/notes.txt"
        block = f"{attachments.HEAD}\n- {path} (text/plain, {len(NOTES)} bytes)\n{attachments.TAIL}"
        flat = f"see the notes\n\n{block}".replace("\n", " ")  # the fake agent logs a message a line
        assert f"{flat}\n" in sent, sent
        assert sent.count(attachments.HEAD) == 1, "the text-only message carries no block"
        assert "and nothing else" in sent
        assert (machine.directory(run_id) / path).read_bytes() == NOTES

    adapters = {name: fake_adapter.FakeAdapter for name in runs.RUNTIMES}
    with_daemon(machine, body, adapters=adapters)


def test_attachment_of_the_message_that_opens_a_chat_run_is_in_its_first_prompt_and_never_committed(
    machine,  # noqa: F811
):
    machine.scenarios(
        {
            "chat": [
                {"sh": "ls -l .evo-run/inbox/chat-55/"},
                {"write": {"alpha/fix.py": "RETRIES = 5\n"}},
                {"say": "Set the retries from the notes."},
            ]
        }
    )

    async def body(hub, daemon):
        notes, trace = hub.attach("notes.txt", NOTES), hub.attach("notes.txt", TRACE, "text/x-log")
        run_id = hub.queue_chat_run(["alpha"], attachments=[notes, trace], chat_message_id=55)
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        (first,) = machine.starts()
        assert first["prompt"].endswith(
            f"{attachments.HEAD}\n"
            f"- .evo-run/inbox/chat-55/notes.txt (text/plain, {len(NOTES)} bytes)\n"
            f"- .evo-run/inbox/chat-55/notes-2.txt (text/x-log, {len(TRACE)} bytes)\n"
            f"{attachments.TAIL}"
        )
        folder = machine.directory(run_id) / ".evo-run" / "inbox" / "chat-55"
        assert (folder / "notes.txt").read_bytes() == NOTES and (folder / "notes-2.txt").read_bytes() == TRACE
        branch = f"chat/{run_id}"
        pushed = machine.origin("alpha", "ls-tree", "-r", "--name-only", branch).splitlines()
        assert pushed == ["README.md", "fix.py"], "the files of .evo-run/ are never committed"

    with_daemon(machine, body)


def test_attachment_of_a_text_only_chat_run_leaves_its_prompt_as_before(machine):  # noqa: F811
    machine.scenarios({"chat": [{"say": "Done."}]})

    async def body(hub, daemon):
        run_id = hub.queue_chat_run(["alpha"], chat_message_id=56)
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        (first,) = machine.starts()
        assert first["prompt"] == hub.runs[run_id]["spec"]["prompt"]
        assert not (machine.directory(run_id) / ".evo-run" / "inbox").exists()
        assert hub.file_reads == []

    with_daemon(machine, body)


def test_attachment_that_fails_to_download_leaves_the_message_with_an_error_line_and_a_system_event(
    machine,  # noqa: F811
):
    go_on = machine.tmp / "go-on"
    machine.scenarios({"chat": [{"no_input": True}, {"wait_for": str(go_on)}, {"say": "First."}], "chat/2": []})

    async def body(hub, daemon):
        run_id = hub.queue_chat_run(["alpha"])
        await hub.wait_state(run_id, "running", timeout=WAIT)
        down, bad = hub.attach("down.txt", NOTES), hub.attach("bad.txt", NOTES, sha256="0" * 64)
        hub.files_down.add(down["id"])
        message_id = hub.thread_reply(run_id, "owner wrote in the thread (message #9):\nboth files", [down, bad])
        await wait_for(lambda: hub.last_heartbeat and run_id in hub.last_heartbeat["runs"], "a heartbeat of the run")
        go_on.write_text("go", encoding="utf-8")
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        _, second = machine.starts()
        assert second["prompt"].startswith(chat_runs.REPLY_PROMPT)
        lines = second["prompt"].splitlines()
        assert lines[-4:] == [
            attachments.HEAD,
            f"- down.txt (text/plain, {len(NOTES)} bytes): NOT saved on this worker: the hub did not send it: "
            + lines[-3].split("the hub did not send it: ", 1)[1],
            f"- bad.txt (text/plain, {len(NOTES)} bytes): NOT saved on this worker: its bytes do not match the "
            "SHA-256 the hub gave",
            attachments.TAIL,
        ]
        assert "both files" in second["prompt"], "the message reaches the agent all the same"
        notes = [t for t in hub.texts(run_id) if "was not saved for the agent" in t]
        assert len(notes) == 2 and all(f"attached to message {message_id}" in t for t in notes), notes
        assert all(m["delivered"] for m in hub.inbox[run_id])
        assert sorted(hub.file_reads) == [(run_id, down["id"]), (run_id, bad["id"])], "asked once, not at each try"
        folder = machine.directory(run_id) / ".evo-run" / "inbox" / str(message_id)
        assert sorted(p.name for p in folder.iterdir()) == []

    with_daemon(machine, body)


def test_attachment_fetch_of_chat_read_saves_the_files_of_the_messages_it_reads(machine):  # noqa: F811
    machine.scenarios(
        {
            "chat": [
                {"cli": ["chat", "read", "12", "--thread", "--fetch"]},
                {"cli": ["chat", "read", "12", "--fetch"]},
                {"say": "Read it."},
            ]
        }
    )

    async def body(hub, daemon):
        notes = hub.attach("notes.txt", NOTES)
        hub.read_messages = [{"id": 12, "attachments": [notes]}, {"id": 13, "attachments": []}]
        run_id = hub.queue_chat_run(["alpha"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        fetched, again = [e for e in machine.commands() if e["run"] == run_id]
        assert fetched["exit"] == 0, fetched
        assert fetched["stdout"].endswith(
            f"\n# Files of message #12\n{attachments.HEAD}\n"
            f"- .evo-run/inbox/chat-12/notes.txt (text/plain, {len(NOTES)} bytes)\n{attachments.TAIL}\n"
        )
        assert again["exit"] == 0 and again["stdout"] == fetched["stdout"]
        assert hub.file_reads == [(run_id, notes["id"])], "the second read finds the file there"
        saved = machine.directory(run_id) / ".evo-run" / "inbox" / "chat-12" / "notes.txt"
        assert saved.read_bytes() == NOTES and mode(saved) == 0o600

    with_daemon(machine, body)


def test_attachment_of_the_opening_message_reaches_an_acp_runtimes_chat_run(machine):  # noqa: F811
    turn = [{"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Read it."}}}]
    scenario, log = machine.tmp / "acp-scenario.json", machine.tmp / "acp.jsonl"
    scenario.write_text(json.dumps({"turns": [turn]}), encoding="utf-8")
    machine.env.update({"FAKE_ACP_SCENARIO": str(scenario), "FAKE_ACP_LOG": str(log), "FAKE_ACP_MODELS": "small,large"})
    found = {}

    async def body(hub, daemon):
        plugin_hub(hub)
        await wait_for(lambda: reported(hub) == FOUND, "the heartbeat to report the plugin's runtime")
        notes = hub.attach("notes.txt", NOTES)
        run_id = hub.queue_chat_run(["alpha"], runtime=RUNTIME, attachments=[notes], chat_message_id=60)
        hub.runs[run_id]["spec"].update(plugin_runtime=dict(DECLARATION))
        hub.leases[run_id] = {"leases": [env_lease(1, "FAKE_API_KEY", "fake-value")], "missing": []}
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    run_id = found["run"]
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    (prompt,) = [line["message"] for line in lines if line["message"].get("method") == "session/prompt"]
    text = "".join(block.get("text", "") for block in prompt["params"]["prompt"])
    assert text.endswith(
        f"{attachments.HEAD}\n- .evo-run/inbox/chat-60/notes.txt (text/plain, {len(NOTES)} bytes)\n{attachments.TAIL}"
    )
    assert all(block["type"] == "text" for block in prompt["params"]["prompt"]), "text alone, never a file block"
    saved = machine.directory(run_id) / ".evo-run" / "inbox" / "chat-60" / "notes.txt"
    assert saved.read_bytes() == NOTES
    assert os.access(saved, os.R_OK)
