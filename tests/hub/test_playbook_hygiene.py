"""The playbook's maintenance: entries that hold until a proposal lands, and entries shown often and never helpful."""

import asyncio

import pytest

from evo_agents.hub import playbook
from tests.hub import pg


def test_the_proposal_an_entry_holds_until_is_read_from_its_text():
    assert playbook.until_proposal("Hold builds until proposal #61 lands") == 61
    assert playbook.until_proposal("skip the probe; #62 lands next week") == 62
    assert playbook.until_proposal("until #7 lands") == 7
    assert playbook.until_proposal("see #4 for why") is None
    assert playbook.until_of({"until_proposal_id": 9, "text": "until proposal #3 lands"}) == 9
    assert playbook.until_of({"until_proposal_id": None, "text": "until proposal #3 lands"}) == 3


def test_the_maintenance_retires_entries_whose_proposal_merged_or_that_never_helped():
    entries = [
        {"id": 3, "text": "until proposal #61 lands"},
        {"id": 4, "text": "until proposal #62 lands"},
        {"id": 5, "text": "x", "until_proposal_id": 61, "retired_at": "2026-11-01"},
        {"id": 6, "text": "x", "prompt_exposures": 30, "helpful": 0, "harmful": 2},
        {"id": 7, "text": "x", "prompt_exposures": 30, "helpful": 1},
        {"id": 8, "text": "x", "prompt_exposures": 29, "helpful": 0},
    ]
    due = playbook.due_retires(entries, {61: "merged", 62: "open"})
    assert [entry_id for entry_id, _ in due] == [3, 6]
    assert "#61" in due[0][1] and "30 review runs" in due[1][1]
    assert [entry_id for entry_id, _ in playbook.due_retires(entries, {}, exposure_limit=29)] == [6, 8]
    assert playbook.due_retires(entries, {}, exposure_limit=0) == []


# On the hub: test_playbook's lab, its night's review run and the next one


needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
if pg.DSN:
    from sqlalchemy import func, insert, select, update
    from sqlalchemy.ext.asyncio import create_async_engine

    from evo_agents.hub import tables
    from evo_agents.hub.server.change_views import set_change
    from tests.hub.live import sql
    from tests.hub.test_history_runs import lab  # noqa: F401 (lab is a fixture)
    from tests.hub.test_history_search import held_review, two_sessions
    from tests.hub.test_playbook import LINE, NEXT, PUSH, delta, next_review, playbook_of, recorded, run_events, written
    from tests.hub.test_review_runs import proposed
    from tests.hub.test_runs import moved


def on_db(db, work):
    """``work(conn)`` on an async connection of ``db``, in one transaction that commits."""

    async def go():
        engine = create_async_engine(db.dsn.replace("postgresql://", "postgresql+psycopg://", 1))
        try:
            async with engine.begin() as conn:
                return await work(conn)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def a_proposal(lab, run_id: int) -> dict:
    found = recorded(lab, run_id, lens="environment", title="The push to origin is rejected", evidence=[LINE])
    return proposed(lab.client, lab.worker, run_id, lens="environment", finding_ids=[found["id"]], evidence=[])


def entries_by_id(lab) -> dict[int, dict]:
    return {entry["id"]: entry for entry in playbook_of(lab, retired="true")["entries"]}


def deltas(lab) -> list[tuple]:
    d = tables.curator_playbook_deltas
    return sql(lab.db, select(d.c.id, d.c.entry_id, d.c.op, d.c.run_id, d.c.actor, d.c.note).order_by(d.c.id))


@needs_pg
def test_an_entry_holds_until_the_proposal_it_names_or_its_text_names(lab):
    two_sessions(lab)
    run_id = held_review(lab)
    made = a_proposal(lab, run_id)
    named = written(lab, run_id, op="add", text="Pin the runner.", evidence=[LINE], until_proposal_id=made["id"])
    assert (named["until_proposal_id"], named["prompt_exposures"]) == (made["id"], 0)
    said = written(lab, run_id, op="add", text=f"Skip the probe until proposal #{made['id']} lands.", evidence=[PUSH])
    assert said["until_proposal_id"] == made["id"]
    elsewhere = written(lab, run_id, op="add", text="Wait until proposal #999999 lands.", evidence=[PUSH])
    assert elsewhere["until_proposal_id"] is None  # no proposal of this project: the text alone says it
    refused = delta(lab, run_id, op="add", text="x", evidence=[LINE], until_proposal_id=999999)
    assert refused.status_code == 422 and "not a proposal of this project" in refused.text
    tagged = delta(lab, run_id, op="helpful", entry_id=named["id"], until_proposal_id=made["id"])
    assert tagged.status_code == 422 and "neither text nor lens" in tagged.text


@needs_pg
def test_a_done_review_counts_its_shown_entries_and_the_curator_retires_the_due_ones(lab):
    two_sessions(lab)
    first = held_review(lab)
    made = a_proposal(lab, first)
    until = written(lab, first, op="add", lens="environment", text="Pin the runner.", evidence=[LINE])
    until = written(lab, first, op="helpful", entry_id=until["id"])  # helpful: only its proposal's merge retires it
    stale = written(lab, first, op="add", text="Count cache reads.", evidence=[PUSH])
    good = written(lab, first, op="add", text="Read the numbers first.", evidence=[PUSH])
    written(lab, first, op="helpful", entry_id=good["id"])
    pb = tables.curator_playbook
    legacy = update(pb).values(text=f"Pin the runner until proposal #{made['id']} lands.")  # as before 0256
    sql(lab.db, legacy.where(pb.c.id == until["id"]))
    near = update(pb).values(prompt_exposures=playbook.EXPOSURE_LIMIT - 1).where(pb.c.id.in_([stale["id"], good["id"]]))
    sql(lab.db, near)
    moved(lab.client, lab.worker, first, "verifying", "done")  # its prompt showed none of them
    assert {entry_id: entry["prompt_exposures"] for entry_id, entry in entries_by_id(lab).items()} == {
        until["id"]: 0,
        stale["id"]: playbook.EXPOSURE_LIMIT - 1,
        good["id"]: playbook.EXPOSURE_LIMIT - 1,
    }
    before = deltas(lab)

    second = next_review(lab, first, NEXT)["id"]
    (logged,) = [event["body"] for event in run_events(lab, second) if "playbook_shown" in event["body"]]
    assert sorted(logged["playbook_shown"]) == sorted([until["id"], stale["id"], good["id"]])
    moved(lab.client, lab.worker, second, "verifying", "done")
    entries = entries_by_id(lab)
    assert entries[until["id"]]["prompt_exposures"] == 1 and not entries[until["id"]]["retired"]
    assert entries[good["id"]]["prompt_exposures"] == playbook.EXPOSURE_LIMIT and not entries[good["id"]]["retired"]
    assert entries[stale["id"]]["retired"] and "never found helpful" in entries[stale["id"]]["retired_note"]
    after = deltas(lab)
    assert after[: len(before)] == before
    assert [row[1:5] for row in after[len(before) :]] == [(stale["id"], "retire", None, playbook.CURATOR_ACTOR)]

    c = tables.curator_changes
    (project_id,) = sql(lab.db, select(pb.c.project_id).where(pb.c.id == until["id"]))[0]
    change = {"project_id": project_id, "proposal_id": made["id"]}
    change.update(plan_id="curator-pin", repo="app", branch="curator/pin", forge="github", tier=1, state="judged")
    (change_id,) = sql(lab.db, insert(c).values(**change).returning(c.c.id))[0]
    merged = {"state": "merged", "merged_at": func.now(), "merge_sha": "a" * 40, "reason": None}
    on_db(lab.db, lambda conn: set_change(conn, change_id, ledger_extra={"actor": "user", "login": "octo"}, **merged))
    gone = entries_by_id(lab)[until["id"]]
    assert gone["retired"] and gone["retired_note"] == f"proposal #{made['id']} it held until is merged"
    last = deltas(lab)
    assert last[: len(after)] == after
    assert [row[1:6] for row in last[len(after) :]] == [
        (until["id"], "retire", None, playbook.CURATOR_ACTOR, gone["retired_note"])
    ]


@needs_pg
def test_the_charters_exposure_limit_is_the_one_the_curator_retires_by(lab):
    ch = tables.charters
    ((project_id, revision, body, created_by),) = sql(
        lab.db,
        select(ch.c.project_id, ch.c.revision, ch.c.body, ch.c.created_by).order_by(ch.c.revision.desc()).limit(1),
    )
    from evo_agents.hub.server.playbook import service as playbook_routes

    assert on_db(lab.db, lambda conn: playbook_routes.exposure_limit_of(conn, project_id)) == playbook.EXPOSURE_LIMIT
    for offset, held, limit in ((1, 5, 5), (2, 0, 0), (3, None, playbook.EXPOSURE_LIMIT)):
        evolved = {**body, "evolve": {**(body.get("evolve") or {}), "exposure_limit": held}}
        row = {"project_id": project_id, "revision": revision + offset, "body": evolved, "created_by": created_by}
        sql(lab.db, insert(ch).values(**row))
        assert on_db(lab.db, lambda conn: playbook_routes.exposure_limit_of(conn, project_id)) == limit
