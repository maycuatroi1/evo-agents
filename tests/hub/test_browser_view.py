"""The live view of a run's browser on the hub (plan agent-browser, criterion a5): the relay between the viewer's
websocket and the worker's.

Only the member who dispatched the run views it, on any worker: another member, the owner of the shared worker that
runs it included, is closed with 4403, as the web terminal's checks close a bad Origin, a stale session and a wrong
CSRF value. Frames pass both ways, the worker hears when someone starts or stops watching (so it screencasts only
then) and whether the run waits on a login decision, and a viewer that joins gets the worker's last state. A frame is
at most 512 KiB, while the terminal keeps its 64 KiB. The audit has browser.view and browser.control, with start and
end marks and counts, never a key, a text or a frame.

Both ends are websocket clients of one TestClient, as in ``tests.hub.test_terminal``.
"""

import json

import pytest

from evo_agents.hub import browser_view as frames
from evo_agents.hub import terminal as terminal_frames
from evo_agents.hub.terminal import FrameError
from tests.hub import pg

# The wire format needs no database.


def test_browser_view_frames_round_trip():
    state = {"mode": "human", "tabs": [{"id": "T1", "url": "https://northwind.example/", "title": "Northwind"}]}
    kind, payload = frames.parse(frames.message(frames.STATE, state))
    assert kind == frames.STATE and frames.body_of(payload) == state and frames.mode_of(state) == frames.HUMAN
    assert frames.mode_of({"mode": "pilot"}) is None
    header = {"seq": 7, "width": 1280, "height": 800, "top": 0, "scale": 1}
    kind, payload = frames.parse(frames.screen(header, b"\xff\xd8jpeg"))
    assert kind == frames.SCREEN and frames.parse_screen(payload) == (header, b"\xff\xd8jpeg")
    for bad in (b"", bytes((9,)), bytes((frames.INPUT,)) + b"x" * frames.MAX_FRAME_BYTES):
        with pytest.raises(FrameError):
            frames.parse(bad)
    with pytest.raises(FrameError):
        frames.parse_screen(b"\x00\x00\x01\x00{}")
    assert frames.MAX_FRAME_BYTES == 512 * 1024 and terminal_frames.MAX_FRAME_BYTES == 64 * 1024


def event(**body) -> bytes:
    return json.dumps(body).encode()


def test_browser_view_events_are_checked_and_keep_only_their_fields():
    assert frames.parse_event(event(type="mouse", action="down", x=10, y=20.5, button="left", clicks=1, extra=1)) == {
        "type": "mouse",
        "action": "down",
        "x": 10.0,
        "y": 20.5,
        "button": "left",
        "buttons": 0,
        "clicks": 1,
        "modifiers": 0,
    }
    assert frames.parse_event(event(type="key", action="down", key="a", code="KeyA", keyCode=65, text="a")) == {
        "type": "key",
        "action": "down",
        "key": "a",
        "code": "KeyA",
        "keyCode": 65,
        "text": "a",
        "modifiers": 0,
    }
    assert frames.parse_event(event(type="text", text="Tiếng Việt")) == {"type": "text", "text": "Tiếng Việt"}
    assert frames.parse_event(event(type="wheel", x=1, y=2, dy=-120)) == {
        "type": "wheel",
        "x": 1.0,
        "y": 2.0,
        "dx": 0.0,
        "dy": -120.0,
        "modifiers": 0,
    }
    assert frames.parse_event(event(type="reload")) == {"type": "reload"}
    assert frames.parse_event(event(type="open", url="lumen.example")) == {"type": "open", "url": "lumen.example"}
    for bad in (
        b"[]",
        b"not json",
        event(type="script"),
        event(type="mouse", action="down", x="1", y=2),
        event(type="mouse", action="down", x=1, y=2, button="left", clicks=True),
        event(type="mouse", action="press", x=1, y=2),
        event(type="mouse", action="move", x=1e9, y=2),
        event(type="key", action="down", key="a", keyCode=256),
        event(type="key", action="down", key="a", modifiers=16),
        event(type="paste", text=None),
        event(type="tab", id="T" * 65),
        event(type="open", url="x" * (frames.MAX_URL_CHARS + 1)),
    ):
        with pytest.raises(FrameError):
            frames.parse_event(bad)


if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select  # noqa: E402

from evo_agents.hub import tables  # noqa: E402
from evo_agents.hub.server import browser_view  # noqa: E402
from evo_agents.hub.server.security import SELF_CHECKED_WEBSOCKETS, SESSION_COOKIE  # noqa: E402
from tests.hub.live import sql  # noqa: E402
from tests.hub.test_browser_claim import add_browser_worker  # noqa: E402
from tests.hub.test_runs import (  # noqa: E402
    OTHER,
    OWNER,
    PROJECT,
    PROTOCOL,
    READER,
    STRANGER,
    add_worker,
    claim,
    control,
    dispatched,
    moved,
)
from tests.hub.test_shared_workers import share  # noqa: E402
from tests.hub.test_terminal import (  # noqa: E402, F401  (client and hub are fixtures)
    HUB,
    audit_rows,
    client,
    closed,
    csrf,
    hub,
    received,
    wait_until,
    web_session,
)

VIEWER_ACTIONS = ("browser.view", "browser.control")


def browser_run(client, hub, *, dispatcher: str = "owner", step: int = 2, state: str = "running", shared=False):
    """A worker of owner with the feature browser (shared, or not) and a run of ``step`` with a browser that
    ``dispatcher`` dispatched to it and that it claimed and moved to ``state``: (worker, run id)."""
    worker = add_browser_worker(client, hub["owner"], f"lab-{step}")
    if shared:
        assert share(client, hub["owner"], worker).status_code == 200
    (run,) = dispatched(client, hub[dispatcher], [step], worker_id=worker["id"], browser=True)
    assert claim(client, worker)["id"] == run["id"]
    if state != "leased":
        moved(client, worker, run["id"], state)
    return worker, run["id"]


def viewer(client, run_id: int, token: str | None, *, origin: str | None = HUB, project: str = PROJECT):
    headers = {}
    if origin is not None:
        headers["origin"] = origin
    if token is not None:
        headers["cookie"] = f"{SESSION_COOKIE}={token}"
    return client.websocket_connect(f"/v1/projects/{project}/runs/{run_id}/browser", headers=headers)


def hello(client, ws, token: str, value: str | None = None) -> None:
    ws.send_text(json.dumps({"csrf": csrf(client, token) if value is None else value}))


def worker_end(client, worker: dict, run_id: int, headers: dict | None = None):
    given = dict(worker["headers"] if headers is None else headers)
    return client.websocket_connect(f"/v1/worker/runs/{run_id}/browser", headers=given)


def body(data: bytes, kind: int) -> dict:
    got, payload = frames.parse(data)
    assert got == kind, (got, data[:80])
    return frames.body_of(payload)


def kept_state(client, run_id: int) -> bytes | None:
    """The worker's last STATE the hub keeps for the run's channel, None while it has none."""
    channel = client.app.state.browser_views.get(run_id)
    return None if channel is None else channel.state


def target(run_id: int, step: int = 2) -> str:
    return f"{PROJECT}/rollout#{step} run:{run_id}"


def state(mode: str = "agent", url: str = "https://northwind.example/") -> bytes:
    return frames.message(
        frames.STATE, {"mode": mode, "tabs": [{"id": "T1", "url": url, "title": "N"}], "active": "T1"}
    )


def opened(client, ws, token: str, *, worker: bool) -> None:
    """Say the hello on a viewer's socket and read the LINK it gets first."""
    hello(client, ws, token)
    assert body(received(ws), frames.LINK) == {"worker": worker}


# Relaying


def test_browser_view_relays_both_ways_and_the_worker_hears_who_watches(client, hub, hub_db):
    worker, run_id = browser_run(client, hub)
    token = web_session(hub_db, OWNER)
    with worker_end(client, worker, run_id) as end:
        end.send_bytes(state())
        wait_until(lambda: kept_state(client, run_id) is not None, "the worker's state kept")
        with viewer(client, run_id, token) as view:
            opened(client, view, token, worker=True)
            assert received(view) == state()  # the worker's last state, as the viewer joins
            assert body(received(end), frames.VIEW) == {"viewing": True, "login": False}

            click = frames.message(frames.INPUT, {"type": "mouse", "action": "down", "x": 5, "y": 6, "button": "left"})
            view.send_bytes(click)
            assert received(end) == click
            jpeg = b"\xff\xd8" + b"j" * (200 * 1024)  # above the terminal's 64 KiB, within the view's 512 KiB
            shot = frames.screen({"seq": 1, "width": 1280, "height": 800, "top": 0, "scale": 1}, jpeg)
            end.send_bytes(shot)
            assert received(view) == shot
            ack = frames.message(frames.ACK, {"seq": 1})
            view.send_bytes(ack)
            assert received(end) == ack
        assert body(received(end), frames.VIEW) == {"viewing": False, "login": False}
        wait_until(lambda: len(audit_rows(hub_db, *VIEWER_ACTIONS)) == 2, "the view's end audited")
    assert audit_rows(hub_db, *VIEWER_ACTIONS) == [
        ("browser.view", f"{target(run_id)} start", OWNER),
        ("browser.view", f"{target(run_id)} end=viewer frames=1 inputs=1", OWNER),
    ]


def test_browser_view_waits_for_the_worker_and_tells_the_viewer_when_it_comes_and_goes(client, hub, hub_db):
    worker, run_id = browser_run(client, hub)
    token = web_session(hub_db, OWNER)
    with viewer(client, run_id, token) as view:
        opened(client, view, token, worker=False)
        view.send_bytes(frames.message(frames.INPUT, {"type": "reload"}))  # nobody takes it: lost, not an error
        with worker_end(client, worker, run_id) as end:
            assert body(received(view), frames.LINK) == {"worker": True}
            assert body(received(end), frames.VIEW) == {"viewing": True, "login": False}
            end.close(code=1011)  # a drop: the viewer waits for the next connection
        assert body(received(view), frames.LINK) == {"worker": False}
        with worker_end(client, worker, run_id) as end:
            assert body(received(view), frames.LINK) == {"worker": True}
            assert body(received(end), frames.VIEW)["viewing"] is True
            end.send_bytes(state())
            assert received(view) == state()
            end.close(code=1000)  # the daemon closed the run's browser: the view ends
        assert closed(view) == (1000, "the run's browser closed")
    wait_until(lambda: len(audit_rows(hub_db, "browser.view")) == 2, "the view's end audited")
    assert audit_rows(hub_db, "browser.view")[-1][1] == f"{target(run_id)} end=worker frames=0 inputs=0"


def test_browser_view_a_newer_viewer_or_worker_connection_takes_the_channel(client, hub, hub_db):
    worker, run_id = browser_run(client, hub)
    token = web_session(hub_db, OWNER)
    with worker_end(client, worker, run_id) as end:
        with viewer(client, run_id, token) as first:
            opened(client, first, token, worker=True)
            assert body(received(end), frames.VIEW)["viewing"] is True
            with viewer(client, run_id, token) as second:  # the phone, while the laptop's tab is still open
                opened(client, second, token, worker=True)
                assert closed(first) == (4409, "the live view opened in another window")
                assert body(received(end), frames.VIEW)["viewing"] is True
                with worker_end(client, worker, run_id) as newer:
                    assert closed(end)[0] == 4409
                    assert body(received(second), frames.LINK) == {"worker": True}
                    assert body(received(newer), frames.VIEW)["viewing"] is True
                    newer.send_bytes(state("agent"))
                    assert received(second) == state("agent")
    wait_until(lambda: len(audit_rows(hub_db, "browser.view")) == 4, "both views audited")
    ends = [row[1] for row in audit_rows(hub_db, "browser.view") if " end=" in row[1]]
    assert ends[0] == f"{target(run_id)} end=replaced frames=0 inputs=0"


# Who views


def test_browser_view_only_the_dispatcher_views_on_any_worker(client, hub, hub_db):
    worker, run_id = browser_run(client, hub, dispatcher="other", shared=True)  # owner's worker, someone-else's run
    only = f"only {OTHER}, who dispatched run {run_id}, may view its browser"
    for login, reason in ((OWNER, only), (READER, only), (STRANGER, None)):
        token = web_session(hub_db, login)
        with viewer(client, run_id, token) as view:
            hello(client, view, token)
            code, said = closed(view)
            assert code == 4403, (login, said)
            assert reason is None or said == reason, (login, said)
    token = web_session(hub_db, OTHER)
    with worker_end(client, worker, run_id) as end, viewer(client, run_id, token) as view:
        opened(client, view, token, worker=True)
        assert body(received(end), frames.VIEW)["viewing"] is True
    rows = audit_rows(hub_db, "browser.view")
    assert {row[2] for row in rows} == {OTHER} and rows[0][1] == f"{target(run_id)} start"


def test_browser_view_refuses_as_the_web_terminal_does(client, hub, hub_db):
    _, run_id = browser_run(client, hub)
    token = web_session(hub_db, OWNER)
    with viewer(client, run_id, token, origin="https://lumen.example") as view:
        assert closed(view) == (4403, "the live view opens only from a page of this hub (Origin)")
    with viewer(client, run_id, None) as view:
        assert closed(view) == (4401, "sign in on the web to open the live view")
    with viewer(client, run_id, token) as view:
        hello(client, view, token, "not-the-csrf-value")
        assert closed(view)[0] == 4403
    with viewer(client, run_id, token) as view:
        view.send_text("hello")
        assert closed(view)[0] == 1003
    stale = web_session(hub_db, OWNER, hours_old=13)
    with viewer(client, run_id, stale) as view:
        hello(client, view, stale)
        assert closed(view) == (4403, "the web session is older than 12 hours: sign in again")
    assert audit_rows(hub_db, *VIEWER_ACTIONS) == []


def test_browser_view_of_a_run_without_browser_or_no_longer_held_is_refused(client, hub, hub_db):
    plain = add_worker(client, hub["owner"], "plain")
    (run,) = dispatched(client, hub["owner"], [4], worker_id=plain["id"])
    assert claim(client, plain)["id"] == run["id"]
    moved(client, plain, run["id"], "running")
    worker, held = browser_run(client, hub, step=5)
    token = web_session(hub_db, OWNER)
    with viewer(client, run["id"], token) as view:
        hello(client, view, token)
        assert closed(view) == (4403, f"run {run['id']} was dispatched without a browser")
    with worker_end(client, plain, run["id"]) as end:
        assert closed(end) == (4403, f"run {run['id']} was dispatched without a browser")
    assert control(client, hub["owner"], held, "cancel").status_code == 200
    moved(client, worker, held, "cancelled")
    with viewer(client, held, token) as view:
        hello(client, view, token)
        assert closed(view) == (4403, f"run {held} is cancelled: its browser lives while a worker holds it")


def test_browser_view_worker_end_needs_the_token_of_the_worker_holding_the_run(client, hub, hub_db):
    worker, run_id = browser_run(client, hub)
    stranger = add_browser_worker(client, hub["other"], "elsewhere")
    with worker_end(client, stranger, run_id) as end:
        assert closed(end)[0] == 4403
    without_protocol = {k: v for k, v in worker["headers"].items() if k not in PROTOCOL}
    with worker_end(client, worker, run_id, without_protocol) as end:
        assert closed(end)[0] == 4426
    with worker_end(client, worker, run_id, {**worker["headers"], "Authorization": "Bearer evw_nope"}) as end:
        assert closed(end)[0] == 4401
    with worker_end(client, worker, run_id, hub["owner"] | PROTOCOL) as end:  # a machine token, not the worker's
        assert closed(end)[0] == 4403


@pytest.mark.parametrize(
    ("sender", "data", "code"),
    [
        ("viewer", bytes((frames.INPUT,)) + b"x" * frames.MAX_FRAME_BYTES, 1009),
        ("viewer", frames.message(frames.STATE, {"mode": "human"}), 1003),
        ("viewer", frames.message(frames.INPUT, {"type": "script", "source": "alert(1)"}), 1003),
        ("viewer", "a text after the hello", 1003),
        ("worker", bytes((frames.SCREEN,)) + b"x" * frames.MAX_FRAME_BYTES, 1009),
        ("worker", frames.message(frames.INPUT, {"type": "reload"}), 1003),
        ("worker", bytes((frames.STATE,)) + b"[]", 1003),
    ],
)
def test_browser_view_a_frame_too_big_or_of_a_type_the_end_may_not_send_closes_that_end(
    client, hub, hub_db, sender, data, code
):
    worker, run_id = browser_run(client, hub)
    token = web_session(hub_db, OWNER)
    with worker_end(client, worker, run_id) as end, viewer(client, run_id, token) as view:
        opened(client, view, token, worker=True)
        assert body(received(end), frames.VIEW)["viewing"] is True
        source = view if sender == "viewer" else end
        if isinstance(data, str):
            source.send_text(data)
        else:
            source.send_bytes(data)
        assert closed(source)[0] == code
        if sender == "viewer":
            assert body(received(end), frames.VIEW)["viewing"] is False
        else:
            assert body(received(view), frames.LINK) == {"worker": False}


def test_browser_view_an_idle_view_is_closed(client, hub, hub_db, monkeypatch):
    worker, run_id = browser_run(client, hub)
    token = web_session(hub_db, OWNER)
    monkeypatch.setattr(browser_view, "IDLE_SECONDS", 0.5)
    with worker_end(client, worker, run_id) as end, viewer(client, run_id, token) as view:
        opened(client, view, token, worker=True)
        assert body(received(end), frames.VIEW)["viewing"] is True
        assert closed(view) == (4408, "closed after 0.00833333 idle minutes")
        assert body(received(end), frames.VIEW)["viewing"] is False
    wait_until(lambda: len(audit_rows(hub_db, "browser.view")) == 2, "the view's end audited")
    assert audit_rows(hub_db, "browser.view")[-1][1].startswith(f"{target(run_id)} end=idle ")


# Control and login


def asked(db, run_id: int, category: str) -> None:
    """An open decision of ``category`` on the run, as its agent asks one."""
    project_id = sql(db, select(tables.runs.c.project_id).where(tables.runs.c.id == run_id))[0][0]
    row = {"run_id": run_id, "project_id": project_id, "category": category, "question": "Signed in at Northwind?"}
    sql(
        db,
        insert(tables.decisions).values(
            {**row, "options": [{"key": "done", "label": "Signed in"}, {"key": "skip", "label": "Skip"}]}
        ),
    )


def test_browser_view_audits_control_as_the_worker_state_says_and_never_what_was_typed(client, hub, hub_db):
    worker, run_id = browser_run(client, hub, state="interactive")  # taken over: the broker says mode human
    token = web_session(hub_db, OWNER)
    secret = "hunter2-northwind"
    with worker_end(client, worker, run_id) as end, viewer(client, run_id, token) as view:
        opened(client, view, token, worker=True)
        assert body(received(end), frames.VIEW)["viewing"] is True
        end.send_bytes(state("human"))
        assert received(view) == state("human")
        wait_until(lambda: len(audit_rows(hub_db, "browser.control")) == 1, "control start audited")
        for typed in ({"type": "paste", "text": secret}, {"type": "text", "text": "é"}):
            view.send_bytes(frames.message(frames.INPUT, typed))
            assert json.loads(received(end)[1:]) == typed
        end.send_bytes(state("agent"))  # Hand back
        assert received(view) == state("agent")
        wait_until(lambda: len(audit_rows(hub_db, "browser.control")) == 2, "control end audited")
        end.send_bytes(state("human"))
        assert received(view) == state("human")
        wait_until(lambda: len(audit_rows(hub_db, "browser.control")) == 3, "control start audited again")
    wait_until(lambda: len(audit_rows(hub_db, *VIEWER_ACTIONS)) == 6, "the view's end audited")
    rows = audit_rows(hub_db, *VIEWER_ACTIONS)
    assert rows == [
        ("browser.view", f"{target(run_id)} start", OWNER),
        ("browser.control", f"{target(run_id)} start", OWNER),
        ("browser.control", f"{target(run_id)} end=agent inputs=2", OWNER),
        ("browser.control", f"{target(run_id)} start", OWNER),
        ("browser.control", f"{target(run_id)} end=viewer inputs=0", OWNER),
        ("browser.view", f"{target(run_id)} end=viewer frames=0 inputs=2", OWNER),
    ]
    stored = sql(hub_db, select(tables.audit.c.target))
    assert not any(secret in row[0] or "northwind.example" in row[0] for row in stored)


def test_browser_view_a_run_waiting_on_a_login_decision_opens_in_human_mode(client, hub, hub_db, monkeypatch):
    # the category login comes with the login decision (a later step); one the check constraint knows stands in
    monkeypatch.setattr(browser_view, "LOGIN_CATEGORY", "architecture")
    worker, run_id = browser_run(client, hub, state="running")
    asked(hub_db, run_id, "scope")
    moved(client, worker, run_id, "waiting")
    token = web_session(hub_db, OWNER)
    with worker_end(client, worker, run_id) as end:
        with viewer(client, run_id, token) as view:
            opened(client, view, token, worker=True)
            assert body(received(end), frames.VIEW) == {"viewing": True, "login": False}  # a decision, not a login
        assert body(received(end), frames.VIEW) == {"viewing": False, "login": False}
        asked(hub_db, run_id, "architecture")
        with viewer(client, run_id, token) as view:
            opened(client, view, token, worker=True)
            assert body(received(end), frames.VIEW) == {"viewing": True, "login": True}


def test_browser_view_both_ends_check_their_own_credential():
    for path in ("/v1/projects/evo-agents/runs/12/browser", "/v1/worker/runs/12/browser"):
        assert any(pattern.fullmatch(path) for pattern in SELF_CHECKED_WEBSOCKETS), path
    assert not any(
        pattern.fullmatch("/v1/projects/evo-agents/runs/12/browser/x") for pattern in SELF_CHECKED_WEBSOCKETS
    )


def test_browser_view_stays_open_through_a_takeover(client, hub, hub_db):
    """Take control is the takeover the web terminal uses; the view stays open through it, and the broker's state
    says the mode it made."""
    worker, run_id = browser_run(client, hub)
    token = web_session(hub_db, OWNER)
    with worker_end(client, worker, run_id) as end, viewer(client, run_id, token) as view:
        opened(client, view, token, worker=True)
        assert body(received(end), frames.VIEW)["viewing"] is True
        response = client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/takeover", headers=hub["owner"])
        assert response.status_code == 200, response.text
        moved(client, worker, run_id, "interactive")
        end.send_bytes(state("human"))
        assert received(view) == state("human")
