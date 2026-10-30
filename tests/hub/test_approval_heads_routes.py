"""A merge approval bound to a head and a scope at the hub (plan super-board-parity-2, criterion a3): a decision of
category merge an agent asks with its pull request keeps the head and scope it was asked at, the hook's route opens the
gate only at them, and the gate of autonomy asks again once the head of a merge it holds moved. A decision without one
holds as before. The world is ``tests.hub.test_credentials_api``'s; the projects and people are fictional."""

from __future__ import annotations

from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update

from evo_agents.hub import approval_heads, runs, tables
from evo_agents.hub.server import autonomy_gates
from tests.hub.live import sql
from tests.hub.test_autonomy_level_backup import plan_run
from tests.hub.test_autonomy_level_lease import autonomy_worker, pref
from tests.hub.test_credentials_api import (  # noqa: F401 (app_key, client, config and hub are fixtures)
    PROJECT,
    app_key,
    client,
    config,
    hub,
)

HEAD, NEW_HEAD = "a" * 40, "b" * 40
SCOPE = approval_heads.scope_of(["app/models.py", "app/migrations/0004_add_index.py"], "needs-you: run the backfill")
OPTIONS = [{"key": "merge", "label": "Merge it"}, {"key": "wait", "label": "Not yet"}]
KEY = "merge acme/app#12"


def answer(client, headers, decision_id: int, option: str) -> None:
    path = f"/v1/projects/{PROJECT}/decisions/{decision_id}/answer"
    response = client.post(path, json={"option": option}, headers=headers)
    assert response.status_code == 200, response.text


def act(client, worker, run_id: int, decision_id: int, **given) -> dict:
    body = {"klass": "K5", "what": "gh pr merge 12", "decision_id": decision_id, **given}
    response = client.post(f"/v1/worker/runs/{run_id}/autonomy-acts", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def asking(client, hub):
    """A plan run whose principal holds K5 at ask, claimed by its worker."""
    pref(client, hub["owner"], K5="ask")
    worker = autonomy_worker(client, hub["owner"])
    run_id, _ = plan_run(client, hub["owner"], worker)
    return run_id, worker


def test_approval_head_a_merge_decision_keeps_its_head_and_the_hook_opens_the_gate_there_alone(client, hub, asking):
    run_id, worker = asking
    bound = {"pull": "acme/app#12", "head": HEAD, "scope": SCOPE}
    asked = {"category": "merge", "question": "Merge #12 of acme/app?", "options": OPTIONS, "approval": bound}
    response = client.post(f"/v1/worker/runs/{run_id}/decisions", json=asked, headers=worker["headers"])
    assert response.status_code == 201, response.text
    decision = response.json()
    assert decision["approval"] == bound
    assert "This answer holds for head aaaaaaaaaaaa of acme/app#12" in decision["context"]
    shown = client.get(f"/v1/projects/{PROJECT}/decisions/{decision['id']}", headers=hub["owner"])
    assert shown.json()["approval"] == bound

    assert act(client, worker, run_id, decision["id"], head=HEAD, scope=SCOPE)["answered"] is False, "not answered"
    answer(client, hub["owner"], decision["id"], "merge")
    assert act(client, worker, run_id, decision["id"], head=HEAD, scope=SCOPE) == {
        "mode": "ask",
        "told": False,
        "answered": True,
        "stale": None,
    }
    moved = act(client, worker, run_id, decision["id"], head=NEW_HEAD, scope=SCOPE)
    assert moved["answered"] is False
    assert moved["stale"] == f"it was given for head {HEAD[:12]}, and the head is {NEW_HEAD[:12]} now"
    rescoped = act(client, worker, run_id, decision["id"], head=HEAD, scope=approval_heads.scope_of(["app/x.py"]))
    assert rescoped["answered"] is False and "scope" in rescoped["stale"]
    unread = act(client, worker, run_id, decision["id"])
    assert unread["answered"] is False and "could not be read" in unread["stale"]


def test_approval_head_a_decision_without_one_holds_as_before(client, hub, asking):
    run_id, worker = asking
    asked = {"category": "merge", "question": "Merge #12 of acme/app?", "options": OPTIONS}
    decision = client.post(f"/v1/worker/runs/{run_id}/decisions", json=asked, headers=worker["headers"]).json()
    assert decision["approval"] is None
    answer(client, hub["owner"], decision["id"], "merge")
    assert act(client, worker, run_id, decision["id"], head=NEW_HEAD, scope=SCOPE)["answered"] is True
    assert act(client, worker, run_id, decision["id"])["answered"] is True


def test_approval_head_an_approval_binds_a_merge_decision_alone(client, hub, asking):
    run_id, worker = asking
    asked = {"category": "scope", "question": "Cut the scope?", "options": OPTIONS, "approval": {"head": HEAD}}
    response = client.post(f"/v1/worker/runs/{run_id}/decisions", json=asked, headers=worker["headers"])
    assert response.status_code == 422, response.text


async def _check(engine, run_id: int, head: str | None, scope: str | None = SCOPE):
    approval = approval_heads.approval(head, scope, "acme/app#12")
    async with engine.begin() as conn:
        return await autonomy_gates.check(
            conn, run_id, "K5", key=KEY, what="merge pull request #12 of acme/app into main", approval=approval
        )


def gate_decisions(db, run_id: int) -> list:
    d = tables.decisions
    query = select(d.c.id, d.c.state, d.c.approval).where(
        d.c.run_id == run_id, d.c.category == runs.AUTONOMY_DECISION_CATEGORY, d.c.step_key == KEY
    )
    return sql(db, query.order_by(d.c.id))


def test_approval_head_the_gate_asks_again_once_the_head_moved(client, hub, hub_db, asking):
    run_id, _ = asking
    engine = client.app.state.engine
    check = partial(client.portal.call, _check, engine, run_id)

    first = check(HEAD)
    assert first.waits and not first.act
    ((asked_id, state, kept),) = gate_decisions(hub_db, run_id)
    assert state == "open" and kept == {"pull": "acme/app#12", "head": HEAD, "scope": SCOPE}
    context = sql(hub_db, select(tables.decisions.c.context).where(tables.decisions.c.id == asked_id))[0][0]
    assert "This answer holds for head aaaaaaaaaaaa of acme/app#12" in context
    assert check(HEAD).waits and len(gate_decisions(hub_db, run_id)) == 1, "the same head asks no second time"

    moved_open = check(NEW_HEAD)
    assert moved_open.waits
    (old, new) = gate_decisions(hub_db, run_id)
    assert old[1] == "cancelled", "the question of the old head no longer waits on anyone"
    assert new[1] == "open" and new[2]["head"] == NEW_HEAD
    answer(client, hub["owner"], new[0], autonomy_gates.APPROVE)
    assert check(NEW_HEAD).act

    after_push = check(HEAD)
    assert after_push.waits and not after_push.act, "an approval of another head opens nothing"
    *_, latest = gate_decisions(hub_db, run_id)
    assert latest[1] == "open" and latest[2]["head"] == HEAD and f"decision #{latest[0]}" in after_push.reason
    answer(client, hub["owner"], latest[0], autonomy_gates.REFUSE)
    assert check(HEAD).act is False
    assert check(HEAD, approval_heads.scope_of(["app/x.py"])).waits, "a new scope asks again, a refusal too"


def test_approval_head_a_gate_decision_asked_before_holds_for_any_head(client, hub, hub_db, asking):
    run_id, _ = asking
    engine = client.app.state.engine
    check = partial(client.portal.call, _check, engine, run_id)
    assert check(HEAD).waits
    ((asked_id, *_),) = gate_decisions(hub_db, run_id)
    sql(hub_db, update(tables.decisions).values(approval=None).where(tables.decisions.c.id == asked_id))
    answer(client, hub["owner"], asked_id, autonomy_gates.APPROVE)
    assert check(NEW_HEAD).act and len(gate_decisions(hub_db, run_id)) == 1
    assert client.portal.call(_check, engine, run_id, None).act, "an act without a head acts as before"
