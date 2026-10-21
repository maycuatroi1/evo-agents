"""The job ``daily`` of the core plugin pm (``evo_agents.core_plugins.pm.job``), as criteria a6 and a8 of plan
pm-daily-tracking name it: on a working day it sends the digest to the PM and posts it in the digest chat, and
escalates each blocker that has lasted too long to the PM and to whom it waits on, with counts, codes and a link and
no title; each thing goes once a day however often the job runs; a notice counts what its recipient may see.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select, update

from evo_agents.core_plugins.pm import DAILY_CRON, MANIFEST, job
from evo_agents.hub import plugin_jobs, runs, tables
from tests.hub.live import sql
from tests.hub.test_pm_daily_plugin import EVERY_DAY, client, pm_core  # noqa: F401 (fixtures)
from tests.hub.test_runs import PROJECT
from tests.hub.test_tasks_plugin import (  # noqa: F401 (fixtures)
    configure,
    created,
    edited,
    enable,
    hub,
    members,
    sprint,
)

TITLE = "Cancel an order before it is packed"
SECRET = "the bank wire of the supplier"
LINK = f"/p/{PROJECT}/x/pm"
MORNING = {**EVERY_DAY, "escalate_after_days": 0}


def morning(days: int = 1) -> datetime:
    """Noon UTC, ``days`` after today: what the job reads as now."""
    return datetime.combine(datetime.now(UTC).date() + timedelta(days=days), time(12), tzinfo=UTC)


def block(client, headers, ref: str, **body) -> dict:
    answer = client.post(f"/v1/x/tasks/{PROJECT}/tasks/{ref}/blockers", json=body, headers=headers)
    assert answer.status_code == 201, answer.text
    return answer.json()


def run_project(client, now: datetime | None = None) -> dict:
    app = client.app
    return client.portal.call(lambda: job.run_project(app.state.engine, PROJECT, now=now or morning()))


def notices(client, headers, kind: str) -> list[dict]:
    answer = client.get("/v1/me/notifications", headers=headers)
    assert answer.status_code == 200, answer.text
    return [item for item in answer.json()["notifications"] if item["notice_kind"] == kind]


def sends(hub_db) -> list[tuple]:
    s = tables.pm_sends
    return sql(hub_db, select(s.c.kind, s.c.key).order_by(s.c.id))


@pytest.fixture
def pm_hub(client, hub) -> dict:
    """``hub`` with pm enabled with both permissions, the owner as PM, every day a working day in UTC."""
    enable(client, hub["owner"], "pm", MANIFEST["permissions"])
    configure(client, hub["owner"], "pm", MORNING)
    return hub


def busy(client, hub) -> None:
    """One task done, one late and one blocked on bao by a hand."""
    alice = hub["alice"]
    sprint(client, alice, "Sprint 7", "active")
    done = created(client, alice, title=TITLE, status="in_progress", assignees=["alice"], sprint="Sprint 7")
    edited(client, alice, done["ref"], status="done")
    created(client, alice, title="Late one", status="in_progress", due_on="2020-01-01", assignees=["alice"])
    waiting = created(client, alice, title="Waits on bao", status="in_progress", assignees=["alice"])
    block(client, alice, waiting["ref"], reason=SECRET, waiting_member="bao")


def test_pm_job_the_manifest_has_a_cron_job_on_weekdays_and_owns_pm_sends():
    [item] = MANIFEST["contributes"]["jobs"]
    assert (item["name"], item["cron"], item["python"]) == (
        "daily",
        "0 1 * * 1-5",
        "evo_agents.core_plugins.pm.job:run",
    )
    assert DAILY_CRON == job.CRON == "0 1 * * 1-5"
    assert [t["name"] for t in MANIFEST["contributes"]["tables"]] == ["pm_sends"]
    assert {job.NOTICE_DAILY, job.NOTICE_ESCALATION} <= set(runs.NOTICE_KINDS)


def test_pm_job_sends_the_digest_and_the_escalation_once_with_counts_and_codes_and_no_title(client, pm_hub, hub_db):
    busy(client, pm_hub)
    done = run_project(client)
    assert done["digest"] == {"told": 1, "posted": False}
    assert (done["escalations"], done["told"]) == (1, 2)  # the PM and bao, whom the blocker waits on

    [digest] = notices(client, pm_hub["owner"], "pm_daily")
    assert digest["link"] == LINK and digest["project"] == PROJECT
    assert digest["details"]["counts"] == {
        "done_tasks": 1,
        "done_use_cases": 0,
        "late": 1,
        "review": 0,
        "stale": 0,
        "blocked": 1,
        "escalate": 1,
    }
    assert digest["details"]["tasks"] == {"late": ["T-2"], "escalate": ["T-3"]}
    [owner_escalation] = notices(client, pm_hub["owner"], "pm_escalation")
    [bao_escalation] = notices(client, pm_hub["bao"], "pm_escalation")
    assert owner_escalation["details"]["task"] == bao_escalation["details"]["task"] == "T-3"
    assert owner_escalation["details"]["age_days"] == 1 and owner_escalation["link"] == LINK
    assert notices(client, pm_hub["bao"], "pm_daily") == []
    assert notices(client, pm_hub["alice"], "pm_escalation") == []  # she set it: she is not waited on
    for found in (digest, owner_escalation, bao_escalation):
        assert (
            TITLE not in json.dumps(found) and SECRET not in json.dumps(found) and "Late one" not in json.dumps(found)
        )

    assert sends(hub_db) == [("digest", ""), ("escalation", sends(hub_db)[1][1])]
    assert sends(hub_db)[1][1].startswith("blocker:")
    again = run_project(client)  # the job runs again, or twice: nothing more is sent
    assert again == {"date": again["date"], "escalations": 0, "told": 0}
    assert len(notices(client, pm_hub["owner"], "pm_daily")) == 1
    assert len(notices(client, pm_hub["bao"], "pm_escalation")) == 1
    assert len(sends(hub_db)) == 2
    next_day = run_project(client, morning(2))  # the next day is a new digest, and the blocker escalates again
    assert next_day["digest"]["told"] == 1 and next_day["escalations"] == 1


def test_pm_job_runs_as_the_hubs_job_worker_runs_it(client, pm_hub):
    busy(client, pm_hub)
    app = client.app

    async def work():
        return await plugin_jobs.run_job(app.state.engine, app.state.config, "pm", job.JOB)

    found = client.portal.call(work)
    assert found["projects"] == [PROJECT] and set(found["result"]["projects"]) == {PROJECT}
    assert "failed" not in found["result"]["projects"][PROJECT]


def test_pm_job_does_nothing_on_a_day_off_or_for_a_project_whose_config_cannot_be_used(client, pm_hub, hub_db):
    busy(client, pm_hub)
    configure(client, pm_hub["owner"], "pm", {"working_days": [1, 2, 3, 4, 5]})
    saturday = morning()
    while saturday.isoweekday() != 6:
        saturday += timedelta(days=1)
    assert run_project(client, saturday)["skipped"] == "not a working day"
    monday = saturday + timedelta(days=2)
    assert "skipped" not in run_project(client, monday) and len(sends(hub_db)) == 2
    configure(client, pm_hub["owner"], "pm", {"timezone": "Not/AZone"})
    assert "no IANA time zone" in run_project(client, morning(5))["skipped"]


def test_pm_job_the_admins_are_the_pm_when_the_config_names_none(client, pm_hub):
    busy(client, pm_hub)
    configure(client, pm_hub["owner"], "pm", {"pm": ""})
    run_project(client)
    assert len(notices(client, pm_hub["owner"], "pm_daily")) == 1
    assert notices(client, pm_hub["alice"], "pm_daily") == [] and notices(client, pm_hub["eve"], "pm_daily") == []


@pytest.mark.parametrize("granted", [[], ["chats:write"]])
def test_pm_job_sends_no_notice_where_the_project_did_not_grant_notifications_send(client, hub, hub_db, granted):
    enable(client, hub["owner"], "pm", MANIFEST["permissions"])
    sql(hub_db, update(tables.project_plugins).values(permissions=granted))  # the project took back what it granted
    configure(client, hub["owner"], "pm", MORNING)
    busy(client, hub)
    done = run_project(client)
    assert "told" not in done and done.get("digest", {}).get("told", 0) == 0
    assert notices(client, hub["owner"], "pm_daily") == [] and notices(client, hub["bao"], "pm_escalation") == []
    assert [kind for kind, _ in sends(hub_db)] == (["digest"] if granted else [])


def test_pm_job_a_notice_counts_what_its_recipient_may_see_and_a_task_they_cannot_see_is_not_escalated_to_them(
    client, pm_hub, hub_db
):
    alice = pm_hub["alice"]
    task = created(client, alice, title=TITLE, status="in_progress", assignees=["alice"], due_on="2020-01-01")
    block(client, alice, task["ref"], reason=SECRET, waiting_member="outsider")  # a reader up to public
    configure(client, pm_hub["owner"], "pm", {"pm": "outsider"})
    run_project(client)
    [digest] = notices(client, pm_hub["outsider"], "pm_daily")
    assert set(digest["details"]["counts"].values()) == {0} and digest["details"]["tasks"] == {}
    assert notices(client, pm_hub["outsider"], "pm_escalation") == []
    assert notices(client, pm_hub["owner"], "pm_escalation") == []  # not the PM now, and not waited on
    assert [kind for kind, _ in sends(hub_db)] == ["digest", "escalation"]  # sent once, to no one who may read it


def chat_with_agent(hub_db, title: str = "Daily") -> int:
    owner = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == "owner"))[0][0]
    project = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    profile = {
        "project_id": project,
        "handle": "pm-bot",
        "name": "PM bot",
        "color": "#336699",
        "permissions": {},
        "created_by": owner,
    }
    profile_id = sql(hub_db, insert(tables.agent_profiles).values(profile).returning(tables.agent_profiles.c.id))[0][0]
    chat_id = sql(hub_db, insert(tables.chats).values(title=title, created_by=owner).returning(tables.chats.c.id))[0][0]
    sql(hub_db, insert(tables.chat_members).values(chat_id=chat_id, profile_id=profile_id, added_by=owner))
    return chat_id


def posted(hub_db, chat_id: int) -> list[str]:
    m = tables.chat_messages
    rows = sql(hub_db, select(m.c.body, m.c.author_user_id, m.c.author_profile_id).where(m.c.chat_id == chat_id))
    assert all(user is None and profile is None for _, user, profile in rows)  # a message of the hub
    return [body for body, _, _ in rows]


def test_pm_job_posts_the_digest_in_the_chat_as_the_hub_once_and_skips_a_chat_that_is_gone(client, pm_hub, hub_db):
    busy(client, pm_hub)
    chat_id = chat_with_agent(hub_db)
    configure(client, pm_hub["owner"], "pm", {"digest_chat": chat_id})
    assert run_project(client)["digest"] == {"told": 1, "posted": True}
    [text] = posted(hub_db, chat_id)
    assert text.startswith(f"Daily digest of {PROJECT} for ") and "done 1 tasks" in text and LINK in text
    assert "late: T-2" in text and "escalate: T-3" in text
    assert TITLE not in text and SECRET not in text and "Late one" not in text
    run_project(client)
    assert len(posted(hub_db, chat_id)) == 1
    configure(client, pm_hub["owner"], "pm", {"digest_chat": chat_id + 1000})
    assert run_project(client, morning(2))["digest"] == {"told": 1, "posted": False}
    assert len(posted(hub_db, chat_id)) == 1


def test_pm_job_the_blocker_of_a_decision_escalates_to_the_owner_of_the_run(client, pm_hub, hub_db):
    from tests.hub.test_runs import dispatched
    from tests.hub.test_tasks_blockers import ask

    [run] = dispatched(client, pm_hub["owner"], [2])
    created(client, pm_hub["alice"], title=TITLE, status="in_progress", links={"runs": [run["id"]]})
    decision = ask(hub_db, run["id"])
    done = run_project(client, morning(3))
    assert done["escalations"] == 1
    assert ("escalation", f"decision:{decision}") in sends(hub_db)
    [found] = notices(client, pm_hub["owner"], "pm_escalation")
    assert found["details"]["kind"] == "decision" and found["details"]["task"] == "T-1"
