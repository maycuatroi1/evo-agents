"""A run's model chosen by its difficulty on the hub (plan super-board-parity-2, criterion a6): the project's policy
section ``routing``, the claim that carries it to the worker, and the state reports that record what the routing did
and the model the runtime used. The worker's side is in ``tests/worker/test_model_router.py``."""

from __future__ import annotations

import pytest

from evo_agents.hub import autonomy
from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from tests.hub.test_runs import (  # noqa: E402  the fixtures of the run queue
    OWNER,
    PROJECT,
    add_worker,
    claim,
    client,  # noqa: F401
    control,
    dispatched,
    hub,  # noqa: F401
    moved,
    of_run,
)

ON = {"routing": {"enabled": True, "ladder": {"hard": "claude-opus-5-5"}}}
LADDER = {"easy": "haiku", "medium": "sonnet", "hard": "claude-opus-5-5"}
ROUTED = {"scorer": "haiku", "difficulty": "hard", "scored": True, "model": "claude-opus-5-5", "cost_usd": 0.0012,
          "error": None}  # fmt: skip


def put_policy(client, hub, policy: dict) -> None:
    """Owner, made an admin of the project, writes ``policy`` as its next revision."""
    grant = {"role": "admin", "max_level": "internal"}
    response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=grant, headers=hub["admin"])
    assert response.status_code == 200, response.text
    response = client.put(f"/v1/projects/{PROJECT}/policy", json={"policy": policy}, headers=hub["owner"])
    assert response.status_code == 200, response.text


def test_model_router_policy_section_is_off_by_default_and_checked():
    made = autonomy.effective(None, {})["routing"]
    assert made == {
        "enabled": False,
        "scorer": "haiku",
        "ladder": {"easy": "haiku", "medium": "sonnet", "hard": "opus"},
    }
    assert autonomy.problems(ON, {}) == []
    wrong = autonomy.problems({"routing": {"enabled": "yes", "ladder": {"hard": "", "giant": "x"}}}, {})
    assert [problem.split(":")[0] for problem in wrong] == [
        "routing.enabled",
        "routing.ladder.hard",
        "routing.ladder.giant",
    ]


def test_model_router_claim_carries_the_ladder_only_while_routing_is_on(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    off = dispatched(client, hub["owner"], [2])[0]
    assert claim(client, worker)["routing"] is None, "routing is off by default"
    moved(client, worker, off["id"], "cancelled")

    put_policy(client, hub, ON)
    routed = dispatched(client, hub["owner"], [2])[0]
    spec = claim(client, worker)
    assert (spec["id"], spec["model"], spec["routing"]) == (routed["id"], None, {"scorer": "haiku", "ladder": LADDER})
    moved(client, worker, routed["id"], "cancelled")

    # a run the dispatch pinned to a model is not rated
    pinned = dispatched(client, hub["owner"], [2], runtime="claude-code", model="claude-opus-4-1")[0]
    spec = claim(client, worker)
    assert (spec["id"], spec["model"], spec["routing"]) == (pinned["id"], "claude-opus-4-1", None)
    moved(client, worker, pinned["id"], "cancelled")


def test_model_router_reports_record_the_difficulty_its_cost_and_the_model_used(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    put_policy(client, hub, ON)
    run = dispatched(client, hub["owner"], [2])[0]
    claim(client, worker)
    moved(client, worker, run["id"], "running", model_used="claude-opus-5-5", routing=ROUTED)
    assert of_run(hub_db, run["id"], "model_used", "routing") == [("claude-opus-5-5", ROUTED)]
    shown = client.get(f"/v1/projects/{PROJECT}/runs/{run['id']}", headers=hub["reader"]).json()
    assert (shown["model_used"], shown["routing"]) == ("claude-opus-5-5", ROUTED)
    assert shown["model"] is None, "the run keeps what the dispatch asked; the routing's choice is in routing"

    # a report without them keeps what an earlier one said; the final one may name the model again
    moved(client, worker, run["id"], "failed", error="it stopped", model_used="claude-opus-5-5-20261001")
    assert of_run(hub_db, run["id"], "model_used", "routing") == [("claude-opus-5-5-20261001", ROUTED)]


def test_model_router_refuses_a_routing_record_that_does_not_read(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    run = dispatched(client, hub["owner"], [2])[0]
    claim(client, worker)
    bad = {**ROUTED, "difficulty": "trivial"}
    response = client.post(f"/v1/worker/runs/{run['id']}/state", json={"state": "running", "routing": bad},
                           headers=worker["headers"])  # fmt: skip
    assert response.status_code == 422
    assert control(client, hub["owner"], run["id"], "cancel").status_code == 200
