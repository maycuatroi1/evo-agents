"""The job hub.analyze_facets (``evo_agents.hub.facet_job``), the night's figures it feeds
(``evo_agents.hub.server.collect``, ``evo_agents.hub.server.facet_figures``) and schema 0054.

Step 3 of plan history-driven-curator, criterion a3 (its deterministic part): each new transcript and each run that
ended is analyzed once, and again when its content or the facet's version changes; the facet lives in Postgres and
outlives the transcript's content; the job holds no lock on the tables the hub writes to, waits for a blob store that
does not answer and tries again later; curator.collect counts the sessions from the facets with the keys it had, adds
a weekly series over the whole history, counts a local run only when its member owns the charter, and counts the same
figures again from the same data. Criterion a13: only additions to the contract."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from psycopg import errors
from sqlalchemy import delete, func, select, update

from evo_agents.hub import facets, jobs, tables
from evo_agents.hub import review as review_model
from evo_agents.hub.blobs import BlobStoreUnavailable
from evo_agents.hub.facet_job import analyze_facets, fingerprint
from evo_agents.hub.server.collect import compute_figures
from evo_agents.hub.server.curator import _owner_access
from evo_agents.hub.worker import queue
from tests.hub.live import sql
from tests.hub.test_curator import (
    NIGHT,
    OWNER,
    night,  # noqa: F401 (a fixture)
)
from tests.hub.test_curator import PROJECT as NIGHT_PROJECT
from tests.hub.test_digest import FAILED_TEST, said, session_lines
from tests.hub.test_local_runs import path as local_path
from tests.hub.test_local_runs import registered
from tests.hub.test_migrate import move_to
from tests.hub.test_review_runs import SESSION as DIGESTED
from tests.hub.test_review_runs import (
    collected,
    digest_body,
    finding,
    held_review,
    review,  # noqa: F401 (a fixture)
)
from tests.hub.test_runs import PROJECT, web_client  # noqa: F401 (a fixture)
from tests.hub.test_session_transcripts import hub, parts_of, send, statuses, transcript  # noqa: F401 (hub: a fixture)

SESSION = "0199b1c2-7d3e-7a10-9c41-5e2f3a4b5c6d"  # tests.hub.test_digest.SESSION, which parts_of sends
LOCAL = "0199a3c1-0000-7000-8000-0000000000b7"
OTHER_LOCAL = "0199a3c1-0000-7000-8000-0000000000b8"


def analyzed(client, now: datetime | None = None, **options) -> dict:
    state = client.app.state
    return client.portal.call(partial(analyze_facets, state.engine, state.blobs, now=now, **options))


def later(db, minutes: int = 60) -> datetime:
    return sql(db, select(func.now()))[0][0] + timedelta(minutes=minutes)


def facet_of(db, *where) -> dict | None:
    f = tables.session_facets
    columns = [f.c[name] for name in ("kind", "session_id", "run_id", "source", "fingerprint", "version", "user_id")]
    columns += [f.c[name] for name in ("label", "at", "summary", "body", "attempts", "error", "retry_at")]
    rows = sql(db, select(*columns).where(*where))
    return dict(zip([column.name for column in columns], rows[0], strict=True)) if rows else None


def session_facet_of(db, session: str = SESSION) -> dict | None:
    f = tables.session_facets
    return facet_of(db, f.c.kind == "session", f.c.session_id == session)


def user_of(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def grown_lines() -> list[dict]:
    return [*session_lines(), said("không, chạy lại test đó thôi", 15)]


def grown(hub, path) -> list:
    """The part of a transcript that grew from ``path``: its lines after ``path``'s."""
    longer = transcript(hub.tmp_path, grown_lines(), name="longer.jsonl")
    return parts_of(hub, longer, start=path.stat().st_size, line=len(session_lines()) + 1)


def seq_of(db, run_id: int, kind: str) -> int:
    e = tables.run_events
    return sql(db, select(func.max(e.c.seq)).where(e.c.run_id == run_id, e.c.kind == kind))[0][0]


def evidence(*texts: str) -> list[dict]:
    return [review_model.parse_evidence(text) for text in texts]


# The job


def test_the_job_analyzes_a_transcript_once_and_again_when_it_grows_or_the_version_changes(hub, monkeypatch):
    path = transcript(hub.tmp_path)
    assert statuses(send(hub, "owner", parts_of(hub, path))) == ["stored"]
    assert analyzed(hub.client) == {}, "a session that just sent a part may go on: it waits"
    assert analyzed(hub.client, later(hub.db)) == {"transcript": 1}
    found = session_facet_of(hub.db)
    t = tables.session_transcripts
    chain = sql(hub.db, select(t.c.sha256).where(t.c.session_id == SESSION))[0][0]
    assert (found["source"], found["version"], found["fingerprint"]) == (
        "transcript",
        1,
        fingerprint({("main", chain)}),
    )
    assert found["user_id"] == user_of(hub.db, "owner") and found["label"]["level"] == "internal"
    assert found["at"] == datetime(2026, 10, 8, 1, 0, 14, tzinfo=UTC), "the session's last line"
    tools = {item["gen_ai.tool.name"]: item for item in found["body"]["tools"]}
    assert (tools["Bash"]["calls"], tools["Bash"]["errors"]) == (3, 2)
    assert found["body"]["errors"][0] == {
        "gen_ai.tool.name": "Bash",
        "text": FAILED_TEST,
        "n": 1,
        "cause": "work",
        "at": "main:5",
    }, "line N on the hub is line N of the file"
    assert found["summary"] == facets.summary(found["body"]) and found["attempts"] == 0
    assert analyzed(hub.client, later(hub.db)) == {}, "once"

    assert statuses(send(hub, "owner", grown(hub, path))) == ["stored"]
    assert analyzed(hub.client, later(hub.db)) == {"transcript": 1}, "it grew"
    assert session_facet_of(hub.db)["body"]["corrections"] == [1]

    monkeypatch.setattr(facets, "FACET_VERSION", 2)
    assert analyzed(hub.client, later(hub.db)) == {"transcript": 1}, "a new version of the facet"
    assert session_facet_of(hub.db)["version"] == 2
    assert analyzed(hub.client, later(hub.db)) == {}


def test_a_blob_store_that_does_not_answer_fails_the_attempt_and_the_job_tries_again_later(hub, monkeypatch):
    path = transcript(hub.tmp_path)
    assert statuses(send(hub, "owner", parts_of(hub, path))) == ["stored"]
    store, now = hub.client.app.state.blobs, later(hub.db)

    def down(*args, **kwargs):
        raise BlobStoreUnavailable("GetObject failed: the blob store did not answer")

    monkeypatch.setattr(store, "fetch", down)
    assert analyzed(hub.client, now) == {"failed": 1}
    found = session_facet_of(hub.db)
    assert (found["body"], found["version"], found["attempts"]) == (None, 0, 1)
    assert "did not answer" in found["error"] and found["retry_at"] == now + timedelta(minutes=5)
    assert analyzed(hub.client, now + timedelta(minutes=1)) == {}, "it waits for its retry"
    assert analyzed(hub.client, now + timedelta(minutes=6)) == {"failed": 1}
    assert session_facet_of(hub.db)["retry_at"] == now + timedelta(minutes=16), "twice as long after each failure"
    monkeypatch.undo()
    assert analyzed(hub.client, now + timedelta(minutes=17)) == {"transcript": 1}
    found = session_facet_of(hub.db)
    assert (found["attempts"], found["error"], found["retry_at"], found["version"]) == (0, None, None, 1)

    assert statuses(send(hub, "owner", grown(hub, path))) == ["stored"]
    monkeypatch.setattr(store, "fetch", down)
    assert analyzed(hub.client, later(hub.db)) == {"failed": 1}
    kept = session_facet_of(hub.db)
    assert kept["body"] == found["body"] and kept["attempts"] == 1, "the facet made before stays"


def test_the_job_holds_no_lock_on_the_tables_it_reads_while_it_reads_the_blobs(hub, monkeypatch):
    path = transcript(hub.tmp_path)
    assert statuses(send(hub, "owner", parts_of(hub, path))) == ["stored"]
    store = hub.client.app.state.blobs
    real, checked = store.fetch, []
    t, f = tables.session_transcripts, tables.session_facets

    def fetch(key, sink, limit):
        with live.engine(hub.db).begin() as conn:  # another connection: NOWAIT fails at once on a row lock
            conn.execute(select(t.c.id).where(t.c.session_id == SESSION).with_for_update(nowait=True)).all()
            conn.execute(update(t).values(cwd="/work/app").where(t.c.session_id == SESSION))
            conn.execute(select(f.c.id).where(f.c.session_id == SESSION).with_for_update(nowait=True)).all()
        checked.append(key)
        return real(key, sink, limit)

    monkeypatch.setattr(store, "fetch", fetch)
    assert analyzed(hub.client, later(hub.db)) == {"transcript": 1}
    assert statuses(send(hub, "owner", grown(hub, path))) == ["stored"]
    assert analyzed(hub.client, later(hub.db)) == {"transcript": 1}
    assert len(checked) == 3, "every part was read while the rows stayed free"


def push(hub, session: str, body: dict) -> None:
    response = hub.client.put(f"/v1/projects/{PROJECT}/digests/{session}", json=body, headers=hub.headers["owner"])
    assert response.status_code == 200, response.text


def test_a_digest_gets_a_facet_until_transcripts_come_and_the_facet_outlives_their_content(hub):
    push(hub, SESSION, digest_body())
    assert analyzed(hub.client, later(hub.db)) == {"digest": 1}
    found = session_facet_of(hub.db)
    assert found["source"] == "digest" and found["body"]["errors"][0]["cause"] == "harness_blocked"
    assert analyzed(hub.client, later(hub.db)) == {}
    push(hub, SESSION, {**digest_body(), "messages": 20})
    assert analyzed(hub.client, later(hub.db)) == {"digest": 1}, "another digest"
    assert session_facet_of(hub.db)["body"]["messages"] == 20

    assert statuses(send(hub, "owner", parts_of(hub, transcript(hub.tmp_path)))) == ["stored"]
    assert analyzed(hub.client, later(hub.db)) == {"transcript": 1}
    made = session_facet_of(hub.db)
    assert made["source"] == "transcript"
    push(hub, SESSION, {**digest_body(), "messages": 30})
    assert analyzed(hub.client, later(hub.db)) == {}, "a digest never replaces a facet made from transcripts"

    deleted = hub.client.delete(f"/v1/projects/{PROJECT}/transcripts/{SESSION}", headers=hub.headers["admin"])
    assert deleted.status_code == 200, deleted.text
    assert analyzed(hub.client, later(hub.db)) == {}
    assert session_facet_of(hub.db) == made, "the facet outlives the transcript's content"


def local_events(client, headers, run_id: int, failure: str) -> None:
    stamp = datetime.now(UTC).isoformat()
    events = [
        {"seq": 1, "at": stamp, "kind": "agent_message_chunk", "body": {"text": "Fetching."}},
        {
            "seq": 2,
            "at": stamp,
            "kind": "tool_call",
            "body": {"toolCallId": "t1", "title": "Bash", "rawInput": {"command": "curl hub"}, "status": "in_progress"},
        },
        {
            "seq": 3,
            "at": stamp,
            "kind": "tool_call_update",
            "body": {
                "toolCallId": "t1",
                "title": "Bash",
                "status": "failed",
                "content": [{"type": "content", "content": {"type": "text", "text": failure}}],
            },
        },
    ]
    stored = client.post(local_path(run_id, "events"), json={"events": events}, headers=headers)
    assert stored.status_code == 200, stored.text


def ended_local_run(client, headers, session: str, failure: str = "curl: connection refused") -> int:
    run = registered(client, headers, session=session)
    local_events(client, headers, run["id"], failure)
    ended = client.post(local_path(run["id"], "state"), json={"state": "ended"}, headers=headers)
    assert ended.status_code == 200, ended.text
    return run["id"]


def test_a_run_that_ended_gets_a_facet_from_its_events_then_from_its_log_once_it_came(hub):
    run_id = ended_local_run(hub.client, hub.headers["owner"], LOCAL)
    assert analyzed(hub.client, later(hub.db)) == {"events": 1}
    f, r = tables.session_facets, tables.runs
    found = facet_of(hub.db, f.c.run_id == run_id)
    assert (found["kind"], found["session_id"], found["user_id"]) == ("run", LOCAL, user_of(hub.db, "owner"))
    assert found["body"]["errors"][0]["cause"] == "network" and found["label"] is None
    assert analyzed(hub.client, later(hub.db)) == {}

    log = hub.tmp_path / "events.jsonl"
    event = {
        "seq": 1,
        "at": "2026-10-08T01:00:00+00:00",
        "kind": "tool_call",
        "body": {"toolCallId": "x", "title": "Read"},
    }
    log.write_text(json.dumps(event) + "\nnot json\n", encoding="utf-8")
    sha = hashlib.sha256(log.read_bytes()).hexdigest()
    sql(hub.db, update(r).values(log_sha256="0" * 64).where(r.c.id == run_id))
    assert analyzed(hub.client, later(hub.db)) == {"events": 1}, "a log the store does not have: the events stand in"
    assert hub.client.app.state.blobs.put_file(sha, log)
    sql(hub.db, update(r).values(log_sha256=sha).where(r.c.id == run_id))
    assert analyzed(hub.client, later(hub.db)) == {"run_log": 1}
    found = facet_of(hub.db, f.c.run_id == run_id)
    assert found["source"] == "run_log" and found["fingerprint"] == f"{sha}:5"
    assert [item["gen_ai.tool.name"] for item in found["body"]["tools"]] == ["Read"]


def test_a_pass_stops_after_its_items_and_its_time_and_the_next_one_goes_on(hub):
    for index in range(3):
        push(hub, f"0199b1c2-7d3e-7a10-9c41-00000000000{index}", digest_body())
    assert analyzed(hub.client, later(hub.db), limit=2) == {"digest": 2}
    assert analyzed(hub.client, later(hub.db), seconds=-1) == {"later": 1}
    assert analyzed(hub.client, later(hub.db)) == {"digest": 1}


def test_the_facet_job_runs_every_five_minutes_one_pass_at_a_time():
    task = queue.tasks[jobs.ANALYZE_FACETS]
    assert (task.lock, task.queueing_lock) == (jobs.ANALYZE_FACETS, jobs.ANALYZE_FACETS)
    periodic = [item for item in queue.periodic_registry.periodic_tasks.values() if item.task is task]
    assert [item.cron for item in periodic] == ["*/5 * * * *"]


# The night's figures


def test_the_night_figures_count_the_sessions_from_their_facets_with_the_keys_they_had(review):
    client, db = review.client, review.db
    assert collected(client, NIGHT)["review"] == 1  # the digest has no facet yet: it counts as its facet would
    cf = tables.curator_figures
    ((before,),) = sql(db, select(cf.c.figures))
    assert analyzed(client, later(db)) == {"digest": 1}
    sql(db, delete(cf))  # the night counted again, now from the facet
    sql(db, update(tables.runs).values(state="cancelled", finished_at=func.now()))
    assert collected(client, NIGHT + timedelta(minutes=5))["review"] == 1
    ((after,),) = sql(db, select(cf.c.figures))
    for key in ("tools", "programs", "environment", "repeated_commands", "corrections", "sessions"):
        assert after[key] == before[key], key
    assert after["sessions"]["from_digests"] == 1 and after["sessions"]["transcripts"] == 0
    assert after["environment"][0]["evidence"] == [f"session:{DIGESTED}:errors:0"]
    assert set(after) == set(before) and {"weekly", "facets", "kg_sync", "lenses"} <= set(after)
    (week,) = after["weekly"]
    assert (week["sessions"], week["tool_calls"], week["tool_errors"], week["environment_errors"]) == (1, 49, 6, 5)
    assert after["facets"] == {
        "version": facets.FACET_VERSION,
        "sessions": 0,
        "analyzed": 0,
        "waiting": 0,
        "from_digests": 1,
        "runs": 1,
        "runs_analyzed": 0,
        "failed": 0,
        "llm": {"version": 1, "items": 0, "analyzed": 0, "waiting": 0, "failed": 0},
    }
    assert after["clusters"] == []


def figures_of(review, owner_id: int | None, since: datetime, until: datetime) -> dict:
    engine = review.client.app.state.engine

    async def count() -> dict:
        async with engine.connect() as conn:
            access = await _owner_access(conn, user_of(review.db, OWNER), OWNER, NIGHT_PROJECT)
            return await compute_figures(conn, access, since, until, owner_id)

    return review.client.portal.call(count)


def test_a_local_run_counts_only_when_its_member_owns_the_charter_and_no_digest_holds_its_session(review):
    client, db, headers = review.client, review.db, review.headers
    mine = ended_local_run(client, headers["owner"], LOCAL)
    ended_local_run(client, headers["other"], OTHER_LOCAL, "429 Too Many Requests")
    ended_local_run(client, headers["owner"], DIGESTED, "curl: could not resolve host")  # its digest counts it
    assert analyzed(client, later(db)) == {"digest": 1, "events": 3}
    since, until = later(db, -60), later(db, 60)
    owner = user_of(db, OWNER)
    figures = figures_of(review, owner, since, until)
    causes = {item["cause"]: item for item in figures["environment"]}
    assert causes["network"]["count"] == 1 and causes["network"]["runs"] == [mine]
    failed_at = seq_of(db, mine, "tool_call_update")
    assert causes["network"]["evidence"] == [f"run:{mine}:{failed_at}"], "the event the failure came in"
    assert "rate_limit" not in causes, "another member's local run stays out"
    assert figures["sessions"]["local_runs"] == 1 and figures["sessions"]["digests"] == 2
    assert figures_of(review, None, since, until)["sessions"]["local_runs"] == 0
    again = figures_of(review, owner, since, until)
    assert json.dumps(again, sort_keys=True) == json.dumps(figures, sort_keys=True), "the same data, the same figures"


def test_session_evidence_resolves_in_the_facet_and_a_local_run_of_the_owner_is_evidence(review):
    client, db, headers = review.client, review.db, review.headers
    mine = ended_local_run(client, headers["owner"], LOCAL)
    theirs = ended_local_run(client, headers["other"], OTHER_LOCAL)
    assert analyzed(client, later(db)) == {"digest": 1, "events": 2}
    sql(db, delete(tables.session_digests))
    run_id = held_review(review)
    worker = review.worker
    failed_at = seq_of(db, mine, "tool_call_update")
    ok = finding(client, worker, run_id, evidence=evidence(f"session:{DIGESTED}:errors:2", f"run:{mine}:{failed_at}"))
    assert ok.status_code == 201, ok.text
    assert [item["resolved"] for item in ok.json()["evidence"]] == ["facet", "run_event"]
    beyond = finding(client, worker, run_id, evidence=evidence(f"session:{DIGESTED}:errors:3"))
    assert beyond.status_code == 422 and "the facet's errors has 3 entries" in beyond.text
    assert finding(client, worker, run_id, evidence=evidence(f"run:{theirs}:{failed_at}")).status_code == 422
    unknown = finding(client, worker, run_id, evidence=evidence("session:0199a3c1-0000-7000-8000-0000000000ff"))
    assert unknown.status_code == 422 and "nor a facet of it" in unknown.text


# Schema 0054


def test_the_rows_of_0054_hold_together_and_it_goes_down_to_0053_and_up_again(hub):
    push(hub, SESSION, digest_body())
    assert analyzed(hub.client, later(hub.db)) == {"digest": 1}
    f = tables.session_facets
    for values in (
        {"kind": "commit"},
        {"kind": "run"},
        {"source": "memory"},
        {"version": -1},
        {"body": ["x"]},
        {"summary": "x"},
        {"session_id": "s" * 101},
        {"error": "x" * 2001},
    ):
        with pytest.raises(errors.CheckViolation):
            sql(hub.db, update(f).values(**values))
    project_id = sql(hub.db, select(f.c.project_id))[0][0]
    with pytest.raises(errors.UniqueViolation):  # one facet a session
        sql(hub.db, f.insert().values(project_id=project_id, kind="session", session_id=SESSION))
    move_to(hub.db, "0053", down=True)
    assert sql(hub.db, select(func.to_regclass("public.session_facets"))) == [(None,)]
    move_to(hub.db, "0054")
    assert sql(hub.db, select(func.count()).select_from(f)) == [(0,)]
    assert analyzed(hub.client, later(hub.db)) == {"digest": 1}, "the job makes them again"
