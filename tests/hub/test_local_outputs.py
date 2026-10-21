"""The outputs of a local session: what SendUserFile sent, the page the Artifact tool published, the files of `hub
send`, and the images of the tools' results become outputs of the session's local run on the hub; the tool's event
keeps a reference in place of the image; a question of the session's dialog carries the files sent since the person's
last message; text files are redacted before they leave the machine. The hooks only queue on this machine and start
the sender in the background, so a hub or blob store that does not answer costs the session nothing.

The payloads are built from the shapes of the transcript of run #84 (a Code tab session of Claude Desktop,
2026-10-17): SendUserFile's input ``files``, ``caption``, ``status``, ``display`` and its result's ``attachments``; the
Artifact tool's ``file_path`` and the ``url`` and ``title`` of its result (a publish of 2026-10-18); a Read of a PNG
(``{"type": "image", "file": {"base64", "type", ...}}``) and the result of ``mcp__Claude_Browser__browser_batch`` (text
blocks and ``{"type": "image", "source": {"type": "base64", ...}}``). The hub is a function, as in
test_local_sync.py, except the last test, which runs the hooks against ``hub serve`` and a fake S3."""

import base64
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.hub import client as hub_client
from evo_agents.hub import hooks, local_outputs, local_send, local_sync
from evo_agents.hub.client import Hub, Unreachable
from evo_agents.hub.memory import MemorySync
from tests.hub import live, pg
from tests.hub import test_local_sync as base_tests
from tests.hub.test_local_sync import SECRET, SESSION, TOKEN, URL, FakeHub, fire, payload, session_file

own_home = base_tests.own_home  # each test has a home of its own

BASE = "/v1/projects/demo/local-runs/7"
STORE = "https://r2.test"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(255, -1, -1)) * 8


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class OutputsHub(FakeHub):
    """A local run's routes and those of its outputs, in memory, with the blob store's presigned PUTs."""

    def __init__(self):
        super().__init__()
        self.tool_images = True
        self.held: dict[str, bytes] = {}  # what the project holds, by SHA-256
        self.uploads: dict[str, tuple[str, bytes | None]] = {}  # upload id: (sha256, bytes PUT)
        self.outputs: list[dict] = []
        self.decisions = 100
        self.refuse: dict[str, int] = {}  # name: status the record of an output of that name gets
        self.store_down = False

    def send(self, method, url, headers, data, timeout):
        if url.startswith(STORE):
            self.calls.append((method, url, None))
            if self.store_down:
                raise Unreachable(f"cannot reach {STORE}")
            upload = url.rsplit("/", 1)[-1]
            self.uploads[upload] = (self.uploads[upload][0], data.read())
            return 200, b""
        path = url.removeprefix(URL)
        body = json.loads(data) if data else None
        if self.down:
            self.calls.append((method, path, body))
            raise Unreachable(f"the hub at {URL} did not answer")
        if method == "GET" and path == f"{BASE}/output-settings":
            self.calls.append((method, path, body))
            return 200, self._json({"tool_images": self.tool_images, "run_id": 7, "outputs": len(self.outputs)})
        if method == "POST" and path == f"{BASE}/output-uploads":
            self.calls.append((method, path, body))
            return self._uploads(body)
        if method == "POST" and path == f"{BASE}/outputs":
            self.calls.append((method, path, body))
            return self._record(body)
        if method == "POST" and path == f"{BASE}/decisions":
            self.calls.append((method, path, body))
            if any(i not in {o["id"] for o in self.outputs} for i in body.get("output_ids", [])):
                return 422, self._json({"message": "run 7 has no output 999"})
            self.decisions += 1
            return 201, self._json({"id": self.decisions})
        return super().send(method, url, headers, data, timeout)

    def _uploads(self, body):
        if not self.tool_images and any(i["source"] == "tool" for i in body["items"]):
            return 409, self._json({"message": "project demo takes no images out of tools' results"})
        tickets = []
        for item in body["items"]:
            if item["sha256"] not in self.held:
                upload = f"u{len(self.uploads) + 1}"
                self.uploads[upload] = (item["sha256"], None)
                tickets.append({"sha256": item["sha256"], "upload_id": upload, "url": f"{STORE}/uploads/{upload}"})
        present = [i["sha256"] for i in body["items"] if i["sha256"] in self.held]
        return 200, self._json({"uploads": tickets, "present": present, "expires_at": "2026-10-21T00:00:00Z"})

    def _record(self, body):
        for item in body["outputs"]:
            if item["name"] in self.refuse:
                return self.refuse[item["name"]], self._json({"message": f"{item['name']} refused"})
        recorded = []
        for item in body["outputs"]:
            if item["upload_id"] is not None:
                digest, data = self.uploads[item["upload_id"]]
                assert digest == item["sha256"] == sha(data) and len(data) == item["size"]
                self.held[digest] = data
            assert item["sha256"] in self.held
            same = [o for o in self.outputs if o["sha256"] == item["sha256"]]
            if same:
                recorded.append(same[0])
                continue
            output = {**{k: v for k, v in item.items() if k != "upload_id"}, "id": len(self.outputs) + 1}
            self.outputs.append(output)
            recorded.append(output)
        return 200, self._json({"outputs": recorded, "added": len(recorded)})


@pytest.fixture
def hub(monkeypatch) -> OutputsHub:
    fake = OutputsHub()
    monkeypatch.setattr(hub_client, "_send", fake.send)
    monkeypatch.setattr(MemorySync, "project_of", lambda self, where=None: "demo")
    monkeypatch.setattr(local_sync, "FLUSH_EVERY", 0.0)
    return fake


@pytest.fixture
def spawned(monkeypatch) -> list[int]:
    """The senders the hooks start, as the waits they were given; none runs unless a test runs it."""
    started: list[int] = []
    monkeypatch.setattr(local_outputs, "spawn_sender", lambda wait=local_outputs.SEND_WAIT: started.append(wait))
    return started


def send_queued() -> int:
    """What the sender a hook started does, in this process."""
    return local_send.send_queued(0)


def queue(home: Path) -> dict:
    return json.loads((home / ".evo" / "hub" / "local-outputs" / SESSION / "queue.json").read_text(encoding="utf-8"))


def shot_name(call: str = "toolu_shot") -> str:
    """The name of the image of a browser_batch call: the tool's own name and the end of the call's id."""
    return f"browser_batch-{call[-8:]}.jpg"


def shot_payload(home: Path, call: str = "toolu_shot", image: bytes = JPEG) -> dict:
    """A browser_batch of run #84: a resize, a navigation and a screenshot, the image last."""
    return payload(
        home,
        tool_name="mcp__Claude_Browser__browser_batch",
        tool_use_id=call,
        tool_input={
            "actions": [
                {"name": "resize_window", "input": {"preset": "mobile"}},
                {"name": "navigate", "input": {"url": "https://agents.omelet.tech/p/evo-agents/runs/80"}},
                {"name": "computer", "input": {"action": "screenshot"}},
            ]
        },
        tool_response=[
            {"type": "text", "text": "resize_window: Viewport set to 375x812"},
            {"type": "text", "text": "navigate: https://agents.omelet.tech/p/evo-agents/runs/80"},
            {"type": "text", "text": "computer: screenshot taken"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(image)}},
        ],
    )


def read_payload(home: Path, image: bytes = PNG) -> dict:
    """A Read of a PNG, as Claude Code hands it to the hook."""
    return payload(
        home,
        tool_name="Read",
        tool_use_id="toolu_read",
        tool_input={"file_path": str(home / "work" / "scratchpad" / "run-page.png")},
        tool_response={
            "type": "image",
            "file": {
                "base64": b64(image),
                "type": "image/png",
                "originalSize": len(image),
                "dimensions": {"originalWidth": 375, "originalHeight": 812, "displayWidth": 375, "displayHeight": 812},
            },
        },
    )


def send_user_file(home: Path, files: list[Path], caption: str, call: str = "toolu_send") -> dict:
    """SendUserFile of run #84: its input and result, the paths of this test's files."""
    return payload(
        home,
        tool_name="SendUserFile",
        tool_use_id=call,
        tool_input={"files": [str(f) for f in files], "caption": caption, "status": "normal", "display": "render"},
        tool_response={
            "caption": caption,
            "display": "render",
            "attachments": [
                {
                    "path": str(f),
                    "size": f.stat().st_size,
                    "isImage": f.suffix == ".png",
                    "media_type": "image/png" if f.suffix == ".png" else "text/markdown",
                    "pathValidated": True,
                    "file_uuid": "bde3edd4-3d57-4cfc-a1e5-30e4bdd73a3a",
                }
                for f in files
            ],
            "rendered_locally": True,
        },
    )


def artifact_payload(home: Path, page: Path) -> dict:
    """A publish of the Artifact tool, its input and result as a transcript of 2026-10-18 holds them."""
    return payload(
        home,
        tool_name="Artifact",
        tool_use_id="toolu_artifact",
        tool_input={"file_path": str(page), "icon": "graph", "description": "Prototype of a Graph view of a plan."},
        tool_response={
            "url": "https://claude.ai/artifact/AZqjuopoXWRJrwLpQFwybt",
            "path": str(page),
            "artifact_id": "4d7815e7-f937-4afa-bb31-7de85da025b7",
            "title": "Plan Step Graph",
            "updated": False,
            "audience": "owner",
            "seq": 1,
            "version": "1791879697-0e7a",
            "liveSubscription": "arming",
        },
    )


def files(home: Path) -> tuple[Path, Path]:
    scratch = home / "work" / "scratchpad"
    scratch.mkdir(parents=True, exist_ok=True)
    image = scratch / "run-page-truoc-sau.png"
    image.write_bytes(PNG)
    report = scratch / "Thiết kế trang run.md"
    report.write_text(f"# Report\n\nThe key was {SECRET} and the hub token {TOKEN}.\n", encoding="utf-8")
    return image, report


# What a payload holds, read defensively


def test_the_shapes_of_run_84_give_their_images_and_files_and_strange_ones_give_nothing(own_home):
    image, report = files(own_home)
    found = local_outputs.found_in(shot_payload(own_home))
    assert [(i.media_type, i.data, i.name) for i in found.images] == [("image/jpeg", JPEG, shot_name())]
    assert found.response[-1] == {"type": "text", "text": ""} and b64(JPEG) not in json.dumps(found.response)

    read = local_outputs.found_in(read_payload(own_home))
    assert [(i.media_type, i.data, i.name) for i in read.images] == [("image/png", PNG, "run-page.png")]

    mcp = payload(own_home, tool_name="mcp__playwright__browser_take_screenshot", tool_use_id="t9")
    mcp["tool_response"] = {"content": [{"type": "image", "data": b64(PNG), "mimeType": "image/png"}]}
    assert [i.sha256 for i in local_outputs.found_in(mcp).images] == [sha(PNG)]

    sent = local_outputs.found_in(send_user_file(own_home, [image, report], "Trang run ở 375 px"))
    assert [(s.path, s.name, s.media_type, s.caption) for s in sent.files] == [
        (str(image), "run-page-truoc-sau.png", "image/png", "Trang run ở 375 px"),
        (str(report), "Thiết kế trang run.md", "text/markdown", "Trang run ở 375 px"),
    ]
    page = own_home / "work" / "step-graph.html"
    (published,) = local_outputs.found_in(artifact_payload(own_home, page)).files
    assert (published.name, published.media_type) == ("step-graph.html", "text/html")
    assert published.caption == (
        "Plan Step Graph: https://claude.ai/artifact/AZqjuopoXWRJrwLpQFwybt\nPrototype of a Graph view of a plan."
    )

    strange = [
        payload(own_home, tool_name="SendUserFile", tool_input={"files": 3}, tool_response="1 file delivered"),
        payload(own_home, tool_name="SendUserFile", tool_input=None, tool_response={"attachments": "x"}),
        payload(own_home, tool_name="Artifact", tool_input={"action": "quickstart", "intent": "design"}),
        payload(own_home, tool_name="Artifact", tool_input={"file_path": str(page)}, tool_response={"ok": True}),
        payload(own_home, tool_name="Artifact", tool_input={"file_path": str(page), "asset": True}),
        payload(own_home, tool_name="Read", tool_response={"type": "image", "file": {"base64": "%%%", "type": "x"}}),
        payload(own_home, tool_name="Read", tool_response={"type": "image", "source": {"type": "url", "url": "x"}}),
        payload(own_home, tool_name="X", tool_response=[{"type": "image", "data": 5, "mimeType": "image/png"}]),
        payload(
            own_home, tool_name="X", tool_response={"type": "image", "data": b64(PNG), "mimeType": "image/svg+xml"}
        ),
    ]
    for data in strange:
        found = local_outputs.found_in(data)
        assert (found.images, found.files) == ([], []), data


def test_a_relative_path_is_the_session_s_and_one_without_a_directory_is_passed_over(own_home):
    data = payload(own_home, tool_name="SendUserFile", tool_input={"files": ["out/a.png"]})
    assert [s.path for s in local_outputs.sent_files(data)] == [str(own_home / "work" / "out" / "a.png")]
    data.pop("cwd")
    assert local_outputs.sent_files(data) == []


def test_a_payload_past_the_parse_limit_still_gives_the_tool_s_event(own_home):
    data = shot_payload(own_home, call="toolu_big", image=JPEG * 20)
    text = json.dumps({**{k: v for k, v in data.items() if k != "tool_use_id"}, "tool_use_id": "toolu_big"})
    assert len(text) > 4096 * 4
    skeleton = local_outputs.read_payload(io.StringIO(text), limit=4096)
    assert skeleton["session_id"] == SESSION and skeleton["tool_name"] == data["tool_name"]
    assert skeleton["tool_use_id"] == "toolu_big" and skeleton["cwd"] == data["cwd"]
    assert skeleton["tool_response"].startswith(f"[evo-hub: a result of {len(text)} characters")
    assert local_outputs.read_payload(io.StringIO(text)) == json.loads(text)
    assert local_outputs.read_payload(io.StringIO("{")) == {} and local_outputs.read_payload(io.StringIO("")) == {}


# The hooks queue, the sender sends


def test_a_screenshot_becomes_a_tool_output_and_its_event_holds_a_reference(
    own_home, hub, spawned, monkeypatch, capsys
):
    big = JPEG * 900  # with its base64, the payload is past the 1 MiB the other hooks read
    data = shot_payload(own_home, image=big)
    assert len(json.dumps(data)) > hooks.MAX_PAYLOAD
    started = time.monotonic()
    assert fire("local-tool", data, monkeypatch, capsys) == (0, "", "")
    assert time.monotonic() - started < local_sync.BUDGET["tool"]
    update = [e for e in hub.events if e["kind"] == "tool_call_update"][0]["body"]
    assert update["content"][1]["content"] == {
        "type": "output",
        "source": "tool",
        "sha256": sha(big),
        "media_type": "image/jpeg",
        "size": len(big),
        "name": shot_name(),
    }
    assert "computer: screenshot taken" in update["content"][0]["content"]["text"]
    encoded = b64(big)[:64]
    assert encoded not in json.dumps(hub.calls) and encoded not in json.dumps(session_file(own_home))
    assert spawned == [local_outputs.SEND_WAIT]  # the hook uploads nothing itself
    assert [(i["source"], i["sha256"], i["tool_call_id"]) for i in queue(own_home)["items"]] == [
        ("tool", sha(big), "toolu_shot")
    ]
    assert not [c for c in hub.calls if "output" in c[1]]

    assert send_queued() == 0
    (output,) = hub.outputs
    assert (output["source"], output["name"], output["tool_call_id"]) == ("tool", shot_name(), "toolu_shot")
    assert hub.held[sha(big)] == big
    assert queue(own_home)["items"] == [] and queue(own_home)["recorded"][sha(big)]["id"] == 1
    assert sorted(p.name for p in (own_home / ".evo" / "hub" / "local-outputs" / SESSION).iterdir()) == [
        "queue.json",
        "queue.lock",
    ]

    fire("local-tool", shot_payload(own_home, call="toolu_again", image=big), monkeypatch, capsys)
    assert queue(own_home)["items"] == []  # the run holds that image already


def test_sent_files_and_a_published_page_go_as_sent_outputs_text_redacted(own_home, hub, spawned, monkeypatch, capsys):
    image, report = files(own_home)
    page = own_home / "work" / "step-graph.html"
    page.write_text(f"<html><body>token={SECRET}</body></html>", encoding="utf-8")
    fire("user-prompt", payload(own_home, prompt="go"), monkeypatch, capsys)
    caption = f"Trang run ở 375 px, key {SECRET}"
    fire("local-tool", send_user_file(own_home, [image, report], caption), monkeypatch, capsys)
    fire("local-tool", artifact_payload(own_home, page), monkeypatch, capsys)
    assert SECRET not in json.dumps(queue(own_home))
    send_queued()
    assert [(o["source"], o["name"], o["media_type"]) for o in hub.outputs] == [
        ("sent", "run-page-truoc-sau.png", "image/png"),
        ("sent", "Thiết kế trang run.md", "text/markdown"),
        ("sent", "step-graph.html", "text/html"),
    ]
    assert hub.outputs[0]["caption"] == "Trang run ở 375 px, key sk-ant-***"
    assert hub.outputs[2]["caption"].startswith("Plan Step Graph: https://claude.ai/artifact/AZqjuopoXWRJrwLpQFwybt")
    assert hub.held[hub.outputs[0]["sha256"]] == PNG  # an image is never changed
    sent_report = hub.held[hub.outputs[1]["sha256"]].decode()
    assert SECRET not in sent_report and TOKEN not in sent_report and "***" in sent_report
    assert SECRET not in hub.held[hub.outputs[2]["sha256"]].decode()
    assert {o["tool_call_id"] for o in hub.outputs} == {"toolu_send", "toolu_artifact"}
    assert report.read_text(encoding="utf-8").count(SECRET) == 1  # the file on this machine stays as it was


def test_hub_send_sends_files_of_a_cli_session_at_once(own_home, hub, spawned, monkeypatch, capsys):
    _, report = files(own_home)
    fire("user-prompt", payload(own_home, prompt="go"), monkeypatch, capsys)
    monkeypatch.setenv(local_sync.SESSION_VARIABLE, SESSION)
    code = main(["hub", "send", str(report), "--caption", "the report", "--json"])
    out, err = capsys.readouterr()
    assert (code, err) == (0, "")
    answer = json.loads(out)
    assert (answer["project"], answer["run_id"], answer["session_id"], answer["queued"]) == ("demo", 7, SESSION, [])
    assert [(o["name"], o["caption"], o["source"]) for o in answer["outputs"]] == [
        ("Thiết kế trang run.md", "the report", "sent")
    ]
    assert SECRET not in hub.held[answer["outputs"][0]["sha256"]].decode()

    hub.down = True
    assert main(["hub", "send", str(report)]) == 0
    out, _ = capsys.readouterr()
    assert out.startswith("queued Thiết kế trang run.md for local run #7: the hub at https://hub.test did not answer")
    assert [i["name"] for i in queue(own_home)["items"]] == ["Thiết kế trang run.md"]

    missing = main(["hub", "send", str(own_home / "nope.png")])
    assert missing == 1 and "no such file" in capsys.readouterr().err
    monkeypatch.delenv(local_sync.SESSION_VARIABLE)
    monkeypatch.chdir(own_home)
    outside = main(["hub", "send", str(report)])
    assert outside == 1 and "no local run of a Claude Code session" in capsys.readouterr().err


def test_tool_images_switched_off_are_never_uploaded_and_no_longer_taken(own_home, hub, spawned, monkeypatch, capsys):
    image, _ = files(own_home)
    hub.tool_images = False
    fire("local-tool", shot_payload(own_home), monkeypatch, capsys)
    fire("local-tool", send_user_file(own_home, [image], "sent on purpose"), monkeypatch, capsys)
    send_queued()
    assert [o["source"] for o in hub.outputs] == ["sent"]
    assert not [c for c in hub.calls if c[1].endswith("/output-uploads") and "tool" in json.dumps(c[2])]
    assert queue(own_home)["tool_images"] is False and queue(own_home)["items"] == []
    fire("local-tool", read_payload(own_home), monkeypatch, capsys)
    assert queue(own_home)["items"] == []  # not even kept on this machine
    hub.tool_images = True  # switched on again: once the switch read is old, the next image goes and reads it again
    monkeypatch.setattr(local_outputs, "SWITCH_TTL", 0.0)
    fire("local-tool", read_payload(own_home, image=PNG * 2), monkeypatch, capsys)
    send_queued()
    assert [o["source"] for o in hub.outputs] == ["sent", "tool"] and queue(own_home)["tool_images"] is True
    update = [e for e in hub.events if e["kind"] == "tool_call_update"][-2]["body"]
    assert b64(PNG)[:64] not in json.dumps(update) and update["content"][1]["content"]["sha256"] == sha(PNG)


def test_a_hub_or_store_that_does_not_answer_keeps_the_outputs_for_later(own_home, hub, spawned, monkeypatch, capsys):
    fire("user-prompt", payload(own_home, prompt="go"), monkeypatch, capsys)
    hub.down = True
    started = time.monotonic()
    assert fire("local-tool", shot_payload(own_home), monkeypatch, capsys) == (0, "", "")
    assert time.monotonic() - started < local_sync.BUDGET["tool"]
    assert spawned == [local_outputs.SEND_WAIT]  # the hook does not wait for the hub: the sender finds it silent
    assert send_queued() == 0
    assert hub.outputs == [] and len(queue(own_home)["items"]) == 1
    state = hooks.HookState(own_home / ".evo" / "hub")
    assert state.down_since(URL, time.time()) is not None
    fire("local-tool", read_payload(own_home), monkeypatch, capsys)
    assert spawned == [local_outputs.SEND_WAIT]  # for COOLDOWN seconds no hook starts another
    assert len(queue(own_home)["items"]) == 2

    hub.down = False
    state.mark_up(URL)
    hub.store_down = True
    monkeypatch.setattr(local_send, "PUT_ATTEMPTS", 1)
    fire("stop", payload(own_home), monkeypatch, capsys)
    assert spawned == [local_outputs.SEND_WAIT] * 2
    assert send_queued() == 0
    assert hub.outputs == [] and len(queue(own_home)["items"]) == 2
    log = (own_home / ".evo" / "hub" / "local-outputs" / "send.log").read_text(encoding="utf-8")
    assert "0 output(s) recorded" in log and "2 waiting" in log and "stopped: cannot reach https://r2.test" in log

    hub.store_down = False
    state.mark_up(URL)
    fire("stop", payload(own_home), monkeypatch, capsys)  # the next hook starts the sender again
    assert spawned == [local_outputs.SEND_WAIT] * 3
    send_queued()
    assert sorted(o["source"] for o in hub.outputs) == ["tool", "tool"] and queue(own_home)["items"] == []


def test_an_output_the_hub_refuses_is_dropped_and_the_others_go(own_home, hub, spawned, monkeypatch, capsys):
    image, report = files(own_home)
    hub.refuse["run-page-truoc-sau.png"] = 413
    fire("local-tool", send_user_file(own_home, [image, report], "two files"), monkeypatch, capsys)
    report.unlink()  # before the sender copied it
    send_queued()
    assert hub.outputs == [] and queue(own_home)["items"] == []
    log = (own_home / ".evo" / "hub" / "local-outputs" / "send.log").read_text(encoding="utf-8")
    assert "run-page-truoc-sau.png: run-page-truoc-sau.png refused" in log and "no such file any more" in log


def test_a_session_outside_a_hub_project_or_unknown_to_the_hub_loses_its_queue(
    own_home, hub, spawned, monkeypatch, capsys
):
    fire("local-tool", shot_payload(own_home), monkeypatch, capsys)
    rec = session_file(own_home)
    path = own_home / ".evo" / "hub" / "local" / f"{SESSION}.json"
    path.write_text(json.dumps({**rec, "run_id": 8}), encoding="utf-8")  # a run the hub does not know
    send_queued()
    assert not (own_home / ".evo" / "hub" / "local-outputs" / SESSION).exists()
    assert hub.outputs == []


def test_a_question_carries_the_files_sent_since_the_person_s_last_message(own_home, hub, spawned, monkeypatch, capsys):
    image, report = files(own_home)
    dialog = payload(
        own_home,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_ask",
        tool_input={"questions": [{"question": "Ship it?", "options": [{"label": "Yes"}, {"label": "No"}]}]},
    )
    fire("user-prompt", payload(own_home, prompt="first"), monkeypatch, capsys)
    fire("local-tool", send_user_file(own_home, [report], "earlier"), monkeypatch, capsys)
    send_queued()
    fire("user-prompt", payload(own_home, prompt="second"), monkeypatch, capsys)
    fire("local-tool", shot_payload(own_home), monkeypatch, capsys)  # a tool image is no file sent
    fire("local-tool", send_user_file(own_home, [image], "now", call="toolu_now"), monkeypatch, capsys)
    monkeypatch.setattr(local_outputs, "spawn_sender", lambda wait=0: (spawned.append(wait), send_queued()))
    assert fire("local-ask", dialog, monkeypatch, capsys) == (0, "", "")
    (asked,) = hub.of("POST", "/decisions")
    now = [o["id"] for o in hub.outputs if o["name"] == "run-page-truoc-sau.png"]
    assert asked["output_ids"] == now and spawned[-1] == 0  # the question started the sender at once

    hub.outputs = [o for o in hub.outputs if o["id"] not in now]  # deleted on the hub meanwhile
    fire("local-ask", {**dialog, "tool_use_id": "toolu_ask2"}, monkeypatch, capsys)
    first, second = hub.of("POST", "/decisions")[1:]
    assert first["output_ids"] == now and "output_ids" not in second  # asked again without them
    fire("user-prompt", payload(own_home, prompt="third"), monkeypatch, capsys)
    fire("local-ask", {**dialog, "tool_use_id": "toolu_ask3"}, monkeypatch, capsys)
    assert "output_ids" not in hub.of("POST", "/decisions")[-1]


def test_the_sender_waits_for_a_session_that_queued_outputs_at_its_first_hook_to_register(own_home, hub, monkeypatch):
    found = local_outputs.found_in(shot_payload(own_home))
    local_outputs.take(SESSION, URL, found, TOKEN, "toolu_shot")  # the hook queues before it registers the session
    monkeypatch.setattr(local_send.time, "sleep", lambda seconds: fire_register(own_home, monkeypatch))
    send_queued()
    assert [o["source"] for o in hub.outputs] == ["tool"]


def fire_register(own_home: Path, monkeypatch) -> None:
    """The hook's registering, done while the sender waits for it."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload(own_home, prompt="go"))))
    main(["hub", "hook", "user-prompt"])


def test_a_question_waits_for_its_files_at_most_ask_wait(own_home, hub, spawned, monkeypatch, capsys):
    _, report = files(own_home)
    fire("local-tool", send_user_file(own_home, [report], "pending"), monkeypatch, capsys)
    monkeypatch.setattr(local_outputs, "ASK_WAIT", 0.3)
    hub.store_down = True
    dialog = payload(
        own_home,
        tool_name="AskUserQuestion",
        tool_use_id="toolu_ask",
        tool_input={"questions": [{"question": "Ship it?", "options": [{"label": "Yes"}, {"label": "No"}]}]},
    )
    started = time.monotonic()
    assert fire("local-ask", dialog, monkeypatch, capsys) == (0, "", "")
    assert time.monotonic() - started < 2 and "output_ids" not in hub.of("POST", "/decisions")[0]


def test_hub_send_queued_starts_from_a_hook_as_a_detached_process(own_home, monkeypatch):
    calls = []
    monkeypatch.setattr(local_outputs.subprocess, "Popen", lambda command, **options: calls.append((command, options)))
    local_outputs.spawn_sender()
    ((command, options),) = calls
    assert command == [sys.executable, "-m", "evo_agents", "hub", "send", "--queued", "--wait", "1"]
    assert options["stdout"] == options["stderr"] == local_outputs.subprocess.DEVNULL
    assert options.get("start_new_session") or os.name == "nt"
    assert options["cwd"] == str(own_home / ".evo" / "hub" / "local-outputs")
    with local_outputs.send_lock() as held:
        assert held and local_outputs.sender_running()
        assert local_outputs.kick(URL, time.time()) is False
    assert local_outputs.sender_running() is False


# The whole of it against a hub


@pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
def test_a_local_session_s_outputs_reach_its_run_on_the_hub(hub_db, tmp_path, s3, monkeypatch):
    from tests.hub.test_local_sync import _project, run_hook

    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN, **s3.env()) as served:
        admin = Hub(served.url, live.insert_token(hub_db, live.ADMIN))
        admin.call("PUT", "/v1/projects/demo", _project())
        admin.call("PUT", "/v1/admin/projects/demo/grants/alice", {"role": "writer", "max_level": "internal"})
        token = live.insert_token(hub_db, "alice")
        home = tmp_path / "home"
        (home / "work").mkdir(parents=True)
        directory = home / ".evo" / "hub"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        (directory / "token").write_text(token + "\n", encoding="utf-8")
        (directory / "config.json").write_text(json.dumps({"url": served.url, "login": "alice"}), encoding="utf-8")
        for name in ("token", "config.json"):
            os.chmod(directory / name, 0o600)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(MemorySync, "project_of", lambda self, where=None: "demo")
        monkeypatch.setattr(local_sync, "FLUSH_EVERY", 0.0)
        monkeypatch.setattr(local_outputs, "spawn_sender", lambda wait=0: local_send.send_queued(0))
        alice = Hub(served.url, token)
        image, report = files(home)

        run_hook("user-prompt", payload(home, prompt="ship it"))
        run_hook("local-tool", shot_payload(home))
        run_hook("local-tool", send_user_file(home, [image, report], f"the page, key {SECRET}"))
        run_hook(
            "local-ask",
            payload(
                home,
                tool_name="AskUserQuestion",
                tool_use_id="toolu_ask",
                tool_input={"questions": [{"question": "Ship it?", "options": [{"label": "Yes"}, {"label": "No"}]}]},
            ),
        )
        run_id = session_file(home)["run_id"]
        listed = alice.call("GET", f"/v1/projects/demo/runs/{run_id}/outputs")
        by_name = {o["name"]: o for o in listed["outputs"]}
        assert {name: o["source"] for name, o in by_name.items()} == {
            shot_name(): "tool",
            "run-page-truoc-sau.png": "sent",
            "Thiết kế trang run.md": "sent",
        }
        assert by_name[shot_name()]["tool_call_id"] == "toolu_shot"
        assert by_name["run-page-truoc-sau.png"]["caption"] == "the page, key sk-ant-***"
        events = json.dumps(alice.call("GET", f"/v1/projects/demo/runs/{run_id}/events"))
        assert sha(JPEG) in events and b64(JPEG)[:64] not in events and SECRET not in events
        md = by_name["Thiết kế trang run.md"]
        text = hub_client._send("GET", served.url + md["url"], {"Authorization": f"Bearer {token}"}, None, 10)[1]
        assert b"sk-ant-***" in text and SECRET.encode() not in text
        (decision,) = alice.call("GET", f"/v1/projects/demo/decisions?run_id={run_id}")["decisions"]
        assert {o["name"] for o in decision["outputs"]} == {"run-page-truoc-sau.png", "Thiết kế trang run.md"}

        grant = {"role": "admin", "max_level": "internal"}
        admin.call("PUT", f"/v1/admin/projects/demo/grants/{live.ADMIN}", grant)
        admin.call("PUT", "/v1/projects/demo/output-settings", {"tool_images": False})
        run_hook("local-tool", read_payload(home))
        assert len(alice.call("GET", f"/v1/projects/demo/runs/{run_id}/outputs")["outputs"]) == 3
        assert not list((home / ".evo" / "hub" / "local-outputs" / SESSION).glob(sha(PNG)))
