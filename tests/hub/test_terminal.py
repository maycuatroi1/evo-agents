"""The web terminal on the hub: the relay between the browser's websocket and the worker's.

The checks step 7 of the worker-fleet plan names: bytes pass both ways; a resize reaches the worker of its run and no
other; an Origin of another site, a web session older than 12 hours, another member than the worker's owner and a
worker that does not allow the terminal are closed with 4403; no session with 4401; an idle session closes both ends;
the audit has a row when the terminal opens and one when it closes. Around them: the hello, the worker's end, the
takeover a headless run gets, frames too big or of a type the end may not send, one browser per run, the 4 hour
limit, and the middleware closing a websocket under /v1 that checks no credential of its own.

Both ends are websocket clients of one TestClient, so the app serves them on one event loop, as the api process does.
"""

import json
import queue
import threading
import time
from contextlib import contextmanager

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from evo_agents.hub import terminal as frames
from evo_agents.hub.server import terminal
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.security import SESSION_COOKIE, WEB, csrf_token, hash_token, new_token
from tests.hub.live import bearer, sql
from tests.hub.test_run_stream import members
from tests.hub.test_runs import (
    CHECKOUTS,
    HOST,
    OTHER,
    OWNER,
    PROJECT,
    PROTOCOL,
    READER,
    RUNTIMES,
    STRANGER,
    beat,
    claim,
    dispatched,
    moved,
)

HUB = "https://hub.test"  # EVO_HUB_PUBLIC_URL of live.web_changes
WAIT = 10.0  # seconds a test waits for a message before failing instead of hanging


@pytest.fixture
def client(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github, **live.web_changes(github))
    with TestClient(create_app(config), base_url=HUB) as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """As ``tests.hub.test_run_stream.members``: owner and someone-else writers of evo-agents, reader a reader, the
    plan rollout pushed; the members' machine-token headers."""
    return members(client, github)


def terminal_worker(client, headers, name: str, *, allow: bool = True) -> dict:
    """A worker of the member ``headers`` names that allows the web terminal (or not), after its first heartbeat."""
    body = {"name": name, "projects": [PROJECT], "allow_web_terminal": allow, **HOST}
    response = client.post("/v1/workers", json=body, headers=headers)
    assert response.status_code == 201, response.text
    answer = response.json()
    worker = {
        "id": answer["worker"]["id"],
        "name": name,
        "token": answer["token"],
        "headers": {**bearer(answer["token"]), **PROTOCOL},
        "runtimes": RUNTIMES,
        "checkouts": CHECKOUTS,
    }
    beat(client, worker)
    return worker


def held_run(client, hub, name: str = "mac-1", *, step: int = 2, state: str = "interactive", allow: bool = True):
    """A worker of owner and a run of ``step`` it claimed and moved to ``state``."""
    worker = terminal_worker(client, hub["owner"], name, allow=allow)
    run_id = dispatched(client, hub["owner"], [step], worker_id=worker["id"])[0]["id"]
    assert claim(client, worker)["id"] == run_id
    if state != "leased":
        moved(client, worker, run_id, state)
    return worker, run_id


def web_session(db, login: str, *, hours_old: int = 0) -> str:
    """A live web session of ``login``, created ``hours_old`` hours ago."""
    token = live.insert_token(db, login, WEB)
    if hours_old:
        sql(
            db,
            "UPDATE tokens SET created_at = now() - make_interval(hours => %s) WHERE token_hash = %s",
            (hours_old, hash_token(token)),
        )
    return token


def csrf(client, token: str) -> str:
    return csrf_token(client.app.state.config.session_secret, hash_token(token))


def browser(client, run_id: int, token: str | None, *, origin: str | None = HUB, project: str = PROJECT):
    headers = {}
    if origin is not None:
        headers["origin"] = origin
    if token is not None:
        headers["cookie"] = f"{SESSION_COOKIE}={token}"
    return client.websocket_connect(f"/v1/projects/{project}/runs/{run_id}/terminal", headers=headers)


def say_hello(client, tab, token: str, **size) -> None:
    tab.send_text(json.dumps({"csrf": csrf(client, token), **size}))


def worker_end(client, worker: dict, run_id: int, headers: dict | None = None):
    given = dict(worker["headers"] if headers is None else headers)  # websocket_connect adds to the dict it gets
    return client.websocket_connect(f"/v1/worker/runs/{run_id}/terminal", headers=given)


def message(ws, timeout: float = WAIT) -> dict:
    """The next message the hub sent on ``ws``. The session's own receive() waits forever; this fails instead.

    Only the public ``receive()`` of the test session is used, from a daemon thread, since how the session queues
    messages changes between Starlette releases. A receive that times out is left waiting: the test fails anyway,
    and the thread ends with the session or with the interpreter."""
    got: queue.Queue = queue.Queue(maxsize=1)

    def receive() -> None:
        try:
            got.put(ws.receive())
        except BaseException as error:  # handed over to the test's thread, which raises it
            got.put(error)

    threading.Thread(target=receive, name="hub-ws-receive", daemon=True).start()
    try:
        answer = got.get(timeout=timeout)
    except queue.Empty:
        pytest.fail(f"no message from the hub within {timeout}s")
    if isinstance(answer, BaseException):
        raise answer
    return answer


def received(ws) -> bytes:
    got = message(ws)
    assert got["type"] == "websocket.send" and got.get("bytes") is not None, got
    return got["bytes"]


def closed(ws) -> tuple[int, str]:
    got = message(ws)
    assert got["type"] == "websocket.close", got
    return got["code"], got.get("reason", "")


def terminal_open(client, worker: dict, run_id: int) -> bool:
    (control,) = beat(client, worker, [run_id])["runs"]
    assert control["held"], control
    return control["terminal_open"]


def wait_until(check, what: str, timeout: float = WAIT) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"{what}: not within {timeout}s"
        time.sleep(0.02)


def audit_rows(db, *actions: str) -> list[tuple[str, str, str]]:
    return sql(
        db,
        "SELECT a.action, a.target, u.login FROM audit a JOIN users u ON u.id = a.actor_id "
        "WHERE a.action = ANY(%s) ORDER BY a.id",
        (list(actions),),
    )


@contextmanager
def paired(client, worker: dict, run_id: int, token: str, *, cols: int = 80, rows: int = 24):
    """A browser and the worker's end on the run's terminal, relaying: (browser, worker's end)."""
    with browser(client, run_id, token) as tab:
        say_hello(client, tab, token, cols=cols, rows=rows)
        wait_until(lambda: terminal_open(client, worker, run_id), "terminal_open in the heartbeat")
        with worker_end(client, worker, run_id) as end:
            assert received(end) == frames.resize(cols, rows)
            yield tab, end


def target(run_id: int, step: int) -> str:
    """The run as audit rows name it."""
    return f"{PROJECT}/rollout#{step} run:{run_id}"


# Relaying


def test_bytes_pass_both_ways_and_a_resize_reaches_the_worker_of_its_run(client, hub, hub_db):
    first, run_a = held_run(client, hub, "mac-a", step=2)
    second, run_b = held_run(client, hub, "mac-b", step=4)
    token = web_session(hub_db, OWNER)
    with browser(client, run_a, token) as tab_a, browser(client, run_b, token) as tab_b:
        say_hello(client, tab_a, token, cols=120, rows=40)
        say_hello(client, tab_b, token, cols=100, rows=30)
        wait_until(lambda: terminal_open(client, first, run_a), "terminal_open for run a")
        wait_until(lambda: terminal_open(client, second, run_b), "terminal_open for run b")
        with worker_end(client, first, run_a) as end_a, worker_end(client, second, run_b) as end_b:
            # each worker gets its own browser's size first, and the heartbeat stops asking it to connect
            assert received(end_a) == frames.resize(120, 40)
            assert received(end_b) == frames.resize(100, 30)
            assert not terminal_open(client, first, run_a)
            keys = frames.frame(frames.INPUT, "ls -la ⌘\r".encode())
            tab_a.send_bytes(keys)
            assert received(end_a) == keys
            screen = frames.frame(frames.OUTPUT, bytes(range(256)) * 64)  # every byte value, 16 KiB
            end_a.send_bytes(screen)
            assert received(tab_a) == screen
            biggest = frames.frame(frames.OUTPUT, b"x" * (frames.MAX_FRAME_BYTES - 1))
            end_a.send_bytes(biggest)
            assert received(tab_a) == biggest
            # a resize goes to the worker of its run: run b's worker gets its own browser's input next, nothing else
            tab_a.send_bytes(frames.resize(132, 50))
            tab_b.send_bytes(frames.frame(frames.INPUT, b"b"))
            assert received(end_a) == frames.resize(132, 50)
            assert received(end_b) == frames.frame(frames.INPUT, b"b")
            end_b.send_bytes(frames.frame(frames.OUTPUT, b"from b"))
            assert received(tab_b) == frames.frame(frames.OUTPUT, b"from b")
            # the browser leaving closes the worker's end, and the session is audited with the bytes of each way
            tab_a.close()
            assert closed(end_a) == (1000, "the browser closed the terminal")
            to_worker = len(frames.resize(120, 40)) + len(keys) + len(frames.resize(132, 50))
            to_browser = len(screen) + len(biggest)
            opened = audit_rows(hub_db, "terminal.open")  # in the order the hub read the two hellos
            assert sorted(opened) == [
                ("terminal.open", target(run_a, 2), OWNER),
                ("terminal.open", target(run_b, 4), OWNER),
            ]
            assert audit_rows(hub_db, "terminal.close") == [
                (
                    "terminal.close",
                    f"{target(run_a, 2)} to_worker={to_worker} to_browser={to_browser} end=browser",
                    OWNER,
                ),
            ]
            # run b's session is not touched by run a's ending
            tab_b.send_bytes(frames.frame(frames.INPUT, b"still here"))
            assert received(end_b) == frames.frame(frames.INPUT, b"still here")
            end_b.close()
            assert closed(tab_b) == (1000, "the worker closed the terminal")
    rows = audit_rows(hub_db, "terminal.close")
    assert rows[-1] == ("terminal.close", f"{target(run_b, 4)} to_worker=18 to_browser=7 end=worker", OWNER)
    in_project = "SELECT DISTINCT p.name FROM audit a JOIN projects p ON p.id = a.project_id WHERE a.action LIKE %s"
    assert sql(hub_db, in_project, ("terminal.%",)) == [(PROJECT,)]
    # the run is free for another browser once the session ended
    with browser(client, run_a, token) as again:
        say_hello(client, again, token)
        wait_until(lambda: terminal_open(client, first, run_a), "terminal_open after a new browser")


def test_input_before_the_worker_connects_is_dropped_and_a_resize_sets_the_size_it_gets(client, hub, hub_db):
    worker, run_id = held_run(client, hub)
    token = web_session(hub_db, OWNER)
    with browser(client, run_id, token) as tab:
        say_hello(client, tab, token)  # no size in the hello
        early = frames.frame(frames.INPUT, b"too early")
        tab.send_bytes(early)
        tab.send_bytes(frames.resize(90, 33))
        wait_until(lambda: terminal_open(client, worker, run_id), "terminal_open")
        session = client.app.state.terminals.get(run_id)
        wait_until(lambda: session.size == (90, 33), "the resize read while the hub waits for the worker")
        assert session.dropped == len(early)
        with worker_end(client, worker, run_id) as end:
            assert received(end) == frames.resize(90, 33)
            tab.send_bytes(frames.frame(frames.INPUT, b"on time"))
            assert received(end) == frames.frame(frames.INPUT, b"on time")
            end.close()
            assert closed(tab)[0] == 1000


# The browser's end


def test_the_browser_end_refuses_with_4403_or_4401(client, hub, hub_db):
    worker, run_id = held_run(client, hub)
    owner = web_session(hub_db, OWNER)

    def refused(token, *, origin=HUB, csrf_value=None, **size):
        with browser(client, run_id, token, origin=origin) as tab:
            value = csrf(client, token) if csrf_value is None and token else csrf_value
            tab.send_text(json.dumps({"csrf": value, **size}))
            return closed(tab)

    # Origin is checked first, so a page of another site learns nothing about the session
    for origin in ("https://evil.test", None, "http://hub.test", "https://hub.test.evil.test"):
        assert refused(owner, origin=origin) == (4403, "the terminal opens only from a page of this hub (Origin)")
    assert refused(None) == (4401, "sign in on the web to open a terminal")
    unknown = new_token(WEB)
    assert refused(unknown)[0] == 4401
    revoked = web_session(hub_db, OWNER)
    sql(hub_db, "UPDATE tokens SET revoked_at = now() WHERE token_hash = %s", (hash_token(revoked),))
    assert refused(revoked)[0] == 4401
    assert refused(owner, csrf_value="not-the-token") == (
        4403,
        "the hello needs the X-Evo-CSRF value of GET /v1/auth/web/csrf",
    )
    assert refused(owner, csrf_value=csrf(client, web_session(hub_db, OWNER)))[0] == 4403  # another session's value
    assert refused(web_session(hub_db, OWNER, hours_old=13)) == (
        4403,
        "the web session is older than 12 hours: sign in again",
    )
    # members other than the worker's owner: another writer, a reader, someone without a grant
    for login in (OTHER, READER, STRANGER):
        code, reason = refused(web_session(hub_db, login))
        assert code == 4403, (login, reason)
    assert refused(web_session(hub_db, OTHER))[1] == f"only {OWNER}, who dispatched run {run_id}, may open its terminal"
    assert refused(owner, cols=0, rows=24)[0] == 1003
    # a worker that does not allow the web terminal
    closed_worker, closed_run = held_run(client, hub, "mac-closed", step=4, allow=False)
    with browser(client, closed_run, owner) as tab:
        say_hello(client, tab, owner)
        assert closed(tab) == (4403, "worker mac-closed does not allow the web terminal")
    assert not terminal_open(client, closed_worker, closed_run)
    # nothing was reserved or audited by a refusal, and a session 11 hours old opens the terminal
    assert not terminal_open(client, worker, run_id)
    assert audit_rows(hub_db, "terminal.open", "run.takeover") == []
    young = web_session(hub_db, OWNER, hours_old=11)
    with browser(client, run_id, young) as tab:
        say_hello(client, tab, young)
        wait_until(lambda: terminal_open(client, worker, run_id), "terminal_open")
    assert [row[0] for row in audit_rows(hub_db, "terminal.open")] == ["terminal.open"]


def test_a_run_its_worker_does_not_hold_opens_no_terminal(client, hub, hub_db):
    token = web_session(hub_db, OWNER)
    worker = terminal_worker(client, hub["owner"], "mac-1")
    queued = dispatched(client, hub["owner"], [2], worker_id=worker["id"])[0]["id"]
    with browser(client, queued, token) as tab:
        say_hello(client, tab, token)
        assert closed(tab) == (
            4403,
            f"run {queued} is queued: a terminal opens on a run that is leased, running or interactive",
        )
    assert claim(client, worker)["id"] == queued
    moved(client, worker, queued, "running", "verifying")
    with browser(client, queued, token) as tab:
        say_hello(client, tab, token)
        assert closed(tab)[0] == 4403
    with browser(client, 999999, token) as tab:
        say_hello(client, tab, token)
        assert closed(tab)[0] == 4403
    with browser(client, queued, token, project="nothing") as tab:
        say_hello(client, tab, token)
        assert closed(tab)[0] == 4403


def test_a_browser_without_a_hello_is_closed_and_a_second_browser_is_refused(client, hub, hub_db, monkeypatch):
    worker, run_id = held_run(client, hub)
    token = web_session(hub_db, OWNER)
    monkeypatch.setattr(terminal, "HELLO_SECONDS", 0.3)
    with browser(client, run_id, token) as tab:
        assert closed(tab) == (4408, "no hello within 0.3 seconds")
    with browser(client, run_id, token) as tab:
        tab.send_bytes(frames.frame(frames.INPUT, b"no hello"))
        assert closed(tab)[0] == 1003
    with browser(client, run_id, token) as first:
        say_hello(client, first, token)
        wait_until(lambda: terminal_open(client, worker, run_id), "terminal_open")
        with browser(client, run_id, token) as second:
            say_hello(client, second, token)
            assert closed(second) == (4409, f"the terminal of run {run_id} is open in another browser")
        first.send_bytes(frames.frame(frames.INPUT, b"the first one stays"))  # dropped: no worker yet
        assert terminal_open(client, worker, run_id)


def test_opening_the_terminal_of_a_headless_run_asks_for_a_takeover(client, hub, hub_db):
    worker, run_id = held_run(client, hub, state="running")
    token = web_session(hub_db, OWNER)
    with browser(client, run_id, token) as tab:
        say_hello(client, tab, token, cols=100, rows=30)
        wait_until(lambda: terminal_open(client, worker, run_id), "terminal_open")
        (control,) = beat(client, worker, [run_id])["runs"]
        assert (control["takeover"], control["terminal_open"]) == (True, True)
        assert sql(hub_db, "SELECT takeover_requested_at IS NOT NULL FROM runs WHERE id = %s", (run_id,)) == [(True,)]
        assert [row[0] for row in audit_rows(hub_db, "run.takeover", "terminal.open")] == [
            "run.takeover",
            "terminal.open",
        ]
        # the worker connects its end once a person may drive the agent
        with worker_end(client, worker, run_id) as early:
            assert closed(early) == (4403, f"run {run_id} is running: connect its terminal once it is interactive")
        moved(client, worker, run_id, "interactive")
        assert beat(client, worker, [run_id])["runs"][0]["takeover"] is False
        with worker_end(client, worker, run_id) as end:
            assert received(end) == frames.resize(100, 30)
            end.send_bytes(frames.frame(frames.OUTPUT, b"$ "))
            assert received(tab) == frames.frame(frames.OUTPUT, b"$ ")
            tab.close()
            assert closed(end)[0] == 1000
    # an interactive run asks for nothing more
    with browser(client, run_id, token) as tab:
        say_hello(client, tab, token)
        wait_until(lambda: terminal_open(client, worker, run_id), "terminal_open")
    assert [row[0] for row in audit_rows(hub_db, "run.takeover")] == ["run.takeover"]


# The worker's end


def test_the_worker_end_needs_the_token_of_the_worker_holding_the_run(client, hub, hub_db):
    worker, run_id = held_run(client, hub)
    other_worker = terminal_worker(client, hub["owner"], "mac-2")
    token = web_session(hub_db, OWNER)

    def refused(headers=None, *, of=worker):
        with worker_end(client, of, run_id, headers) as end:
            return closed(end)

    # no browser waits yet
    assert refused() == (4403, f"no browser waits for the terminal of run {run_id}")
    with browser(client, run_id, token) as tab:
        say_hello(client, tab, token, cols=80, rows=24)
        wait_until(lambda: terminal_open(client, worker, run_id), "terminal_open")
        assert refused(bearer(worker["token"]))[0] == 4426  # without the protocol header
        assert refused({**bearer(worker["token"]), "X-Evo-Worker-Protocol": "2"})[0] == 4426
        assert refused(PROTOCOL)[0] == 4401
        assert refused({"Authorization": "Basic abc", **PROTOCOL})[0] == 4401
        assert refused({**bearer(new_token("worker")), **PROTOCOL})[0] == 4401
        assert refused({**hub["owner"], **PROTOCOL})[0] == 4403  # a machine token
        assert refused(of=other_worker)[0] == 4403  # a worker that does not hold the run
        assert terminal_open(client, worker, run_id)  # none of them took the browser's place
        with worker_end(client, worker, run_id) as end:
            assert received(end) == frames.resize(80, 24)
            assert refused() == (4409, f"the worker's end of run {run_id}'s terminal is connected already")
            tab.send_bytes(frames.frame(frames.INPUT, b"x"))
            assert received(end) == frames.frame(frames.INPUT, b"x")
            tab.close()
            assert closed(end)[0] == 1000
    # a revoked worker's token is refused before anything else is looked at
    assert client.post(f"/v1/workers/{worker['id']}/revoke", headers=hub["owner"]).status_code == 200
    assert refused()[0] == 4401


# Frames, limits and timeouts


@pytest.mark.parametrize(
    ("sender", "data", "codes"),
    [
        ("browser", bytes((frames.INPUT,)) + b"x" * frames.MAX_FRAME_BYTES, (1009, 1000)),
        ("browser", frames.frame(frames.OUTPUT, b"not mine"), (1003, 1000)),
        ("browser", b"", (1003, 1000)),
        ("browser", bytes((7,)) + b"unknown type", (1003, 1000)),
        ("browser", bytes((frames.RESIZE, 0, 80)), (1003, 1000)),
        ("browser", "a text message", (1003, 1000)),
        ("worker", frames.frame(frames.INPUT, b"not mine"), (1000, 1003)),
        ("worker", bytes((frames.OUTPUT,)) + b"x" * frames.MAX_FRAME_BYTES, (1000, 1009)),
        ("worker", "a text message", (1000, 1003)),
    ],
)
def test_a_frame_too_big_or_of_a_type_the_end_may_not_send_closes_the_session(client, hub, hub_db, sender, data, codes):
    worker, run_id = held_run(client, hub)
    token = web_session(hub_db, OWNER)
    with paired(client, worker, run_id, token) as (tab, end):
        source = tab if sender == "browser" else end
        if isinstance(data, str):
            source.send_text(data)
        else:
            source.send_bytes(data)
        assert (closed(tab)[0], closed(end)[0]) == codes
    wait_until(lambda: audit_rows(hub_db, "terminal.close"), "the close audited")
    assert audit_rows(hub_db, "terminal.close")[0][1].endswith(" end=protocol")


def test_an_idle_session_closes_both_ends(client, hub, hub_db, monkeypatch):
    worker, run_id = held_run(client, hub)
    token = web_session(hub_db, OWNER)
    monkeypatch.setattr(terminal, "IDLE_SECONDS", 0.6)
    started = time.monotonic()
    with paired(client, worker, run_id, token) as (tab, end):
        for _ in range(3):  # a frame either way keeps the session open
            time.sleep(0.3)
            end.send_bytes(frames.frame(frames.OUTPUT, b"."))
            assert received(tab) == frames.frame(frames.OUTPUT, b".")
        reason = "closed after 0.01 idle minutes"
        assert closed(tab) == (4408, reason)
        assert closed(end) == (4408, reason)
        assert time.monotonic() - started >= 0.9 + 0.6
    assert audit_rows(hub_db, "terminal.close") == [
        ("terminal.close", f"{target(run_id, 2)} to_worker=5 to_browser=6 end=idle", OWNER)
    ]
    assert not terminal_open(client, worker, run_id)


def test_a_session_closes_after_its_longest_time_even_when_busy(client, hub, hub_db, monkeypatch):
    worker, run_id = held_run(client, hub)
    token = web_session(hub_db, OWNER)
    monkeypatch.setattr(terminal, "MAX_SECONDS", 0.5)
    with paired(client, worker, run_id, token) as (tab, end):
        assert closed(tab) == (4408, "a terminal stays open at most 0.000138889 hours")
        assert closed(end)[0] == 4408
    assert audit_rows(hub_db, "terminal.close")[0][1].endswith(" end=timeout")


# The middleware


def test_a_websocket_under_v1_that_checks_no_credential_is_closed_before_the_app_sees_it(hub_db, tmp_path, github):
    app = create_app(live.hub_config(hub_db, tmp_path, github, **live.web_changes(github)))
    reached = []

    async def echo(websocket: WebSocket):
        reached.append(websocket.url.path)
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    for path in ("/v1/projects/{project}/echo", "/v1/worker/echo", "/v1/projects/{project}/runs/{run}/terminal/x"):
        app.add_api_websocket_route(path, echo)
    app.add_api_websocket_route("/echo", echo)
    with TestClient(app, base_url=HUB) as client:
        for path in ("/v1/projects/evo-agents/echo", "/v1/worker/echo", "/v1/projects/evo-agents/runs/1/terminal/x"):
            with pytest.raises(WebSocketDisconnect) as refused:
                with client.websocket_connect(path, headers={"origin": HUB}):
                    pass
            assert refused.value.code == 4403, path
        assert reached == []
        with client.websocket_connect("/echo") as ws:  # outside /v1 the middleware has nothing to say
            assert ws.receive_text() == "hello"
        assert reached == ["/echo"]


def test_frames_and_resizes_round_trip():
    assert frames.parse(frames.resize(80, 24)) == (frames.RESIZE, b"\x00\x50\x00\x18")
    assert frames.parse_resize(frames.parse(frames.resize(1000, 1))[1]) == (1000, 1)
    for cols, rows in ((0, 24), (80, 1001), (True, 24), (80.0, 24)):
        with pytest.raises(frames.FrameError):
            frames.resize(cols, rows)
    with pytest.raises(frames.FrameError):
        frames.frame(frames.OUTPUT, b"x" * frames.MAX_FRAME_BYTES)
    with pytest.raises(frames.FrameError):
        frames.frame(3)
    assert len(frames.close_reason("é" * 100).encode()) <= frames.MAX_REASON_BYTES
    assert frames.close_reason("short") == "short"
