"""The evolution of an agent's configuration (target agent_config, plan pareto-curator-evolution, step 2, criterion
a2): its run's inputs hold the agent's instructions and its skill axis as the baseline, and its end pins the best
candidate as the profile's next version, which the system pins as an accepted proposal's change would, only when it
holds on the holdout split (``system_curation.holdout_verdict``); otherwise the profile keeps its version and the
evolution says why.

Without a database, the holdout verdict. With one (``EVO_HUB_TEST_DSN``), on ``tests.hub.test_curator_system_lens``'s
system ``desk``, whose curator optimizes the agent node of @scout. Every name here is made up.
"""

from __future__ import annotations

import pytest

from tests.hub import pg
from tests.hub.test_curator_system_lens import _system_curation

if pg.DSN:
    from sqlalchemy import select

    from evo_agents.hub import tables
    from tests.hub.live import sql
    from tests.hub.test_curator import (
        PROJECT,
        charter_body,
        night,  # noqa: F401 (a fixture)
        written,
    )
    from tests.hub.test_curator_changes import taking
    from tests.hub.test_curator_system_lens import desk, pinned  # noqa: F401 (desk is a fixture)
    from tests.hub.test_evolve_server import candidate, evolution, keyed, suite
    from tests.hub.test_review_runs import review  # noqa: F401 (a fixture)
    from tests.hub.test_runs import claim, moved, web_client  # noqa: F401 (a fixture)

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def test_the_holdout_verdict_holds_on_a_win_or_on_no_regression_and_never_without_a_task():
    holdout_verdict = _system_curation().holdout_verdict
    assert holdout_verdict({}) == (False, "no holdout task was measured: the profile keeps its version")
    held, why = holdout_verdict(
        {"replay:1": {"baseline": 0.0, "candidate": 1.0}, "replay:2": {"baseline": 1.0, "candidate": 0.5}}
    )
    assert held and why.startswith("it wins on the holdout split, 1.5 against 1 on 2 holdout task(s)")
    held, why = holdout_verdict({"replay:1": {"baseline": 1.0, "candidate": 1.0}})
    assert held and why.startswith("it regresses on no holdout task")
    held, why = holdout_verdict(
        {"replay:1": {"baseline": 1.0, "candidate": 0.0}, "replay:2": {"baseline": 0.0, "candidate": 1.0}}
    )
    assert not held and why.endswith("worse on replay:1")
    assert holdout_verdict({"replay:1": {"baseline": "1"}})[0] is False  # a result that is no pair counts for nothing


BETTER = "Give the invoice number first."


@pytest.fixture
def evolving(desk):  # noqa: F811
    """desk, with evolve enabled in the charter and its worker taking every kind of run; the ids of the system desk,
    and refs of two select and one holdout replay task."""
    written(desk.client, desk.headers["owner"], charter_body(night_plans=[], evolve={"enabled": True}))
    taking(desk.client, desk.worker)
    s = tables.agent_systems
    desk.system_id = sql(desk.db, select(s.c.id).where(s.c.slug == "desk"))[0][0]
    desk.refs = suite(desk)
    return desk


def started(fixture, agent: str = "scout"):
    sent = {"target": "agent_config", "system_id": fixture.system_id, "agent": agent}
    return fixture.client.post(f"/v1/projects/{PROJECT}/curator/evolve", json=sent, headers=fixture.headers["owner"])


def running(fixture) -> tuple[dict, dict]:
    """The evolve run of the evolution started, claimed and running: (its spec, its inputs)."""
    spec = claim(fixture.client, fixture.worker)
    assert spec is not None and spec["kind"] == "evolve", spec
    moved(fixture.client, fixture.worker, spec["id"], "running")
    found = fixture.client.get(
        f"/v1/worker/runs/{spec['id']}/evolve", headers=keyed(fixture, spec["curator"]["evolve_key"])
    )
    assert found.status_code == 200, found.text
    return spec, found.json()


def post(fixture, spec: dict, what: str, body: dict, status: int = 200) -> dict:
    url = f"/v1/worker/runs/{spec['id']}/evolve/{what}"
    found = fixture.client.post(url, json=body, headers=keyed(fixture, spec["curator"]["evolve_key"]))
    assert found.status_code == status, found.text
    return found.json()


def ended(fixture, holdout: dict | None, status: int = 200) -> tuple[int, dict]:
    """Start an evolution of @scout and end it done with a better candidate and ``holdout``; (its id, @scout now)."""
    evolve_id = started(fixture).json()["id"]
    spec, found = running(fixture)
    one = fixture.refs["select1"]
    seed = {**candidate({one: 0.0}, ""), "origin": "seed", "iteration": 0, "components": found["baseline"]}
    post(fixture, spec, "candidate", seed)
    best = {**candidate({one: 1.0}, ""), "components": {**found["baseline"], "instructions": BETTER}}
    best_id = post(fixture, spec, "candidate", best)["candidate_id"]
    sent = {"state": "done", "best_candidate_id": best_id, **({"holdout": holdout} if holdout is not None else {})}
    post(fixture, spec, "report", sent, status)
    agent = fixture.client.get(f"/v1/projects/{PROJECT}/agents/scout", headers=fixture.headers["owner"])
    return evolve_id, agent.json()


@needs_pg
def test_an_agent_config_evolution_reads_the_agents_instructions_and_skill_axis_as_its_baseline(evolving):
    made = started(evolving)
    assert made.status_code == 201, made.text
    view = made.json()
    assert view["state"] == "running", view["reason"]
    assert (view["target"], view["system_id"], view["agent"], view["repo"]) == (
        "agent_config",
        evolving.system_id,
        "scout",
        None,
    )
    spec, found = running(evolving)
    assert spec["curator"]["evolve"] == {"evolve_id": view["id"], "target": "agent_config"}
    assert set(found["baseline"]) == {"instructions", "skill"} and found["repo"] is None
    assert (found["system_id"], found["agent"], found["paths"]) == (evolving.system_id, "scout", [])
    assert evolving.refs["holdout"] not in str(found)


@needs_pg
def test_an_agent_config_evolution_starts_only_for_an_agent_an_optimizes_edge_reaches(evolving):
    refused = started(evolving, agent="helper")
    assert refused.status_code == 422 and "no optimizes edge" in refused.text, refused.text
    sent = {"target": "agent_config", "system_id": 10**9, "agent": "scout"}
    owner = evolving.headers["owner"]
    assert evolving.client.post(f"/v1/projects/{PROJECT}/curator/evolve", json=sent, headers=owner).status_code == 422


@needs_pg
def test_a_best_candidate_that_wins_on_the_holdout_split_is_the_next_version_the_system_pins(evolving):
    holdout = {evolving.refs["holdout"]: {"baseline": 0.0, "candidate": 1.0}}
    evolve_id, agent = ended(evolving, holdout)
    assert agent["version"] == 2 and agent["instructions"] == BETTER
    desk_now = pinned(evolving.client, evolving.headers["owner"], "desk")
    assert desk_now["revision"] == 2
    assert next(n for n in desk_now["spec"]["nodes"] if n["id"] == "scout")["version"] == 2
    row = evolution(evolving, evolve_id)
    assert (
        row.state == "done" and "it wins on the holdout split" in row.reason and "@scout is at version 2" in row.reason
    )


@pytest.mark.parametrize(
    ("holdout", "why"),
    [
        (None, "no holdout task was measured: the profile keeps its version"),
        ({"HOLDOUT": {"baseline": 1.0, "candidate": 0.0}}, "it neither wins nor holds on the holdout split"),
    ],
)
@needs_pg
def test_a_best_candidate_without_a_holdout_win_leaves_the_profile_and_says_why(evolving, holdout, why):
    if holdout is not None:
        holdout = {evolving.refs["holdout"]: holdout["HOLDOUT"]}
    evolve_id, agent = ended(evolving, holdout)
    assert agent["version"] == 1 and agent["instructions"] != BETTER
    row = evolution(evolving, evolve_id)
    assert row.state == "done" and why in row.reason


@needs_pg
def test_a_holdout_result_of_a_task_off_the_holdout_split_is_refused(evolving):
    evolve_id, agent = ended(evolving, {evolving.refs["select1"]: {"baseline": 0.0, "candidate": 1.0}}, status=422)
    assert agent["version"] == 1 and evolution(evolving, evolve_id).state == "running"
