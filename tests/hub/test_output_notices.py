"""The notice of a run's files on the hub (plan run-outputs, step 4): criteria a6 and the hub's part of a4.

The files a run's agent sent, or left in its outputs directory, become one notice ``run_outputs`` to the run's owner,
gathered a minute a run and never with an image taken out of a tool's result; it only reports (plan
hub-attention-runtime, a4), so it is read already and the web alone gets it; the Inbox lists their outputs; a file a
question carries goes with the question; a member turns the notice off and still gets questions; a channel cleared
for the project gets the files of a decision with presigned links, a restricted project's channel the link alone; and
Telegram's message of a restricted project names no file. The people, projects and files are fictional.
"""

from __future__ import annotations

import dataclasses

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import extract, func, select, update

from evo_agents.hub import runs, tables
from evo_agents.hub.server import core_plugin_api as hub_api
from evo_agents.hub.server import notifications, output_notices
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub.live import sql
from tests.hub.s3 import get_url
from tests.hub.test_notifications import add_channel, due_now, listed, read
from tests.hub.test_run_outputs import REGISTER, REPORT, VIDEO, item, running, sent
from tests.hub.test_runs import OWNER, PROJECT, audit_rows, members

SHOT = b"\x89PNG\r\n\x1a\n" + b"lumen settings page" * 20
TOOL_SHOT = b"\x89PNG\r\n\x1a\n" + b"browser screenshot of a tool" * 20
LATE = b"%PDF-1.7\n" + b"northwind invoice draft" * 10
YES_NO = [{"key": "yes", "label": "Yes"}, {"key": "no", "label": "No"}]
TELEGRAM = hub_api.Clearance("customer", "domestic-only", below=True)  # the clearance the Telegram channel holds
N = tables.notifications
D = tables.notification_deliveries
OUTPUT_NOTICES = select(N.c.id, N.c.title, N.c.details, N.c.read_at).where(N.c.notice_kind == runs.OUTPUT_NOTICE)


@pytest.fixture
def client(hub_db, tmp_path, github, s3):
    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    return members(client, github)


class Recording(notifications.Channel):
    """A channel cleared as Telegram's is, which records what the hub's prepare gives it."""

    sent: list = []

    async def prepare(self, conn, notification):
        return await hub_api.prepare(conn, notification, TELEGRAM)

    async def send(self, notification, config):
        Recording.sent.append(notification)


@pytest.fixture
def recording(monkeypatch):
    monkeypatch.setitem(notifications.CHANNELS, "recording", Recording)
    monkeypatch.setattr(Recording, "sent", [])
    return Recording


def local_run(client, hub, session: str = "0199a3c1-0000-7000-8000-00000000c001") -> tuple[int, str]:
    """A local run of owner's session: (its id, the prefix its outputs and questions go to)."""
    registered = client.put(f"/v1/projects/{PROJECT}/local-runs/{session}", json=REGISTER, headers=hub["owner"])
    assert registered.status_code == 201, registered.text
    run_id = registered.json()["id"]
    return run_id, f"/v1/projects/{PROJECT}/local-runs/{run_id}"


def ask_local(client, hub, prefix: str, output_ids: list[int]) -> dict:
    body = {"question": "Ship the settings page?", "options": YES_NO, "output_ids": output_ids}
    asked = client.post(f"{prefix}/decisions", json=body, headers=hub["owner"])
    assert asked.status_code == 201, asked.text
    return asked.json()


def output_notices_of(db) -> list[tuple]:
    return sql(db, OUTPUT_NOTICES.order_by(N.c.id))


def due_in(db, notification_id: int) -> list[float]:
    """Seconds until each delivery of notification ``notification_id`` is due."""
    query = select(extract("epoch", D.c.next_at - func.now())).where(D.c.notification_id == notification_id)
    return [float(seconds) for (seconds,) in sql(db, query)]


def deliver(client, *, blobs=True) -> dict:
    store = client.app.state.blobs if blobs else None
    return client.portal.call(lambda: notifications.deliver_notifications(client.app.state.engine, blobs=store))


def clear_hub_sink(db, level: str) -> None:
    """The project's hub sink cleared for ``level`` in any location: internal is below Telegram's clearance."""
    sinks = tables.project_sinks
    sql(db, update(sinks).values(clearance={"level": level, "location": "any"}).where(sinks.c.kind == "hub"))


def test_the_files_a_run_sent_become_one_notice_a_minute_to_its_owner_and_never_a_tool_image(client, hub, hub_db):
    run_id, prefix = local_run(client, hub)
    tool = item(TOOL_SHOT, "browser.png", "image/png", "tool", tool_call_id="toolu_shot")
    sent(client, prefix, hub["owner"], [(TOOL_SHOT, tool)])
    assert output_notices_of(hub_db) == [], "an image taken out of a tool's result sends no notice"

    report = item(REPORT, "report.md", "text/markdown", caption="What changed, for the owner")
    (shown_report,) = sent(client, prefix, hub["owner"], [(REPORT, report)])["outputs"]
    ((notice_id, title, details, _),) = output_notices_of(hub_db)
    assert (title, details) == (f"Local run #{run_id} sent report.md", {"outputs": [shown_report["id"]]})
    assert all(55 <= seconds <= 61 for seconds in due_in(hub_db, notice_id)), "the web waits a minute for more"
    assert len(due_in(hub_db, notice_id)) == 1, "the notice only reports: the web alone gets it (a4)"

    demo = item(VIDEO, "demo.mp4", "video/mp4", "outputs")
    (shown_demo,) = sent(client, prefix, hub["owner"], [(VIDEO, demo)])["outputs"]
    ((same_id, title, details, read_at),) = output_notices_of(hub_db)
    assert same_id == notice_id and read_at is not None, "a file within the minute joins the notice, read still"
    assert (title, details) == (
        f"Local run #{run_id} sent 2 files",
        {"outputs": [shown_report["id"], shown_demo["id"]]},
    )
    page = listed(client, hub["owner"], kind="notice")["notifications"]
    (notice,) = [n for n in page if n["id"] == notice_id]
    assert (notice["notice_kind"], notice["link"]) == ("run_outputs", f"/p/{PROJECT}/runs/{run_id}")
    assert "- report.md (" in notice["body"] and ": What changed, for the owner" in notice["body"]
    assert [(o["id"], o["source"], o["viewer"]) for o in notice["outputs"]] == [
        (shown_report["id"], "sent", "text"),
        (shown_demo["id"], "outputs", "video"),
    ]
    assert notice["outputs"][0]["url"] == f"/v1/projects/{PROJECT}/runs/{run_id}/outputs/{shown_report['id']}/content"
    assert client.get(notice["outputs"][0]["url"], headers=hub["owner"]).content == REPORT

    due_now(hub_db)
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 0}
    sent(client, prefix, hub["owner"], [(VIDEO, {**demo, "caption": "The new flow, 30 s"})])
    assert len(output_notices_of(hub_db)) == 1, "a file sent again with a caption is not told twice"
    (shown_late,) = sent(client, prefix, hub["owner"], [(LATE, item(LATE, "invoice.pdf", "application/pdf"))])[
        "outputs"
    ]
    first, second = output_notices_of(hub_db)
    assert first[2] == {"outputs": [shown_report["id"], shown_demo["id"]]}, "a notice a channel took stays as it went"
    assert second[2] == {"outputs": [shown_late["id"]]}
    assert all(55 <= seconds <= 61 for seconds in due_in(hub_db, second[0]))


def test_a_worker_runs_files_in_its_outputs_directory_notify_its_owner(client, hub, hub_db):
    worker, run_id, prefix = running(client, hub)
    sent(client, prefix, worker["headers"], [(SHOT, item(SHOT, "settings.png", "image/png", "outputs"))])
    ((_, title, _, _),) = output_notices_of(hub_db)
    assert title == f"Step run #{run_id} sent settings.png"
    owner = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == OWNER))[0][0]
    assert sql(hub_db, select(N.c.user_id, N.c.run_id).where(N.c.notice_kind == runs.OUTPUT_NOTICE)) == [
        (owner, run_id)
    ]


def before_tiers(settings: dict) -> dict:
    """The keys of the notification settings before the tiers and quiet hours came (tests/hub/test_push_tiers.py):
    the same, with the same values."""
    return {key: settings[key] for key in ("notices", "hide_details")}


def test_a_member_turns_the_notice_off_and_still_gets_questions(client, hub, hub_db):
    path = "/v1/me/notification-settings"
    shown = {"notices": [{"kind": "run_outputs", "on": True}], "hide_details": False}
    assert before_tiers(client.get(path, headers=hub["owner"]).json()) == shown
    for bad in ({"notices": [{"kind": "plan_finished", "on": False}]}, {"notices": []}):
        assert client.put(path, json=bad, headers=hub["owner"]).status_code == 422, bad
    twice = {"notices": [{"kind": "run_outputs", "on": False}, {"kind": "run_outputs", "on": True}]}
    assert client.put(path, json=twice, headers=hub["owner"]).status_code == 422
    off = client.put(path, json={"notices": [{"kind": "run_outputs", "on": False}]}, headers=hub["owner"])
    assert off.status_code == 200, off.text
    assert before_tiers(off.json()) == {"notices": [{"kind": "run_outputs", "on": False}], "hide_details": False}
    again = client.put(path, json={"notices": [{"kind": "run_outputs", "on": False}]}, headers=hub["owner"])
    assert again.status_code == 200, "turning it off twice is no error"
    assert client.get(path, headers=hub["other"]).json()["notices"][0]["on"] is True, "another member's stays on"
    assert audit_rows(hub_db, "notification")[-1][:3] == (
        "notification.settings",
        "notifications:settings run_outputs=off",
        OWNER,
    )

    run_id, prefix = local_run(client, hub)
    (shot,) = sent(client, prefix, hub["owner"], [(SHOT, item(SHOT, "settings.png", "image/png"))])["outputs"]
    assert output_notices_of(hub_db) == []
    decision = ask_local(client, hub, prefix, [shot["id"]])
    (asked,) = listed(client, hub["owner"], kind="decision")["notifications"]
    assert asked["decision_id"] == decision["id"] and [o["id"] for o in asked["outputs"]] == [shot["id"]]

    on = client.put(path, json={"notices": [{"kind": "run_outputs", "on": True}]}, headers=hub["owner"])
    assert before_tiers(on.json()) == shown
    sent(client, prefix, hub["owner"], [(REPORT, item(REPORT, "report.md", "text/markdown"))])
    assert [title for _, title, _, _ in output_notices_of(hub_db)] == [f"Local run #{run_id} sent report.md"]


def test_a_file_a_question_carries_goes_with_the_question_and_leaves_the_notice(client, hub, hub_db):
    run_id, prefix = local_run(client, hub)
    report, shot = sent(
        client,
        prefix,
        hub["owner"],
        [(REPORT, item(REPORT, "report.md", "text/markdown")), (SHOT, item(SHOT, "settings.png", "image/png"))],
    )["outputs"]
    tool = item(TOOL_SHOT, "browser.png", "image/png", "tool")
    (tool_shot,) = sent(client, prefix, hub["owner"], [(TOOL_SHOT, tool)])["outputs"]
    ((notice_id, _, details, _),) = output_notices_of(hub_db)
    assert details == {"outputs": [report["id"], shot["id"]]}

    first = ask_local(client, hub, prefix, [report["id"], tool_shot["id"]])
    ((_, title, details, _),) = output_notices_of(hub_db)
    assert (title, details) == (f"Local run #{run_id} sent settings.png", {"outputs": [shot["id"]]})
    ask_local(client, hub, prefix, [shot["id"]])
    assert output_notices_of(hub_db) == [], "a notice left with no file goes"
    assert sql(hub_db, select(func.count()).select_from(D).where(D.c.notification_id == notice_id)) == [(0,)]
    asked = {n["decision_id"]: n for n in listed(client, hub["owner"], kind="decision")["notifications"]}
    url = f"/v1/projects/{PROJECT}/decisions/{first['id']}/outputs/{report['id']}/content"
    assert [(o["id"], o["url"]) for o in asked[first["id"]]["outputs"]] == [
        (report["id"], url),
        (tool_shot["id"], f"/v1/projects/{PROJECT}/decisions/{first['id']}/outputs/{tool_shot['id']}/content"),
    ], "the web shows a question's files, a tool image among them"


def test_a_channel_gets_a_questions_files_with_links_and_a_notice_of_files_stays_on_the_web(client, hub, hub_db, recording):
    add_channel(hub_db, OWNER, "recording")
    clear_hub_sink(hub_db, "internal")
    run_id, prefix = local_run(client, hub)
    tool = item(TOOL_SHOT, "browser.png", "image/png", "tool")
    files = [(TOOL_SHOT, tool), (SHOT, item(SHOT, "settings.png", "image/png", caption="After the fix"))]
    tool_shot, shot = sent(client, prefix, hub["owner"], files)["outputs"]
    decision = ask_local(client, hub, prefix, [tool_shot["id"], shot["id"]])
    report, demo = sent(
        client,
        prefix,
        hub["owner"],
        [(REPORT, item(REPORT, "report.md", "text/markdown")), (VIDEO, item(VIDEO, "demo.mp4", "video/mp4"))],
    )["outputs"]

    assert deliver(client)["delivered"] == 2, "the question goes at once, the notice waits its minute"
    (question,) = recording.sent
    assert question.decision_id == decision["id"] and question.extra["restricted"] is False
    assert question.extra["outputs_count"] == 1
    (carried,) = question.extra["outputs"]
    assert {key: carried[key] for key in ("id", "name", "media_type", "viewer", "caption")} == {
        "id": shot["id"],
        "name": "settings.png",
        "media_type": "image/png",
        "viewer": "image",
        "caption": "After the fix",
    }
    assert get_url(carried["url"]) == SHOT

    due_now(hub_db)
    assert deliver(client)["delivered"] == 1, "the notice of files only reports: the web alone takes it (a4)"
    assert recording.sent == [question]
    ((notice_id, title, details, _),) = output_notices_of(hub_db)
    assert details == {"outputs": [report["id"], demo["id"]]}
    outside = dataclasses.replace(
        question, id=notice_id, kind="notice", notice_kind="run_outputs", decision_id=None, details=details, extra=None
    )
    files, count = client.portal.call(_channel_outputs, client.app.state.engine, outside)
    assert count == 2 and [(f["name"], f["size"]) for f in files] == [("report.md", len(REPORT)), ("demo.mp4", 1024)]
    assert [f["url"] for f in files] == [None, None], "outside the job's pass there is no store to sign with"


async def _channel_outputs(engine, notification):
    async with engine.begin() as conn:
        return await output_notices.channel_outputs(conn, notification)


def test_a_restricted_projects_channel_gets_neither_files_nor_their_names(client, hub, hub_db, recording):
    add_channel(hub_db, OWNER, "recording")
    clear_hub_sink(hub_db, "customer")  # at Telegram's level: restricted
    _, prefix = local_run(client, hub)
    (shot,) = sent(client, prefix, hub["owner"], [(SHOT, item(SHOT, "northwind-contract.png", "image/png"))])["outputs"]
    ask_local(client, hub, prefix, [shot["id"]])
    sent(client, prefix, hub["owner"], [(REPORT, item(REPORT, "northwind-terms.md", "text/markdown"))])
    due_now(hub_db)
    assert deliver(client)["delivered"] == 3, "the question on the web and the channel, the notice on the web alone"
    assert [n.extra for n in recording.sent] == [{"restricted": True}]


def test_telegrams_message_of_a_restricted_projects_files_holds_the_project_the_kind_and_the_link_alone():
    server = pytest.importorskip("evo_core_plugins.telegram.server", reason="install deploy/hub/core-plugins.txt")
    notice = notifications.Outgoing(
        id=1,
        user_id=1,
        login=OWNER,
        kind="notice",
        notice_kind="run_outputs",
        project="lumen",
        run_id=7,
        decision_id=None,
        title="Local run #7 sent northwind-contract.png",
        body="- northwind-contract.png (1.2 KiB)\n\nFiles of local run #7 in project lumen.",
        details={"outputs": [3]},
        link="/p/lumen/runs/7",
        created_at=None,
        extra={"restricted": True},
    )
    text = server.TelegramChannel().message(notice).text
    assert "lumen" in text and "/p/lumen/runs/7" in text
    assert "northwind" not in text and "contract" not in text
    shown = server.TelegramChannel().message(dataclasses.replace(notice, extra={"restricted": False})).text
    assert "northwind-contract.png" in shown, "a project that is not restricted gets the names"
