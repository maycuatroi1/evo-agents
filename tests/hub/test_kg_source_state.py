"""The state of a source as the hub holds it (plan kg-centralized-on-hub, step 3): the facts of each run the hub reads
from its log at ingest (migration 0252), the facts the next build fills in for runs pushed before, and
GET /v1/kg/{project}/sources/{source}/state, which ``kg sync`` reads instead of a corpus.sqlite."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from unittest.mock import ANY

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)
pytest.importorskip("sqlalchemy", reason="a source's state needs the hub-server extra, evo-ak[hub-server]")

from sqlalchemy import Text, cast, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from evo_agents.hub import kg_ingest, tables
from evo_agents.hub.kg_source_state import fill_run_facts
from evo_agents.kg.sync import sync_project
from tests.hub.test_kg import PROJECT, Machine, doc, hub, latest, push, query, run_jobs  # noqa: F401

FACTS = ("status", "lineage", "epoch", "run_started_at", "run_finished_at", "cursor")


def state(hub, source: str = "docs", member: str = "alice", **params) -> dict:
    response = hub.client.get(f"/v1/kg/{PROJECT}/sources/{source}/state", params=params, headers=getattr(hub, member))
    assert response.status_code == 200, response.text
    return response.json()


def sync(machine: Machine, *items: dict, cursor=None, **spec) -> str:
    spec = {"items": list(items), "cursor": cursor, **spec}
    machine.files["docs"].write_text(json.dumps(spec), encoding="utf-8")
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
    status, lineage, epoch, started, finished, cursor = facts
    assert status == "ok" and lineage and epoch and cursor == {"cursor": {"page": 1}}
    assert started and finished and started <= finished  # from the run_end of the log, which due_sources reads

    held = state(hub)
    assert (held["runs"], held["unread"]) == (1, 0)
    assert held["last_run"]["run_id"] == first and held["last_ok"]["run_id"] == first
    assert (held["last_ok"]["started_at"], held["last_ok"]["finished_at"]) == (started, finished)
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
    assert held["unread"] == 0 and held["last_ok"]["run_id"] == first and held["last_ok"]["finished_at"]
    assert held["cursor"] == {"run_id": first, "cursor": {"page": 1}}
    assert held["lineage"]["run_id"] == first


def test_a_run_whose_log_says_no_end_ends_when_received_and_one_whose_log_is_gone_is_read_as_failed(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    first = sync(laptop, doc("guide", "How the app works."))
    assert push(hub, laptop).ok
    ingests = tables.kg_ingests
    query(hub.db, update(ingests).values(run_finished_at=None))  # a run_end without finished_at, as older clients
    (received,) = query(hub.db, select(ingests.c.received_at))[0]
    stamp = received.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert state(hub)["last_ok"] == {"run_id": first, "status": "ok", "started_at": ANY, "finished_at": stamp}

    query(hub.db, update(ingests).values({name: None for name in FACTS}))
    (project_id,) = query(hub.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0]

    async def fill() -> int:
        engine = create_async_engine(hub.db.dsn.replace("postgresql://", "postgresql+psycopg://", 1))
        try:
            return await fill_run_facts(engine, tmp_path / "no-cache", project_id)
        finally:
            await engine.dispose()

    assert asyncio.run(fill()) == 1  # its log is not in the cache: read, as failed, so unread reaches 0
    held = state(hub)
    assert (held["unread"], held["last_run"]["status"], held["last_ok"]) == (0, "failed", None)
    assert asyncio.run(fill()) == 0


def test_the_removals_a_guard_held_back_and_the_runs_no_build_read_yet_are_in_the_state(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    sync(laptop, doc("guide", "How the app works."), doc("setup", "Install it."))
    assert push(hub, laptop).ok
    assert (state(hub)["unbuilt"], state(hub)["held"]) == (1, [])  # no build yet: every run is unbuilt
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    assert latest(hub)["status"] == "succeeded" and state(hub)["unbuilt"] == 0

    emptied = sync(laptop)  # an empty listing would remove both: the deletion guard holds them back
    assert push(hub, laptop).ok
    held = state(hub)
    assert held["unbuilt"] == 1 and held["last_ok"]["run_id"] == emptied
    (removal,) = held["held"]
    assert (removal["run_id"], removal["scope"]) == (emptied, "docs:")
    assert removal["reason"].startswith("empty listing")
    assert sorted(removal["ids"]) == ["docs:doc:guide", "docs:doc:setup"]
    ingests = tables.kg_ingests
    (facts,) = query(hub.db, select(ingests.c.held, ingests.c.listed).where(ingests.c.held.is_not(None)))
    assert facts == ([{key: removal[key] for key in ("scope", "reason", "ids")}], ["docs:"])

    sync(laptop, doc("note", "A new one."), listing=False)  # lists nothing completely: the hold stays
    assert push(hub, laptop).ok
    assert state(hub)["held"] == [removal]

    sync(laptop, doc("guide", "How the app works."), doc("setup", "Install it."))  # lists docs: and holds none
    assert push(hub, laptop).ok
    assert (state(hub)["held"], state(hub)["unbuilt"]) == ([], 3)


def test_the_hold_of_each_scope_is_that_of_the_newest_run_that_listed_it(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    kinds = ["doc", "memo"]
    sync(laptop, doc("guide", "How the app works."), doc("minutes", "We met.", kind="memo"), kinds=kinds)
    assert push(hub, laptop).ok
    emptied = sync(laptop, kinds=kinds)  # both scopes would lose every item: both held back
    assert push(hub, laptop).ok
    assert [(r["run_id"], r["scope"], r["ids"]) for r in state(hub)["held"]] == [
        (emptied, "docs:doc:", ["docs:doc:guide"]),
        (emptied, "docs:memo:", ["docs:memo:minutes"]),
    ]

    sync(laptop, doc("guide", "How the app works."), kinds=["doc"])  # lists docs:doc: only, which holds none now
    assert push(hub, laptop).ok
    assert [(r["run_id"], r["scope"]) for r in state(hub)["held"]] == [(emptied, "docs:memo:")]


def test_a_hold_the_hub_cannot_keep_leaves_the_older_hold_of_its_scope(hub, tmp_path, monkeypatch):
    laptop = Machine(tmp_path, "laptop")
    sync(laptop, doc("guide", "How the app works."), doc("setup", "Install it."))
    assert push(hub, laptop).ok
    emptied = sync(laptop)
    assert push(hub, laptop).ok
    (removal,) = state(hub)["held"]
    assert removal["run_id"] == emptied

    monkeypatch.setattr(kg_ingest, "MAX_HELD_IDS", 1)  # the next run holds back 2 ids: more than the hub keeps
    sync(laptop)
    assert push(hub, laptop).ok
    ingests = tables.kg_ingests
    newest = query(hub.db, select(ingests.c.held, ingests.c.listed).order_by(ingests.c.run_id.desc()).limit(1))
    assert newest == [(None, None)]  # it names no scope it listed, so it clears none
    assert state(hub)["held"] == [removal]
