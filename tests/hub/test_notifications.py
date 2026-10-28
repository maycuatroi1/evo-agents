"""Notifications on the hub: the notices a plan run's worker sends, the ones the hub sends when a plan run ends, the
outbox with a delivery per channel that is on, the job that delivers them through the channel registry, and the
routes a member reads them with.

The checks step 4 of the plan-runs-and-decisions plan names for notifications: a fake channel in the registry gets
its delivery and is tried again when it fails. Around it: the web is delivered at once, the fifth failure fails the
delivery, a kind without a class or a channel turned off fails at once, the member's list, count and read, and the
audit row of a read that names ids and no text."""

from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import extract, func, insert, select, update

from evo_agents.hub import jobs, tables
from evo_agents.hub.server import notifications
from evo_agents.hub.server.app import create_app
from evo_agents.hub.worker import queue
from tests.hub.live import sql
from tests.hub.test_plan_runs import PLAN, dispatch_steps, fleet_worker, plan_body, push, reported, started
from tests.hub.test_runs import OWNER, PASSED, PROJECT, PROTOCOL, SHA, audit_rows, claim, members, moved

PUSHED = {
    "kind": "push_default_branch",
    "title": "Pushed main of agent-skills",
    "body": "Two commits of step 3.",
    "repo": "agent-skills",
    "branch": "main",
    "commits": [SHA, "b" * 40],
}
# A notice that asks for attention, so its member's channels get it: a push only reports (``runs.REPORT_NOTICE_KINDS``).
FAILED = {**PUSHED, "kind": "run_failed", "title": "Step 3 of fleet failed", "body": "Its verify exited 1."}


@pytest.fixture
def client(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """test_plan_runs' ``hub``: the project, its members, and the plan fleet pushed by owner."""
    headers = members(client, github)
    push(client, headers["owner"], plan_body())
    return headers


class FakeChannel(notifications.Channel):
    """Records what it is handed; fails while ``failures`` says so."""

    sent: list = []
    failures = 0

    async def send(self, notification, config):
        if FakeChannel.failures > 0:
            FakeChannel.failures -= 1
            raise RuntimeError(f"the fake service answered 503 for chat {config.get('chat_id')}")
        FakeChannel.sent.append((notification, config))


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setitem(notifications.CHANNELS, "fake", FakeChannel)
    monkeypatch.setattr(FakeChannel, "sent", [])
    monkeypatch.setattr(FakeChannel, "failures", 0)
    return FakeChannel


CHANNELS = tables.notification_channels
DELIVERIES = tables.notification_deliveries
NOTIFICATION_COUNT = select(func.count()).select_from(tables.notifications)


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def add_channel(db, login: str, kind: str, *, enabled: bool = True, config=None) -> int:
    added = insert(CHANNELS).values(user_id=user_id(db, login), kind=kind, config=config or {}, enabled=enabled)
    return sql(db, added.returning(CHANNELS.c.id))[0][0]


def notice(client, worker: dict, run_id: int, **body):
    return client.post(f"/v1/worker/runs/{run_id}/notices", json={**PUSHED, **body}, headers=worker["headers"])


def alert(client, worker: dict, run_id: int, **body):
    """A notice of FAILED, which reaches the member's channels."""
    return notice(client, worker, run_id, **{**FAILED, **body})


def deliver(client) -> dict:
    return client.portal.call(notifications.deliver_notifications, client.app.state.engine)


def deliveries(db) -> list[tuple]:
    return sql(
        db,
        select(
            func.coalesce(CHANNELS.c.kind, "web"), DELIVERIES.c.state, DELIVERIES.c.attempts, DELIVERIES.c.last_error
        )
        .select_from(DELIVERIES.outerjoin(CHANNELS, CHANNELS.c.id == DELIVERIES.c.channel_id))
        .order_by(DELIVERIES.c.id),
    )


def due_now(db) -> None:
    sql(db, update(DELIVERIES).values(next_at=func.now()).where(DELIVERIES.c.state == "pending"))


def due_in(db, *where) -> list[tuple]:
    """Seconds until each delivery that matches ``where`` is due."""
    return sql(db, select(extract("epoch", DELIVERIES.c.next_at - func.now())).where(*where))


def listed(client, headers, **params) -> dict:
    response = client.get("/v1/me/notifications", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def count(client, headers) -> dict:
    response = client.get("/v1/me/notifications/count", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def read(client, headers, **body):
    return client.post("/v1/me/notifications/read", json=body, headers=headers)


# Notices


def test_a_notice_notifies_the_owner_with_a_delivery_per_channel_that_is_on(client, hub, hub_db, fake):
    add_channel(hub_db, OWNER, "fake", config={"chat_id": 42})
    add_channel(hub_db, OWNER, "mail", enabled=False)
    add_channel(hub_db, "someone-else", "fake")
    worker, run = started(client, hub)
    response = alert(client, worker, run["id"])
    assert response.status_code == 201, response.text
    sent = response.json()
    assert {key: sent[key] for key in ("kind", "notice_kind", "project", "run_id", "decision_id", "title", "link")} == {
        "kind": "notice",
        "notice_kind": "run_failed",
        "project": PROJECT,
        "run_id": run["id"],
        "decision_id": None,
        "title": "Step 3 of fleet failed",
        "link": f"/p/{PROJECT}/runs/{run['id']}",
    }
    assert sent["details"] == {"repo": "agent-skills", "branch": "main", "commits": [SHA, "b" * 40]}
    assert (sent["body"], sent["read_at"], sent["decision_state"]) == ("Its verify exited 1.", None, None)
    assert sent["report"] is False
    # the web and the owner's channel that is on; not the one turned off, nor anyone else's
    assert deliveries(hub_db) == [("web", "pending", 0, None), ("fake", "pending", 0, None)]
    run_events = tables.run_events
    events = sql(
        hub_db, select(run_events.c.body).where(run_events.c.run_id == run["id"], run_events.c.kind == "system")
    )
    assert events[-1][0]["text"] == "notice run_failed: Step 3 of fleet failed"
    assert events[-1][0]["notice"]["notification_id"] == sent["id"]
    assert [item["id"] for item in listed(client, hub["owner"])["notifications"]] == [sent["id"]]
    assert listed(client, hub["other"])["notifications"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "deploy"},
        {"title": "two\nlines"},
        {"title": ""},
        {"repo": "evo-cli"},  # not a repo of the run
        {"commits": ["abc"]},
        {"body": "é" * (8 * 1024 + 1)},
    ],
)
def test_a_notice_the_route_does_not_take_gets_422(client, hub, hub_db, body):
    worker, run = started(client, hub)
    assert notice(client, worker, run["id"], **body).status_code == 422
    assert sql(hub_db, NOTIFICATION_COUNT) == [(0,)]


def test_only_the_worker_holding_a_plan_run_sends_its_notices(client, hub, hub_db):
    worker, run = started(client, hub)
    other = fleet_worker(client, hub["owner"], "linux-box")
    assert notice(client, other, run["id"]).status_code == 404
    as_member = {**hub["owner"], **PROTOCOL}
    assert client.post(f"/v1/worker/runs/{run['id']}/notices", json=PUSHED, headers=as_member).status_code == 403
    (single,) = dispatch_steps(client, hub["owner"], [2], plan_id="rollout").json()
    assert claim(client, other)["id"] == single["id"]
    assert notice(client, other, single["id"], repo=None).status_code == 404  # a run of one step sends no notice
    assert sql(hub_db, NOTIFICATION_COUNT) == [(0,)]


def test_a_plan_run_that_finishes_or_fails_notifies_its_owner(client, hub, hub_db):
    worker, run = started(client, hub)
    run_id = run["id"]
    for key in (2, 3, 4):
        reported(client, worker, run_id, key, "done", verify=PASSED, commit_sha=SHA)
    moved(client, worker, run_id, "verifying", "done")
    (finished,) = listed(client, hub["owner"])["notifications"]
    assert {key: finished[key] for key in ("kind", "notice_kind", "run_id", "title", "details")} == {
        "kind": "notice",
        "notice_kind": "plan_finished",
        "run_id": run_id,
        "title": f"Plan {PLAN} finished",
        "details": {"plan_id": PLAN, "steps": ["2", "3", "4"]},
    }

    # a plan with a step left: done sends nothing, failed sends run_failed
    push(client, hub["owner"], plan_body("second"))
    second = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": "second"}, headers=hub["owner"]).json()
    assert claim(client, worker)["id"] == second["id"]
    moved(client, worker, second["id"], "running", "failed", error="the agent stopped")
    failed = listed(client, hub["owner"], kind="notice")["notifications"][0]
    assert {key: failed[key] for key in ("notice_kind", "run_id", "title", "body", "details")} == {
        "notice_kind": "run_failed",
        "run_id": second["id"],
        "title": f"Plan run #{second['id']} of second failed",
        "body": "the agent stopped",
        "details": {"plan_id": "second", "error": "the agent stopped"},
    }
    assert sql(hub_db, NOTIFICATION_COUNT) == [(2,)]


# Delivery


def test_the_job_delivers_on_the_web_at_once_and_tries_a_failing_channel_again(client, hub, hub_db, fake):
    add_channel(hub_db, OWNER, "fake", config={"chat_id": 42})
    worker, run = started(client, hub)
    sent = alert(client, worker, run["id"]).json()
    fake.failures = 2
    assert deliver(client) == {"delivered": 1, "retried": 1, "failed": 0}
    web, channel = deliveries(hub_db)
    assert web == ("web", "delivered", 1, None)
    assert channel == ("fake", "pending", 1, "RuntimeError: the fake service answered 503 for chat 42")
    ((wait,),) = due_in(hub_db, DELIVERIES.c.channel_id.is_not(None))
    assert 50 < wait <= 60  # tried again after a minute
    assert deliver(client) == {"delivered": 0, "retried": 0, "failed": 0}  # not due yet
    due_now(hub_db)
    assert deliver(client) == {"delivered": 0, "retried": 1, "failed": 0}
    ((wait,),) = due_in(hub_db, DELIVERIES.c.state == "pending")
    assert 110 < wait <= 120  # then after two
    due_now(hub_db)
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 0}
    assert deliveries(hub_db)[1][:3] == ("fake", "delivered", 3)
    ((outgoing, config),) = fake.sent
    assert config == {"chat_id": 42}
    assert (outgoing.id, outgoing.login, outgoing.kind, outgoing.notice_kind, outgoing.project) == (
        sent["id"],
        OWNER,
        "notice",
        "run_failed",
        PROJECT,
    )
    assert (outgoing.title, outgoing.run_id, outgoing.details["repo"]) == (sent["title"], run["id"], "agent-skills")


def test_the_fifth_failure_fails_a_delivery_and_a_channel_without_a_class_fails_at_once(client, hub, hub_db, fake):
    add_channel(hub_db, OWNER, "fake")
    add_channel(hub_db, OWNER, "pager")  # no class in the registry
    worker, run = started(client, hub)
    alert(client, worker, run["id"])
    fake.failures = 10
    assert deliver(client) == {"delivered": 1, "retried": 1, "failed": 1}
    assert deliveries(hub_db)[2] == ("pager", "failed", 1, "the hub has no class for channels of kind pager")
    for _ in range(3):
        due_now(hub_db)
        assert deliver(client) == {"delivered": 0, "retried": 1, "failed": 0}
    due_now(hub_db)
    assert deliver(client) == {"delivered": 0, "retried": 0, "failed": 1}
    assert deliveries(hub_db)[1] == ("fake", "failed", 5, "RuntimeError: the fake service answered 503 for chat None")
    due_now(hub_db)
    assert deliver(client) == {"delivered": 0, "retried": 0, "failed": 0}  # a failed delivery is never tried again
    assert fake.sent == []

    # a channel turned off after the notification was stored fails its delivery instead of sending
    channel = add_channel(hub_db, "someone-else", "fake")
    notification = client.portal.call(_notify, client.app.state.engine, user_id(hub_db, "someone-else"))
    sql(hub_db, update(CHANNELS).values(enabled=False).where(CHANNELS.c.id == channel))
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 1}
    assert sql(
        hub_db,
        select(DELIVERIES.c.state, DELIVERIES.c.last_error).where(
            DELIVERIES.c.notification_id == notification, DELIVERIES.c.channel_id == channel
        ),
    ) == [("failed", "the channel was turned off before the notification went out")]


async def _notify(engine, member: int) -> int:
    """A notice for ``member``, stored through the outbox."""
    async with engine.begin() as conn:
        return await notifications.notify(
            conn, user_id=member, kind="notice", notice_kind="run_failed", title="Plan run of fleet failed"
        )


def test_the_job_runs_every_minute_queued_at_most_once_and_takes_the_hubs_pool(client, hub, hub_db):
    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.DELIVER_NOTIFICATIONS].cron == "* * * * *"
    assert queue.tasks[jobs.DELIVER_NOTIFICATIONS].queueing_lock == jobs.DELIVER_NOTIFICATIONS
    worker, run = started(client, hub)
    notice(client, worker, run["id"])
    context = SimpleNamespace(
        additional_context={
            "hub": SimpleNamespace(
                engine=client.app.state.engine, config=client.app.state.config, blobs=client.app.state.blobs
            )
        }
    )
    report = client.portal.call(queue.tasks[jobs.DELIVER_NOTIFICATIONS].func, context)
    assert report == {"delivered": 1, "retried": 0, "failed": 0}


def test_backoff_doubles_up_to_an_hour():
    assert [int(notifications.backoff(n).total_seconds()) for n in range(1, 9)] == [
        60,
        120,
        240,
        480,
        960,
        1920,
        3600,
        3600,
    ]


# The member's notifications


def test_a_member_lists_counts_and_reads_their_notifications(client, hub, hub_db):
    worker, run = started(client, hub)
    first = alert(client, worker, run["id"]).json()
    ask = {
        "category": "external_send",
        "question": "Post the staging link to the team now?",
        "options": [{"key": "yes", "label": "Post"}, {"key": "no", "label": "Wait"}],
    }
    asked = client.post(f"/v1/worker/runs/{run['id']}/decisions", json=ask, headers=worker["headers"])
    assert asked.status_code == 201, asked.text
    second = notice(client, worker, run["id"], kind="merge_default_branch", title="Merged into main").json()
    page = listed(client, hub["owner"])
    # the open decision first, then the newest first
    assert [item["kind"] for item in page["notifications"]] == ["decision", "notice", "notice"]
    assert [item["id"] for item in page["notifications"][1:]] == [second["id"], first["id"]]
    assert page["total"] == 3 and page["notifications"][0]["decision_state"] == "open"
    assert listed(client, hub["owner"], kind="notice")["total"] == 2
    assert listed(client, hub["owner"], project="elsewhere")["total"] == 0
    assert listed(client, hub["owner"], limit=1, offset=1)["notifications"][0]["id"] == second["id"]
    # the merge only reports: it is read already (criterion a4)
    assert count(client, hub["owner"]) == {"unread": 2, "open_decisions": 1, "open_proposals": 0}
    assert count(client, hub["other"]) == {"unread": 0, "open_decisions": 0, "open_proposals": 0}

    marked = read(client, hub["owner"], ids=[first["id"], 999999])
    assert marked.status_code == 200 and marked.json() == {"read": 1, "unread": 1}
    assert read(client, hub["owner"], ids=[first["id"]]).json() == {"read": 0, "unread": 1}  # read already
    assert read(client, hub["other"], ids=[second["id"]]).json() == {"read": 0, "unread": 0}  # not theirs
    assert [item["kind"] for item in listed(client, hub["owner"], unread=True)["notifications"]] == ["decision"]
    for body in ({}, {"ids": [first["id"]], "all": True}, {"ids": []}, {"all": False}):
        assert read(client, hub["owner"], **body).status_code == 422, body
    assert read(client, hub["owner"], all=True).json() == {"read": 1, "unread": 0}
    assert count(client, hub["owner"]) == {
        "unread": 0,
        "open_decisions": 1,
        "open_proposals": 0,
    }  # read, still to be answered
    # the audit names the notifications, never their text
    assert [row[:3] for row in audit_rows(hub_db, "notification")] == [
        ("notification.read", f"notifications:{first['id']}", OWNER),
        ("notification.read", "notifications:all count=1", OWNER),
    ]
    assert listed(client, hub["owner"])["notifications"][0]["read_at"] is not None

    # without a grant on the project, its notifications are not shown
    grants = f"/v1/admin/projects/{PROJECT}/grants/{OWNER}"
    assert client.delete(grants, headers=hub["admin"]).status_code in (200, 204)
    assert listed(client, hub["owner"])["total"] == 0
    assert count(client, hub["owner"]) == {"unread": 0, "open_decisions": 0, "open_proposals": 0}


def test_an_answered_decision_is_listed_by_age_and_the_project_filter_keeps_the_projects_own(client, hub):
    worker, run = started(client, hub)
    first = notice(client, worker, run["id"]).json()
    ask = {
        "category": "external_send",
        "question": "Post the staging link to the team now?",
        "options": [{"key": "yes", "label": "Post"}, {"key": "no", "label": "Wait"}],
    }
    asked = client.post(f"/v1/worker/runs/{run['id']}/decisions", json=ask, headers=worker["headers"])
    assert asked.status_code == 201, asked.text
    second = notice(client, worker, run["id"], kind="merge_default_branch", title="Merged into main").json()
    answer = f"/v1/projects/{PROJECT}/decisions/{asked.json()['id']}/answer"
    answered = client.post(answer, json={"option": "yes"}, headers=hub["owner"])
    assert answered.status_code == 200, answered.text
    page = listed(client, hub["owner"], project=PROJECT)
    # no decision is open any more: the newest first, the answered decision among the notices
    assert [(item["kind"], item["decision_state"]) for item in page["notifications"]] == [
        ("notice", None),
        ("decision", "answered"),
        ("notice", None),
    ]
    assert [page["notifications"][0]["id"], page["notifications"][2]["id"]] == [second["id"], first["id"]]
    assert page["total"] == 3
    assert listed(client, hub["owner"], project=PROJECT, kind="decision")["total"] == 1
    assert listed(client, hub["owner"], project=PROJECT, kind="notice", limit=1)["total"] == 2


def test_the_list_finds_words_of_a_title_or_body_and_keeps_one_runs_own(client, hub):
    worker, run = started(client, hub)
    pushed = notice(
        client, worker, run["id"], title="Pushed 100% of the docs", body="The catalog now lists runs."
    ).json()
    merged = notice(client, worker, run["id"], kind="merge_default_branch", title="Merged into main").json()
    assert [item["id"] for item in listed(client, hub["owner"], q="merged")["notifications"]] == [merged["id"]]
    assert [item["id"] for item in listed(client, hub["owner"], q=" CATALOG ")["notifications"]] == [pushed["id"]]
    # the words are taken as they are: % and _ match only themselves
    assert [item["id"] for item in listed(client, hub["owner"], q="100%")["notifications"]] == [pushed["id"]]
    assert listed(client, hub["owner"], q="%")["total"] == 1
    assert listed(client, hub["owner"], q="_")["total"] == 0
    assert listed(client, hub["owner"], q="   ")["total"] == 2
    assert listed(client, hub["owner"], run=run["id"])["total"] == 2
    assert listed(client, hub["owner"], run=run["id"] + 1)["total"] == 0
    assert listed(client, hub["other"], q="merged")["total"] == 0  # not theirs
    too_long = client.get("/v1/me/notifications", params={"q": "x" * 201}, headers=hub["owner"])
    assert too_long.status_code == 422


def test_a_notification_is_one_line_of_at_most_200_characters():
    assert notifications.one_line("Pushed\n\tmain   of\x00 evo-agents") == "Pushed main of evo-agents"
    long = notifications.one_line("x" * 300)
    assert len(long) == 200 and long.endswith("...")


# Notices that only report (plan hub-attention-runtime, criterion a4)


def _store(client, **fields) -> int:
    """A notification stored through ``notifications.notify``, as the hub's own senders store theirs; its id."""

    async def store():
        async with client.app.state.engine.begin() as conn:
            return await notifications.notify(conn, **fields)

    return client.portal.call(store)


def _project_id(db) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


def _reports(client, hub, hub_db, worker, run) -> dict[str, int]:
    """One notice of each kind that only reports, to the owner of ``run``: their ids by kind."""
    owner, project = user_id(hub_db, OWNER), _project_id(hub_db)
    common = {"user_id": owner, "kind": "notice", "project_id": project, "run_id": run["id"]}
    return {
        "push_default_branch": notice(client, worker, run["id"]).json()["id"],
        "merge_default_branch": notice(client, worker, run["id"], kind="merge_default_branch", title="Merged").json()[
            "id"
        ],
        "plan_finished": _store(client, notice_kind="plan_finished", title=f"Plan {PLAN} finished", **common),
        "run_outputs": _store(client, notice_kind="run_outputs", title="2 files from run", **common),
    }


def test_report_notices_are_read_reach_no_channel_and_are_still_listed(client, hub, hub_db, fake):
    add_channel(hub_db, OWNER, "fake", config={"chat_id": 42})
    worker, run = started(client, hub)
    ids = _reports(client, hub, hub_db, worker, run)
    failed = alert(client, worker, run["id"]).json()
    owner, project = user_id(hub_db, OWNER), _project_id(hub_db)
    asking = _store(
        client,
        user_id=owner,
        kind="notice",
        notice_kind="merge_default_branch",
        project_id=project,
        title="Was the merge of !4 fine?",
        details={"ask": "sample", "sample_id": 1},
    )
    # what only reports is not counted unread: the failed run and the merge that asks are
    assert count(client, hub["owner"]) == {"unread": 2, "open_decisions": 0, "open_proposals": 0}
    page = listed(client, hub["owner"])
    shown = {item["id"]: item for item in page["notifications"]}
    assert page["total"] == 6 and set(ids.values()) | {failed["id"], asking} == set(shown)
    for kind, notification_id in ids.items():
        item = shown[notification_id]
        assert (item["notice_kind"], item["report"]) == (kind, True)
        assert item["read_at"] is not None, kind
    assert (shown[failed["id"]]["report"], shown[failed["id"]]["read_at"]) == (False, None)
    assert (shown[asking]["report"], shown[asking]["read_at"]) == (False, None)
    assert {item["id"] for item in listed(client, hub["owner"], unread=True)["notifications"]} == {failed["id"], asking}

    # a report has its web delivery alone: no channel and no device gets it
    of = DELIVERIES.c.notification_id
    for notification_id in ids.values():
        assert sql(hub_db, select(DELIVERIES.c.channel_id, DELIVERIES.c.device_id).where(of == notification_id)) == [
            (None, None)
        ]
    assert deliver(client) == {"delivered": 8, "retried": 0, "failed": 0}  # 4 reports and 2 others, on the web and fake
    assert sorted(outgoing.id for outgoing, _ in fake.sent) == sorted([failed["id"], asking])


def test_report_notices_count_in_today_one_item_a_project_and_kind(client, hub, hub_db):
    worker, run = started(client, hub)
    _reports(client, hub, hub_db, worker, run)
    notice(client, worker, run["id"], title="Pushed main again")
    response = client.get("/v1/me/today", headers=hub["owner"])
    assert response.status_code == 200, response.text
    today = response.json()
    reports = [item for item in today["items"] if item["kind"] == "report"]
    assert len(reports) == 4 and {item["project"] for item in reports} == {PROJECT}
    by_title = {item["title"]: item for item in reports}
    pushes = by_title[f"2 pushes to a default branch in {PROJECT}"]
    assert (pushes["body"], pushes["link"]) == ("The newest: Pushed main again", f"/inbox?kind=notice&project={PROJECT}")
    assert {"Merged", f"Plan {PLAN} finished", "2 files from run"} <= set(by_title)
    assert by_title["Merged"]["link"] == f"/p/{PROJECT}/runs/{run['id']}"
    assert "4 reports of your work" in today["summary"], today["summary"]
    assert listed(client, hub["other"])["total"] == 0
    other = client.get("/v1/me/today", headers=hub["other"]).json()
    assert [item for item in other["items"] if item["kind"] == "report"] == []
