"""The token of a KG run (``evo_agents.hub.server.kg_run_access``).

A KG run (the review run of lens kg_init or kg_questions of a kg_init run, or the run of a batch of an answer job) is a
run of the Curator, so it never runs as its owner; its worker's ask for leases gets a token of its own, named
``kg-run:<project>``, so ``hub kg init --decide``, ``--questions`` and ``hub kg answer --reply`` work with no login on
the node. The token reaches its project's KG routes alone, takes its own kg_init run and its own batch alone, reads
through the agents' sink, never as a hub admin, and ends with its run. Any other run gets no such lease.
"""

from __future__ import annotations

import argparse
import json
import secrets
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from evo_agents.hub import kg_init_cli, tables
from evo_agents.hub.client import env_credentials
from evo_agents.hub.kg_health_eval import AGENT_SINK
from evo_agents.hub.server import kg_run_access
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.sealing import KEY_BYTES
from tests.hub.live import bearer, sql
from tests.hub.test_credentials_api import leased
from tests.hub.test_curator import PROJECT, night, set_run  # noqa: F401 (night is a fixture)
from tests.hub.test_kg_answers import advanced as answers_advanced
from tests.hub.test_kg_answers import base, run_of
from tests.hub.test_kg_init import INIT, advanced, configured, init_row, profile, project_id, with_profile
from tests.hub.test_kg_pipeline import AppHub
from tests.hub.test_kg_shared_pool import kg_beat
from tests.hub.test_review_runs import held_review, review  # noqa: F401 (review is a fixture)
from tests.hub.test_runs import claim

KG_LEASE = f"kg-run:{PROJECT}"
DECISION = {
    "settings": {"semantic_mode": "index"},
    "reasons": [{"about": "settings", "why": "prose is a third of the lines", "figures": {"prose": 0.33}}],
}


@pytest.fixture
def web_client(hub_db, tmp_path, github):
    """test_runs' web_client on a hub that keeps secrets, so its runs are leased."""
    changes = {**live.web_changes(github), "secrets_key": secrets.token_bytes(KEY_BYTES)}
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github, **changes)), base_url="https://hub.test") as c:
        yield c


def hub_leases(world, run_id: int) -> list[dict]:
    return [item for item in leased(world.client, world.worker, run_id)["leases"] if item["kind"] == "hub"]


def kg_token(world, run_id: int) -> str:
    (lease,) = hub_leases(world, run_id)
    assert (lease["name"], lease["provider"], lease["env_var"]) == (KG_LEASE, "run-access", "EVO_HUB_TOKEN")
    assert lease["base_url"] and lease["value"].startswith("evr_")
    return lease["value"]


def kg_init_run(world) -> tuple[int, int]:
    """(a kg_init run of the project, its review run of lens kg_init), the review run claimed by the worker on duty."""
    db, owner = world.db, world.headers["owner"]
    configured(world)
    with_profile(db, project_id(db), profile())
    init_id = world.client.post(INIT, headers=owner).json()["id"]
    advanced(world)
    agent = init_row(db, init_id)["agent_run_id"]
    assert agent and claim(world.client, world.worker)["id"] == agent
    return init_id, agent


def test_a_kg_init_run_decides_with_its_own_token_and_reaches_nothing_else(review):
    client, db = review.client, review.db
    init_id, agent = kg_init_run(review)
    token = kg_token(review, agent)
    assert kg_token(review, agent) == token, "the same token on every ask"
    auth = bearer(token)

    assert client.get(INIT, headers=auth).status_code == 200
    read = client.post(f"/v1/kg/{PROJECT}/tools/kg_search", headers=auth, json={"arguments": {"query": "x"}})
    assert read.status_code not in (401, 403), read.text  # past the token: this world's build keeps no graph
    assert client.get(f"/v1/kg/{PROJECT}/eval-set", headers=auth).status_code in (200, 404)
    for method, path in (
        ("GET", "/v1/projects"),
        ("GET", f"/v1/kg/{PROJECT}/settings"),
        ("POST", INIT),
        ("PUT", f"/v1/kg/{PROJECT}/eval-set"),
        ("GET", "/v1/secrets"),
        ("GET", "/v1/kg/lumen-notes/init"),
        ("POST", f"/v1/projects/{PROJECT}/runs"),
    ):
        refused = client.request(method, path, headers=auth, json={})
        assert refused.status_code == 403 and "KG run's token" in refused.json()["message"], (method, path)

    t = tables.kg_init_runs
    sql(db, update(t).values(agent_run_id=None).where(t.c.id == init_id))
    other = client.post(f"{INIT}/{init_id}/decision", headers=auth, json={"decision": DECISION})
    assert other.status_code == 403 and f"run {agent} is not the review run" in other.json()["message"]
    sql(db, update(t).values(agent_run_id=agent).where(t.c.id == init_id))
    done = client.post(f"{INIT}/{init_id}/decision", headers=auth, json={"decision": DECISION})
    assert done.status_code == 200, done.text
    assert done.json()["applied"]["settings"] == {"semantic_mode": "index"}
    a = tables.audit
    written = [row[0] for row in sql(db, select(a.c.target).where(a.c.action == "run_access.write"))]
    assert any(f"POST /v1/kg/{PROJECT}/init/{init_id}/decision 200" in row for row in written), written
    assert not any(token in row for row in written)

    set_run(db, agent, state="cancelled", finished_at=func.now())
    assert client.get(INIT, headers=auth).status_code == 401, "the token ends with its run"


def test_hub_kg_init_decide_runs_on_the_token_alone(review, monkeypatch, tmp_path, capsys):
    init_id, agent = kg_init_run(review)
    token = kg_token(review, agent)
    monkeypatch.setenv("EVO_HUB_TOKEN", token)
    monkeypatch.setenv("EVO_HUB_URL", AppHub.url)
    found = env_credentials()
    assert found is not None and found.token == token, "the CLI signs in with the run's variables, no login"
    monkeypatch.setattr(kg_init_cli, "signed_in", lambda: (AppHub(review.client, bearer(found.token)), found))
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(DECISION))
    args = dict(project=PROJECT, all=False, status=False, decide=str(path), questions=None, run=None, limit=10)
    assert kg_init_cli.cmd_init(argparse.Namespace(**args, json=False)) == 0
    assert f"kg init run #{init_id} of project {PROJECT} decided" in capsys.readouterr().out


def test_the_run_of_an_answer_batch_sends_its_answers_with_its_token(review):
    client, owner = review.client, review.headers["owner"]
    asked = client.post(base(), headers=owner, json={"questions": [{"id": "1", "question": "Where is the ledger?"}]})
    assert asked.status_code == 201, asked.text
    job_id = asked.json()["id"]
    assert answers_advanced(review) == {"queued": 1}
    run_id = run_of(review, job_id)
    kg_beat(client, review.worker)  # a daemon that runs a review with no repo
    assert claim(client, review.worker)["id"] == run_id
    auth = bearer(kg_token(review, run_id))
    sent = client.post(f"{base()}/{job_id}/replies", headers=auth, json={"answers": [{"id": "1", "answer": "vault 7"}]})
    assert sent.status_code == 200 and sent.json() == {"kept": 1, "problems": []}, sent.text
    other = client.post(f"{base()}/{job_id + 1}/replies", headers=auth, json={"answers": [{"id": "1", "answer": "a"}]})
    assert other.status_code in (403, 404)
    person = client.post(f"{base()}/{job_id}/replies", headers=owner, json={"answers": [{"id": "1", "answer": "a"}]})
    assert person.status_code == 403, "a member's own token still sends no answer of a run"


def test_a_review_run_that_is_no_kg_run_gets_no_token(review):
    run_id = held_review(review)
    assert hub_leases(review, run_id) == []


def test_a_kg_runs_token_reads_through_the_agents_sink_whatever_it_names():
    kg = SimpleNamespace(kg_project=PROJECT)
    assert kg_run_access.sink(kg, "cli") == AGENT_SINK and kg_run_access.sink(kg, None) == AGENT_SINK
    assert kg_run_access.sink(SimpleNamespace(kg_project=None), "cli") == "cli"
    assert kg_run_access.refusal("POST", f"/v1/kg/{PROJECT}/answers/3/replies", PROJECT) is None
    assert kg_run_access.refusal("POST", "/v1/kg/lumen-notes/answers/3/replies", PROJECT) is not None
    assert kg_run_access.refusal("GET", f"/v1/kg/{PROJECT}/answers/3/replies", PROJECT) is not None
    assert kg_run_access.refusal("POST", f"/v1/kg/{PROJECT}/init/3/decision/x", PROJECT) is not None
