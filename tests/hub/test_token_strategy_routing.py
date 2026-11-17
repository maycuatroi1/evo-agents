"""The Token strategy at a run's claim (plan token-and-quota-economy, criterion a3): the hub rates the run with no
model call, escalates the next run of a step that failed, takes the overflow's gateway model by the strategy from a
pool of native and provider accounts and leases the run the matching key, keeps its decision in ``runs.routing``, and
gives a step's difficulty and model to its handoff. The rules alone are in
``tests/worker/test_model_router_escalation.py``."""

from __future__ import annotations

import pytest

from evo_agents.hub import autonomy
from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from tests.hub.test_credentials_api import leased  # noqa: E402
from tests.hub.test_gateway_runs import (  # noqa: E402  the gateway, a provider account and a daemon that runs it
    MODEL,
    client,  # noqa: F401
    gateway,  # noqa: F401
    new_daemon,
    provider_account,
)
from tests.hub.test_model_router import put_policy  # noqa: E402
from tests.hub.test_provider_accounts import hub  # noqa: E402, F401
from tests.hub.test_runs import PLAN, PROJECT, claim, dispatched, moved, of_run  # noqa: E402

LADDER = {"easy": "haiku", "medium": "sonnet", "hard": "opus"}


def routing(**extra) -> dict:
    return {"routing": {"enabled": True, "ladder": LADDER, **extra}}


def test_token_strategy_policy_is_off_by_default_and_checks_its_keys():
    assert autonomy.effective(None, {})["routing"]["enabled"] is False
    good = routing(strategy="cost-first", scorer="off", overflow={"easy": "openrouter/auto", "medium": "glm/glm-4.6"})
    assert autonomy.problems(good, {}) == []
    bad = routing(strategy="cheapest", hard_reserve_pct=101, five_hour_ceiling_pct=0, overflow={"easy": "/auto"})
    assert [problem.split(":")[0] for problem in autonomy.problems(bad, {})] == [
        "routing.strategy",
        "routing.hard_reserve_pct",
        "routing.five_hour_ceiling_pct",
        "routing.overflow.easy",
    ]


def test_token_strategy_hub_rates_the_run_and_escalates_the_next_run_of_a_failed_step(client, hub, hub_db):
    put_policy(client, hub, routing())
    worker = new_daemon(client, hub_db, hub["owner"])
    docs = dispatched(client, hub["owner"], [4], runtime="claude-code")[0]  # step 4: "the docs"
    spec = claim(client, worker)
    assert spec["id"] == docs["id"]
    assert (spec["routing"]["difficulty"], spec["routing"]["source"]) == ("easy", "rule")
    assert spec["routing"]["ladder"] == dict.fromkeys(LADDER, "haiku"), "every rung is the model the hub chose"
    [(kept,)] = of_run(hub_db, docs["id"], "routing")
    assert (kept["model"], kept["branch"], kept["strategy"], kept["scored"]) == ("haiku", "ladder", "surplus-first",
                                                                                 False)  # fmt: skip
    moved(client, worker, docs["id"], "running", "failed", error="it stopped")

    again = dispatched(client, hub["owner"], [4], runtime="claude-code")[0]
    spec = claim(client, worker)
    assert spec["id"] == again["id"]
    assert (spec["routing"]["difficulty"], spec["routing"]["escalated_from"]) == ("medium", "easy")
    assert spec["routing"]["ladder"]["easy"] == "sonnet"
    moved(client, worker, again["id"], "cancelled")


def test_token_strategy_mixed_pool_leases_a_gateway_key_or_the_native_accounts_by_the_model(client, hub, hub_db):
    provider_account(client, hub["owner"])  # the default agent's pool: a native account and a provider account
    put_policy(client, hub, routing(strategy="cost-first", overflow={"easy": MODEL, "medium": MODEL}))
    worker = new_daemon(client, hub_db, hub["owner"])

    cheap = dispatched(client, hub["owner"], [4], runtime="claude-code")[0]
    spec = claim(client, worker)
    assert (spec["id"], spec["routing"]["branch"], spec["routing"]["ladder"]["easy"]) == (
        cheap["id"],
        "overflow",
        MODEL,
    )
    assert of_run(hub_db, cheap["id"], "gateway_provider") == [("openrouter",)]
    kinds = {lease["kind"] for lease in leased(client, worker, cheap["id"])["leases"]}
    assert "gateway" in kinds
    moved(client, worker, cheap["id"], "cancelled")

    hard = dispatched(client, hub["owner"], [2], runtime="claude-code")[0]  # a prescribed step: hard, the ladder
    spec = claim(client, worker)
    assert (spec["id"], spec["routing"]["branch"], spec["routing"]["ladder"]["hard"]) == (hard["id"], "ladder", "opus")
    assert of_run(hub_db, hard["id"], "gateway_provider") == [(None,)]
    kinds = {lease["kind"] for lease in leased(client, worker, hard["id"])["leases"]}
    assert "gateway" not in kinds
    moved(client, worker, hard["id"], "cancelled")


def test_token_strategy_overflow_the_worker_cannot_run_falls_back_to_the_ladder(client, hub, hub_db):
    from tests.hub.test_runs import add_worker

    provider_account(client, hub["owner"])
    put_policy(client, hub, routing(strategy="cost-first", overflow={"easy": MODEL}))
    old = add_worker(client, hub["owner"], "old-daemon")  # a daemon from before gateway runs
    run = dispatched(client, hub["owner"], [4], runtime="claude-code")[0]
    spec = claim(client, old)
    assert (spec["id"], spec["routing"]["branch"], spec["routing"]["ladder"]["easy"]) == (run["id"], "ladder", "haiku")
    assert "through the model gateway" in spec["routing"]["reason"]
    assert of_run(hub_db, run["id"], "gateway_provider") == [(None,)]
    moved(client, old, run["id"], "cancelled")


def test_token_strategy_handoff_gives_the_steps_difficulty_and_model(client, hub):
    put_policy(client, hub, routing(strategy="cost-first", overflow={"easy": MODEL}))
    url = f"/v1/projects/{PROJECT}/plans/{PLAN}/handoff"
    easy = client.get(url, params={"step": "4"}, headers=hub["reader"]).json()
    assert (easy["difficulty"], easy["recommended_model"]) == ("easy", MODEL)
    hard = client.get(url, params={"step": "2"}, headers=hub["reader"]).json()
    assert (hard["difficulty"], hard["recommended_model"]) == ("hard", "opus")
    whole = client.get(url, headers=hub["reader"]).json()
    assert whole["difficulty"] is None and whole["recommended_model"] is None
