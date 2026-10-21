"""A history run (kind ``history``, ``HistoryRun``): the night's reading of the project's sessions and runs with a cheap
model, which writes a facet of each that has none (``evo_agents.hub.history``).

The claim names how the run reads its batches and hands it the run's own key, which only this object holds. Once it
holds its leases (the runtime's login, as the charter's env_secrets give it; it has no repo), the run works in a new
empty directory of its own, and asks the hub for batch after batch (POST /v1/worker/runs/{id}/history/next, with the
key): up to ``history.BATCH_ITEMS`` items, the latest first, each an excerpt cut around its errors and corrections.
Each batch is one new session of the agent, with the run's runtime and model, on the batch's prompt
(``history.facet_prompt``), at most ``history.BATCH_TURNS`` turns, and what is left of the run's caps; its last message
ends with the facets as one JSON object (``history.facets_of``). The worker posts each facet, and why an item got none
(POST .../history/report).

The run stops at its caps rather than failing on them: before a session, when what is left of its cost, time or turns
(the smallest share) is no more than the largest share a session took so far, it starts none; and a session a cap
stops ends the run, the items of its batch left for the next night. Once the facets are written, or ``history.NAMING_SHARE`` of the caps is all
that is left, it asks the hub for the groups of facets that have no name yet, has the model name them in one more
session (``history.naming_prompt``) and posts the names; then it goes on with the facets while the caps let it. It
ends done with the usage of every session added up, having pushed nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import curator, history
from evo_agents.worker.hubapi import HubProblem
from evo_agents.worker.runner import access
from evo_agents.worker.runner.common import RunFailed, cut
from evo_agents.worker.runner.conversation import Conversation
from evo_agents.worker.runner.directory import RunDirectory
from evo_agents.worker.runner.sessions import LARGEST, add_usage
from evo_agents.worker.runner.transitions import MAX_SUMMARY_CHARS

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


class Turns:
    """The turns of one session of the agent, from its usage events."""

    def __init__(self):
        self.turns = 0

    def __call__(self, kind: str, body: dict) -> None:
        if kind == "usage_update" and isinstance(body.get("numTurns"), int):
            self.turns += body["numTurns"]


def _cost(usage) -> float:
    return curator.run_cost(usage) if isinstance(usage, dict) else 0.0


class HistoryRun:
    """A history run: batches of excerpts read by the agent in sessions of their own, each batch's facets posted, then
    the new groups of facets named; nothing pushed."""

    parks = False
    chat = False

    def __init__(self, run: Run):
        self.run = run
        spec = (run.curator or {}).get("history") or {}
        self.batch_turns = int(spec.get("batch_turns") or history.BATCH_TURNS)
        self.naming_share = float(spec.get("naming_share") or history.NAMING_SHARE)
        run.title = run.spec.get("title") or "history"
        self.directory = RunDirectory(run)
        self.conversation = Conversation(run, self.directory, after_turn=self.no_more)
        self.total = curator.budget_of(run.spec) or {}  # the run's caps, which its sessions share
        self.spent_usd = 0.0
        self.turns = 0
        self.used: list[float] = []  # the share of the caps each session took
        self.usages: list = []
        self.counts = {"stored": 0, "stale": 0, "failed": 0, "named": 0}
        self.batches = 0
        self.waiting: int | None = None
        self.capped: str | None = None  # the cap that stopped a session, which ends the run

    async def no_more(self) -> list[dict]:
        return []  # the agent of a batch has one turn: nobody answers it

    async def steps(self) -> None:
        run = self.run
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: the reading of project {run.project}'s history "
            f"with {run.spec.get('model') or 'the runtime model'} on {run.runtime}, in batches, timeout "
            f"{run.timeout_s // 60} min of agent time. It reads no repo and pushes nothing."
        )
        cls = run.agent.runtime_class()
        await access.take_credentials(run, [])
        run.check()
        self.directory.claim("queue the history run again")
        self.directory.enter()
        self.directory.save()
        emptied = await self.facets(cls, keep=self.naming_share)
        await self.name(cls)
        if not emptied:
            await self.facets(cls, keep=0.0)
        await self.end()

    # The caps

    def room(self) -> float:
        """The share of the run's caps still left: of its cost, its agent time and its turns, the smallest."""
        run, total, shares = self.run, self.total, []
        if total.get("max_usd"):
            shares.append(1 - self.spent_usd / total["max_usd"])
        if total.get("max_seconds"):
            shares.append(1 - (run.watchdog.spent_before + run.agent_seconds()) / total["max_seconds"])
        if total.get("max_turns"):
            shares.append(1 - self.turns / total["max_turns"])
        return min(shares) if shares else 1.0

    def fits(self, keep: float) -> bool:
        """Whether one more session fits in what is left, ``keep`` of the caps set aside: what is left is at least
        what the largest session took so far, and at least the cost a run starts with."""
        if self.capped is not None:
            return False
        if self.total.get("max_usd") is not None and self.total["max_usd"] - self.spent_usd < curator.MIN_RUN_USD:
            return False
        return self.room() - max(self.used, default=0.0) > keep

    def session_budget(self) -> dict | None:
        """The caps of the next session: what is left of the run's cost and turns (at most BATCH_TURNS), and its agent
        time, which counts across the run."""
        if not self.total:
            return None
        found = {key: self.total.get(key) for key in curator.BUDGET_KEYS}
        if found.get("max_usd") is not None:
            found["max_usd"] = round(max(found["max_usd"] - self.spent_usd, curator.MIN_RUN_USD), 4)
        turns = found.get("max_turns")
        found["max_turns"] = self.batch_turns if turns is None else max(1, min(self.batch_turns, turns - self.turns))
        return {**found, "spent_usd": 0.0, "spent_seconds": 0.0}

    # The sessions

    async def ask(self, cls, prompt: str) -> str | None:
        """One new session of the agent on ``prompt``: its last message, or None when a cap of the run stopped it,
        which ends the run (``capped``)."""
        run = self.run
        before = self.room()
        budget = self.session_budget()
        if budget is not None:
            run.spec["budget"] = budget
        run.watchdog.spent_usd = 0.0  # a new session, whose cost starts at nothing
        turns = Turns()
        run.listeners.append(turns)
        usage, message = None, None
        try:
            await self.conversation.turns(cls, prompt, None)
            usage = run.outcome.usage if run.outcome else None
            message = run.outcome.summary if run.outcome else None
        except RunFailed as exc:
            usage = exc.fields.get("usage")
            if not exc.fields.get("cap"):
                raise
            self.capped = exc.fields["cap"]
            run.note(f"The run's {self.capped} cap stopped the agent: the items of its batch wait for the next night.")
        finally:
            run.listeners.remove(turns)
            self.usages.append(usage)
            self.spent_usd += _cost(usage)
            self.turns += turns.turns or 1
            self.used.append(max(before - self.room(), 0.0))
        return message

    async def facets(self, cls, *, keep: float) -> bool:
        """Batches of facets while one fits in what is left beyond ``keep`` of the caps; whether nothing waits."""
        while self.fits(keep):
            self.run.check()
            batch = await self.call("next", {"clusters": False})
            self.waiting = batch.get("waiting")
            items = batch.get("items") or []
            if not items:
                return True
            message = await self.ask(cls, history.facet_prompt(self.run.project, items))
            self.batches += 1
            if message is None and self.capped is not None:
                return False
            found, problems = history.facets_of(message, [item["item"] for item in items])
            reported = []
            for item in items:
                name = item["item"]
                entry = {"item": name, "fingerprint": item["fingerprint"]}
                reported.append(
                    {**entry, "facet": found[name]} if name in found else {**entry, "error": problems[name]}
                )
            await self.report({"facets": reported})
        return False

    async def name(self, cls) -> None:
        """Have the model name the groups of facets that have no name yet, when one more session fits."""
        if not self.fits(0.0):
            return
        batch = await self.call("next", {"clusters": True})
        groups, named = batch.get("groups") or [], batch.get("named") or []
        if not groups:
            return
        message = await self.ask(cls, history.naming_prompt(self.run.project, groups, named))
        if message is None:
            return
        names = history.names_of(message, [group["key"] for group in groups], [item["key"] for item in named])
        if names:
            await self.report({"names": [{"key": key, **name} for key, name in sorted(names.items())]})

    # The hub

    async def call(self, what: str, body: dict) -> dict:
        run = self.run
        hub = run.daemon.hub
        send = hub.history_next if what == "next" else hub.history_report
        try:
            return await send(run.id, body, run.history_key)
        except HubProblem as exc:
            raise RunFailed(f"the hub did not answer the history run's {what}: {exc}") from None

    async def report(self, body: dict) -> None:
        found = await self.call("report", body)
        for key in self.counts:
            self.counts[key] += int(found.get(key) or 0)
        if isinstance(found.get("waiting"), int):
            self.waiting = found["waiting"]

    async def end(self) -> None:
        """End the run done, with the usage of every session added up."""
        run, counts = self.run, self.counts
        said = (
            f"Wrote {counts['stored']} facet(s) of the model in {self.batches} batch(es) and named "
            f"{counts['named']} group(s); {counts['failed']} item(s) got none and wait, {counts['stale']} changed "
            "since they were read."
        )
        left = "Nothing waits." if not self.waiting else f"{self.waiting} item(s) wait for the next night."
        if self.capped is not None:
            left = f"The {self.capped} cap stopped it; {self.waiting or 'the other'} item(s) wait for the next night."
        summary = f"{said} {left}"
        run.summary = summary
        run.note(summary, **counts)
        usage = None
        for found in self.usages:
            usage = add_usage(usage, found, largest=LARGEST)
        await self.conversation.ensure_running()
        await run.reports.report("verifying")
        run.check()
        await run.reports.end("done", summary=cut(summary, MAX_SUMMARY_CHARS), usage=usage)

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return []  # its directory holds no repo: the watchdog compares no protected path, only the caps

    async def diff(self) -> bytes:
        """A history run changes nothing of the project: no diff is uploaded."""
        return b""

    async def release_branch(self) -> None:
        return None  # it checks out no branch of a checkout
