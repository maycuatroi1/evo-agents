"""The moves of a run reported to the hub, and its log and diff uploaded once it ended.

A hub that does not answer holds nothing up: each report is sent again with the backoff until the hub answers, or
says the run is no longer this worker's. Every event is sent before the report that ends the run. After the last
report the log (the run's events) and the diff are uploaded as blobs when the hub has a blob store.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from typing import TYPE_CHECKING

from evo_agents.worker.hubapi import Backoff, HubProblem, Outdated, Refused, Unreachable
from evo_agents.worker.runner.common import HARD_STOP_TRIES, ReportRefused, RunGone, log, wait_or
from evo_agents.worker.runner.transitions import bare_report, end_fields, report_body

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

LOG_LIMIT = 64 * 1024 * 1024  # the run-log blob
DIFF_LIMIT = 8 * 1024 * 1024  # the run-diff blob


class Reports:
    """The run's reports, one at a time."""

    def __init__(self, run: Run):
        self.run = run
        self.lock = asyncio.Lock()

    async def report(self, state: str, *, only_from: tuple[str, ...] | None = None, **fields) -> dict:
        """Report a move, sending it again until the hub answers. ``only_from``: send it only while the run is in one
        of these states, which a report queued behind another may no longer be."""
        run = self.run
        body = report_body(state, fields)
        async with self.lock:
            if only_from is not None and run.state not in only_from:
                return {}
            backoff = Backoff()
            stripped = False
            while True:
                try:
                    answer = await run.daemon.hub.report(run.id, body)
                except Unreachable as exc:
                    if run.daemon.hard_stop.is_set() and backoff.failures >= HARD_STOP_TRIES:
                        raise RunGone(f"the daemon stopped before the hub took the report of {state}") from None
                    delay = exc.retry_after if exc.retry_after is not None else backoff.next()
                    log.warning(
                        "report not sent; trying again",
                        extra={"run_id": run.id, "state": state, "retry_in_s": round(delay, 1), "error": str(exc)},
                    )
                    await wait_or(run.daemon.hard_stop, delay)
                    continue
                except Refused as exc:
                    if isinstance(exc, Outdated) or exc.status in (401, 403):
                        run.daemon.fatal(exc)
                        raise RunGone(str(exc)) from None
                    if exc.status == 422 and not stripped and len(body) > 2:
                        # A field the hub does not take must not keep the run from ending: send the move alone.
                        log.error(
                            "report refused; sending it without its details",
                            extra={"run_id": run.id, "error": str(exc)},
                        )
                        body = bare_report(body)
                        stripped = True
                        continue
                    raise ReportRefused(f"the hub refused the report of {state}: {exc}", exc.status) from None
                run.state = state
                run.record["state"] = state
                with contextlib.suppress(OSError):
                    run.daemon.home.save_run(run.record)
                return answer if isinstance(answer, dict) else {}

    async def report_running(self, only_from: tuple[str, ...]) -> None:
        adapter = self.run.agent.adapter
        await self.report_session("running", only_from, adapter.session_id if adapter is not None else None)

    async def report_session(self, state: str, only_from: tuple[str, ...], session_id: str | None) -> None:
        run = self.run
        try:
            await self.report(state, only_from=only_from, session_id=session_id)
            run.agent.session_reported = session_id
        except RunGone as exc:
            log.warning("run is not this worker's any more", extra={"run_id": run.id, "error": str(exc)})
            run.request_stop("gone")

    async def end(self, state: str, **fields) -> None:
        """Send every event, then the report that ends the run."""
        run = self.run
        if not await run.sender.drained():
            log.warning("ending the run with events unsent", extra={"run_id": run.id, "why": run.sender.gave_up})
        adapter = run.agent.adapter
        fields = end_fields(fields, adapter.session_id if adapter is not None else None)
        await self.report(state, **fields)
        run.ended = True
        log.info("run ended", extra={"run_id": run.id, "state": state, "error": fields.get("error")})

    async def upload(self, diff: bytes) -> None:
        """Upload the run's log and ``diff`` to the hub's blob store; a hub without one, or a failure, is logged
        only."""
        run = self.run
        payloads: dict[str, bytes] = {}
        items = []
        with contextlib.suppress(OSError):
            if 0 < run.log_path.stat().st_size <= LOG_LIMIT:
                payloads["run-log"] = run.log_path.read_bytes()
        if 0 < len(diff) <= DIFF_LIMIT:
            payloads["run-diff"] = diff
        if not payloads:
            return
        by_sha = {}
        for kind, data in payloads.items():
            sha = hashlib.sha256(data).hexdigest()
            by_sha[sha] = data
            items.append({"sha256": sha, "size": len(data), "kind": kind})
        try:
            answer = await run.daemon.hub.uploads(run.id, items)
            ids = []
            for ticket in answer.get("uploads") or []:
                await run.daemon.hub.put(ticket["url"], by_sha[ticket["sha256"]])
                ids.append(ticket["upload_id"])
            if ids:
                await run.daemon.hub.commit_blobs(run.id, ids)
            log.info("run log and diff uploaded", extra={"run_id": run.id, "kinds": sorted(payloads)})
        except (HubProblem, KeyError, TypeError) as exc:
            log.info("run log and diff not uploaded", extra={"run_id": run.id, "error": str(exc)})
