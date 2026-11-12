"""Cron jobs of a project (``docs/hub.md``, Cron jobs; plan hub-cron-jobs, criterion a1): ``evo_agents.hub.cron``, the
routes of ``evo_agents.hub.server.cron_jobs`` and ``evo-agents hub cron``.

Without a database: the helper validates five fields in an IANA zone, refuses aliases, crons that never fire and
crons finer than the limit of their kind, and finds the next fire in the job's zone; ``hub cron set`` merges the flags
over the job it read. With a test database (``EVO_HUB_TEST_DSN``): a writer creates and changes a job by revision, with
409 ``revision_conflict``; validate refusals are 422; the roles: a reader reads, a writer manages only their own, an
admin pauses, runs and deletes another's but does not change its work; run now records a fire at the press.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("sqlalchemy", reason="the hub tables need the hub-server extra, evo-ak[hub-server]")
pytest.importorskip("croniter", reason="the cron helper needs the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import cli_cron, cron
from tests.hub import pg

# The helper


@pytest.mark.parametrize(
    "expr",
    ["* * * *", "0 9 * * * *", "@daily", "H 9 * * *", "61 * * * *", "0 25 * * *", "a b c d e", "0 0 30 2 *"],
)
def test_validate_refuses_a_cron_that_does_not_read_or_never_fires(expr):
    with pytest.raises(cron.CronRefused):
        cron.check(expr, "UTC", "command")


def test_validate_refuses_an_unknown_time_zone():
    with pytest.raises(cron.CronRefused, match="Mars/Olympus"):
        cron.check("0 9 * * *", "Mars/Olympus", "agent")
    with pytest.raises(cron.CronRefused):
        cron.zone("../etc/passwd")


@pytest.mark.parametrize(
    ("expr", "kind", "taken"),
    [
        ("*/15 * * * *", "agent", True),
        ("*/10 * * * *", "agent", False),
        ("0,10 9 * * *", "plan", False),  # 10 minutes apart once a day is still too close
        ("*/10 * * * *", "command", True),
        ("*/5 * * * *", "command", True),
        ("*/4 * * * *", "command", False),
        ("* * * * *", "command", False),
        ("0 9 * * 1-5", "agent", True),
    ],
)
def test_validate_holds_each_kind_to_its_shortest_gap(expr, kind, taken):
    if taken:
        assert cron.check(expr, "UTC", kind) == expr
    else:
        with pytest.raises(cron.CronRefused, match=f"every {cron.MIN_MINUTES[kind]} minutes"):
            cron.check(expr, "UTC", kind)


def test_the_next_fire_is_read_in_the_jobs_time_zone():
    after = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)
    assert cron.next_fire("0 9 * * *", "Asia/Ho_Chi_Minh", after) == datetime(2026, 11, 12, 2, 0, tzinfo=UTC)
    assert cron.next_fire("0 9 * * *", "UTC", after) == datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


# The CLI


class FakeHub:
    def __init__(self, held: dict | None):
        self.held, self.calls = held, []

    def call(self, method, path, body=None):
        from evo_agents.hub.client import HubError

        self.calls.append((method, path, body))
        if method == "GET":
            if self.held is None:
                raise HubError("no such job", status=404)
            return self.held
        return {**(body or {}), "revision": 2, "created": False, "changed": True, "paused": False}


def test_set_keeps_what_it_is_not_given_and_sends_the_revision_it_read(monkeypatch):
    from evo_agents.cli import main

    held = {"kind": "agent", "cron": "0 9 * * *", "timezone": "Europe/Paris", "revision": 4}
    held |= {"agent": "lumen-pm", "chat_id": None, "prompt": "Standup"}
    hub = FakeHub(held)
    monkeypatch.setattr(cli_cron, "signed_in", lambda: (hub, None))
    assert main(["hub", "cron", "set", "standup", "--project", "northwind", "--cron", "0 10 * * *"]) == 0
    method, path, body = hub.calls[-1]
    assert (method, path) == ("PUT", "/v1/projects/northwind/cron-jobs/standup")
    assert body["cron"] == "0 10 * * *" and body["timezone"] == "Europe/Paris"
    assert (body["agent"], body["prompt"], body["if_revision"]) == ("lumen-pm", "Standup", 4)


def test_set_refuses_flags_of_two_kinds(monkeypatch):
    from evo_agents.cli import main

    hub = FakeHub(None)
    monkeypatch.setattr(cli_cron, "signed_in", lambda: (hub, None))
    argv = [
        "hub",
        "cron",
        "set",
        "x",
        "--project",
        "northwind",
        "--cron",
        "0 9 * * *",
        "--plan",
        "p",
        "--command",
        "ls",
    ]
    assert main(argv) == 2
    assert [c[0] for c in hub.calls] == ["GET"]


# The API

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
AGENT = "lumen-pm"


@pytest.fixture
def client(hub_db, tmp_path, github, s3):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app
    from tests.hub import live

    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as found:
        yield found


@pytest.fixture
def hub(client, github):
    """The members of ``tests.hub.test_runs`` (owner and other writers, reader a reader) with ``admin`` an admin of
    the project (their stranger, granted it), and agent AGENT, made by that admin."""
    from tests.hub.test_runs import PROJECT, STRANGER, members

    headers = members(client, github)
    grant = {"role": "admin", "max_level": "internal"}
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{STRANGER}", json=grant, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    admin = headers["stranger"]
    made = client.post(f"/v1/projects/{PROJECT}/agents", json={"handle": AGENT, "name": AGENT}, headers=admin)
    assert made.status_code in (200, 201), made.text
    return {**headers, "admin": admin, "project": PROJECT}


def path(hub, name: str | None = None) -> str:
    base = f"/v1/projects/{hub['project']}/cron-jobs"
    return base if name is None else f"{base}/{name}"


def agent_job() -> dict:
    return {"cron": "0 9 * * 1-5", "timezone": "Asia/Ho_Chi_Minh", "kind": "agent", "agent": AGENT, "prompt": "Standup"}


@needs_pg
def test_a_writer_creates_and_changes_a_job_by_revision(client, hub, hub_db):
    from tests.hub.test_runs import audit_rows

    made = client.put(path(hub, "standup"), json=agent_job(), headers=hub["owner"])
    assert made.status_code == 200, made.text
    job = made.json()
    assert (job["revision"], job["created"], job["owner"], job["agent"]) == (1, True, "owner", AGENT)
    assert job["next_fire_at"] is not None and job["cost_24h_usd"] == 0
    again = client.put(path(hub, "standup"), json=agent_job(), headers=hub["owner"])
    assert again.status_code == 409 and again.json()["error"] == "revision_conflict"
    same = client.put(path(hub, "standup"), json={**agent_job(), "if_revision": 1}, headers=hub["owner"])
    assert same.status_code == 200 and same.json()["changed"] is False
    changed = client.put(
        path(hub, "standup"), json={**agent_job(), "cron": "0 10 * * *", "if_revision": 1}, headers=hub["owner"]
    )
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    stale = client.put(
        path(hub, "standup"), json={**agent_job(), "cron": "0 11 * * *", "if_revision": 1}, headers=hub["owner"]
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == "revision_conflict" and stale.json()["current"]["revision"] == 2
    assert [row[0] for row in audit_rows(hub_db, "cron")] == ["cron.create", "cron.update"]


@needs_pg
@pytest.mark.parametrize(
    "body",
    [
        {"cron": "0 9 * *"},
        {"cron": "@daily"},
        {"timezone": "Mars/Olympus"},
        {"cron": "*/10 * * * *"},  # an agent job every 10 minutes
        {"agent": "lumen-nobody"},
        {"plan": "rollout"},  # a field of another kind
        {"kind": "command", "agent": None, "prompt": None, "command": "make test", "cron": "*/2 * * * *"},
        {"kind": "command", "agent": None, "prompt": None, "command": "make test", "repo": "nowhere"},
        {"kind": "plan", "agent": None, "prompt": None, "plan": "no-such-plan"},
    ],
)
def test_validate_refusals_are_422_and_write_nothing(client, hub, body):
    refused = client.put(path(hub, "standup"), json={**agent_job(), **body}, headers=hub["owner"])
    assert refused.status_code == 422, refused.text
    assert client.get(path(hub), headers=hub["owner"]).json() == []


@needs_pg
def test_validate_takes_a_command_job_every_five_minutes_and_a_plan_job(client, hub):
    from tests.hub.test_plans import REPOS

    command = {"cron": "*/5 * * * *", "kind": "command", "command": "make test", "repo": REPOS[0]}
    made = client.put(path(hub, "tests"), json=command, headers=hub["owner"])
    assert made.status_code == 200, made.text
    assert (made.json()["timeout_minutes"], made.json()["repo"]) == (30, REPOS[0])
    plan = client.put(
        path(hub, "rollout"), json={"cron": "0 3 * * *", "kind": "plan", "plan": "rollout"}, headers=hub["owner"]
    )
    assert plan.status_code == 200, plan.text


@needs_pg
def test_role_a_reader_reads_and_cannot_write(client, hub):
    assert client.put(path(hub, "standup"), json=agent_job(), headers=hub["owner"]).status_code == 200
    assert client.put(path(hub, "mine"), json=agent_job(), headers=hub["reader"]).status_code == 403
    listed = client.get(path(hub), headers=hub["reader"])
    assert listed.status_code == 200 and [j["name"] for j in listed.json()] == ["standup"]
    assert listed.json()[0]["can_manage"] is False
    for action in ("pause", "resume", "run"):
        assert client.post(f"{path(hub, 'standup')}/{action}", json={}, headers=hub["reader"]).status_code == 403
    assert client.delete(path(hub, "standup"), headers=hub["reader"]).status_code == 403


@needs_pg
def test_role_a_writer_manages_only_their_own_job(client, hub):
    assert client.put(path(hub, "standup"), json=agent_job(), headers=hub["owner"]).status_code == 200
    body = {**agent_job(), "prompt": "Mine now", "if_revision": 1}
    assert client.put(path(hub, "standup"), json=body, headers=hub["other"]).status_code == 403
    assert client.post(f"{path(hub, 'standup')}/pause", json={}, headers=hub["other"]).status_code == 403
    assert client.delete(path(hub, "standup"), headers=hub["other"]).status_code == 403
    paused = client.post(f"{path(hub, 'standup')}/pause", json={"reason": "holiday"}, headers=hub["owner"])
    assert paused.status_code == 200
    assert (paused.json()["paused"], paused.json()["paused_by"], paused.json()["next_fire_at"]) == (True, "owner", None)
    assert paused.json()["revision"] == 1  # a pause changes no work


@needs_pg
def test_role_an_admin_pauses_runs_and_deletes_another_persons_job_but_does_not_change_its_work(client, hub):
    assert client.put(path(hub, "standup"), json=agent_job(), headers=hub["owner"]).status_code == 200
    body = {**agent_job(), "prompt": "Changed by the admin", "if_revision": 1}
    assert client.put(path(hub, "standup"), json=body, headers=hub["admin"]).status_code == 403
    paused = client.post(f"{path(hub, 'standup')}/pause", json={"reason": "budget"}, headers=hub["admin"])
    assert paused.status_code == 200 and paused.json()["paused_reason"] == "budget"
    assert client.post(f"{path(hub, 'standup')}/resume", json={}, headers=hub["admin"]).json()["paused"] is False
    before = datetime.now(UTC)
    fired = client.post(f"{path(hub, 'standup')}/run", json={}, headers=hub["admin"])
    assert fired.status_code == 200, fired.text
    fire = fired.json()
    assert fire["manual"] is True and fire["outcome"] is None
    assert datetime.fromisoformat(fire["scheduled_for"]) >= before
    fires = client.get(f"{path(hub, 'standup')}/fires", headers=hub["reader"]).json()
    assert [f["id"] for f in fires] == [fire["id"]]
    shown = client.get(path(hub, "standup"), headers=hub["owner"]).json()
    assert shown["last_fire"]["id"] == fire["id"] and shown["prompt"] == "Standup"
    deleted = client.delete(path(hub, "standup"), headers=hub["admin"])
    assert deleted.status_code == 200 and deleted.json()["deleted"] == "standup"
    assert client.get(path(hub, "standup"), headers=hub["owner"]).status_code == 404
