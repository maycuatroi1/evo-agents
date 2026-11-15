"""The state of a source as the hub holds it (plan kg-centralized-on-hub, step 3): the facts of each run the hub reads
from its log at ingest (migration 0252), the facts the next build fills in for runs pushed before, and
GET /v1/kg/{project}/sources/{source}/state, which ``kg sync`` reads instead of a corpus.sqlite."""

from __future__ import annotations

import json

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import Text, cast, select, update

from evo_agents.hub import tables
from evo_agents.kg.sync import sync_project
from tests.hub.test_kg import PROJECT, Machine, doc, hub, latest, push, query, run_jobs  # noqa: F401

FACTS = ("status", "lineage", "epoch", "run_started_at", "run_finished_at", "cursor")


def state(hub, source: str = "docs", member: str = "alice", **params) -> dict:
    response = hub.client.get(f"/v1/kg/{PROJECT}/sources/{source}/state", params=params, headers=getattr(hub, member))
    assert response.status_code == 200, response.text
    return response.json()


def sync(machine: Machine, *items: dict, cursor=None) -> str:
    machine.files["docs"].write_text(json.dumps({"items": list(items), "cursor": cursor}), encoding="utf-8")
    (result,) = sync_project(machine.project(), ["docs"])
    assert result.ok, result.issues
    return result.run_id


def ingest_facts(hub) -> dict[str, tuple]:
    ingests = tables.kg_ingests
    found = query(hub.db, select(cast(ingests.c.run_id, Text), *(ingests.c[name] for name in FACTS)))
    return {row[0]: tuple(row[1:]) for row in found}


def test_a_pushed_run_gives_the_source_its_cursor_lineage_and_last_run(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    assert state(hub)["runs"] == 0 and state(hub)["last_run"] is None and state(hub)["items"] == []

    first = sync(laptop, doc("guide", "How the app works."), doc("setup", "Install it."), cursor={"page": 1})
    assert push(hub, laptop).ok
    (facts,) = ingest_facts(hub).values()
    status, lineage, epoch, started, _, cursor = facts
    assert status == "ok" and lineage and epoch and started and cursor == {"cursor": {"page": 1}}

    held = state(hub)
    assert (held["runs"], held["unread"]) == (1, 0)
    assert held["last_run"]["run_id"] == first and held["last_ok"]["run_id"] == first
    assert held["cursor"] == {"run_id": first, "cursor": {"page": 1}}
    assert held["lineage"] == {"run_id": first, "lineage": lineage, "epoch": epoch}
    assert held["items"] == []  # no build yet: the items are those of the latest build
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert latest(hub)["status"] == "succeeded"
    assert [(item["id"], item["rev"]) for item in state(hub)["items"]] == [
        ("docs:doc:guide", "1"),
        ("docs:doc:setup", "1"),
    ]
    assert [item["id"] for item in state(hub, scope="docs:doc:se")["items"]] == ["docs:doc:setup"]

    second = sync(laptop, doc("guide", "How the app works now.", rev="2"))  # no state message: the cursor stays
    assert push(hub, laptop).ok
    held = state(hub)
    assert held["runs"] == 2 and held["last_ok"]["run_id"] == second
    assert held["cursor"]["run_id"] == first and held["lineage"]["lineage"] == lineage
    assert state(hub, "vault", member="dave")["runs"] == 0
    refused = hub.client.get(f"/v1/kg/{PROJECT}/sources/docs/state", headers=hub.carol)
    assert refused.status_code == 403, "a reader does not sync"


def test_runs_pushed_before_0252_are_unread_until_the_next_build_fills_their_facts(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    first = sync(laptop, doc("guide", "How the app works."), cursor={"page": 1})
    assert push(hub, laptop).ok
    ingests = tables.kg_ingests
    query(hub.db, update(ingests).values({name: None for name in FACTS}))  # as 0252 leaves a run held before it
    held = state(hub)
    assert (held["runs"], held["unread"]) == (1, 1)
    assert held["last_run"] == {"run_id": first, "status": None, "started_at": None, "finished_at": None}
    assert held["last_ok"] is None and held["cursor"] is None and held["lineage"] is None

    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert latest(hub)["status"] == "succeeded"
    held = state(hub)
    assert held["unread"] == 0 and held["last_ok"]["run_id"] == first
    assert held["cursor"] == {"run_id": first, "cursor": {"page": 1}}
    assert held["lineage"]["run_id"] == first
