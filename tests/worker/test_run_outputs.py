"""The outputs of a worker's run (step 2 of the run-outputs plan): the files its agent sends with ``evo-agents worker
output``, the images Claude Code's adapter takes out of the results of tools, the files left in .evo-run/outputs/, and
the files ``worker ask --attach`` and ``worker chat post --attach`` carry, on their way to the hub.

The adapter's part runs without a daemon; the rest runs a plan run with the daemon in this process, the fake runtime
of ``tests.worker.fake_adapter`` and the in-memory hub of ``tests.worker.fake_hub``, which answers the routes of the
outputs as the hub does (uploads, presigned PUTs, outputs, settings, limits, the switch of tool images).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.cli import main as cli_main
from evo_agents.hub import outputs
from evo_agents.hub.credentials import Lease
from evo_agents.worker import credentials, interactive
from evo_agents.worker.daemon import Daemon
from evo_agents.worker.home import WorkerHome
from evo_agents.worker.output_queue import OutputQueue
from evo_agents.worker.runner.outputs import SENT_FILE, outputs_files, redacted
from evo_agents.worker.runtimes import claude_code
from tests.worker.fake_hub import FakeHub
from tests.worker.test_plan_runs import PLAN, machine, plan_body, run_repos, wait_for, with_daemon  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + b"a screenshot" * 20
OTHER_PNG = b"\x89PNG\r\n\x1a\n" + b"another one" * 20


def image_block(data: bytes, media_type: str = "image/png") -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()},
    }


def screenshot(call: str, data: bytes = PNG) -> dict:
    """A tool's result as Claude Code wrote a browser's screenshot in the transcript of run #84: text, the image, and a
    text naming where it keeps the image."""
    return {
        "tool_result": {
            "id": call,
            "content": [
                {"type": "text", "text": "[computer:screenshot] Screenshot size: 375x812"},
                image_block(data),
                {"type": "text", "text": "[Image: source: /tmp/tool-results/mcp-blob.jpg]"},
            ],
        }
    }


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The adapter: images out of a tool's result


def test_a_tool_result_keeps_its_text_and_links_to_each_image_once_never_holding_its_base64():
    content = screenshot("toolu_1")["tool_result"]["content"] + [image_block(PNG), image_block(OTHER_PNG, "image/jpeg")]
    event = claude_code.tool_result("toolu_1", "completed", content)
    assert event.kind == "tool_call_update" and event.body["toolCallId"] == "toolu_1"
    texts = [item["content"] for item in event.body["content"] if item["content"]["type"] == "text"]
    assert texts == [
        {
            "type": "text",
            "text": "[computer:screenshot] Screenshot size: 375x812\n[Image: source: /tmp/tool-results/mcp-blob.jpg]",
        }
    ]
    links = [item["content"] for item in event.body["content"] if item["content"]["type"] == "resource_link"]
    assert links == [
        {
            "type": "resource_link",
            "uri": f"evo-output:sha256:{sha(PNG)}",
            "name": f"image-{sha(PNG)[:12]}.png",
            "mimeType": "image/png",
            "size": len(PNG),
        },
        {
            "type": "resource_link",
            "uri": f"evo-output:sha256:{sha(OTHER_PNG)}",
            "name": f"image-{sha(OTHER_PNG)[:12]}.jpg",
            "mimeType": "image/jpeg",
            "size": len(OTHER_PNG),
        },
    ]
    assert [(f.media_type, f.data, f.sha256) for f in event.files] == [
        ("image/png", PNG, sha(PNG)),
        ("image/jpeg", OTHER_PNG, sha(OTHER_PNG)),
    ]
    assert base64.b64encode(PNG).decode()[:40] not in json.dumps(event.body)


def test_an_image_that_does_not_decode_is_left_out_and_no_result_shows_base64():
    broken = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "not base64!"}}
    event = claude_code.tool_result("toolu_2", "completed", [broken])
    assert event.files == () and "content" not in event.body
    only_image = claude_code.tool_result("toolu_3", "completed", [image_block(PNG)])
    assert [item["content"]["type"] for item in only_image.body["content"]] == ["resource_link"]
    assert claude_code._text_of([image_block(PNG)]) is None
    assert (
        claude_code._text_of([{"type": "tool_reference", "tool_name": "x"}])
        == "[{'type': 'tool_reference', 'tool_name': 'x'}]"
    )
    assert claude_code.tool_result("toolu_4", "failed", "plain text").body == {
        "toolCallId": "toolu_4",
        "status": "failed",
        "content": [{"type": "content", "content": {"type": "text", "text": "plain text"}}],
    }


def test_headless_and_interactive_runs_take_the_images_of_tool_results_the_same_way():
    from claude_agent_sdk import types as sdk

    content = screenshot("toolu_5")["tool_result"]["content"]
    block = sdk.ToolResultBlock(tool_use_id="toolu_5", content=content, is_error=False)
    message = sdk.UserMessage(content=[block], parent_tool_use_id="toolu_agent")  # a subagent's, as the main thread's
    [headless] = claude_code.events_of(message)
    record = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_5", "content": content}]},
    }
    [driven] = interactive.claude_transcript_events(record)
    assert headless.body == driven.body and headless.files == driven.files
    assert [f.sha256 for f in headless.files] == [sha(PNG)]


# The pieces of the daemon


def test_text_is_redacted_with_the_leases_the_daemon_holds(tmp_path):
    secret = "ghs_" + secrets.token_hex(16)
    credentials.remember(secret, 4242)
    try:
        assert redacted(f"token {secret}\n".encode()) == b"token ***\n"
        assert redacted(b"\xff\xfe binary " + secret.encode()) == b"\xff\xfe binary ***"
        assert redacted(b"nothing to mask") == b"nothing to mask"
    finally:
        credentials.forget(4242)


def test_the_outputs_directory_lists_regular_files_by_their_path_below_it(tmp_path):
    base = tmp_path / "outputs"
    (base / "clips").mkdir(parents=True)
    (base / ".cache").mkdir()
    (base / "report.md").write_text("r")
    (base / "clips" / "demo.webm").write_text("v")
    (base / ".hidden").write_text("h")
    (base / ".cache" / "x.txt").write_text("c")
    (base / "link.md").symlink_to(base / "report.md")
    assert [name for name, _ in outputs_files(base)] == ["clips/demo.webm", "report.md"]


def test_the_queue_keeps_a_copy_and_answers_the_command_that_waits(tmp_path):
    home = WorkerHome(tmp_path / "worker")
    queue = OutputQueue(home, 7)
    source = tmp_path / "shot.png"
    source.write_bytes(PNG)
    item_id = queue.put_file(source, name="shot.png", media_type="image/png", caption="Login", wait=True)
    queue.put_scan()
    source.unlink()  # the agent may remove it: the queue holds a copy
    first, scan = queue.items()
    assert first.id == item_id and first.waits and not first.scan and scan.scan
    assert queue.data(first) == PNG
    assert first.entry["source"] == outputs.SENT and first.entry["caption"] == "Login"
    queue.finish(first, {"output_id": 3})
    queue.finish(scan)
    assert queue.pending() == 0 and queue.answer(item_id) == {"output_id": 3} and queue.answer(item_id) is None
    assert not any((queue.root / "files").iterdir())


def test_the_commands_refuse_outside_a_run(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("EVO_RUN_ID", raising=False)
    (tmp_path / "a.png").write_bytes(PNG)
    assert cli_main(["worker", "output", str(tmp_path / "a.png")]) == 1
    assert "works only inside a run of this worker" in capsys.readouterr().err


# A plan run


def run_plan(machine, scenarios: dict, setup=None, *, answer: str | None = None) -> tuple[int, FakeHub]:
    """Run the plan of ``plan_body`` with ``scenarios``; ``setup(hub, run_id)`` before the daemon claims it (nothing
    awaits in between), and ``answer`` the option of decision 1 when the run waits for it."""
    machine.scenarios(scenarios)
    found = {}

    async def body(hub: FakeHub, daemon: Daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        if setup is not None:
            setup(hub, run_id)
        if answer is not None:
            assert await hub.wait_state(run_id, "waiting", "done", "failed") == "waiting", hub.texts(run_id)
            hub.answer(1, answer)
        assert await hub.wait_state(run_id, "done", "failed") == "done", (hub.runs[run_id], hub.texts(run_id))
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    return found["run"], found["hub"]


def tool_updates(hub: FakeHub, run_id: int, call: str) -> list[dict]:
    events = hub.runs[run_id]["events"]
    return [e["body"] for e in events if e["kind"] == "tool_call_update" and e["body"].get("toolCallId") == call]


def test_sent_files_tool_images_and_the_outputs_directory_reach_the_hub_each_once(machine):
    token = "sk-ant-oat01-" + secrets.token_hex(16)
    lease = Lease(1, "env", "secret", "claude-token", env_var="CLAUDE_CODE_OAUTH_TOKEN", value=token)
    run_dir = ".evo-run/outputs"
    scenarios = {
        f"plan:{PLAN}": [
            {
                "sh": "printf 'PNGDATA' > shot.png; printf '# Report\\nthe token %s\\n' \"$CLAUDE_CODE_OAUTH_TOKEN\" > notes.md"
            },
            {"cli": ["output", "shot.png", "notes.md", "--caption", "The login page"]},
            {
                "write": {
                    f"{run_dir}/report.md": "# Report\nall good\n",
                    f"{run_dir}/clips/demo.webm": "webm",
                    f"{run_dir}/.hidden": "x",
                }
            },
            {"cli": ["step", "1", "in_progress", "--repo", "alpha"]},
            screenshot("toolu_shot"),
            screenshot("toolu_again"),  # the same screenshot: one output, of the first call
            {"write": {"alpha/a.txt": "a\n"}},
            {"cli": ["step", "1", "done", "--verify", "test -f a.txt"]},
            {"write": {f"{run_dir}/late.txt": "left at the end"}},
            {"result": {"summary": "Step 1 done."}},
        ]
    }

    def setup(hub, run_id):
        hub.leases[run_id] = {"leases": [lease.to_json()], "missing": []}

    run_id, hub = run_plan(machine, scenarios, setup)
    commands = machine.commands()
    sent = commands[1]
    assert sent["exit"] == 0 and "Sending 2 file(s) to the outputs of run" in sent["stdout"], sent
    by_name = {o["name"]: o for o in hub.run_outputs(run_id)}
    assert sorted(by_name) == sorted(
        ["shot.png", "notes.md", "report.md", "clips/demo.webm", f"image-{sha(PNG)[:12]}.png", "late.txt"]
    ), by_name
    assert len(hub.run_outputs(run_id)) == 6, "each file once: report.md was not sent again at the second step"
    assert (by_name["shot.png"]["source"], by_name["shot.png"]["caption"]) == ("sent", "The login page")
    assert by_name["shot.png"]["media_type"] == "image/png" and hub.output_data(by_name["shot.png"]) == b"PNGDATA"
    assert {by_name[name]["source"] for name in ("report.md", "clips/demo.webm", "late.txt")} == {"outputs"}
    assert by_name["clips/demo.webm"]["media_type"] == "video/webm"
    tool = by_name[f"image-{sha(PNG)[:12]}.png"]
    assert (tool["source"], tool["tool_call_id"], hub.output_data(tool)) == ("tool", "toolu_shot", PNG)

    # A file of text goes through the redaction of the run's events: the lease's value never leaves the machine.
    notes = hub.output_data(by_name["notes.md"])
    assert token not in notes.decode() and b"the token ***" in notes

    # The events of the tool link to the output, and none holds the image's base64.
    for call in ("toolu_shot", "toolu_again"):
        [update] = tool_updates(hub, run_id, call)
        links = [item["content"] for item in update["content"] if item["content"]["type"] == "resource_link"]
        assert [link["uri"] for link in links] == [outputs.link_uri(sha(PNG))]
    assert base64.b64encode(PNG).decode()[:40] not in json.dumps(hub.runs[run_id]["events"])
    ledger = json.loads((machine.directory(run_id) / ".evo-run" / SENT_FILE).read_text())
    assert sorted(ledger) == ["clips/demo.webm", "late.txt", "report.md"]


def test_attachments_go_with_the_question_and_the_chat_message(machine):
    scenarios = {
        f"plan:{PLAN}": [
            {"sh": "printf 'PNGDATA' > shot.png"},
            {"cli": ["chat", "post", "The screenshot of the login page", "--chat", "7", "--attach", "shot.png"]},
            {
                "cli": [
                    "ask",
                    "--category",
                    "deploy",
                    "--question",
                    "Deploy this page?",
                    "--option",
                    "yes=Deploy",
                    "--option",
                    "no=Wait",
                    "--attach",
                    "shot.png",
                ]
            },
        ],
        f"plan:{PLAN}/2": [
            {"write": {"alpha/a.txt": "a\n"}},
            {"cli": ["step", "1", "done", "--verify", "test -f a.txt"]},
            {"result": {"summary": "Asked with a screenshot."}},
        ],
    }
    run_id, hub = run_plan(machine, scenarios, answer="yes")
    posted, asked = machine.commands()[1], machine.commands()[2]
    assert posted["exit"] == 0 and asked["exit"] == 0, (posted, asked)
    [output] = hub.run_outputs(run_id)
    assert (output["name"], output["source"]) == ("shot.png", "sent")
    [message] = [call for call in hub.chat_calls if call["route"] == "chat/messages"]
    assert message["output_ids"] == [output["id"]] and message["chat_id"] == 7
    assert hub.decisions[1]["output_ids"] == [output["id"]]


def test_past_a_limit_the_command_says_why_and_exits_1_and_a_tool_image_is_left_out_once(machine):
    scenarios = {
        f"plan:{PLAN}": [
            {"sh": "printf '0123456789abcdef' > big.txt; printf 'one' > one.txt; printf 'two' > two.txt"},
            {"cli": ["output", "big.txt"]},
            {"cli": ["chat", "post", "One file", "--chat", "7", "--attach", "one.txt"]},  # waits until the hub has it
            {"cli": ["output", "two.txt"]},
            screenshot("toolu_one"),
            screenshot("toolu_two", OTHER_PNG),
            {"write": {"alpha/a.txt": "a\n"}},
            {"cli": ["step", "1", "done", "--verify", "test -f a.txt"]},
            {"result": {"summary": "Past the limits."}},
        ]
    }

    def setup(hub, run_id):
        hub.output_limits.update(max_file_bytes=10, max_outputs=1)

    run_id, hub = run_plan(machine, scenarios, setup)
    big, one, two = machine.commands()[1:4]
    assert big["exit"] == 1 and "big.txt has 16 bytes, and an output is at most 10 bytes" in big["stderr"], big
    assert one["exit"] == 0, one
    assert two["exit"] == 1 and "holds 1 outputs, and a run holds at most 1" in two["stderr"], two
    assert [o["name"] for o in hub.run_outputs(run_id)] == ["one.txt"]
    texts = hub.texts(run_id)
    left_out = [text for text in texts if text.startswith("An image of a tool's result was not kept")]
    assert len(left_out) == 1, texts
    for call in ("toolu_one", "toolu_two"):
        [update] = tool_updates(hub, run_id, call)
        shown = [item["content"] for item in update["content"]]
        assert all(block["type"] == "text" for block in shown), shown
        assert "not kept: an output is at most 10 bytes" in shown[-1]["text"], shown
    assert hub.moves(run_id)[-1] == "done"


def test_with_the_switch_off_no_tool_image_is_sent(machine):
    scenarios = {
        f"plan:{PLAN}": [
            screenshot("toolu_off"),
            {"write": {"alpha/a.txt": "a\n"}},
            {"cli": ["step", "1", "done", "--verify", "test -f a.txt"]},
            {"result": {"summary": "No tool images here."}},
        ]
    }

    def setup(hub, run_id):
        hub.tool_images = False

    run_id, hub = run_plan(machine, scenarios, setup)
    assert hub.run_outputs(run_id) == []
    assert ("output-uploads", run_id) not in hub.output_calls, "the machine sends none"
    [update] = tool_updates(hub, run_id, "toolu_off")
    assert "not kept: the project takes no images out of tools' results" in update["content"][-1]["content"]["text"]


SENT_ONE = {
    f"plan:{PLAN}": [
        {"sh": "printf 'PNGDATA' > shot.png"},
        {"cli": ["output", "shot.png"]},
        {"write": {"alpha/a.txt": "a\n"}},
        {"cli": ["step", "1", "done", "--verify", "test -f a.txt"]},
        {"result": {"summary": "Sent a file."}},
    ]
}


def test_a_hub_that_does_not_answer_gets_the_outputs_later(machine):
    def down(hub, run_id):
        hub.outputs_down = 1  # the first call of the outputs answers 503: the worker sends again with its backoff

    run_id, hub = run_plan(machine, SENT_ONE, down)
    assert [o["name"] for o in hub.run_outputs(run_id)] == ["shot.png"]
    assert hub.output_calls.count(("output-uploads", run_id)) == 2, hub.output_calls
    assert hub.moves(run_id)[-1] == "done"


def test_a_hub_without_outputs_never_fails_the_run(machine):
    def older(hub, run_id):
        hub.output_routes = False  # 404: a hub older than the outputs

    run_id, hub = run_plan(machine, SENT_ONE, older)
    sent = machine.commands()[1]
    assert sent["exit"] == 1 and f"the hub takes no outputs of run {run_id}" in sent["stderr"], sent
    assert hub.moves(run_id)[-1] == "done" and hub.runs[run_id]["summary"] == "Sent a file."
