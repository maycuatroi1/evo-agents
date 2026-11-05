"""The cleanup of a worker's daemon (``daemon``): at its start and every hour, the worktrees of the runs that ended go.

It asks the hub which ended runs whose worktrees it keeps had their work merged (POST /v1/worker/merged) and removes
those at once, after it committed what each worktree has not to a ref ``refs/evo-recovery/run-<id>/<folder>`` of its
checkout and noted it in recovery.jsonl; a run it holds, a parked run, one another run went on from and a directory a
run not ended uses stay. The worktrees of runs that ended more than 7 days ago go whatever the hub says. Only the
evo-run branch of a run is deleted with its worktree.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.worker import gitops
from evo_agents.worker.hubapi import HubProblem, Refused

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

log = logging.getLogger("evo_agents.worker")

CLEANUP_EVERY = 3600.0
KEEP_WORKTREES = timedelta(days=7)  # the sweep's: the worktrees of a run whose work merged go at the next cleanup
MAX_MERGED_ASKED = 200  # run ids in one POST /v1/worker/merged, as the hub takes them
# the kinds of run whose record names a directory with a worktree of each repo; a run of one step has one worktree
DIRECTORY_KINDS = (
    "plan",
    "review",
    "judge",
    "author",
    "chat",
    "task",
    "replay",
    "history",
    "release",
    "question",
    "probe",
)


def _paths(record: dict) -> list[Path]:
    """The directory and worktrees a run's record names."""
    found = [record.get("dir"), record.get("worktree")]
    found += [item.get("worktree") for item in record.get("repos") or [] if isinstance(item, dict)]
    return [Path(item) for item in found if isinstance(item, str) and item]


def _finished(record: dict, now: datetime) -> datetime:
    """When a run's record says it ended, in UTC; ``now`` for one that has not, or says it unreadably."""
    try:
        finished = datetime.fromisoformat(str(record["finished_at"])) if record.get("finished_at") else now
    except ValueError:
        return now
    return finished.replace(tzinfo=UTC) if finished.tzinfo is None else finished


def _goes_on_from(spec: dict) -> set:
    """The runs whose worktrees a run of claim ``spec`` may adopt: the one it resumes and its lost attempt."""
    lost = spec.get("lost_attempt")
    lost = lost if isinstance(lost, dict) else {}
    ids = (spec.get("resume_of_run_id"), lost.get("run_id"))
    return {int(found) for found in ids if isinstance(found, int | str) and str(found).isdigit()}


class Cleanup:
    """The worktrees and records of the runs of ``daemon`` that ended, and what removes them."""

    def __init__(self, daemon: Daemon):
        self.daemon = daemon
        self.asks_merged = True  # until a hub before POST /v1/worker/merged answers 404

    async def loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("cleaning up old worktrees failed")
            await asyncio.sleep(CLEANUP_EVERY)

    async def sweep(self, now: datetime | None = None) -> list[int]:
        """Remove the worktrees and records of runs that ended more than 7 days ago, and those of ended runs whose work
        the hub says merged (``_merged_work``), each worktree's uncommitted changes kept first (``_recover``); their
        ids."""
        home = self.daemon.home
        now = now or datetime.now(UTC)
        records = home.load_runs()
        old = {record["id"] for record in records if now - _finished(record, now) >= KEEP_WORKTREES}
        merged = await self._merged_work([record for record in records if record["id"] not in old], records)
        removed = []
        for record in records:
            run_id = record["id"]
            if run_id not in old and run_id not in merged:
                continue
            record = self._still_ended(run_id)  # read again: a run may have resumed or adopted it since
            if record is None:
                continue
            if run_id in old:
                await self.remove_worktree(record)
                log.info("old worktree removed", extra={"run_id": run_id, "worktree": record.get("worktree")})
            elif await self._recover(record) and self._still_ended(run_id) is not None:
                await self.remove_worktree(record)
                where = record.get("dir") or record.get("worktree")
                log.info("worktree of merged work removed", extra={"run_id": run_id, "worktree": where})
            else:
                continue
            await asyncio.to_thread(home.remove_run, run_id)
            removed.append(run_id)
        return removed

    def _still_ended(self, run_id: int) -> dict | None:
        """The record of run ``run_id`` as it is on disk now, while the cleanup may still remove what it names: the run
        ended, this daemon holds it no more, and no run this daemon holds resumes it or goes on from its lost attempt
        (a run that went on from it already took its worktrees out of the record, ``Conversation.adopt``). Else None."""
        held = self.daemon.runs
        record = self.daemon.home.load_run(run_id)
        if record is None or run_id in held or not record.get("finished_at"):
            return None
        if any(run_id in _goes_on_from(run.spec) for run in held.values()):
            return None
        return record

    async def _merged_work(self, young: list[dict], records: list[dict]) -> set[int]:
        """Of the runs of ``young`` (within their 7 days) whose worktrees this worker may remove (``_removable``, over
        all its ``records``), those the hub says the work of merged (POST /v1/worker/merged); none without a hub, from
        a hub before that route (asked once a daemon), or when it cannot answer now."""
        hub = self.daemon.hub
        if hub is None or not self.asks_merged:
            return set()
        asked = [record["id"] for record in young if self._removable(record, records)]
        found: set[int] = set()
        for start in range(0, len(asked), MAX_MERGED_ASKED):
            try:
                found |= set(await hub.merged(asked[start : start + MAX_MERGED_ASKED]))
            except Refused as exc:
                if exc.status in (404, 405):
                    self.asks_merged = False
                    log.info("the hub does not say which runs merged: their worktrees wait out the 7 days")
                else:
                    log.warning("the hub refused to say which runs merged", extra={"status": exc.status})
                break
            except HubProblem as exc:
                log.info("the hub did not say which runs merged; the next cleanup asks", extra={"error": str(exc)})
                break
        return found

    def _removable(self, record: dict, records: list[dict]) -> bool:
        """Whether ``record``'s run is one whose worktrees may go once its work merged: it ended and this daemon holds
        it no more, it is not parked, no run went on from it (``Conversation.adopt``, ``adopt_lost``), its record names
        a worktree, and no run that has not ended uses its directory or one of its worktrees."""
        held = self.daemon.runs
        if record["id"] in held or not record.get("finished_at") or record.get("resumed_by"):
            return False
        if str(record.get("state") or "").startswith("parked") or record.get("moved_to"):
            return False
        mine = _paths(record)
        if not mine:
            return False
        for other in records:
            if other["id"] == record["id"] or (other.get("finished_at") and other["id"] not in held):
                continue
            if any(a == b or a in b.parents or b in a.parents for a in mine for b in _paths(other)):
                return False
        return True

    async def _recover(self, record: dict) -> bool:
        """Commit what each worktree of ``record``'s run has not committed to a ref of its checkout
        (``gitops.save_wip``, ``refs/evo-recovery/run-<id>/<folder>``) and add a line for it to the worker's recovery
        file (``WorkerHome.note_recovery``): the run, the repo, the branch, HEAD and the WIP commit. Whether every
        worktree was kept or had nothing to keep; the run's worktrees stay when one could not be."""
        run_id = record["id"]
        items = record.get("repos") if record.get("kind") in DIRECTORY_KINDS else [record]
        for item in items or []:
            if not isinstance(item, dict) or not item.get("worktree") or not item.get("checkout"):
                continue
            path, checkout = Path(item["worktree"]), Path(item["checkout"])
            if not path.is_dir() or not checkout.is_dir():
                continue
            ref = f"{gitops.RECOVERY_REFS}run-{int(run_id)}/{path.name}"
            try:
                async with self.daemon.repo_lock(checkout):
                    wip = await gitops.save_wip(path, ref, f"WIP of run {run_id}, kept before its worktree went")
            except gitops.GitError as exc:
                log.warning(
                    "the uncommitted changes of a merged run were not kept: its worktree stays",
                    extra={"run_id": run_id, "worktree": str(path), "error": str(exc)},
                )
                return False
            if wip is not None:
                self.daemon.home.note_recovery(
                    {
                        "run": run_id,
                        "repo": item.get("repo"),
                        "branch": item.get("local_branch") or item.get("branch"),
                        "head": wip.head,
                        "wip": wip.wip,
                        "ref": wip.ref,
                        "checkout": str(checkout),
                    }
                )
                log.info("uncommitted changes kept", extra={"run_id": run_id, "ref": wip.ref, "wip": wip.wip})
        return True

    async def remove_worktree(self, record: dict) -> None:
        """Remove a run's worktree and its evo-run branch; for a plan run, each repo's, then the run's directory. A
        plan run whose worktrees a resumed run took over names none of them any more."""
        if record.get("kind") in DIRECTORY_KINDS:
            for item in record.get("repos") or []:
                if isinstance(item, dict):
                    await self._remove_one(item)
            await self._remove_one({"worktree": record.get("dir")})
        else:
            await self._remove_one(record)

    async def _remove_one(self, item: dict) -> None:
        path = Path(item["worktree"]) if item.get("worktree") else None
        checkout = Path(item["checkout"]) if item.get("checkout") else None
        if checkout is not None and checkout.is_dir():
            async with self.daemon.repo_lock(checkout):
                if path is not None and path.exists():
                    with contextlib.suppress(gitops.GitError):
                        await gitops.remove_worktree(checkout, path)
                await gitops.prune_worktrees(checkout)
                branch = item.get("local_branch")
                if isinstance(branch, str) and branch.startswith("evo-run/"):
                    await gitops.delete_branch(checkout, branch)
        worktrees = self.daemon.home.worktrees_dir.resolve()
        if path is not None and path.exists() and path.resolve().is_relative_to(worktrees):
            await asyncio.to_thread(shutil.rmtree, path, True)
