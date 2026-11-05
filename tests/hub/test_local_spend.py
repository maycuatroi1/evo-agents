"""The job hub.local_spend (``evo_agents.hub.local_spend``): a local run whose machine sent no usage reports gets its
spend from the transcripts the hub holds of its session, once a fingerprint, and a run with reports of its own is left
to them."""

from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select

from evo_agents.hub import jobs, spend_store, tables
from evo_agents.hub.local_spend import local_spend
from evo_agents.hub.worker import queue
from tests.hub.live import sql
from tests.hub.test_local_runs import path as local_path
from tests.hub.test_local_runs import registered
from tests.hub.test_session_transcripts import hub, parts_of, send, statuses, transcript  # noqa: F401 (hub: a fixture)

SESSION = "0199a3c1-0000-7000-8000-0000000000c1"
OPUS, HAIKU = "claude-opus-5-5", "claude-haiku-5-5"


def assistant(message_id: str, model: str, output: int, hour: int = 0) -> dict:
    usage = {
        "input_tokens": 2,
        "output_tokens": output,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": hour,
        "cache_creation": {"ephemeral_1h_input_tokens": hour, "ephemeral_5m_input_tokens": 0},
    }
    return {
        "type": "assistant",
        "sessionId": SESSION,
        "timestamp": "2026-11-05T01:00:00Z",
        "message": {"id": message_id, "model": model, "role": "assistant", "usage": usage, "content": []},
    }


COST_STATE = {
    "type": "cost-state",
    "sessionId": SESSION,
    "totalCostUSD": 0.9,
    "modelUsage": {
        OPUS: {"inputTokens": 4, "outputTokens": 3000, "cacheReadInputTokens": 200, "costUSD": 0.85},
        HAIKU: {"inputTokens": 50, "outputTokens": 20, "costUSD": 0.05},
    },
}
LINES = [
    assistant("m1", OPUS, 1000, 500),
    assistant("m1", OPUS, 1000, 500),  # a second block of the same message
    assistant("m2", OPUS, 2000),
    COST_STATE,
    assistant("m3", OPUS, 500),
]


def read(hub, minutes: int = 60) -> dict:
    now = sql(hub.db, select(func.now()))[0][0] + timedelta(minutes=minutes)
    state = hub.client.app.state
    return hub.client.portal.call(partial(local_spend, state.engine, state.blobs, now=now))


def spend_rows(db, run_id: int) -> dict:
    s = tables.run_spend
    rows = sql(
        db, select(s.c.model, s.c.output_tokens, s.c.reported_usd, s.c.estimated_usd).where(s.c.run_id == run_id)
    )
    return {model: (output, reported, estimated) for model, output, reported, estimated in rows}


def test_a_local_run_without_usage_reports_gets_its_spend_from_its_sessions_transcripts(hub):
    run = registered(hub.client, hub.headers["owner"], session=SESSION)
    path = transcript(hub.tmp_path, LINES)
    assert statuses(send(hub, "owner", parts_of(hub, path, session=SESSION))) == ["stored"]
    assert read(hub, minutes=0) == {}, "a session that just sent a part may go on: it waits"
    assert read(hub) == {"read": 1}
    rows = spend_rows(hub.db, run["id"])
    assert rows[OPUS][0] == 3500 and rows[OPUS][1] == pytest.approx(0.85), "the cost-state holds the calls before it"
    assert rows[OPUS][2] == pytest.approx(500 * 20 / 1e6 + 2 * 4 / 1e6 + 100 * 0.2 / 1e6), "the call after it priced"
    assert rows[HAIKU][1:] == (pytest.approx(0.05), None)
    shown = hub.client.get(f"/v1/projects/{run['project']}/runs/{run['id']}", headers=hub.headers["owner"]).json()
    assert shown["spend"]["reported_usd"] == pytest.approx(0.9) and shown["spend"]["sessions"] == 1
    assert read(hub) == {}, "once a fingerprint"
    hub.client.portal.call(partial(_record, hub.client.app.state.engine, run["id"]))
    assert spend_rows(hub.db, run["id"]) == rows, "a record with no report to read keeps the transcripts' rows"


async def _record(engine, run_id: int) -> None:
    async with engine.begin() as conn:
        await spend_store.record(conn, run_id)


def test_a_local_run_whose_machine_reports_its_usage_is_left_to_its_reports(hub):
    run = registered(hub.client, hub.headers["owner"], session=SESSION)
    body = {"source": "local", "scope": "delta", "session_id": SESSION, "models": {OPUS: {"outputTokens": 10}}}
    event = {"seq": 1, "at": datetime.now(UTC).isoformat(), "kind": "usage_update", "body": body}
    stored = hub.client.post(local_path(run["id"], "events"), json={"events": [event]}, headers=hub.headers["owner"])
    assert stored.status_code == 200, stored.text
    assert statuses(send(hub, "owner", parts_of(hub, transcript(hub.tmp_path, LINES), session=SESSION))) == ["stored"]
    assert read(hub) == {}
    assert spend_rows(hub.db, run["id"]) == {OPUS: (10, None, pytest.approx(10 * 20 / 1e6))}


def test_the_job_runs_every_five_minutes_one_pass_at_a_time():
    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.LOCAL_SPEND].cron == "*/5 * * * *" and periodic[jobs.RECORD_SPEND].cron == "* * * * *"
    assert queue.tasks[jobs.LOCAL_SPEND].queueing_lock == jobs.LOCAL_SPEND
