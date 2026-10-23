"""The worker's calls to the hub: ``/v1/worker/*`` over aiohttp, each with ``X-Evo-Worker-Protocol: 1`` and the
worker token as a Bearer header (the join has no token: the pairing code is its credential).

A call answers the decoded JSON, or raises:

- ``Unreachable`` when no answer came (refused, reset, timed out) or the hub answered 5xx or 429: the call may be
  sent again later, and ``Backoff`` says when (1 second, doubling up to 60, with a little jitter).
- ``Refused`` for any other 4xx, with the status, the hub's error code and message, and ``Retry-After``. A 4xx
  without the hub's own error body (``HubProblem.from_hub``) came from a proxy in front of it, which answers 404 for a
  moment while the hub is redeployed.
- ``Outdated`` (a ``Refused``) for 426: the hub speaks another protocol version and this daemon must be upgraded.

Redirects are not followed: they would hand the token to whatever host they name. The URL is https, or http to a
loopback address (``evo_agents.hub.client.check_url``).
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from urllib.parse import quote

import aiohttp

from evo_agents import __version__
from evo_agents.hub.client import HubError, check_url
from evo_agents.hub.client import Unreachable as HubUnreachable
from evo_agents.hub.runs import CLAIM_WAIT_SECONDS, PROTOCOL_HEADER, PROTOCOL_VERSION

TIMEOUT = 30.0  # seconds for one call
CLAIM_SLACK = 20.0  # seconds a claim may take beyond its wait
UPLOAD_TIMEOUT = 300.0
DOWNLOAD_TIMEOUT = 600.0  # seconds for the bytes of one file, at most 50 MiB
CHUNK_BYTES = 256 * 1024
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

    def hub_error(self, message: str | None = None) -> HubError:
        """This problem as the HubError of a command, whose exit status (``client.exit_status``) says what went wrong:
        a call no answer came to is ``client.Unreachable``; a 5xx or 429 keeps its status."""
        text = message or str(self)
        if isinstance(self, Unreachable) and self.status is None:
            return HubUnreachable(text)
        return HubError(text, self.status, self.code)

    @property
    def from_hub(self) -> bool:
        """The answer is the hub's own error, ``{error, message}`` (``evo_agents.hub.server.errors``), and not a page
        of a proxy in front of it."""
        return (
            isinstance(self.code, str)
            and isinstance(self.payload, dict)
            and isinstance(self.payload.get("message"), str)
        )


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
        if isinstance(body, bytes):  # sent as it is, with the caller's Content-Type
            data = body
        else:
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
        raise self._problem(method, path, status, payload, retry_after)

    def _problem(self, method: str, path: str, status: int, payload, retry_after: float | None) -> HubProblem:
        """The HubProblem of the hub's answer ``status`` with the decoded ``payload`` to ``method path``."""
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
        return problem

    async def download(self, path: str, write: Callable[[bytes], None], *, timeout: float = DOWNLOAD_TIMEOUT) -> None:
        """GET the bytes of ``path`` and hand them to ``write`` chunk by chunk as they come, in at most ``timeout``
        seconds, no chunk later than TIMEOUT after the one before; the same problems as ``call``. What ``write`` raises
        ends the download."""
        try:
            async with self.session.get(
                self.url + path,
                headers={**self._headers(False), "Accept": "*/*"},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout, sock_read=TIMEOUT),
            ) as response:
                status = response.status
                if status >= 300:
                    raw = await response.read()
                    try:
                        payload = json.loads(raw) if raw else None
                    except ValueError:
                        payload = None
                    raise self._problem("GET", path, status, payload, _retry_after(response.headers.get("Retry-After")))
                async for chunk in response.content.iter_chunked(CHUNK_BYTES):
                    write(chunk)
        except TimeoutError:
            raise Unreachable(f"no answer from {self.url} within {timeout:g}s") from None
        except aiohttp.ClientError as exc:
            raise Unreachable(f"cannot reach {self.url}: {type(exc).__name__}: {exc}") from None

    # The protocol

    async def join(self, code: str, host: dict) -> dict:
        return await self.call("POST", "/v1/worker/join", {"code": code, **host})

    async def claim(self, wait_s: float = CLAIM_WAIT_SECONDS) -> dict | None:
        answer = await self.call("POST", "/v1/worker/claim", {"wait_s": wait_s}, timeout=wait_s + CLAIM_SLACK)
        return answer.get("run") if isinstance(answer, dict) else None

    async def heartbeat(self, body: dict) -> dict:
        return await self.call("POST", "/v1/worker/heartbeat", body)

    async def doctor(self, body: dict) -> dict:
        """Send the findings of the machine's doctor (``evo_agents.worker.doctor_report``)."""
        return await self.call("POST", "/v1/worker/doctor", body)

    async def report(self, run_id: int, body: dict) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/state", body)

    async def events(self, run_id: int, events: list[dict]) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/events", {"events": events})

    async def inbox(self, run_id: int, ack: int | None = None) -> list[dict]:
        body = {} if ack is None else {"ack": ack}
        answer = await self.call("POST", f"/v1/worker/runs/{int(run_id)}/inbox", body)
        return list(answer.get("messages") or []) if isinstance(answer, dict) else []

    async def attachment(self, run_id: int, attachment_id: int, write: Callable[[bytes], None]) -> None:
        """The bytes of a file a person attached to a message the run's agent may read, handed to ``write``."""
        path = f"/v1/worker/runs/{int(run_id)}/attachments/{int(attachment_id)}/content"
        await self.download(path, write)

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

    # The profiles of a run's browser identities (evo_agents.worker.browser.profiles): no caller logs one

    async def browser_profile(self, run_id: int, identity_id: int) -> dict:
        """One browser identity of a run this worker holds: its name, domains, version and profile (base64 or
        null)."""
        path = f"/v1/worker/runs/{int(run_id)}/browser-profile"
        answer = await self.call("POST", path, {"identity_id": int(identity_id)}, timeout=UPLOAD_TIMEOUT)
        return answer if isinstance(answer, dict) else {}

    async def save_browser_profile(
        self, run_id: int, identity_id: int, archive: bytes, *, base_version: int, chromium_version: str | None
    ) -> dict:
        """Send the profile ``archive`` of a browser identity of the run as the version after ``base_version``;
        ``{identity_id, version}``."""
        query = f"base_version={int(base_version)}"
        if chromium_version:
            query += f"&chromium_version={quote(chromium_version, safe='')}"
        path = f"/v1/worker/runs/{int(run_id)}/browser-profile/{int(identity_id)}?{query}"
        headers = {"Content-Type": "application/octet-stream"}
        answer = await self.call("PUT", path, archive, timeout=UPLOAD_TIMEOUT, headers=headers)
        return answer if isinstance(answer, dict) else {}

    async def browser_needs_login(self, run_id: int, identity_id: int, cause: str) -> dict:
        """Say that the run's Chromium could not use the identity's profile (``cause``); ``{identity_id, status}``."""
        path = f"/v1/worker/runs/{int(run_id)}/browser-profile/{int(identity_id)}/needs-login"
        answer = await self.call("POST", path, {"cause": cause})
        return answer if isinstance(answer, dict) else {}

    async def browser_login(self, run_id: int, body: dict) -> dict:
        """Ask the run's dispatcher to sign in on one of its browser identities (or, for a run that cannot wait, fail
        the run): ``{outcome, decision_id, message}``."""
        answer = await self.call("POST", f"/v1/worker/runs/{int(run_id)}/browser-login", body)
        return answer if isinstance(answer, dict) else {}

    async def local_browser_login(self, body: dict) -> dict:
        """Open a login of a browser identity on this worker's own machine, with no run (``browser_local``): the
        identity (made when ``body`` says how) and its profile, as ``browser_profile`` answers, with ``project``."""
        answer = await self.call("POST", "/v1/worker/browser-identities/login", body, timeout=UPLOAD_TIMEOUT)
        return answer if isinstance(answer, dict) else {}

    async def save_local_browser_profile(
        self, identity_id: int, archive: bytes, *, base_version: int, chromium_version: str | None
    ) -> dict:
        """Send the profile ``archive`` a local login made as the version after ``base_version``; ``{identity_id,
        version}``."""
        query = f"base_version={int(base_version)}"
        if chromium_version:
            query += f"&chromium_version={quote(chromium_version, safe='')}"
        path = f"/v1/worker/browser-identities/{int(identity_id)}/profile?{query}"
        headers = {"Content-Type": "application/octet-stream"}
        answer = await self.call("PUT", path, archive, timeout=UPLOAD_TIMEOUT, headers=headers)
        return answer if isinstance(answer, dict) else {}

    async def rate_limit(self, run_id: int, body: dict) -> dict:
        """Say what the agent's CLI said of the account behind a lease of the run (``evo_agents.worker.runner.limits``):
        ``{action, secret_id, failovers, ...}``; an older hub without the route answers 404 (Refused)."""
        answer = await self.call("POST", f"/v1/worker/runs/{int(run_id)}/rate-limit", body)
        return answer if isinstance(answer, dict) else {}

    # A plan run: its plan, its steps, its decisions and its notices

    async def plan(self, run_id: int) -> dict:
        return await self.call("GET", f"/v1/worker/runs/{int(run_id)}/plan")

    async def put_plan(self, run_id: int, body: dict) -> dict:
        """An author run's plan put on the hub: ``body`` holds ``body`` and, to replace the plan, ``if_revision``."""
        return await self.call("PUT", f"/v1/worker/runs/{int(run_id)}/plan", body)

    async def chat(self, run_id: int, text: str) -> dict:
        """The last message of a turn of an author run's agent, kept as a message of the run's chat; a chat run's,
        its reply in the thread of its group chat."""
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/chat", {"text": text})

    async def in_chats(self, run_id: int, route: str, body: dict) -> dict:
        """What the agent of run ``run_id`` does in group chats (``evo_agents.hub.server.chat_agents``): ``route``
        is chat/messages, chat/reactions, chat/mentions, chats, chat/pulls for the daemon's push of a chat run, or
        chat/repos for a repo the daemon opens in a chat run for its agent."""
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/{route}", body)

    async def release_branch(self, run_id: int, body: dict) -> dict:
        """Tell the hub release run ``run_id`` pushed ``body``: its repo, branch release/<version>, head, version and
        the agent's Decision line (``evo_agents.hub.server.release_claims``)."""
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/release/branch", body)

    async def step(self, run_id: int, key: str, body: dict) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/steps/{quote(str(key), safe='')}", body)

    async def decision(self, run_id: int, body: dict) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/decisions", body)

    async def notice(self, run_id: int, body: dict) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/notices", body)

    async def backup(self, run_id: int, body: dict) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/backups", body)

    async def autonomy_act(self, run_id: int, body: dict, *, timeout: float = TIMEOUT) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/autonomy-acts", body, timeout=timeout)

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

    async def merge_judge_inputs(self, run_id: int, key: str | None) -> dict:
        """What a judge run of the hub's merge queue reads, with the run's own key."""
        return await self.call("GET", f"/v1/worker/runs/{int(run_id)}/merge-judge", headers=_judge_key(key))

    async def merge_verdict(self, run_id: int, body: dict, key: str | None) -> dict:
        """The verdict of a judge run of the hub's merge queue, with the run's own key."""
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/merge-verdict", body, headers=_judge_key(key))

    async def replay(self, run_id: int, body: dict, key: str | None) -> dict:
        """POST one arm of a replay run, or why it replays nothing, with the run's own key."""
        from evo_agents.hub.replay import REPLAY_KEY_HEADER

        headers = {REPLAY_KEY_HEADER: key} if key else {}
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/replay", body, headers=headers)

    async def history_next(self, run_id: int, body: dict, key: str | None) -> dict:
        """POST for the next batch of a history run, with the run's own key."""
        from evo_agents.hub.history import HISTORY_KEY_HEADER

        headers = {HISTORY_KEY_HEADER: key} if key else {}
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/history/next", body, headers=headers)

    async def history_report(self, run_id: int, body: dict, key: str | None) -> dict:
        """POST the facets and the names a history run's model wrote, with the run's own key."""
        from evo_agents.hub.history import HISTORY_KEY_HEADER

        headers = {HISTORY_KEY_HEADER: key} if key else {}
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/history/report", body, headers=headers)

    async def uploads(self, run_id: int, items: list[dict]) -> dict:
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/uploads", {"items": items})

    async def commit_blobs(self, run_id: int, upload_ids: list[str]) -> dict:
        body = {"upload_ids": upload_ids}
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/blobs", body, timeout=UPLOAD_TIMEOUT)

    # The outputs of a run (evo_agents.hub.outputs, docs/workers.md "Outputs")

    async def output_settings(self, run_id: int) -> dict:
        """``{tool_images, max_file_bytes, max_outputs, max_run_bytes, outputs, bytes, ...}`` of a run this worker
        holds; an older hub without the route answers 404 (Refused)."""
        answer = await self.call("GET", f"/v1/worker/runs/{int(run_id)}/output-settings")
        return answer if isinstance(answer, dict) else {}

    async def output_uploads(self, run_id: int, items: list[dict]) -> dict:
        """The presigned PUTs of the outputs ``items`` ({sha256, size, source}) whose bytes the project lacks."""
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/output-uploads", {"items": items})

    async def record_outputs(self, run_id: int, items: list[dict]) -> dict:
        """Commit the uploads of ``items`` and record them as outputs of the run: ``{outputs, added}``, in order."""
        body = {"outputs": items}
        return await self.call("POST", f"/v1/worker/runs/{int(run_id)}/outputs", body, timeout=UPLOAD_TIMEOUT)

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
