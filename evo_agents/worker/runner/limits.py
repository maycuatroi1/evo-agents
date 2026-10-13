"""The usage limit of the account a headless agent runs on, and the move to another account (``docs/credentials.md``,
section "Accounts").

Claude Code says how much of its account's usage the session has spent in a ``rate_limit_event``, which its adapter
passes on raw as an ``output`` event: ``{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed",
"resetsAt": 1791507000, "rateLimitType": "five_hour", "overageStatus": "rejected", "isUsingOverage": false,
"unifiedWindows": {"five_hour": {"utilization": 0.01, "resetsAt": 1791507000}, ...}}}``, ``status`` being ``allowed``,
``allowed_warning`` (with ``utilization`` and ``surpassedThreshold``) or ``rejected``. While the run holds a lease of
CLAUDE_CODE_OAUTH_TOKEN, each one is reported to the hub (``WorkerHub.rate_limit``) with the lease's id, and the hub
keeps the usage on the account; an older hub refuses it and nothing else changes. The reports of one run reach the
hub in the order of the events: each usage report waits for the one before it, and the report of a limit waits for
them all, so the hub never keeps an ``allowed`` sent before a ``rejected`` as the account's last usage.

A ``rejected`` event, unless the CLI goes on with overage, means the account hit its limit: the agent is stopped. The
same holds when the turn ended with an error that reads as a usage limit (``claude_code.limit_of_text``, which the
adapter puts in ``Outcome.limit``). Once the agent ended, ``Limits.after`` reports the limit: when the hub answers
``failover`` the run asks for its leases again (``RunCredentials.swap``), which puts the new account's value in memory,
in the agent's environment and on the run's socket, notes the move, and a new agent goes on in the same session with
FAILOVER_PROMPT; else, or with no lease to move, an older hub, or one that did not answer, the run fails with cause
``rate_limit`` and the earliest time an account is ready again. Each note names an account by its secret's id, never its
name or value. A run moves at most MAX_FAILOVERS times here, whatever the hub answers.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from evo_agents.hub import runs
from evo_agents.hub.accounts import MAX_FAILOVERS
from evo_agents.worker.adapter import AgentEvent, Outcome
from evo_agents.worker.runner.common import log
from evo_agents.worker.runtimes.claude_code import OAUTH_TOKEN, unix_time

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

STATUSES = ("allowed", "allowed_warning", "rejected")
OVERAGE = ("allowed", "allowed_warning")  # an overageStatus with which a rejected limit lets the CLI go on
MAX_WINDOWS = 16
WINDOW = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# What the agent is told when it goes on in its session with another account.
FAILOVER_PROMPT = (
    "The account this session ran on hit its usage limit, and the run goes on with another account. Go on with the "
    "task of the run from where the session and the working tree stand now, and finish it as the first message of "
    f"this session asks, writing {runs.RESULT_FILE} at the end."
)


def _share(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        return None
    return float(value)


def _windows(found) -> dict:
    windows: dict[str, dict] = {}
    for name, window in (found if isinstance(found, dict) else {}).items():
        if len(windows) == MAX_WINDOWS or not isinstance(name, str) or not WINDOW.match(name):
            continue
        if isinstance(window, dict):
            item = {"utilization": _share(window.get("utilization")), "resets_at": unix_time(window.get("resetsAt"))}
            windows[name] = {key: value for key, value in item.items() if value is not None}
    return windows


def rate_limit_of(event: AgentEvent) -> dict | None:
    """The report of a ``rate_limit_event`` the agent's CLI wrote, as ``WorkerHub.rate_limit`` sends it without the
    lease: status, rate_limit_type, resets_at, utilization and windows; None for any other event. A rejected limit
    the CLI goes on past with overage counts as allowed_warning."""
    raw = event.body.get("raw") if event.kind == "output" and isinstance(event.body, dict) else None
    if not isinstance(raw, dict) or raw.get("type") != "rate_limit_event":
        return None
    info = raw.get("rate_limit_info")
    info = info if isinstance(info, dict) else {}
    status = info.get("status")
    if status not in STATUSES:
        return None
    if status == "rejected" and (info.get("isUsingOverage") is True or info.get("overageStatus") in OVERAGE):
        status = "allowed_warning"
    kind = info.get("rateLimitType")
    windows = _windows(info.get("unifiedWindows"))
    utilization = _share(info.get("utilization"))
    if utilization is None and isinstance(kind, str):
        utilization = windows.get(kind, {}).get("utilization")
    report = {
        "status": status,
        "rate_limit_type": kind if isinstance(kind, str) and WINDOW.match(kind) else None,
        "resets_at": unix_time(info.get("resetsAt")),
        "utilization": utilization,
        "windows": windows or None,
    }
    return {key: value for key, value in report.items() if value is not None}


class Limits:
    """The usage limit of the account of one run's agent, as the module says."""

    def __init__(self, run: Run):
        self.run = run
        self.rejected: dict | None = None  # the limit a rejected event of the agent that runs now gave
        self.moves = 0
        self.reports: asyncio.Task | None = None  # the last usage report sent, which the next one waits for

    def lease_id(self) -> int | None:
        """The id of the run's lease of CLAUDE_CODE_OAUTH_TOKEN, the account the hub may move; None for none."""
        for lease in self.run.credentials.leases:
            if lease.kind == "env" and lease.provider == "secret" and lease.env_var == OAUTH_TOKEN:
                return lease.id
        return None

    def see(self, agent, event: AgentEvent) -> None:
        """An event of the agent: report a rate_limit_event's usage, and stop the agent on a rejected one."""
        report = rate_limit_of(event)
        if report is None:
            return
        lease = self.lease_id()
        if report["status"] != "rejected":
            if lease is not None and self.run.credentials.hub is not None:
                self.reports = self.run.spawn(self._usage(lease, report, self.reports))
            return
        if self.rejected is None:
            self.rejected = report
            if lease is not None:
                self.run.note("The agent's account hit its usage limit: stopping the agent.")
                self.run.spawn(agent.interrupt())

    async def _usage(self, lease: int, report: dict, before: asyncio.Task | None) -> None:
        from evo_agents.worker.hubapi import HubProblem  # aiohttp is the worker extra's: a core install imports limits

        await self._sent(before)
        try:
            await self.run.credentials.hub.rate_limit(self.run.id, {"lease_id": lease, **report})
        except HubProblem as exc:  # an older hub, or one that did not answer: the usage is not kept
            log.info("usage not reported", extra={"run_id": self.run.id, "error": str(exc)})

    @staticmethod
    async def _sent(report: asyncio.Task | None) -> None:
        """Wait until ``report`` reached the hub, failed or was cancelled; raise nothing of its own."""
        if report is not None and not report.done():
            await asyncio.wait({report})

    async def after(self, outcome: Outcome) -> Outcome | None:
        """How the agent's end counts once its usage limit is reported: None when the run moved to another account
        and goes on in the same session; else ``outcome``, or one failed with cause rate_limit."""
        limit, self.rejected = self.rejected or outcome.limit, None
        if limit is None or outcome.completed:
            return outcome
        kind = f" ({limit['rate_limit_type']})" if limit.get("rate_limit_type") else ""
        said = f"the agent's account hit its usage limit{kind}"
        lease, hub = self.lease_id(), self.run.credentials.hub
        if lease is None or hub is None:
            return self._failed(outcome, f"{said}; the run holds no account of the hub to move", limit.get("resets_at"))
        if self.moves >= MAX_FAILOVERS:
            return self._failed(outcome, f"{said}; the run moved {self.moves} times already", limit.get("resets_at"))
        from evo_agents.worker.hubapi import HubProblem  # aiohttp is the worker extra's: a core install imports limits

        await self._sent(self.reports)  # the usage the agent reported before its limit reaches the hub first
        try:
            answer = await hub.rate_limit(self.run.id, {"lease_id": lease, **limit})
        except HubProblem as exc:
            why = f"{said}; the hub moved the run to no other account ({exc})"
            return self._failed(outcome, why, limit.get("resets_at"))
        if answer.get("action") == "failover" and await self.run.credentials.swap() and self.lease_id() != lease:
            self.moves += 1
            self.run.note(
                f"The agent's account hit its usage limit: the run goes on with account secret "
                f"{answer.get('secret_id')} (failover {answer.get('failovers')}) in the same session."
            )
            return None
        reason = answer.get("reason") or "the hub moved the run to no other account"
        return self._failed(outcome, f"{said}; {reason}", answer.get("ready_at") or limit.get("resets_at"))

    def _failed(self, outcome: Outcome, why: str, ready_at: str | None) -> Outcome:
        error = f"{why}; an account is ready again at {ready_at}" if ready_at else why
        self.run.note(error[0].upper() + error[1:] + ".")
        return Outcome(False, error, outcome.usage, outcome.summary, cause="rate_limit")
