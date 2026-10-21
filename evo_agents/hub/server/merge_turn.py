"""The turn of the hub's merge queue on GitLab (plan gitlab-merge-queue, criterion a5): the merge requests whose gates
passed (``ready``, ``evo_agents.hub.server.merge_gate``) are merged into their target branch one at a time, each with
the exact head its pipeline passed on. ``take_turns`` moves the turn of one target branch on, in a pass of the job
hub.merge_queue over its repo.

Each target branch of a repo has one queue, and one merge request of it has the turn at a time (``merge_gate.
TURN_STATES``): the one ready first (``queued_at``; a merge request its turn sends back keeps its place, so it stays
at the head of the queue), unless it waits for something on GitLab (a draft), and once each merge request merged
before it reached the policy's ``merge.hold_until`` (``merge_gate.holds_turn``: ``merged``, the default, holds
nothing; ``uat_check`` and ``prod_check`` wait for that check of the merge, a line of the ledger what follows a merge
writes). Its turn:

- ``rebasing``: the hub reads the head of the target branch (``before_sha``), asks GitLab to rebase the merge request
  onto it, and reads it until GitLab is done. GitLab failing to rebase it blocks it, and so does a rebase still running
  after ``merge.pipeline_minutes``. A head the gates read that already holds the target branch's head
  (``gitlab.merge_base``; a revert opened on that head does) is not rebased: GitLab would start another pipeline on
  that same head, and merge it only once that pipeline ends, while the head pipeline it shows is still the green one
  before it.
- ``testing``: the target branch moving, or GitLab saying the merge request needs a rebase, sends it back to
  ``ready`` at the head of the queue, its next turn rebasing it again; after MAX_REBASES rebases again in a row it is
  blocked. A push of someone else sends it back to its gates (``gating``). Then the pipeline of the new head: one that
  fails runs again once (the ledger's ``retried``) and failing again blocks it, as does one not green after
  ``merge.pipeline_minutes``. Green, the cheap gates are read again at that head (0 the repo, 1 the pipeline, 3 score
  hacking) with its patch: the patch judged goes on to its merge with the Judges' verdicts; a new patch sends it to its
  gates and Judges again, keeping its place.
- ``merging``: GitLab merges it only while its head is still the sha its pipeline passed on (``gitlab.merge``), by
  fast-forward, so ``after_sha`` is that head. A push in between (409) sends it back to its gates; GitLab refusing the
  merge because the target branch moved sends it back to ``ready``, because a pipeline of its head still runs
  (``still_running``: one GitLab started after the head pipeline the hub read) back to ``testing`` to wait for it, for
  any other reason blocks it with GitLab's words. A merge GitLab made while the hub did not hear its answer is read
  from GitLab at the next move.

``merged`` is where what follows a merge takes it on (``evo_agents.hub.server.post_merge``): a revert merged reverts
its culprit, and the merge may be drawn for an audit sample. A revert the hub opened goes first in line, and no merge
before it holds its turn back. The Curator's change of a merge request merged is ``merged`` too, with its line in the
proposal's ledger. Each move is a line of the queue's ledger
(merge_events: ``rebased``, ``pipeline``, ``retried``, ``gate``, ``head``, ``queued``, ``merged``), each merge a line of
audit (``merge.merge``), and the hub comments once on a merge request its turn blocked (``merge_gate.RepoPass``).

One merge request of a target branch has the turn, whatever runs the queue: the job holds the procrastinate lock of
its name, so one pass runs at a time on all the hub's job workers, and the unique index merge_queue_turn_key (schema
0082) refuses a second merge request of the branch in a turn state. Each move is written to Postgres before the GitLab
call it leads to, and what GitLab did is read back at the next move, so a hub that restarts mid-turn goes on where it
was. The forge secret's token stays in the GitLab client; no row, ledger line, audit line or log line holds it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import merge_gate, post_merge, tables
from evo_agents.hub.server import audit, ledger
from evo_agents.hub.server import merge_gate as queue
from evo_agents.hub.server import post_merge as after_merge
from evo_agents.hub.server.gitlab import GitLabRefused, MergeRequest, NotMergeable, ShaMismatch

log = logging.getLogger(__name__)

MAX_MOVES = 20  # moves of the turn of one target branch in one pass
RETRYABLE = frozenset({"failed", "canceled"})  # a pipeline the turn runs again once
NEEDS_REBASE = "need_rebase"  # GitLab's detailed_merge_status of a merge request behind its fast-forward target
CI_RUNNING = "ci_still_running"  # ... of one GitLab merges only once the pipeline of its head ends


async def take_turns(found: queue.RepoPass) -> None:
    """Move the turn of the target branch of ``found``, a pass over one repo, as far as it goes in this pass."""
    turn = Turn(found)
    for _ in range(MAX_MOVES):
        row_id = await turn.holder()
        if row_id is None:
            row_id = await turn.start()
        if row_id is None:
            return
        try:
            moved = await turn.move(row_id)
        except GitLabRefused as exc:
            await found.refused(row_id, exc)
            return
        if not moved:
            return


def still_running(mr: MergeRequest) -> bool:
    """Whether GitLab says a pipeline of the head of ``mr`` still runs, by its merge status or the head pipeline it
    shows: GitLab merges it only once that pipeline ends."""
    pipeline = mr.head_pipeline
    return mr.detailed_merge_status == CI_RUNNING or (pipeline is not None and pipeline.status in merge_gate.RUNNING)


async def curator_merged(conn: AsyncConnection, row, after: str) -> None:
    """The Curator's change that merge request ``row`` carries is merged at ``after``, with its line in the proposal's
    ledger (``ledger.change_moved``)."""
    c = tables.curator_changes
    old = (await conn.execute(select(c.c.state).where(c.c.id == row.change_id).with_for_update())).scalar_one_or_none()
    if old is None or old == "merged":
        return
    values = {"state": "merged", "merged_at": func.now(), "merge_sha": after, "reason": None, "updated_at": func.now()}
    await conn.execute(update(c).values(values).where(c.c.id == row.change_id))
    extra = {"actor": "curator", "before_sha": row.before_sha, "merge_request": row.iid, "into": row.target_branch}
    await ledger.change_moved(conn, row.change_id, old, "merged", extra)


class Turn:
    """The turn of one target branch of a repo, in a pass over the repo (``queue.RepoPass``)."""

    def __init__(self, found: queue.RepoPass):
        self.found = found
        self.engine, self.gl, self.path, self.target = found.engine, found.gl, found.path, found.target
        self.minutes: int = found.project.policy["merge"]["pipeline_minutes"]

    def _branch(self):
        q, found = tables.merge_queue, self.found
        return (q.c.project_id == found.project.id) & (q.c.repo == found.repo) & (q.c.target_branch == self.target)

    # Whose turn

    async def holder(self) -> int | None:
        """The merge request whose turn it is; None when none has it."""
        q = tables.merge_queue
        async with self.engine.begin() as conn:
            query = select(q.c.id).where(self._branch(), q.c.state.in_(merge_gate.TURN_STATES)).limit(1)
            return (await conn.execute(query)).scalar_one_or_none()

    async def start(self) -> int | None:
        """Give the turn to the merge request ready first, a revert before any other, unless it waits for something
        on GitLab or one merged before holds the turn (``held``; a revert is never held), or the repo's breaker tripped
        (a revert alone goes on); its id, or None."""
        q = tables.merge_queue
        not_waiting = or_(q.c.reason.is_(None), ~q.c.reason.startswith(merge_gate.WAITS, autoescape=True))
        query = (
            select(q)
            .where(self._branch(), q.c.state == "ready", q.c.iid.is_not(None), not_waiting)
            .order_by(q.c.source != post_merge.REVERT, q.c.queued_at.asc().nulls_last(), q.c.id)
            .limit(1)
            .with_for_update()
        )
        if self.found.tripped:  # the repo's breaker tripped: a revert alone takes the turn
            query = query.where(q.c.source == post_merge.REVERT)
        async with self.engine.begin() as conn:
            row = (await conn.execute(query)).one_or_none()
            if row is None:
                return None
            held = None if row.source == post_merge.REVERT else await self.held(conn)
            if held is not None:
                if held != row.reason:
                    await queue.set_row(conn, row.id, reason=held)
                return None
            try:
                async with conn.begin_nested():
                    await queue.set_row(
                        conn,
                        row.id,
                        state="rebasing",
                        reason=None,
                        before_sha=None,
                        head_at=func.now(),
                        rebases=q.c.rebases + 1,
                    )
            except IntegrityError:  # another merge request of the branch has the turn (merge_queue_turn_key)
                return None
        return row.id

    async def held(self, conn: AsyncConnection) -> str | None:
        """Why the next turn waits for a merge request merged before it (``merge.hold_until``); None when nothing
        holds it."""
        hold = self.found.project.policy["merge"]["hold_until"]
        if not merge_gate.holds_turn(hold, ()):
            return None
        q, e = tables.merge_queue, tables.merge_events
        merged = select(q.c.id, q.c.iid).where(self._branch(), q.c.state.in_(merge_gate.HOLDING_STATES))
        for row in (await conn.execute(merged.order_by(q.c.merged_at, q.c.id))).all():
            passed = select(e.c.kind).where(e.c.queue_id == row.id, e.c.kind == hold, e.c.passed.is_(True))
            if merge_gate.holds_turn(hold, [kind for (kind,) in await conn.execute(passed)]):
                return merge_gate.reason_line(
                    f"its turn waits for !{row.iid}, merged before it, to pass its {hold} (merge.hold_until)"
                )
        return None

    # Its moves

    async def move(self, row_id: int) -> bool:
        """One move of the merge request whose turn it is; whether the turn may move again in this pass."""
        async with self.engine.begin() as conn:
            row = await queue.row_of(conn, row_id)
        if row is None or row.state not in merge_gate.TURN_STATES:
            return True
        mr = await self.gl.merge_request(self.path, row.iid)
        if row.state == "merging" and mr.state == "merged" and mr.sha == row.head_sha:
            return await self.merged(row_id, mr)  # GitLab merged it while the hub did not hear its answer
        if not await self.still_queued(row_id, mr):
            return True
        if row.state == "rebasing":
            return await self.rebasing(row, mr)
        if row.state == "testing":
            return await self.testing(row, mr)
        return await self.merging(row, mr)

    async def still_queued(self, row_id: int, mr: MergeRequest) -> bool:
        """Whether merge request ``mr`` keeps its turn: out of scope it leaves the queue, waiting for something on
        GitLab it is ready again and waits, and a push of someone else sends it back to its gates."""
        async with self.engine.begin() as conn:
            row = await queue.row_of(conn, row_id, lock=True)
            if row is None or row.state not in merge_gate.TURN_STATES:
                return False
            verdict, why = self.found.scope_of(row, mr)
            if verdict == "out":
                await queue.close(conn, row, f"it left the queue: {why}")
                self.found.outcomes["closed"] += 1
                return False
            if verdict == "wait":
                await self.back(conn, row, merge_gate.reason_line(merge_gate.WAITS + why))
                return False
            if row.state != "rebasing" and mr.sha != row.head_sha:
                await self.found.new_head(conn, row, mr)
                return False
        return True

    async def rebasing(self, row, mr: MergeRequest) -> bool:
        """Ask GitLab to rebase the merge request onto the head of the target branch, then read it until it did. A
        head the gates read that already holds the target branch's head is not rebased: GitLab would start another
        pipeline on that same head, which the head pipeline it shows does not name yet."""
        if row.before_sha is None:
            base = await self.gl.branch_head(self.path, self.target)
            if base is None:
                return await self.waits(row, "rebase", f"branch {self.target} is not on GitLab")
            if mr.sha and mr.sha == row.head_sha and await self.gl.merge_base(self.path, mr.sha, base) == base:
                return await self.rebased(row.id, mr, base)
            async with self.engine.begin() as conn:
                await queue.set_row(conn, row.id, before_sha=base)
            await self.gl.rebase(self.path, row.iid)
            return True
        if mr.rebase_in_progress:
            return await self.waits(row, "rebase", "GitLab is rebasing it")
        if mr.merge_error and mr.sha == row.head_sha:
            why = f"GitLab could not rebase it onto {self.target}: {mr.merge_error}"
            return await self.blocked(row.id, why, stage="rebase", kind="rebased")
        return await self.rebased(row.id, mr)

    async def rebased(self, row_id: int, mr: MergeRequest, base: str | None = None) -> bool:
        """The merge request sits on the target branch, rebased onto the head the hub read before asking, or already
        on top of ``base`` with no rebase asked: its turn tests its head."""
        async with self.engine.begin() as conn:
            locked = await queue.row_of(conn, row_id, lock=True)
            if locked is None or locked.state != "rebasing" or not mr.sha:
                return False
            before = base or locked.before_sha
            new = mr.sha != locked.head_sha
            values = {"head_at": func.now()} if new else {}  # its pipeline's wait counts from its new head
            await queue.set_row(
                conn, row_id, state="testing", reason=None, head_sha=mr.sha, before_sha=before, **values
            )
            onto = f"{self.target} at {before[:12]}"
            why = f"rebased onto {onto}: its head is {mr.sha[:12]}" if new else f"already on top of {onto}"
            detail = {"from": locked.head_sha, "to": mr.sha, "onto": before, "rebases": locked.rebases}
            if base is not None:
                why += ": not rebased, which would only start its pipeline again"
                detail["asked"] = False
            await queue.record(conn, locked, "rebased", passed=True, reason=why, sha=mr.sha, detail=detail)
        return True

    async def testing(self, row, mr: MergeRequest) -> bool:
        """The target branch where the rebase left it, the project's settings, then the pipeline of the new head."""
        moved = await self.target_moved(row, mr)
        if moved is not None:
            return await self.again(row.id, moved)
        missing = await self.found.repo_check()
        if missing:  # it waits at gate 0 for the settings, and gives the turn back meanwhile
            async with self.engine.begin() as conn:
                locked = await queue.row_of(conn, row.id, lock=True)
                await queue.hold(conn, locked, 0, "the GitLab project " + "; ".join(missing))
            return True
        pipeline = mr.head_pipeline
        state, why = merge_gate.pipeline_state(
            mr.sha,
            pipeline.sha if pipeline else None,
            pipeline.status if pipeline else None,
            await self.found.head_message(mr),
        )
        if state == "pending":
            return await self.waits(row, "pipeline", why)
        if state == "red":
            return await self.red(row, mr, why)
        return await self.green(row, mr)

    async def target_moved(self, row, mr: MergeRequest) -> str | None:
        """Why the merge request needs a rebase again: the target branch moved since its rebase, or GitLab says so;
        None when it sits on the target branch."""
        head = await self.gl.branch_head(self.path, self.target)
        if head != row.before_sha:
            return f"{self.target} moved to {head[:12] if head else 'nothing'} during its turn"
        if mr.detailed_merge_status == NEEDS_REBASE:
            return f"GitLab says it needs a rebase onto {self.target}"
        return None

    async def red(self, row, mr: MergeRequest, why: str) -> bool:
        """A pipeline that failed at the rebased head runs again once, to rule a flake out; failing again, skipped or
        asked to skip CI, it blocks the merge request."""
        pipeline = mr.head_pipeline
        e = tables.merge_events
        async with self.engine.begin() as conn:
            done = exists().where(e.c.queue_id == row.id, e.c.kind == "retried", e.c.sha == mr.sha)
            retried = bool((await conn.execute(select(done))).scalar_one())
        if pipeline is not None and pipeline.status in RETRYABLE and not retried:
            await self.gl.retry_pipeline(self.path, pipeline.id)
            async with self.engine.begin() as conn:
                locked = await queue.row_of(conn, row.id, lock=True)
                reason = merge_gate.turn_reason("pipeline", f"{why}: the hub runs it again once")
                await queue.set_row(conn, row.id, reason=reason, head_at=func.now())
                detail = {"pipeline": pipeline.id}
                await queue.record(conn, locked, "retried", reason=reason, sha=mr.sha, detail=detail)
            return False
        after = " after a retry" if retried else ""
        async with self.engine.begin() as conn:
            locked = await queue.row_of(conn, row.id, lock=True)
            if locked is None or locked.state != "testing":
                return False
            detail = {"pipeline": pipeline.id if pipeline else None}
            await queue.record(conn, locked, "pipeline", passed=False, reason=why + after, sha=mr.sha, detail=detail)
            await queue.hold(conn, locked, 1, f"at its rebased head, {why}{after}", blocked=True)
        return True

    async def green(self, row, mr: MergeRequest) -> bool:
        """Green at the rebased head: gate 3 and the patch read again there. The patch the Judges judged goes on to
        its merge; a new one goes to its gates and Judges again, keeping its place."""
        async with self.engine.begin() as conn:
            _, body = await self.found.expected_branch(conn, row)
        files = merge_gate.files_of(await self.gl.merge_request_diffs(self.path, mr.iid))
        hacked = self.found.hacked(files, body) if row.source != post_merge.REVERT else None
        if hacked is not None:
            return await self.blocked(row.id, f"at its rebased head, {hacked}", gate=3)
        same = merge_gate.patch_fingerprint(files) == row.fingerprint
        async with self.engine.begin() as conn:
            locked = await queue.row_of(conn, row.id, lock=True)
            if locked is None or locked.state != "testing" or locked.head_sha != mr.sha:
                return False
            detail = {"pipeline": mr.head_pipeline.id}
            why = "green at its rebased head"
            await queue.record(conn, locked, "pipeline", passed=True, reason=why, sha=mr.sha, detail=detail)
            for gate in (0, 1, 3):
                why = "read again at its rebased head"
                await queue.record(conn, locked, "gate", gate=gate, passed=True, reason=why, sha=mr.sha)
            if same:
                await queue.set_row(conn, row.id, state="merging", reason=None)
                return True
            await queue.set_row(conn, row.id, state="gating", reason=None)
            why = f"its rebase onto {self.target} changed its patch: its gates and Judges start again, at its place"
            await queue.record(conn, locked, "head", reason=why, sha=mr.sha)
        await self.found.gate(row.id, mr)
        return True

    async def merging(self, row, mr: MergeRequest) -> bool:
        """Merge the merge request while its head is still the one its pipeline passed on; GitLab fast-forwards the
        target branch to it."""
        try:
            merged = await self.gl.merge(self.path, row.iid, row.head_sha)
        except ShaMismatch as exc:
            now = await self.gl.merge_request(self.path, row.iid)
            if now.sha == row.head_sha:
                return await self.blocked(row.id, f"GitLab refused the merge: {exc}", stage="merge", kind="merged")
            async with self.engine.begin() as conn:  # pushed since the hub read it: its gates read the new head
                locked = await queue.row_of(conn, row.id, lock=True)
                if locked is not None and locked.state == "merging":
                    await self.found.new_head(conn, locked, now)
            return True
        except NotMergeable as exc:
            moved = await self.target_moved(row, mr)
            if moved is not None:
                return await self.again(row.id, moved)
            if still_running(await self.gl.merge_request(self.path, row.iid)):
                why = f"GitLab would not merge it yet ({exc.status}): a pipeline of its head still runs"
                return await self.retest(row.id, why)
            return await self.blocked(row.id, f"GitLab would not merge it: {exc}", stage="merge", kind="merged")
        return await self.merged(row.id, merged)

    async def merged(self, row_id: int, mr: MergeRequest) -> bool:
        """GitLab merged the merge request: its row, its ledger, the audit, and the Curator's change it carries."""
        f = self.found
        async with self.engine.begin() as conn:
            row = await queue.row_of(conn, row_id, lock=True)
            if row is None or row.state != "merging":
                return False
            after = mr.merge_commit_sha or mr.sha or row.head_sha
            await queue.set_row(conn, row.id, state="merged", reason=None, merged_at=func.now(), after_sha=after)
            before = (row.before_sha or "an unknown commit")[:12]
            why = f"merged into {self.target}, which moved from {before} to {after[:12]}"
            detail = {"before_sha": row.before_sha, "after_sha": after, "rebases": row.rebases}
            await queue.record(conn, row, "merged", passed=True, reason=why, sha=after, detail=detail)
            await audit.record(
                conn,
                actor_id=None,
                token_id=None,
                action=audit.MERGE_MERGE,
                target=f"{f.project.name}/{f.repo} !{row.iid} into={self.target} sha={after} before={row.before_sha}",
                project_id=f.project.id,
            )
            if row.change_id is not None:
                await curator_merged(conn, row, after)
            await after_merge.merged(conn, f, row, after)
        f.outcomes["merged"] += 1
        log.info("merge queue merged", extra={"project": f.project.name, "queue_id": row_id, "sha": after})
        return True

    # Giving the turn back

    async def back(self, conn: AsyncConnection, row, reason: str) -> None:
        """Merge request ``row`` gives its turn back and is ready again, at the place it had in the queue."""
        await queue.set_row(conn, row.id, state="ready", reason=reason)
        await queue.record(conn, row, "queued", reason=reason, sha=row.head_sha, detail={"turn": "back"})

    async def again(self, row_id: int, why: str) -> bool:
        """The merge request needs another rebase: it is ready again at the head of the queue, and its next turn
        rebases it; after MAX_REBASES rebases again in a row, it is blocked."""
        async with self.engine.begin() as conn:
            row = await queue.row_of(conn, row_id, lock=True)
            if row is None or row.state not in merge_gate.TURN_STATES:
                return False
            if row.rebases > merge_gate.MAX_REBASES:
                reason = merge_gate.turn_reason("rebase", f"{why}, after {row.rebases} rebases in a row")
                await queue.set_row(conn, row.id, state="blocked", reason=reason)
                await queue.record(conn, row, "rebased", passed=False, reason=reason, sha=row.head_sha)
            else:
                await self.back(conn, row, merge_gate.turn_reason("rebase", f"{why}: it keeps its place, first"))
        return True

    async def retest(self, row_id: int, why: str) -> bool:
        """GitLab would not merge the merge request while a pipeline of its head runs: it keeps its turn, back in
        ``testing`` to wait for that pipeline, still at most ``merge.pipeline_minutes`` from its head; the pass ends."""
        async with self.engine.begin() as conn:
            row = await queue.row_of(conn, row_id, lock=True)
            if row is None or row.state != "merging":
                return False
            reason = merge_gate.turn_reason("pipeline", why)
            await queue.set_row(conn, row.id, state="testing", reason=reason)
            await queue.record(conn, row, "pipeline", reason=reason, sha=row.head_sha, detail={"turn": "testing"})
        return False

    async def waits(self, row, stage: str, why: str) -> bool:
        """Keep the turn while ``why`` lasts, at most ``merge.pipeline_minutes`` from its head; then block it."""
        async with self.engine.begin() as conn:
            late = row.head_at is not None and await queue.older(conn, row.head_at, timedelta(minutes=self.minutes))
            if not late:
                reason = merge_gate.turn_reason(stage, why)
                if reason != row.reason:
                    await queue.set_row(conn, row.id, reason=reason)
                return False
        why = f"{why} after {self.minutes} minutes"
        if stage == "pipeline":
            return await self.blocked(row.id, f"at its rebased head, {why}", gate=1)
        return await self.blocked(row.id, why, stage=stage, kind="rebased")

    async def blocked(
        self, row_id: int, why: str, *, gate: int | None = None, stage: str = "merge", kind: str = "rebased"
    ) -> bool:
        """Block the merge request whose turn it is, with why: at ``gate`` (a line of the gate), else at ``stage`` of
        its turn (rebase, pipeline or merge) with a line of ``kind``; the turn goes to the next one."""
        async with self.engine.begin() as conn:
            row = await queue.row_of(conn, row_id, lock=True)
            if row is None or row.state not in merge_gate.TURN_STATES:
                return False
            if gate is not None:
                await queue.hold(conn, row, gate, why, blocked=True)
                return True
            reason = merge_gate.turn_reason(stage, why)
            await queue.set_row(conn, row.id, state="blocked", reason=reason)
            await queue.record(conn, row, kind, passed=False, reason=reason, sha=row.head_sha)
        return True
