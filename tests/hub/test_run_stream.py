"""A run's log on the hub: the worker's batches of events, reading them, the live stream, the owner's messages and
takeover, the run list, and the log and diff the worker uploads.

The checks step 6 of the worker-fleet plan names: the stream gets a new event within 2 seconds; a stream resumed
with Last-Event-ID misses nothing and repeats nothing; a batch sent again is stored once and ack_seq stops at a gap
in the seq; a body over 64 KiB is cut; someone without a grant gets 404; a reader sending a message gets 403; a
takeover shows in the next heartbeat; the worker commits a run-log blob, and a kind it does not take is refused. And
from step 18, a claim whose worker hung up leases nothing, whether it hung up while the claim waited or while it
leased a run.

The hub runs under uvicorn in a thread of this process, as ``hub serve`` runs it: the stream needs a server that
sends a response while it is still being written, a claim needs one that sees its client go, and the module's
constants can be patched."""

import asyncio
import json
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import fastapi.routing
import fastapi.sse
import httpx
import uvicorn
from sqlalchemy import column, func, select, table, update

from evo_agents.hub import runs, tables
from evo_agents.hub.server import listen, run_events
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.runs.service import claims as run_claims
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.s3 import get_url, put_presigned
from tests.hub.test_plans import registration
from tests.hub.test_runs import (
    OTHER,
    OWNER,
    PASSED,
    PLAN,
    PROJECT,
    PROTOCOL,
    READ_ONLY,
    READER,
    SHA,
    STRANGER,
    WRITER,
    add_worker,
    beat,
    claim,
    dispatched,
    moved,
    plan_body,
)

PUBLIC_READER = "public-reader"
EVENTS = f"/v1/projects/{PROJECT}/runs/{{run_id}}/events"
STREAM = f"/v1/projects/{PROJECT}/runs/{{run_id}}/stream"
CUT = "[cut by the hub: the event was over 65536 bytes]"


@contextmanager
def serving(app):
    """``app`` under uvicorn on a free port, in a thread; its URL until the block ends."""
    port = pg.free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, lifespan="on", log_config=None, access_log=False, timeout_graceful_shutdown=2
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="hub-under-test", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert thread.is_alive(), "the hub did not start"
        assert time.monotonic() < deadline, "the hub did not start within 30s"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(30)
        assert not thread.is_alive(), "the hub did not stop"


@pytest.fixture
def start(hub_db, tmp_path, github):
    """Start the hub with the configuration changes given; the members of ``members`` signed in on it."""
    with ExitStack() as stack:

        def started(**changes) -> SimpleNamespace:
            app = create_app(live.hub_config(hub_db, tmp_path, github, **changes))
            url = stack.enter_context(serving(app))
            client = stack.enter_context(httpx.Client(base_url=url, timeout=10))
            return SimpleNamespace(app=app, client=client, headers=members(client, github), db=hub_db)

        yield started


@pytest.fixture
def hub(start) -> SimpleNamespace:
    return start()


def members(client, github) -> dict:
    """As ``tests.hub.test_runs.hub``: project evo-agents with writers owner and someone-else, readers reader and
    public-reader (who reads up to public only), the plan rollout pushed by owner; the headers of each member, of a
    stranger and of a hub admin without a grant."""
    logins = {
        "admin": ADMIN,
        "owner": OWNER,
        "other": OTHER,
        "reader": READER,
        "public": PUBLIC_READER,
        "stranger": STRANGER,
    }
    headers = {
        name: bearer(live.sign_in(client, github, login, number)["token"])
        for number, (name, login) in enumerate(logins.items(), start=701)
    }
    assert client.put(f"/v1/projects/{PROJECT}", json=registration(), headers=headers["admin"]).status_code == 200
    public = {"role": "reader", "max_level": "public"}
    for login, grant in ((OWNER, WRITER), (OTHER, WRITER), (READER, READ_ONLY), (PUBLIC_READER, public)):
        response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
        assert response.status_code == 200, response.text
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/{PLAN}", json={"body": plan_body()}, headers=headers["owner"])
    assert pushed.status_code == 200, pushed.text
    return headers


def running(hub, step: int = 2, **dispatch) -> tuple[dict, int]:
    """A worker of owner and a run of ``step`` it claimed and started: its state events are seq 1 and 2."""
    worker = add_worker(hub.client, hub.headers["owner"], f"mac-{step}")
    run_id = dispatched(hub.client, hub.headers["owner"], [step], **dispatch)[0]["id"]
    assert claim(hub.client, worker)["id"] == run_id
    moved(hub.client, worker, run_id, "running")
    return worker, run_id


def event(seq: int, text: str = "", kind: str = "agent_message_chunk", **body) -> dict:
    at = datetime.now(UTC).isoformat()
    return {"seq": seq, "at": at, "kind": kind, "body": {"text": text or f"chunk {seq}", **body}}


def send(hub, worker: dict, run_id: int, *events: dict):
    return hub.client.post(f"/v1/worker/runs/{run_id}/events", json={"events": list(events)}, headers=worker["headers"])


def sent(hub, worker: dict, run_id: int, *events: dict) -> dict:
    response = send(hub, worker, run_id, *events)
    assert response.status_code == 200, response.text
    return response.json()


def read_events(hub, run_id: int, who: str = "reader", **params) -> dict:
    response = hub.client.get(EVENTS.format(run_id=run_id), params=params, headers=hub.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


def worker_texts(hub, run_id: int) -> list[str]:
    return [e["body"]["text"] for e in read_events(hub, run_id)["events"] if e["kind"] == "agent_message_chunk"]


class Stream:
    """One response of server-sent events, read a message at a time."""

    def __init__(self, hub, run_id: int, who: str = "reader", headers=None, **params):
        self._context = hub.client.stream(
            "GET", STREAM.format(run_id=run_id), params=params, headers={**hub.headers[who], **(headers or {})}
        )
        self.response = self._context.__enter__()
        self._lines = self.response.iter_lines()

    def next(self, *, pings: bool = False) -> dict:
        """The next message as {field: value}, ``data`` decoded; comments only when ``pings``. EOFError when the
        stream ended."""
        while True:
            message: dict = {}
            for line in self._lines:
                if line == "":
                    if message:
                        break
                    continue
                field, _, value = line.partition(":")
                message[field or "comment"] = value.removeprefix(" ")
            else:
                raise EOFError("the stream ended")
            if set(message) == {"comment"} and not pings:
                continue
            if "data" in message:
                message["data"] = json.loads(message["data"])
            return message

    def events(self, count: int) -> list[dict]:
        return [self.next() for _ in range(count)]

    def close(self) -> None:
        self._context.__exit__(None, None, None)


def seqs(messages: list[dict]) -> list[int]:
    assert all(int(m["id"]) == m["data"]["seq"] for m in messages)
    return [m["data"]["seq"] for m in messages]


def wait_for(condition, timeout: float = 5.0, what: str = "the condition"):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"{what} did not hold within {timeout}s"
        time.sleep(0.05)


# The stream


def test_the_stream_gets_a_new_event_within_two_seconds(hub, monkeypatch):
    monkeypatch.setattr(run_events, "STREAM_POLL_SECONDS", 60.0)  # only the notification can wake it in time
    worker, run_id = running(hub)
    stream = Stream(hub, run_id)
    try:
        assert stream.response.status_code == 200
        assert stream.response.headers["content-type"].startswith("text/event-stream")
        # a proxy in between neither caches nor compresses it, which would hold the events back
        assert stream.response.headers.get_list("cache-control") == ["no-cache, no-transform"]
        assert stream.response.headers["x-accel-buffering"] == "no"
        first = stream.events(2)
        assert seqs(first) == [1, 2]
        assert [(m["data"]["kind"], m["data"]["body"]["to"]) for m in first] == [
            ("state", "leased"),
            ("state", "running"),
        ]
        for number in (1, 2):
            sent_at = time.monotonic()
            assert sent(hub, worker, run_id, event(number, f"hello {number}"))["ack_seq"] == number
            message = stream.next()
            assert time.monotonic() - sent_at < 2
            assert message["id"] == str(number + 2)
            assert message["data"]["kind"] == "agent_message_chunk"
            assert message["data"]["body"] == {"text": f"hello {number}"}
            assert message["data"]["truncated"] is False
        # the owner's message and the worker's moves come down the same stream, numbered by the hub
        sent_at = time.monotonic()
        posted = hub.client.post(
            f"/v1/projects/{PROJECT}/runs/{run_id}/messages",
            json={"text": "also run ruff"},
            headers=hub.headers["owner"],
        )
        assert posted.status_code == 201, posted.text
        message = stream.next()
        assert time.monotonic() - sent_at < 2
        assert (message["data"]["seq"], message["data"]["kind"]) == (5, "user_message")
        assert message["data"]["body"] == {"text": "also run ruff", "from": OWNER, "message_id": posted.json()["id"]}
        moved(hub.client, worker, run_id, "verifying")
        assert stream.next()["data"]["body"]["to"] == "verifying"
    finally:
        stream.close()
    wait_for(lambda: hub.app.state.run_streams.open_streams() == 0, what="the closed stream ending on the hub")


def test_a_stream_resumed_with_last_event_id_misses_and_repeats_nothing_and_ends_with_the_run(hub):
    worker, run_id = running(hub, approval="auto")
    sent(hub, worker, run_id, *(event(n) for n in range(1, 4)))  # hub seqs 3 to 5
    stream = Stream(hub, run_id)
    try:
        before = seqs(stream.events(4))
    finally:
        stream.close()
    assert before == [1, 2, 3, 4]
    sent(hub, worker, run_id, *(event(n) for n in range(4, 8)))  # hub seqs 6 to 9, while nobody listened
    resumed = Stream(hub, run_id, headers={"Last-Event-ID": "4"})
    try:
        middle = seqs(resumed.events(5))
        sent(hub, worker, run_id, event(8))  # seq 10, live
        middle += seqs(resumed.events(1))
        moved(hub.client, worker, run_id, "verifying", "done", verify=PASSED, commit_sha=SHA)  # seqs 11 and 12
        tail = resumed.events(2)
        middle += seqs(tail)
        assert [m["data"]["body"]["to"] for m in tail] == ["verifying", "done"]
        end = resumed.next()
        assert (end["event"], end["data"]) == ("end", {"state": "done", "last_seq": 12})
        with pytest.raises(EOFError):
            resumed.next()
    finally:
        resumed.close()
    assert before + middle == list(range(1, 13))  # nothing missed, nothing twice
    # after= starts a first connection; Last-Event-ID wins over it, as a reconnecting browser keeps its URL
    late = Stream(hub, run_id, after=10)
    try:
        assert seqs(late.events(2)) == [11, 12] and late.next()["event"] == "end"
    finally:
        late.close()
    again = Stream(hub, run_id, headers={"Last-Event-ID": "12"}, after=3)
    try:
        assert again.next() == {"event": "end", "data": {"state": "done", "last_seq": 12}}
    finally:
        again.close()
    page = read_events(hub, run_id, after=4, limit=3)
    assert ([e["seq"] for e in page["events"]], page["more"], page["last_seq"], page["state"]) == (
        [5, 6, 7],
        True,
        12,
        "done",
    )
    states = read_events(hub, run_id, kind="state")["events"]
    assert [e["seq"] for e in states] == [1, 2, 11, 12]


def test_an_idle_stream_gets_a_ping_and_one_listen_connection_serves_claims_and_streams(hub, monkeypatch):
    assert fastapi.sse._PING_INTERVAL == 15.0  # what docs/workers.md promises
    monkeypatch.setattr(fastapi.routing, "_PING_INTERVAL", 0.5)
    worker, run_id = running(hub)
    stream = Stream(hub, run_id)
    waiting = {}
    claimer = threading.Thread(target=lambda: waiting.update(run=claim(hub.client, worker, wait=3)))
    try:
        assert seqs(stream.events(2)) == [1, 2]
        assert stream.next(pings=True) == {"comment": "ping"}
        claimer.start()
        time.sleep(0.5)
        activity = table("pg_stat_activity", column("datname"), column("application_name"))
        listening = sql(
            hub.db,
            select(func.count())
            .select_from(activity)
            .where(activity.c.datname == hub.db.name, activity.c.application_name == listen.APPLICATION_NAME),
        )
        assert listening == [(1,)]
    finally:
        claimer.join(10)
        stream.close()
    assert waiting == {"run": None}


# A claim whose worker hung up


def hang_up_claim(hub, worker: dict, wait: float, after: float) -> None:
    """A claim that waits up to ``wait`` seconds, whose client hangs up after ``after`` seconds, as a daemon that
    stops drops the claim it waits on."""
    with httpx.Client(base_url=str(hub.client.base_url), timeout=after) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.post("/v1/worker/claim", json={"wait_s": wait}, headers=worker["headers"])


def claims_waiting(hub) -> dict:
    return dict(hub.app.state.run_wakeups._tickets)  # worker id: its claim waiting in this process


def test_a_claim_whose_worker_hung_up_while_it_waited_leaves_the_next_run_queued(hub, monkeypatch):
    monkeypatch.setattr(run_claims, "CLAIM_POLL_SECONDS", 60.0)  # only the notification wakes the claim
    worker = add_worker(hub.client, hub.headers["owner"], "mac-mini")
    hang_up_claim(hub, worker, wait=20, after=1)
    assert worker["id"] in claims_waiting(hub), "the hub's side of the claim waits on"
    run_id = dispatched(hub.client, hub.headers["owner"], [2])[0]["id"]
    wait_for(lambda: worker["id"] not in claims_waiting(hub), what="the abandoned claim to end")
    r = tables.runs
    assert sql(hub.db, select(r.c.state, r.c.worker_id, r.c.lease_expires_at).where(r.c.id == run_id)) == [
        ("queued", None, None)
    ]
    assert claim(hub.client, worker)["id"] == run_id, "the next claim takes it at once"


def test_a_claim_whose_worker_hangs_up_while_it_leases_a_run_rolls_the_lease_back(hub, monkeypatch):
    worker = add_worker(hub.client, hub.headers["owner"], "mac-mini")
    run_id = dispatched(hub.client, hub.headers["owner"], [2])[0]["id"]
    spec_of = run_claims._run_spec

    async def slow_spec(conn, leased_id):
        await asyncio.sleep(1.5)  # the run is leased in the claim's transaction; the worker hangs up meanwhile
        return await spec_of(conn, leased_id)

    monkeypatch.setattr(run_claims, "_run_spec", slow_spec)
    hang_up_claim(hub, worker, wait=0, after=0.5)
    wait_for(lambda: worker["id"] not in claims_waiting(hub), what="the abandoned claim to end")
    r, e = tables.runs, tables.run_events
    assert sql(hub.db, select(r.c.state, r.c.worker_id, r.c.event_seq).where(r.c.id == run_id)) == [
        ("queued", None, 0)
    ], "the lease, its state event and its seq were rolled back"
    assert sql(hub.db, select(func.count()).select_from(e).where(e.c.run_id == run_id)) == [(0,)]

    monkeypatch.setattr(run_claims, "_run_spec", spec_of)
    assert claim(hub.client, worker)["id"] == run_id
    assert [(e["seq"], e["body"]["to"]) for e in read_events(hub, run_id)["events"]] == [(1, "leased")]


# The worker's batches


def test_a_batch_sent_again_is_stored_once_and_ack_seq_stops_at_a_gap(hub):
    worker, run_id = running(hub)
    assert sent(hub, worker, run_id, *(event(n) for n in (1, 2, 3))) == {"ack_seq": 3, "stored": 3}
    assert sent(hub, worker, run_id, *(event(n) for n in (1, 2, 3))) == {"ack_seq": 3, "stored": 0}
    assert sent(hub, worker, run_id, event(5), event(6)) == {"ack_seq": 3, "stored": 0}  # 4 is missing
    assert sent(hub, worker, run_id, event(6), event(4), event(5)) == {"ack_seq": 6, "stored": 3}  # in seq order
    assert sent(hub, worker, run_id, event(5), event(7), event(7), event(9)) == {"ack_seq": 7, "stored": 1}
    assert sent(hub, worker, run_id) == {"ack_seq": 7, "stored": 0}
    assert worker_texts(hub, run_id) == [f"chunk {n}" for n in range(1, 8)]
    r, e = tables.runs, tables.run_events
    stored = sql(hub.db, select(e.c.seq, e.c.kind).where(e.c.run_id == run_id).order_by(e.c.seq))
    assert [seq for seq, _ in stored] == list(range(1, 10))  # the hub's own numbers, after its two state events
    assert sql(hub.db, select(r.c.event_seq, r.c.events_acked).where(r.c.id == run_id)) == [(9, 7)]

    # what a worker may not send: the hub's kinds, a naive time, a body that is no object, too many events
    for bad in (
        event(8, kind="state"),
        event(8, kind="user_message"),
        {**event(8), "at": "2026-10-05T10:00:00"},
        {**event(8), "body": "raw text"},
        {**event(8), "seq": 0},
    ):
        assert send(hub, worker, run_id, bad).status_code == 422, bad
    assert send(hub, worker, run_id, *(event(n) for n in range(8, 8 + runs.MAX_BATCH_EVENTS + 1))).status_code == 422
    large = send(hub, worker, run_id, *(event(n, "x" * 300_000) for n in range(8, 12)))
    assert large.status_code == 413 and large.json()["detail"] == [{"limit": "batch_bytes"}]
    # a time ahead of the hub's clock is taken as the hub's now
    future = {**event(8), "at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
    assert sent(hub, worker, run_id, future)["ack_seq"] == 8
    ((at,),) = sql(hub.db, select(e.c.at).where(e.c.run_id == run_id, e.c.seq == 10))
    assert at <= datetime.now(UTC)

    # only the worker that claimed the run sends its events
    other = add_worker(hub.client, hub.headers["owner"], "linux-box")
    assert send(hub, other, run_id, event(9)).status_code == 404
    assert send(hub, worker, 999999, event(1)).status_code == 404
    as_member = {**hub.headers["owner"], **PROTOCOL}
    assert (
        hub.client.post(f"/v1/worker/runs/{run_id}/events", json={"events": []}, headers=as_member).status_code == 403
    )

    # a run keeps at most 20,000 events: a batch beyond refuses everything and says where the ack stands
    sql(hub.db, update(r).values(event_seq=runs.MAX_RUN_EVENTS - 1).where(r.c.id == run_id))
    full = send(hub, worker, run_id, event(9), event(10))
    assert full.status_code == 413
    assert full.json()["detail"] == [{"limit": "events_per_run", "max": runs.MAX_RUN_EVENTS, "ack_seq": 8}]
    assert sent(hub, worker, run_id, event(9)) == {"ack_seq": 9, "stored": 1}  # one more still fits
    # the spool may be sent once the run ended, by the worker that held it
    moved(hub.client, worker, run_id, "failed", error="the agent stopped")
    sql(hub.db, update(r).values(event_seq=20).where(r.c.id == run_id))
    assert sent(hub, worker, run_id, event(10)) == {"ack_seq": 10, "stored": 1}


def test_a_body_over_64_kib_is_cut_and_marked_truncated(hub):
    worker, run_id = running(hub)
    big = "é" * 40_000 + "x" * 40_000  # 120,000 bytes of UTF-8
    sent(hub, worker, run_id, event(1, big, tool="Bash"), event(2, "x" * 65_000))
    first, second = read_events(hub, run_id, after=2)["events"]
    assert first["truncated"] is True
    assert len(json.dumps(first["body"], ensure_ascii=False, separators=(",", ":")).encode()) <= 64 * 1024
    assert first["body"]["tool"] == "Bash"  # the keys stay; the long text ends early, with a mark
    assert first["body"]["text"].startswith("é" * 1000) and first["body"]["text"].endswith(CUT)
    assert second["truncated"] is False and second["body"]["text"] == "x" * 65_000
    many = {str(n): n for n in range(12_000)}  # no long string to cut: the start of the JSON is kept as text
    sent(hub, worker, run_id, {**event(3), "body": many})
    (third,) = read_events(hub, run_id, after=4)["events"]
    assert third["truncated"] is True and list(third["body"]) == ["cut"]
    assert third["body"]["cut"].startswith('{"0":0,"1":1,') and third["body"]["cut"].endswith(CUT)


# Who reads


def test_someone_without_a_grant_gets_404_and_a_reader_reads_runs_of_plans_it_may_read(hub):
    worker, run_id = running(hub)
    sent(hub, worker, run_id, event(1))
    paths = (
        f"/v1/projects/{PROJECT}/runs",
        f"/v1/projects/{PROJECT}/runs/{run_id}",
        EVENTS.format(run_id=run_id),
        STREAM.format(run_id=run_id),
    )
    for path in paths:
        assert hub.client.get(path, headers=hub.headers["stranger"]).status_code == 404, path
        assert hub.client.get(path, headers=hub.headers["admin"]).status_code == 403, path  # an admin without a grant
    for path in paths[1:]:
        # a reader whose grant stops below the plan's label is told what a stranger is told
        assert hub.client.get(path, headers=hub.headers["public"]).status_code == 404, path
        assert hub.client.get(path.replace(str(run_id), "999999"), headers=hub.headers["reader"]).status_code == 404
    listed = hub.client.get(paths[0], headers=hub.headers["public"]).json()
    assert (listed["runs"], listed["total"]) == ([], 0)
    shown = hub.client.get(paths[1], headers=hub.headers["reader"]).json()
    assert {key: shown[key] for key in ("id", "title", "state", "last_seq", "log_sha256", "diff_sha256")} == {
        "id": run_id,
        "title": "Queue",
        "state": "running",
        "last_seq": 3,
        "log_sha256": None,
        "diff_sha256": None,
    }
    assert worker_texts(hub, run_id) == ["chunk 1"]


def test_the_run_list_filters_searches_pages_and_counts_by_state(hub):
    worker, two = running(hub)
    four, five = (dispatched(hub.client, hub.headers["owner"], [key])[0]["id"] for key in (4, 5))
    assert (
        hub.client.post(f"/v1/projects/{PROJECT}/runs/{five}/cancel", headers=hub.headers["owner"]).status_code == 200
    )
    moved(hub.client, worker, two, "failed", error="ruff found 3 problems")
    path = f"/v1/projects/{PROJECT}/runs"

    def listed(**params) -> dict:
        response = hub.client.get(path, params=params, headers=hub.headers["reader"])
        assert response.status_code == 200, response.text
        return response.json()

    every = listed()
    assert [run["id"] for run in every["runs"]] == [five, four, two]  # newest first
    assert (every["total"], every["limit"], every["offset"]) == (3, 50, 0)
    assert {state: n for state, n in every["counts"].items() if n} == {"queued": 1, "cancelled": 1, "failed": 1}
    assert set(every["counts"]) == set(runs.RUN_STATES)
    # the counts follow the other filters but not the state one, so the facets show what each state would leave
    queued = listed(state="queued")
    assert ([run["id"] for run in queued["runs"]], queued["total"]) == ([four], 1)
    assert queued["counts"] == every["counts"]
    both = listed(state=["queued", "failed"])
    assert ([run["id"] for run in both["runs"]], both["total"]) == ([four, two], 2)
    by_step = listed(plan_id=PLAN, step="2")
    assert [run["id"] for run in by_step["runs"]] == [two] and by_step["counts"]["queued"] == 0
    assert listed(plan_id="another-plan")["total"] == 0
    assert [run["id"] for run in listed(worker_id=worker["id"])["runs"]] == [two]
    assert listed(dispatched_by=OWNER.upper())["total"] == 3 and listed(dispatched_by=OTHER)["total"] == 0
    # text: the title, the step, the error, the worker, a run number; the caller's wildcards are taken literally
    assert [run["id"] for run in listed(q="docs")["runs"]] == [four]
    assert [run["id"] for run in listed(q="RUFF")["runs"]] == [two]
    assert [run["id"] for run in listed(q="mac-2")["runs"]] == [two]
    assert [run["id"] for run in listed(q=f"#{five}")["runs"]] == [five]
    assert listed(q="%")["total"] == 0 and listed(q="_")["total"] == 0
    page = listed(limit=1, offset=1)
    assert ([run["id"] for run in page["runs"]], page["total"], page["limit"], page["offset"]) == ([four], 3, 1, 1)
    assert hub.client.get(path, params={"state": "sleeping"}, headers=hub.headers["reader"]).status_code == 422
    assert hub.client.get(path, params={"limit": 201}, headers=hub.headers["reader"]).status_code == 422


# Messages and takeover


def test_a_reader_sending_a_message_gets_403_and_the_owners_reaches_the_worker(hub):
    worker, run_id = running(hub)
    path = f"/v1/projects/{PROJECT}/runs/{run_id}/messages"
    for who, status in (("reader", 403), ("other", 403), ("admin", 403), ("stranger", 404)):
        refused = hub.client.post(path, json={"text": "stop"}, headers=hub.headers[who])
        assert refused.status_code == status, (who, refused.text)
    assert "only owner" in hub.client.post(path, json={"text": "x"}, headers=hub.headers["reader"]).json()["message"]
    for text in ("", "   ", "é" * (runs.MAX_MESSAGE_BYTES // 2 + 1)):
        assert hub.client.post(path, json={"text": text}, headers=hub.headers["owner"]).status_code == 422
    first = hub.client.post(path, json={"text": "also run ruff"}, headers=hub.headers["owner"])
    assert first.status_code == 201, first.text
    second = hub.client.post(path, json={"text": "é" * (runs.MAX_MESSAGE_BYTES // 2)}, headers=hub.headers["owner"])
    assert second.status_code == 201
    assert {key: first.json()[key] for key in ("run_id", "seq", "text", "sent_by", "delivered_at")} == {
        "run_id": run_id,
        "seq": 3,
        "text": "also run ruff",
        "sent_by": OWNER,
        "delivered_at": None,
    }
    assert beat(hub.client, worker, runs_held=[run_id])["runs"][0]["inbox"] == 2
    inbox = f"/v1/worker/runs/{run_id}/inbox"
    taken = hub.client.post(inbox, headers=worker["headers"])
    assert taken.status_code == 200
    assert [(m["id"], m["text"][:5], m["sent_by"]) for m in taken.json()["messages"]] == [
        (first.json()["id"], "also ", OWNER),
        (second.json()["id"], "é" * 5, OWNER),
    ]
    acked = hub.client.post(inbox, json={"ack": first.json()["id"]}, headers=worker["headers"]).json()
    assert [m["id"] for m in acked["messages"]] == [second.json()["id"]]
    assert beat(hub.client, worker, runs_held=[run_id])["runs"][0]["inbox"] == 1
    assert hub.client.post(inbox, json={"ack": second.json()["id"]}, headers=worker["headers"]).json() == {
        "messages": []
    }
    assert beat(hub.client, worker, runs_held=[run_id])["runs"][0]["inbox"] == 0
    other = add_worker(hub.client, hub.headers["owner"], "linux-box")
    assert hub.client.post(inbox, headers=other["headers"]).status_code == 404
    a = tables.audit
    audited = sql(hub.db, select(a.c.target).where(a.c.action == "run.message").order_by(a.c.id))
    assert audited == [
        (f"{PROJECT}/{PLAN}#2 run:{run_id} message:{first.json()['id']}",),
        (f"{PROJECT}/{PLAN}#2 run:{run_id} message:{second.json()['id']}",),
    ]
    # a message waits only for an agent that may still read it
    moved(hub.client, worker, run_id, "failed", error="the agent stopped")
    late = hub.client.post(path, json={"text": "too late"}, headers=hub.headers["owner"])
    assert late.status_code == 409 and "is failed" in late.json()["message"]
    assert hub.client.post(inbox, headers=worker["headers"]).status_code == 404


def test_a_takeover_shows_in_the_next_heartbeat_and_a_handback_after_it(hub):
    worker, run_id = running(hub)

    def control(action: str, who: str = "owner"):
        return hub.client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/{action}", headers=hub.headers[who])

    def flags() -> tuple[bool, bool]:
        (answer,) = beat(hub.client, worker, runs_held=[run_id])["runs"]
        return answer["takeover"], answer["handback"]

    assert flags() == (False, False)
    for who, status in (("reader", 403), ("other", 403), ("stranger", 404)):
        assert control("takeover", who).status_code == status, who
    early = control("handback")
    assert early.status_code == 409 and "runs headless" in early.json()["message"]
    for _ in range(2):  # asked twice: the same answer, one audit row
        asked = control("takeover")
        assert asked.status_code == 200 and asked.json()["takeover_requested_at"] is not None
    assert flags() == (True, False)
    assert moved(hub.client, worker, run_id, "interactive")["takeover_requested_at"] is None
    assert flags() == (False, False)
    twice = control("takeover")
    assert twice.status_code == 409 and "a person drives it already" in twice.json()["message"]
    assert control("handback").json()["handback_requested_at"] is not None
    assert flags() == (False, True)
    assert moved(hub.client, worker, run_id, "running")["handback_requested_at"] is None
    assert flags() == (False, False)
    # an ask the worker has not acted on goes with the run's next move out of the states it was asked in
    control("takeover")
    assert moved(hub.client, worker, run_id, "verifying")["takeover_requested_at"] is None
    over = control("takeover")
    assert over.status_code == 409 and "no agent of it runs now" in over.json()["message"]
    a = tables.audit
    actions = [row[0] for row in sql(hub.db, select(a.c.action).where(a.c.action.like("run.%")).order_by(a.c.id))]
    assert actions == ["run.dispatch", "run.takeover", "run.handback", "run.takeover"]


# The log and the diff


def test_the_worker_commits_the_run_log_and_diff_and_a_kind_it_does_not_take_is_refused(start, s3):
    hub = start(**s3.config())
    worker, run_id = running(hub)
    log_bytes = b'{"seq":1,"kind":"agent_message_chunk"}\n' * 100
    diff_bytes = b"diff --git a/x b/x\n+hello\n"
    items = [
        {"sha256": sha(log_bytes), "size": len(log_bytes), "kind": "run-log"},
        {"sha256": sha(diff_bytes), "size": len(diff_bytes), "kind": "run-diff"},
    ]
    uploads = f"/v1/worker/runs/{run_id}/uploads"
    for kind in ("kg-log", "skill-bundle", "bogus"):
        refused = hub.client.post(uploads, json={"items": [{**items[0], "kind": kind}]}, headers=worker["headers"])
        assert refused.status_code == 422, kind
    twice = hub.client.post(
        uploads, json={"items": [items[0], {**items[1], "kind": "run-log"}]}, headers=worker["headers"]
    )
    assert twice.status_code == 422
    over = {**items[1], "size": 8 * 1024 * 1024 + 1}
    assert hub.client.post(uploads, json={"items": [over]}, headers=worker["headers"]).status_code == 413
    other = add_worker(hub.client, hub.headers["owner"], "linux-box")
    assert hub.client.post(uploads, json={"items": items}, headers=other["headers"]).status_code == 404

    diff_path = f"/v1/projects/{PROJECT}/runs/{run_id}/diff"
    missing = hub.client.get(diff_path, headers=hub.headers["reader"])
    assert missing.status_code == 404 and "no diff" in missing.json()["message"]
    moved(hub.client, worker, run_id, "verifying", "review", verify=PASSED, commit_sha=SHA)  # the run has ended
    asked = hub.client.post(uploads, json={"items": items}, headers=worker["headers"])
    assert asked.status_code == 200, asked.text
    tickets = {ticket["sha256"]: ticket for ticket in asked.json()["uploads"]}
    for data in (log_bytes, diff_bytes):
        assert put_presigned(tickets[sha(data)]["url"], data) == 200
    ids = [ticket["upload_id"] for ticket in tickets.values()]
    committed = hub.client.post(f"/v1/worker/runs/{run_id}/blobs", json={"upload_ids": ids}, headers=worker["headers"])
    assert committed.status_code == 200, committed.text
    assert committed.json() == {"log_sha256": sha(log_bytes), "diff_sha256": sha(diff_bytes)}
    b = tables.blobs
    blobs = sql(hub.db, select(b.c.kind, b.c.size).order_by(b.c.kind))
    assert blobs == [("run-diff", len(diff_bytes)), ("run-log", len(log_bytes))]
    shown = hub.client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=hub.headers["reader"]).json()
    assert (shown["log_sha256"], shown["diff_sha256"]) == (sha(log_bytes), sha(diff_bytes))

    link = hub.client.get(diff_path, headers=hub.headers["reader"])
    assert link.status_code == 200, link.text
    assert (link.json()["sha256"], link.json()["size"]) == (sha(diff_bytes), len(diff_bytes))
    assert get_url(link.json()["url"]) == diff_bytes
    download = hub.client.get(diff_path, params={"download": "true"}, headers=hub.headers["reader"]).json()
    assert "run-" in download["url"] and "attachment" in download["url"]
    for who, status in (("stranger", 404), ("public", 404), ("admin", 403)):
        assert hub.client.get(diff_path, headers=hub.headers[who]).status_code == status, who

    # an upload of another kind is unknown to the run's commit, though its owner asked for it
    stray = b"a knowledge graph log"
    ticket = hub.client.post(
        "/v1/blobs/uploads",
        json={"project": PROJECT, "items": [{"sha256": sha(stray), "size": len(stray), "kind": "kg-log"}]},
        headers=hub.headers["owner"],
    ).json()["uploads"][0]
    assert put_presigned(ticket["url"], stray) == 200
    unknown = hub.client.post(
        f"/v1/worker/runs/{run_id}/blobs", json={"upload_ids": [ticket["upload_id"]]}, headers=worker["headers"]
    )
    assert unknown.status_code == 422 and "unknown uploads" in unknown.json()["message"]
    # members upload the run kinds through the blob routes too, within the kinds' limits
    member = hub.client.post(
        "/v1/blobs/uploads",
        json={"project": PROJECT, "items": [{"sha256": sha(b"x"), "size": 64 * 1024 * 1024 + 1, "kind": "run-log"}]},
        headers=hub.headers["owner"],
    )
    assert member.status_code == 413


def sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
