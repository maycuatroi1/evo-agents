"""The worker's calls to the hub: ``/v1/worker/*`` over aiohttp, each with ``X-Evo-Worker-Protocol: 1`` and the
worker token as a Bearer header (the join has no token: the pairing code is its credential).

A call answers the decoded JSON, or raises:

- ``Unreachable`` when no answer came (refused, reset, timed out) or the hub answered 5xx or 429: the call may be
  sent again later, and ``Backoff`` says when (1 second, doubling up to 60, with a little jitter).
- ``Refused`` for any other 4xx, with the status, the hub's error code and message, and ``Retry-After``.
- ``Outdated`` (a ``Refused``) for 426: the hub speaks another protocol version and this daemon must be upgraded.

Redirects are not followed: they would hand the token to whatever host they name. The URL is https, or http to a
loopback address (``evo_agents.hub.client.check_url``).
"""

from __future__ import annotations

import json
import random
from urllib.parse import quote

import aiohttp

from evo_agents import __version__
from evo_agents.hub.client import check_url
from evo_agents.hub.runs import CLAIM_WAIT_SECONDS, PROTOCOL_HEADER, PROTOCOL_VERSION

TIMEOUT = 30.0  # seconds for one call
CLAIM_SLACK = 20.0  # seconds a claim may take beyond its wait
UPLOAD_TIMEOUT = 300.0
USER_AGENT = f"evo-agents-worker/{__version__}"
FIRST_DELAY = 1.0
MOST_DELAY = 60.0


class HubProblem(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None, payload=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload
        self.retry_after: float | None = None


class Unreachable(HubProblem):
    """No usable answer; send again later."""


class Refused(HubProblem):
    """The hub answered 4xx: sending the same call again gets the same answer."""


class Outdated(Refused):
    """426: this daemon speaks a worker protocol the hub no longer takes."""


class Backoff:
    """Delays between tries of a failing call: FIRST_DELAY, doubling up to MOST_DELAY, each within 20% of that."""

    def __init__(self, first: float = FIRST_DELAY, most: float = MOST_DELAY, jitter: float = 0.2):
        self.first = first
        self.most = most
        self.jitter = jitter
        self.failures = 0

    def next(self) -> float:
        delay = min(self.most, self.first * (2**self.failures))
        self.failures += 1
        return max(0.0, delay * (1 + random.uniform(-self.jitter, self.jitter)))

    def reset(self) -> None:
        self.failures = 0


def _judge_key(key: str | None) -> dict:
    from evo_agents.hub.judge import JUDGE_KEY_HEADER

    return {JUDGE_KEY_HEADER: key} if key else {}


def _retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


class WorkerHub:
    """Calls to one hub, as the worker that holds ``token`` (none for the join)."""

    def __init__(self, url: str, token: str | None, session: aiohttp.ClientSession):
        self.url = check_url(url)
        self.token = token
        self.session = session

    def __repr__(self) -> str:  # the token stays out of tracebacks
        return f"WorkerHub({self.url!r})"

    def _headers(self, body: bool) -> dict:
        headers = {PROTOCOL_HEADER: PROTOCOL_VERSION, "Accept": "application/json", "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body:
            headers["Content-Type"] = "application/json"
        return headers

    async def call(self, method: str, path: str, body=None, *, timeout: float = TIMEOUT, headers: dict | None = None):
        data = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            async with self.session.request(
                method,
                self.url + path,
                data=data,
                headers={**self._headers(body is not None), **(headers or {})},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                raw = await response.read()
                status = response.status
                retry_after = _retry_after(response.headers.get("Retry-After"))
        except TimeoutError:
            raise Unreachable(f"no answer from {self.url} within {timeout:g}s") from None
        except aiohttp.ClientError as exc:
            raise Unreachable(f"cannot reach {self.url}: {type(exc).__name__}: {exc}") from None
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        if status < 300:
            return payload
        message = payload.get("message") if isinstance(payload, dict) else None
        code = payload.get("error") if isinstance(payload, dict) else None
        text = str(message or f"the hub at {self.url} answered HTTP {status} to {method} {path}")
        if status >= 500 or status == 429:
            problem: HubProblem = Unreachable(text, status, code, payload)
        elif status == 426:
            problem = Outdated(
                f"the hub at {self.url} does not take worker protocol {PROTOCOL_VERSION}: upgrade evo-agents ({text})",
                status,
                code,
                payload,
            )
        else:
            problem = Refused(text, status, code, payload)
        problem.retry_after = retry_after
        raise problem

    # The protocol

    async def join(self, code: str, host: dict) -> dict:
        return await self.call("POST", "/v1/worker/join", {"code": code, **host})

    async def claim(self, wait_s: float = CLAIM_WAIT_SECONDS) -> dict | None:
        answer = await self.call("POST", "/v1/worker/claim", {"wait_s": wait_s}, timeout=wait_s + CLAIM_SLACK)
        return answer.get("run") if isinstance(answer, dict) else None

    async def heartbeat(self, body: dict) -> dict:
        return await self.call("POST", "/v1/worker/heartbeat", body)

    async def report(self, run_id: int, body: dict) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/state", body)

    async def events(self, run_id: int, events: list[dict]) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/events", {"events": events})

    async def inbox(self, run_id: int, ack: int | None = None) -> list[dict]:
        body = {} if ack is None else {"ack": ack}
        answer = await self.call("POST", f"/v1/worker/runs/{int(run_id)}/inbox", body)
        return list(answer.get("messages") or []) if isinstance(answer, dict) else []

    # The leases of a run (docs/credentials.md): the answer holds their values, which no caller logs

    async def credentials(self, run_id: int) -> dict:
        """``{leases, missing}`` of a run this worker holds; asked again, the same leases, a GitHub token near its end
        replaced."""
        answer = await self.call("POST", f"/v1/worker/runs/{int(run_id)}/credentials")
        return answer if isinstance(answer, dict) else {}

    async def release_credentials(self, run_id: int) -> dict:
        """Give back every lease this worker holds of the run, in whatever state it is; ``{revoked}``."""
        answer = await self.call("DELETE", f"/v1/worker/runs/{int(run_id)}/credentials")
        return answer if isinstance(answer, dict) else {}

    # A plan run: its plan, its steps, its decisions and its notices

    async def plan(self, run_id: int) -> dict:
        return await self.call("GET", f"/v1/worker/runs/{int(run_id)}/plan")

    async def put_plan(self, run_id: int, body: dict) -> dict:
        """An author run's plan put on the hub: ``body`` holds ``body`` and, to replace the plan, ``if_revision``."""
        return await self.call("PUT", f"/v1/worker/runs/{int(run_id)}/plan", body)

    async def step(self, run_id: int, key: str, body: dict) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/steps/{quote(str(key), safe='')}", body)

    async def decision(self, run_id: int, body: dict) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/decisions", body)

    async def notice(self, run_id: int, body: dict) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/notices", body)

    # A review run: its findings and proposals

    async def finding(self, run_id: int, body: dict) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/findings", body)

    async def proposal(self, run_id: int, body: dict) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/proposals", body)

    # A judge run: what it reads, and its verdict, each with the run's own key, which its claim handed the daemon. The
    # answer of judge_inputs holds the project's hidden checks, which no caller writes to a file, an event or a log
    # line.

    async def judge_inputs(self, run_id: int, key: str | None) -> dict:
        return await self.call("GET", f"/v1/worker/runs/{int(run_id)}/judge", headers=_judge_key(key))

    async def verdict(self, run_id: int, body: dict, key: str | None) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/verdict", body, headers=_judge_key(key))

    async def uploads(self, run_id: int, items: list[dict]) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/uploads", {"items": items})

    async def commit_blobs(self, run_id: int, upload_ids: list[str]) -> dict:
        body = {"upload_ids": upload_ids}
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/blobs", body, timeout=UPLOAD_TIMEOUT)

    async def put(self, url: str, data: bytes) -> None:
        """PUT ``data`` to a presigned URL of the blob store. The URL is a credential until it expires: an error
        names nothing of it."""
        headers = {"Content-Type": "application/octet-stream", "User-Agent": USER_AGENT}
        try:
            async with self.session.put(
                url,
                data=data,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT),
            ) as response:
                await response.read()
                status = response.status
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise Unreachable(f"the blob store did not take an upload: {type(exc).__name__}") from None
        if not 200 <= status < 300:
            raise Refused(f"the blob store answered HTTP {status} to an upload", status)


def new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None), trust_env=True)
