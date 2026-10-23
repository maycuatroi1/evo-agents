"""What the worker tells the hub of a run's autonomy (``evo_agents.hub.server.worker_autonomy``), as criteria a8 and a9
of plan autonomy-levels name it: ``evo-agents worker backup`` records a backup as a ``system`` event of the run and the
audit row ``autonomy.backup``, a time in the future refused; the hook of autonomy asks the hub about a command of a
class in notify, which tells the principal (the notice ``autonomy``, audited ``autonomy.notified``) and answers the mode
now, and about one in ask, which it lets through only after a decision of the run, of the class's category, answered.
Both take the token of the worker that claimed the run alone. The card the claim gives names what set each class's
mode (``sources``). The projects and people are fictional."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_autonomy_level_lease import autonomy_worker, pref
from tests.hub.test_credentials_api import (  # noqa: F401 (app_key, client, config and hub are fixtures)
    OWNER,
    PLAN,
    PROJECT,
    app_key,
    audit_rows,
    client,
    config,
    hub,
    worker_of,
)
from tests.hub.test_runs import claim

OPTIONS = [{"key": "drop", "label": "Drop the table"}, {"key": "keep", "label": "Keep it"}]


def plan_run(client, headers, worker: dict) -> tuple[int, dict]:
    """A plan run pinned to ``worker`` and claimed by it, and its claim."""
    response = client.post(
        f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN, "worker_id": worker["id"]}, headers=headers
    )
    assert response.status_code == 201, response.text
    run = claim(client, worker)
    assert run["id"] == response.json()["id"]
    return run["id"], run


def events(db, run_id: int) -> list[dict]:
    e = tables.run_events
    return [body for (body,) in sql(db, select(e.c.body).where(e.c.run_id == run_id, e.c.kind == "system"))]


def notices(client, headers) -> list[dict]:
    response = client.get("/v1/me/notifications", headers=headers)
    assert response.status_code == 200, response.text
    return [item for item in response.json()["notifications"] if item["notice_kind"] == "autonomy"]


def test_autonomy_level_backup_is_a_run_event_and_an_audit_row(client, hub, hub_db):
    run_id, run = plan_run(client, hub["owner"], worker := autonomy_worker(client, hub["owner"]))
    assert run["autonomy"]["sources"]["K9"], "the card names what set each class's mode"
    made = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)
    body = {
        "target": "db app",
        "location": "s3://backups/app.dump",
        "made_at": made.isoformat(),
        "restore": "pg_restore",
    }
    response = client.post(f"/v1/worker/runs/{run_id}/backups", json=body, headers=worker["headers"])
    assert response.status_code == 201, response.text
    assert response.json()["fresh"] is True
    (event,) = [item for item in events(hub_db, run_id) if "backup" in item]
    assert event["backup"] == {**body, "made_at": made.isoformat()}
    assert event["text"].startswith("Backup of db app at s3://backups/app.dump")
    ((target, login, project, by_token),) = audit_rows(hub_db, "autonomy.backup")
    assert target == f"run:{run_id} db app at=s3://backups/app.dump made={made.isoformat()}"
    assert (login, project, by_token) == (OWNER, PROJECT, True)

    old = {"target": "db", "location": "/b", "made_at": (made - timedelta(days=2)).isoformat()}
    assert (
        client.post(f"/v1/worker/runs/{run_id}/backups", json=old, headers=worker["headers"]).json()["fresh"] is False
    )
    future = {"target": "db", "location": "/b", "made_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    refused = client.post(f"/v1/worker/runs/{run_id}/backups", json=future, headers=worker["headers"])
    assert refused.status_code == 422 and "in the future" in refused.text
    other = worker_of(client, hub["owner"], "other-mac")
    assert client.post(f"/v1/worker/runs/{run_id}/backups", json=body, headers=other["headers"]).status_code == 404


def test_autonomy_level_hook_act_in_notify_tells_the_principal(client, hub, hub_db):
    pref(client, hub["owner"], K10="notify")
    run_id, run = plan_run(client, hub["owner"], worker := autonomy_worker(client, hub["owner"]))
    assert run["autonomy"]["modes"]["K10"] == "notify"
    act = {"klass": "K10", "what": "gh issue create --repo other/lib"}
    response = client.post(f"/v1/worker/runs/{run_id}/autonomy-acts", json=act, headers=worker["headers"])
    assert response.status_code == 200, response.text
    assert response.json() == {"mode": "notify", "told": True, "answered": False}
    (notice,) = notices(client, hub["owner"])
    assert "K10" in notice["title"] and "gh issue create --repo other/lib" in notice["title"]
    ((target, login, _, _),) = audit_rows(hub_db, "autonomy.notified")
    assert target.startswith(f"run:{run_id} K10 hook ") and login == OWNER
    auto = client.post(
        f"/v1/worker/runs/{run_id}/autonomy-acts", json={**act, "klass": "K5"}, headers=worker["headers"]
    )
    assert auto.json() == {"mode": "auto", "told": False, "answered": False}
    assert len(notices(client, hub["owner"])) == 1


def test_autonomy_level_hook_act_in_ask_needs_an_answered_decision_of_the_class(client, hub, hub_db):
    pref(client, hub["owner"], K9="ask")
    run_id, _ = plan_run(client, hub["owner"], worker := autonomy_worker(client, hub["owner"]))
    asked = {"category": "delete_data", "question": "Drop the table users?", "options": OPTIONS}
    decision = client.post(f"/v1/worker/runs/{run_id}/decisions", json=asked, headers=worker["headers"])
    assert decision.status_code == 201, decision.text
    decision_id = decision.json()["id"]
    act = {"klass": "K9", "what": "psql -c 'DROP TABLE users'", "decision_id": decision_id}
    before = client.post(f"/v1/worker/runs/{run_id}/autonomy-acts", json=act, headers=worker["headers"])
    assert before.json() == {"mode": "ask", "told": False, "answered": False}
    path = f"/v1/projects/{PROJECT}/decisions/{decision_id}/answer"
    assert client.post(path, json={"option": "drop"}, headers=hub["owner"]).status_code == 200
    after = client.post(f"/v1/worker/runs/{run_id}/autonomy-acts", json=act, headers=worker["headers"])
    assert after.json() == {"mode": "ask", "told": False, "answered": True}
    other_class = client.post(
        f"/v1/worker/runs/{run_id}/autonomy-acts", json={**act, "klass": "K6"}, headers=worker["headers"]
    )
    assert other_class.json()["answered"] is False, "a decision of category delete_data says nothing of K6"
