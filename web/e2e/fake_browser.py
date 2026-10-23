"""The worker's end of a run's live browser view for the Playwright stack (``hub_stack.py``): ``FakeBrowser`` connects
to WS /v1/worker/runs/{id}/browser the way the daemon's broker does (``evo_agents.worker.browser.live``), with the
worker's token and protocol header, and plays a browser of two tabs on the wire format of ``evo_agents.hub.browser_view``.
No Chromium runs: each tab's screen is a still JPEG of one colour, northwind blue and lumen amber.

It follows the broker where a test can see it: the mode is the run's state (human while the run is interactive), read
from the database when its end attaches, when someone starts viewing, and every MODE_POLL seconds while someone views,
and STATE says it with the tabs each time it changes; nobody viewing, it reads nothing, so a fake its test left behind
costs the stack nothing for the rest of the suite. The hub's VIEW starts the screen (a SCREEN of the tab watched,
again after each change), and stops it; in agent mode only a tab picked reaches the browser, in human mode back,
forward, reload and a URL to open do too, where a URL with another scheme than http and https gets the broker's own
notice. Every event the viewer sends, and every ACK, is recorded as it arrived, so a test reads what the page sent
(``state``, which GET /browser/worker/<run_id> answers).

``drop`` closes its end as a worker that went away (the viewer waits), ``resume`` connects it again, and ``close``
closes it as the run's browser closing (CLOSE_NORMAL, which ends the view).
"""

from __future__ import annotations

import base64
import threading
import time
from urllib.parse import urlsplit

from evo_agents.hub import browser_view as frames
from evo_agents.hub.runs import PROTOCOL_HEADER, PROTOCOL_VERSION
from evo_agents.worker.browser.live import REFUSED_URL, mode, openable

CONNECT_FOR = 30.0  # seconds the fake keeps trying while the hub refuses it
MODE_POLL = 0.3  # seconds between two looks at the run's state, while someone views
WIDTH, HEIGHT = 1280, 800  # the page each SCREEN header measures, in CSS pixels; the JPEGs are 320 x 200

# Two stills of 320 x 200, one colour each: #1d4ed8 (northwind) and #d97706 (lumen).
NORTHWIND_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxO"
    "UlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09P"
    "T09PT09PT0//wAARCADIAUADASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QA"
    "FgEBAQEAAAAAAAAAAAAAAAAAAAUG/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AggNYlAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP"
    "/9k="
)
LUMEN_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxO"
    "UlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09P"
    "T09PT09PT0//wAARCADIAUADASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAP/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QA"
    "FQEBAQAAAAAAAAAAAAAAAAAAAAb/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCgCVUIAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD//"
    "2Q=="
)


def _tabs() -> list[dict]:
    """The browser as the run's agent left it: its orders page in front, a second site behind."""
    return [
        {"id": "northwind", "title": "Northwind orders", "history": ["https://northwind.example/orders"], "at": 0},
        {"id": "lumen", "title": "Lumen", "history": ["https://lumen.example/"], "at": 0},
    ]


class FakeBrowser:
    def __init__(self, api_url: str, run_id: int, token: str, dsn: str):
        self.url = api_url.replace("http://", "ws://", 1) + f"/v1/worker/runs/{run_id}/browser"
        self.run_id = run_id
        self.dsn = dsn
        self.headers = {"Authorization": f"Bearer {token}", PROTOCOL_HEADER: PROTOCOL_VERSION}
        self.tabs = {tab["id"]: tab for tab in _tabs()}
        self.active = "northwind"
        self.mode = frames.AGENT
        self.viewing = False
        self.seq = 0
        self.ws = None
        self.lock = threading.Lock()  # one frame at a time, from the receiving loop and the mode's poll
        self.back = threading.Event()  # set while the fake should be connected
        self.back.set()
        self.stopped = False
        self.state = {
            "connected": False,
            "attaches": 0,
            "refused": [],
            "closed": None,
            "mode": self.mode,
            "active": self.active,
            "views": [],
            "screens": [],
            "acks": [],
            "inputs": [],
            "notices": [],
        }

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._poll_mode, daemon=True).start()

    # What a test asks of it

    def drop(self) -> None:
        """Go away as a worker that lost the hub: the viewer waits for the next connection."""
        self.back.clear()
        ws = self.ws
        if ws is not None:
            ws.close(1001, "the fake worker went away")

    def resume(self) -> None:
        self.back.set()

    def close(self) -> None:
        """Close the run's browser: the hub ends the view."""
        self.stopped = True
        self.back.set()
        ws = self.ws
        if ws is not None:
            ws.close(1000, "the run's browser closed")

    # The wire

    def _send(self, data: bytes) -> None:
        ws = self.ws
        if ws is None:
            return
        try:
            with self.lock:
                ws.send(data)
        except Exception:  # the hub closed this end; the receiving loop sees it
            pass

    def _send_state(self, notice: str | None = None) -> None:
        tabs = [
            {"id": tab["id"], "url": tab["history"][tab["at"]], "title": tab["title"]} for tab in self.tabs.values()
        ]
        body = {"mode": self.mode, "tabs": tabs, "active": self.active}
        if notice is not None:
            body["notice"] = notice
            self.state["notices"].append(notice)
        self._send(frames.message(frames.STATE, body))

    def _send_screen(self) -> None:
        if not self.viewing:
            return
        self.seq += 1
        jpeg = NORTHWIND_JPEG if self.active == "northwind" else LUMEN_JPEG
        header = {"seq": self.seq, "width": WIDTH, "height": HEIGHT, "top": 0, "scale": 1}
        self.state["screens"].append({"seq": self.seq, "tab": self.active})
        self._send(frames.screen(header, jpeg))

    def _run_state(self, conn=None) -> str:
        """The run's state, on `conn` or on a connection of its own."""
        import psycopg

        if conn is None:
            with psycopg.connect(self.dsn, autocommit=True) as own:
                return self._run_state(own)
        row = conn.execute("SELECT state FROM runs WHERE id = %s", (self.run_id,)).fetchone()
        return row[0] if row else "gone"

    def _check_mode(self, conn=None) -> bool:
        now = mode(self._run_state(conn), False)
        if now == self.mode:
            return False
        self.mode = self.state["mode"] = now
        return True

    def _poll_mode(self) -> None:
        """While someone views, the run's state every MODE_POLL seconds, on one connection held as long as they do."""
        import psycopg

        conn = None
        try:
            while not self.stopped:
                time.sleep(MODE_POLL)
                if not (self.viewing and self.ws is not None):
                    if conn is not None:
                        conn.close()
                        conn = None
                    continue
                try:
                    if conn is None:
                        conn = psycopg.connect(self.dsn, autocommit=True)
                    if self._check_mode(conn) and self.ws is not None:
                        self._send_state()
                except Exception:  # the stack's database going away as the stack stops; a new connection next time
                    if conn is not None:
                        conn.close()
                    conn = None
        finally:
            if conn is not None:
                conn.close()

    def _connect(self):
        from websockets.exceptions import ConnectionClosed, InvalidStatus
        from websockets.sync.client import connect

        deadline = time.monotonic() + CONNECT_FOR
        while True:
            try:
                return connect(self.url, additional_headers=self.headers, open_timeout=5, max_size=2**20)
            except InvalidStatus as exc:
                self.state["refused"].append(exc.response.status_code)
            except ConnectionClosed as exc:
                self.state["refused"].append(exc.rcvd.code if exc.rcvd else None)
            except (OSError, TimeoutError) as exc:
                self.state["refused"].append(type(exc).__name__)
            if time.monotonic() > deadline:
                return None
            time.sleep(0.3)

    def _run(self) -> None:
        from websockets.exceptions import ConnectionClosed

        while not self.stopped:
            self.back.wait()
            if self.stopped:
                return
            ws = self._connect()
            if ws is None:
                self.state["closed"] = "never connected"
                return
            self.ws = ws
            self.viewing = False
            self.state["connected"] = True
            self.state["attaches"] += 1
            try:
                self._check_mode()  # the mode the run is in now, before the viewer sees a STATE
                self._send_state()  # the hub forgets the last STATE when a worker's end attaches
                for data in ws:
                    if isinstance(data, bytes):
                        self._receive(data)
                self.state["closed"] = ws.close_code
            except ConnectionClosed as exc:
                self.state["closed"] = exc.rcvd.code if exc.rcvd else None
            finally:
                self.ws = None
                self.state["connected"] = False
                ws.close()

    def _receive(self, data: bytes) -> None:
        kind, payload = frames.parse(data)
        if kind == frames.VIEW:
            view = frames.body_of(payload)
            self.state["views"].append(view)
            self.viewing = bool(view.get("viewing"))
            if self.viewing:
                try:
                    if self._check_mode():  # the run's mode changed while nobody watched
                        self._send_state()
                except Exception:  # the stack's database going away as the stack stops
                    pass
            self._send_screen()
        elif kind == frames.ACK:
            self.state["acks"].append(frames.body_of(payload).get("seq"))
        elif kind == frames.INPUT:
            event = frames.parse_event(payload)
            self.state["inputs"].append(event)
            self._input(event)

    def _input(self, event: dict) -> None:
        kind = event["type"]
        if kind == "tab":
            if event["id"] in self.tabs and event["id"] != self.active:
                self.active = self.state["active"] = event["id"]
                self._send_state()
                self._send_screen()
            return
        if self.mode != frames.HUMAN:
            return  # the agent drives the browser: the viewer watches
        tab = self.tabs[self.active]
        if kind == "open":
            url = openable(event["url"])
            if url is None:
                self._send_state(REFUSED_URL)
                return
            tab["history"] = tab["history"][: tab["at"] + 1] + [url]
            tab["at"] += 1
            tab["title"] = urlsplit(url).hostname or url
        elif kind in ("back", "forward"):
            at = tab["at"] + (-1 if kind == "back" else 1)
            if not 0 <= at < len(tab["history"]):
                return
            tab["at"] = at
        elif kind != "reload":
            return  # mouse, wheel, keys and text: what they did shows in the next screencast frame
        self._send_state()
        self._send_screen()
