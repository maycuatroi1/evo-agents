"""An in-memory hub of the worker protocol for the daemon's tests that need no Postgres: an aiohttp server on
127.0.0.1 that answers the routes of ``/v1/worker/*`` a daemon and the commands of a plan run's agent call, with the
rules of the real hub that those tests lean on (the transition table with the worker as actor, a waiting report only
with a decision open or an answer not taken, a step done only with verify results that all exited 0).

The test drives the rest: it queues runs, answers decisions (an answer to a parked run queues the run that resumes it,
pinned, with ``resume_of_run_id`` and the session, as ``decisions._resume`` does), parks a waiting run as the reaper
would, sets what a run's ask for credentials gets (``leases``, nothing by default), and reads what the worker sent.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
from datetime import UTC, datetime, timedelta

from aiohttp import web

from evo_agents.hub import runs

TOKEN = "evw_" + "f" * 43


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FakeHub:
    def __init__(self, project: str = "demo"):
        self.project = project
        self.plans: dict[str, dict] = {}  # plan id -> {"body", "revision"}
        self.runs: dict[int, dict] = {}  # run id -> {"spec", "state", "moves", "events", ...}
        self.queue: list[int] = []
        self.decisions: dict[int, dict] = {}
        self.inbox: dict[int, list[dict]] = {}
        self.notices: list[dict] = []
        self.step_reports: list[dict] = []
        self.leases: dict[int, dict] = {}  # run id -> the {leases, missing} its ask for credentials gets
        self.credential_calls: list[tuple[str, int]] = []  # ("ask" or "give back", run id), in order
        self.heartbeats = 0
        self._ids = itertools.count(101)
        self._messages = itertools.count(1)
        self._decision_ids = itertools.count(1)
        self._queued = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self.url = ""

    # The server

    async def start(self) -> str:
        app = web.Application(middlewares=[self._auth])
        get, post = web.get, web.post
        app.add_routes(
            [
                post("/v1/worker/claim", self._claim),
                post("/v1/worker/heartbeat", self._heartbeat),
                post("/v1/worker/runs/{id}/state", self._state),
                post("/v1/worker/runs/{id}/events", self._events),
                post("/v1/worker/runs/{id}/inbox", self._inbox),
                post("/v1/worker/runs/{id}/uploads", self._uploads),
                get("/v1/worker/runs/{id}/plan", self._plan),
                post("/v1/worker/runs/{id}/steps/{key}", self._step),
                post("/v1/worker/runs/{id}/decisions", self._decision),
                post("/v1/worker/runs/{id}/notices", self._notice),
                post("/v1/worker/runs/{id}/credentials", self._credentials),
                web.delete("/v1/worker/runs/{id}/credentials", self._give_back),
            ]
        )
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        if request.headers.get(runs.PROTOCOL_HEADER) != runs.PROTOCOL_VERSION:
            return _error(426, "upgrade_required", "the worker protocol header is missing")
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return _error(401, "unauthorized", "not a worker token")
        return await handler(request)

    # What the test does

    def put_plan(self, body: dict) -> None:
        held = self.plans.get(body["id"])
        self.plans[body["id"]] = {"body": copy.deepcopy(body), "revision": (held["revision"] + 1) if held else 1}

    def queue_run(self, **spec) -> int:
        run_id = next(self._ids)
        self.runs[run_id] = {"spec": spec | {"id": run_id}, "state": "queued", "moves": [], "events": []}
        self.inbox[run_id] = []
        self.queue.append(run_id)
        self._queued.set()
        return run_id

    def queue_plan_run(
        self, plan_id: str, repos: list[dict], *, resume_of: int | None = None, session_id: str | None = None
    ) -> int:
        held = self.plans[plan_id]
        return self.queue_run(
            kind="plan",
            project=self.project,
            plan_id=plan_id,
            step_key=None,
            title=held["body"].get("title") or plan_id,
            plan_revision=held["revision"],
            attempt=1,
            max_attempts=3,
            parent_run_id=None,
            resume_of_run_id=resume_of,
            session_id=session_id,
            runtime="claude-code",
            model=None,
            mode="headless",
            approval="auto",
            timeout_min=120,
            repo=None,
            branch=None,
            repos=repos,
            lease_expires_at=_now(),
            prompt=runs.build_plan_prompt(held["body"], repos),
            plan={"revision": held["revision"], "body": copy.deepcopy(held["body"])},
        )

    def queue_step_run(self, plan_id: str, key: str, repo: str, branch: str) -> int:
        held = self.plans[plan_id]
        step = next(item for item in held["body"]["steps"] if str(item["id"]) == key)
        return self.queue_run(
            kind="step",
            project=self.project,
            plan_id=plan_id,
            step_key=key,
            title=step.get("title"),
            plan_revision=held["revision"],
            attempt=1,
            max_attempts=3,
            parent_run_id=None,
            resume_of_run_id=None,
            session_id=None,
            runtime="claude-code",
            model=None,
            mode="headless",
            approval="auto",
            timeout_min=60,
            repo=repo,
            branch=branch,
            repos=None,
            lease_expires_at=_now(),
            prompt=runs.build_prompt(held["body"], step, {"repo": repo, "branch": branch}),
            plan=None,
        )

    def answer(self, decision_id: int, option: str) -> int:
        """Answer a decision as its run's owner; the run whose inbox took the answer."""
        decision = self.decisions[decision_id]
        assert decision["state"] == "open", decision
        decision.update(state="answered", answer=option)
        run_id = decision["run_id"]
        run = self.runs[run_id]
        if run["state"] == "parked":
            spec = run["spec"]
            new_id = self.queue_plan_run(
                spec["plan_id"], spec["repos"], resume_of=run_id, session_id=run.get("session_id")
            )
            self._move(run_id, "done")
            run["resumed_by"] = new_id
            for other in self.decisions.values():
                if other["run_id"] == run_id and other["state"] == "open":
                    other["run_id"] = new_id
            run_id = new_id
        text = f"Answer to decision #{decision_id} ({decision['category']}): {decision['question']}\n"
        text += f"Chosen option: {option}."
        self.inbox[run_id].append(
            {"id": next(self._messages), "text": text, "decision_id": decision_id, "delivered": False}
        )
        return run_id

    def park(self, run_id: int) -> None:
        """What the reaper does to a run that waited too long for its owner."""
        assert self.runs[run_id]["state"] == "waiting", self.runs[run_id]["state"]
        self._move(run_id, "parked")

    def moves(self, run_id: int) -> list[str]:
        return [move for move in self.runs[run_id]["moves"]]

    def texts(self, run_id: int) -> list[str]:
        return [event["body"].get("text", "") for event in self.runs[run_id]["events"] if event["kind"] == "system"]

    async def wait_state(self, run_id: int, *states: str, timeout: float = 60.0) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while self.runs[run_id]["state"] not in states:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"run {run_id} is {self.runs[run_id]['state']}, not {' or '.join(states)}; moves "
                    f"{self.moves(run_id)}, events {self.texts(run_id)}"
                )
            await asyncio.sleep(0.05)
        return self.runs[run_id]["state"]

    # Routes

    def _move(self, run_id: int, state: str) -> None:
        run = self.runs[run_id]
        run["state"] = state
        run["moves"].append(state)

    def _run(self, request: web.Request) -> tuple[int, dict]:
        run_id = int(request.match_info["id"])
        if run_id not in self.runs:
            raise web.HTTPNotFound()
        return run_id, self.runs[run_id]

    def _held_plan_run(self, request: web.Request) -> tuple[int, dict]:
        run_id, run = self._run(request)
        if run["state"] not in runs.HELD_STATES or run["spec"]["kind"] != "plan":
            raise _refusal(404, f"run {run_id} is not held by this worker, or not a plan run")
        return run_id, run

    async def _claim(self, request: web.Request) -> web.Response:
        body = await request.json()
        deadline = asyncio.get_running_loop().time() + min(float(body.get("wait_s") or 0), 1.0)
        while not self.queue:
            self._queued.clear()
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                return web.json_response({"run": None})
            try:
                await asyncio.wait_for(self._queued.wait(), left)
            except TimeoutError:
                pass
        run_id = self.queue.pop(0)
        self._move(run_id, "leased")
        return web.json_response({"run": self.runs[run_id]["spec"]})

    async def _heartbeat(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.heartbeats += 1
        controls = []
        for run_id in body.get("runs") or []:
            run = self.runs.get(run_id)
            if run is None:
                controls.append({"id": run_id, "held": False, "state": None, "lease_expires_at": None, "cancel": True})
                continue
            open_count = sum(1 for d in self.decisions.values() if d["run_id"] == run_id and d["state"] == "open")
            if run["state"] == "parked" or (run["state"] == "done" and run.get("resumed_by")):
                controls.append(
                    {
                        "id": run_id,
                        "held": False,
                        "state": run["state"],
                        "lease_expires_at": None,
                        "cancel": False,
                        "park": True,
                        "decisions": open_count,
                    }
                )
            elif run["state"] in runs.HELD_STATES:
                waiting = sum(1 for message in self.inbox[run_id] if not message["delivered"])
                lease = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
                controls.append(
                    {
                        "id": run_id,
                        "held": True,
                        "state": run["state"],
                        "lease_expires_at": lease,
                        "cancel": bool(run.get("cancel")),
                        "inbox": waiting,
                        "decisions": open_count,
                    }
                )
            else:
                controls.append({"id": run_id, "held": False, "state": None, "lease_expires_at": None, "cancel": True})
        return web.json_response({"drain": False, "runs": controls})

    async def _state(self, request: web.Request) -> web.Response:
        run_id, run = self._run(request)
        body = await request.json()
        state, new = run["state"], body["state"]
        for name in ("session_id", "summary", "diffstat", "usage", "error", "commit_sha", "verify"):
            if body.get(name) is not None:
                run[name] = body[name]
        if state == new:
            return web.json_response({"id": run_id, "state": state})
        if state not in runs.HELD_STATES:
            return _error(404, "not_found", f"run {run_id} is not held by this worker")
        try:
            runs.check_transition(state, new, "worker")
        except runs.TransitionRefused as exc:
            return _error(409, "conflict", f"run {run_id}: {exc}")
        if new == "waiting":
            open_ = any(d["run_id"] == run_id and d["state"] == "open" for d in self.decisions.values())
            answer = any(m.get("decision_id") and not m["delivered"] for m in self.inbox[run_id])
            if not (open_ or answer):
                return _error(409, "conflict", f"run {run_id} has no open decision and no answer waiting")
        if new == "done" and run["spec"]["kind"] == "step" and not body.get("verify"):
            return _error(409, "conflict", "done needs verify results")
        self._move(run_id, new)
        return web.json_response({"id": run_id, "state": new})

    async def _events(self, request: web.Request) -> web.Response:
        run_id, run = self._run(request)
        body = await request.json()
        events = run["events"]
        for event in body["events"]:
            if event["seq"] == len(events) + 1:
                events.append(event)
        return web.json_response({"ack_seq": len(events), "stored": len(body["events"])})

    async def _inbox(self, request: web.Request) -> web.Response:
        run_id, run = self._run(request)
        if run["state"] not in runs.HELD_STATES:
            return _error(404, "not_found", f"run {run_id} is not held by this worker")
        body = await request.json() if request.can_read_body else {}
        ack = (body or {}).get("ack")
        for message in self.inbox[run_id]:
            if ack is not None and message["id"] <= ack and not message["delivered"]:
                message["delivered"] = True
                if message.get("decision_id"):
                    self.decisions[message["decision_id"]]["delivered"] = True
        waiting = [
            {
                "id": m["id"],
                "text": m["text"],
                "sent_by": "owner",
                "created_at": _now(),
                "decision_id": m["decision_id"],
            }
            for m in self.inbox[run_id]
            if not m["delivered"]
        ]
        return web.json_response({"messages": waiting})

    async def _uploads(self, request: web.Request) -> web.Response:
        self._run(request)
        return web.json_response({"uploads": []})

    async def _plan(self, request: web.Request) -> web.Response:
        _, run = self._held_plan_run(request)
        plan_id = run["spec"]["plan_id"]
        held = self.plans[plan_id]
        return web.json_response(
            {"project": self.project, "plan_id": plan_id, "revision": held["revision"], "body": held["body"]}
        )

    async def _step(self, request: web.Request) -> web.Response:
        run_id, run = self._held_plan_run(request)
        key = request.match_info["key"]
        body = await request.json()
        plan = self.plans[run["spec"]["plan_id"]]
        step = next((item for item in plan["body"]["steps"] if str(item["id"]) == key), None)
        if step is None:
            return _error(404, "not_found", f"plan has no step {key}")
        if body.get("repo") is not None and body["repo"] not in [entry["repo"] for entry in run["spec"]["repos"]]:
            return _error(422, "invalid", f"run {run_id} does not work in {body['repo']}")
        if body["status"] == "done":
            verify = body.get("verify") or []
            if not verify or any(item["exit_code"] != 0 for item in verify):
                return _error(422, "invalid", f"step {key} is done only with verify results that all exited 0")
        self.step_reports.append({"run_id": run_id, "key": key, **body})
        written = step.get("status") != body["status"]
        step["status"] = body["status"]
        if body.get("evidence"):
            step["evidence"] = body["evidence"]
        if written:
            plan["revision"] += 1
        return web.json_response(
            {
                "run_id": run_id,
                "plan_id": run["spec"]["plan_id"],
                "step_key": key,
                "status": step["status"],
                "revision": plan["revision"],
                "written": written,
            }
        )

    async def _decision(self, request: web.Request) -> web.Response:
        run_id, _ = self._held_plan_run(request)
        body = await request.json()
        if body.get("category") not in runs.DECISION_CATEGORIES:
            return _error(422, "invalid", "not a decision category")
        if not 2 <= len(body.get("options") or []) <= 6:
            return _error(422, "invalid", "2 to 6 options")
        decision_id = next(self._decision_ids)
        self.decisions[decision_id] = {"id": decision_id, "run_id": run_id, "state": "open", **body}
        return web.json_response({"id": decision_id, "run_id": run_id, "state": "open"}, status=201)

    async def _notice(self, request: web.Request) -> web.Response:
        run_id, _ = self._held_plan_run(request)
        body = await request.json()
        if body.get("kind") not in runs.NOTICE_KINDS:
            return _error(422, "invalid", "not a notice kind")
        self.notices.append({"run_id": run_id, **body})
        return web.json_response({"id": len(self.notices), "kind": "notice", **body}, status=201)

    async def _credentials(self, request: web.Request) -> web.Response:
        run_id, run = self._run(request)
        if run["state"] not in runs.HELD_STATES:
            return _error(404, "not_found", f"run {run_id} is not held by this worker")
        self.credential_calls.append(("ask", run_id))
        return web.json_response(self.leases.get(run_id) or {"leases": [], "missing": []})

    async def _give_back(self, request: web.Request) -> web.Response:
        run_id, _ = self._run(request)
        self.credential_calls.append(("give back", run_id))
        return web.json_response({"revoked": len((self.leases.get(run_id) or {}).get("leases") or [])})


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


def _refusal(status: int, message: str) -> web.HTTPException:
    """A refusal to raise from a route, in the hub's error shape."""
    error = web.HTTPNotFound if status == 404 else web.HTTPUnprocessableEntity
    return error(text=json.dumps({"error": "refused", "message": message}), content_type="application/json")
