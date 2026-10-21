"""What follows a merge of the hub's merge queue on GitLab (plan gitlab-merge-queue, criterion a6; the rules are
``evo_agents.hub.post_merge``'s): ``follow`` moves, in a pass of the job hub.merge_queue over one repo, the merges of
its target branch on through their checks, PROD, the rollback and the revert; ``merged`` is what the hub does at once
when its queue merged a merge request (the revert it was, the audit sample); ``revert_gated`` gates a revert the hub
opened; the admin's answers (``answer_sample``, ``approve_release``, ``ask_revert``) back the routes of
``evo_agents.hub.server.merge_view``.

A merge goes ``merged`` (the hub waits for its commit's pipeline on the target branch) to ``verifying`` (its UAT check,
the job ``jobs.uat_check``, or the pipeline itself when the repo names none, there being then no check after deploy),
then, once the checks of the branch settle it (``post_merge.settle``), to ``releasing`` when the repo names a PROD
deploy job, else ``done``. In ``releasing`` it waits for its deploy in merge order (``post_merge.deploy_target``: one
deploy of a branch at a time, ``prod_release`` immediate, after_uat or manual, the last asking each admin of the project
in the Inbox), then its deploy job and its PROD check job run; passed, it is ``done``, with every merge its deploy
carried. A PROD check that fails again after its retry, or has no result, rolls PROD back (``rolling_back``: the job
``jobs.rollback`` with EVO_ROLLBACK_TO_SHA at the last commit whose PROD check passed) before anything else. The
culprit of a check goes ``reverting``: the hub opens its revert on ``evo-revert/<iid>``, first in the queue, gated by
its pipeline and by its diff undoing the merged one; once the revert merges, the culprit is ``reverted``. When the hub
cannot tell the culprit, it reverts nothing and asks each admin of the project in the Inbox, listing the suspects; an
admin asks for the revert of one (``ask_revert``). Once the checks of a pass are settled, and again before the turn,
the repo's breaker may trip (``merge_breaker.watch``); tripped, PROD starts no deploy and a deploy job that failed is
not run again, while a deploy under way, the rollback and the reverts go on.

Each step is a line of the ledger (``uat_check``, ``prod_deploy``, ``prod_check``, ``retried``, ``rollback``,
``revert_opened``, ``reverted``, ``sampled``, ``sample_answered``); a wait only updates the row's reason. What acts on
GitLab is audited too: ``merge.deploy``, ``merge.rollback``, ``merge.revert``. The Inbox notices are of the existing
kind ``merge_default_branch``, naming the repo, the branch and the commits, with what the hub asks in ``details.ask``
(``sample``, ``release``, ``revert``) and a link to the project's merge queue. GitLab refusing a call for one merge
leaves it waiting with why; GitLab failing leaves the repo for the next pass. No token is ever in a row or a line.
"""

from __future__ import annotations

import logging
import random
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import exists, func, insert, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import merge_gate, post_merge, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server import merge_gate as queue
from evo_agents.hub.server.gitlab import GitLabRefused, MergeRequest, RevertRefused

log = logging.getLogger(__name__)

NOTICE = "merge_default_branch"  # the Inbox kind of the queue's notices (runs.NOTICE_KINDS)
MAX_NOTE_CHARS = 2000  # of an admin's note on a sample (merge_samples.note)
PASSED = {"passed": True, "failed": False, "none": None}  # a check's result as merge_events.passed
draw = random.random  # the audit sample's draw; the tests fix it


def short(sha: str | None) -> str:
    return (sha or "an unknown commit")[:12]


def link(project: str) -> str:
    return f"/p/{project}/merge-queue"


def _order(q):
    return tuple_(q.c.merged_at, q.c.id)


def with_item(follow: dict | None, key: str, item: str) -> dict:
    """``follow`` with ``item`` added to its list ``key``."""
    kept = dict(follow or {})
    kept[key] = sorted({*kept.get(key, []), item})
    return kept


async def mark_behind(conn: AsyncConnection, culprit) -> None:
    """Each merge of the branch after ``culprit`` and not past PROD yet is behind it: only a deploy after its revert
    carries it."""
    q = tables.merge_queue
    later = select(q).where(
        q.c.project_id == culprit.project_id,
        q.c.repo == culprit.repo,
        q.c.target_branch == culprit.target_branch,
        q.c.state.in_((*post_merge.UAT_STATES, "releasing")),
        _order(q) > tuple_(culprit.merged_at, culprit.id),
    )
    for each in (await conn.execute(later.with_for_update())).all():
        if not (each.follow or {}).get("behind"):
            await queue.set_row(conn, each.id, follow={**(each.follow or {}), "behind": culprit.id})


async def admins(conn: AsyncConnection, project_id: int) -> list[int]:
    g = tables.grants
    query = select(g.c.user_id).where(g.c.project_id == project_id, g.c.role == "admin").order_by(g.c.user_id)
    return [user_id for (user_id,) in await conn.execute(query)]


async def tell_admins(
    conn: AsyncConnection, project, row, *, title: str, body: str, details: dict, sha: str | None = None
) -> None:
    """A notice in the Inbox of each admin of ``project`` about merge request ``row``, merged at ``sha`` (its
    ``after_sha`` when None)."""
    from evo_agents.hub.server.notifications import notify  # it reaches the plugins' channels

    commits = [found for found in (sha or row.after_sha,) if found]
    for user_id in await admins(conn, project.id):
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=NOTICE,
            project_id=project.id,
            title=title,
            body=body,
            details={"repo": row.repo, "branch": row.target_branch, "commits": commits, "queue_id": row.id, **details},
            link=link(project.name),
        )


# At the merge


async def merged(conn: AsyncConnection, found, row, after: str) -> None:
    """The queue merged ``row`` at ``after``, in the caller's transaction: the culprit a revert undoes is reverted, a
    merge after a culprit not yet reverted is behind it (only the revert's deploy carries it), its wait starts, and it
    may be drawn for an audit sample."""
    q = tables.merge_queue
    if row.source == post_merge.REVERT and row.reverts_id is not None:
        culprit = await queue.row_of(conn, row.reverts_id, lock=True)
        if culprit is not None and culprit.state == "reverting":
            await queue.set_row(conn, culprit.id, state="reverted", reason=None)
            why = f"its revert !{row.iid} merged into {row.target_branch} as {short(after)}"
            await queue.record(conn, culprit, "reverted", passed=True, reason=why, sha=after, detail={"revert": row.id})
    branch = (q.c.project_id == row.project_id) & (q.c.repo == row.repo) & (q.c.target_branch == row.target_branch)
    open_culprit = select(q.c.id).where(branch, q.c.state == "reverting").order_by(q.c.id).limit(1)
    behind = (await conn.execute(open_culprit)).scalar_one_or_none()
    await queue.set_row(conn, row.id, waits_since=func.now(), follow={"behind": behind} if behind else {})
    rate = found.project.policy["audit"]["sample_rate"]
    if row.source != post_merge.REVERT and post_merge.sampled(rate, draw()):
        await sample(conn, found.project, row, after, rate)


async def sample(conn: AsyncConnection, project, row, after: str, rate: float) -> None:
    """Merge request ``row`` is an audit sample: its row, its line and a notice to each admin of the project."""
    s = tables.merge_samples
    sample_id = (
        await conn.execute(insert(s).values(project_id=row.project_id, queue_id=row.id).returning(s.c.id))
    ).scalar_one()
    why = f"drawn for an audit sample (audit.sample_rate {rate:g})"
    await queue.record(conn, row, "sampled", reason=why, sha=after, detail={"sample": sample_id})
    first = (row.verdict or {}).get("first") or {}
    judged = [f"- {name}: {value.get('verdict')}" for name, value in sorted((first.get("dimensions") or {}).items())]
    body = "\n".join(
        [
            f"The hub merged !{row.iid} of {row.repo} ({row.web_url or 'no link'}) into {row.target_branch} as "
            f"{short(after)}. Read its diff and say whether the Judge was right: ok, or faulty with a note.",
            "",
            "The Judge's dimensions:",
            *(judged or ["- none recorded"]),
        ]
    )
    await tell_admins(
        conn,
        project,
        row,
        title=f"Audit sample: check !{row.iid} of {row.repo}, merged into {row.target_branch}",
        body=body,
        details={"ask": "sample", "sample_id": sample_id, "options": list(post_merge.SAMPLE_RESULTS)},
        sha=after,
    )


# A revert at its gates


async def revert_gated(found, row_id: int, mr: MergeRequest, files: list) -> None:
    """Gates 0 to 2 passed at the head of revert ``row_id``: in the place of gate 3 and the Judges, its diff must undo
    exactly the diff of the merge request it reverts; then it is ready, first in the queue."""
    async with found.engine.begin() as conn:
        row = await queue.row_of(conn, row_id)
        culprit = await queue.row_of(conn, row.reverts_id) if row.reverts_id is not None else None
    if culprit is None or culprit.iid is None:
        problem = "the hub knows no merge request it reverts"
    else:
        merged_files = merge_gate.files_of(await found.gl.merge_request_diffs(found.path, culprit.iid))
        problem = post_merge.undo_problem(files, merged_files)
    async with found.engine.begin() as conn:
        row = await queue.row_of(conn, row_id, lock=True)
        if row is None or row.state != "gating" or row.head_sha != mr.sha:
            return
        if problem is not None:
            await queue.hold(conn, row, 3, f"a revert: {problem}", blocked=True)
            return
        for gate in range(3):
            await queue.record(conn, row, "gate", gate=gate, passed=True, sha=mr.sha)
        why = f"a revert: its diff undoes exactly the diff of !{culprit.iid} merged"
        await queue.record(conn, row, "gate", gate=3, passed=True, reason=why, sha=mr.sha)
        fingerprint = merge_gate.patch_fingerprint(files)
        await queue.set_row(conn, row.id, fingerprint=fingerprint, title=mr.title)
        await queue.ready(conn, row, f"the revert of !{culprit.iid}: its pipeline is green; no Judge, first in line")
    found.outcomes["ready"] += 1


# After the merges of a target branch


async def follow(found) -> None:
    """Move what follows each merge of the target branch of ``found``, a pass over one repo, as far as it goes. Once
    the checks are settled the repo's breaker may trip (``merge_breaker.watch``), and PROD then starts no deploy; what
    PROD and the reverts met may trip it again before the turn."""
    from evo_agents.hub.server import merge_breaker  # it reads the admins and the link through this module

    after = After(found)
    await after.uat()
    await after.settle()
    await merge_breaker.watch(found)
    await after.prod()
    await after.reverts()
    await merge_breaker.watch(found, read_target=False)


class After:
    """What follows the merges of one target branch of a repo, in a pass over the repo (``queue.RepoPass``)."""

    def __init__(self, found):
        self.found = found
        self.engine, self.gl, self.path, self.target = found.engine, found.gl, found.path, found.target
        self.project = found.project
        self.jobs: dict = found.settings["jobs"]
        self.release: str = found.settings["prod_release"]
        self.hours: float = found.settings["after_uat_hours"]
        self.minutes: int = found.project.policy["merge"]["pipeline_minutes"]
        self._jobs: dict[int, dict] = {}

    def _branch(self):
        q, found = tables.merge_queue, self.found
        return (q.c.project_id == found.project.id) & (q.c.repo == found.repo) & (q.c.target_branch == self.target)

    async def rows(self, *states: str) -> list:
        q = tables.merge_queue
        async with self.engine.begin() as conn:
            query = select(q).where(self._branch(), q.c.state.in_(states)).order_by(q.c.merged_at, q.c.id)
            return (await conn.execute(query)).all()

    async def row(self, row_id: int):
        async with self.engine.begin() as conn:
            return await queue.row_of(conn, row_id)

    async def refused(self, row_id: int, exc: GitLabRefused) -> None:
        log.warning("what follows a merge was refused", extra={"row": row_id, "why": str(exc)})
        async with self.engine.begin() as conn:
            await queue.set_row(conn, row_id, reason=merge_gate.reason_line(f"GitLab refused the hub: {exc}"))
        self.found.outcomes["refused"] += 1

    # GitLab

    async def jobs_of(self, pipeline_id: int) -> dict:
        if pipeline_id not in self._jobs:
            self._jobs[pipeline_id] = post_merge.latest_jobs(await self.gl.jobs(self.path, pipeline_id))
        return self._jobs[pipeline_id]

    async def pipeline_of(self, row) -> int | None:
        """The pipeline of ``row``'s commit on the target branch, once GitLab has one: it is ``verifying`` then."""
        known = (row.follow or {}).get("pipeline")
        if known:
            return known
        pipelines = await self.gl.pipelines(self.path, row.after_sha, ref=self.target)
        if not pipelines:
            return None
        async with self.engine.begin() as conn:
            locked = await queue.row_of(conn, row.id, lock=True)
            kept = {**(locked.follow or {}), "pipeline": pipelines[0].id}
            await queue.set_row(conn, row.id, state="verifying", follow=kept, waits_since=func.now(), reason=None)
        return pipelines[0].id

    async def late(self, row, why: str) -> tuple[str, str] | None:
        """``("none", why)`` once the hub waited ``merge.pipeline_minutes`` for what ``why`` names; else None, the
        row's reason saying what it waits for."""
        since = row.waits_since or row.merged_at
        async with self.engine.begin() as conn:
            if since is not None and await queue.older(conn, since, timedelta(minutes=self.minutes)):
                return "none", f"{why} after {self.minutes} minutes"
            reason = merge_gate.reason_line(f"after its merge, it waits: {why}")
            if reason != row.reason:
                await queue.set_row(conn, row.id, reason=reason)
        return None

    async def moved(self, row, stage: str, key: str, kind: str, why: str, detail: dict) -> None:
        """The hub played or retried what ``stage`` of ``row`` watches: a line of ``kind``, and its wait restarts."""
        async with self.engine.begin() as conn:
            locked = await queue.row_of(conn, row.id, lock=True)
            kept = with_item(locked.follow, key, stage)
            reason = merge_gate.reason_line(f"after its merge, {why}")
            await queue.set_row(conn, row.id, follow=kept, waits_since=func.now(), reason=reason)
            await queue.record(conn, locked, kind, reason=why, sha=row.after_sha, detail={"stage": stage, **detail})
            if stage == "rollback" and key == "played":
                await self.audited(conn, locked, audit.MERGE_ROLLBACK, f"job={detail['job']} to={detail['to']}")

    async def audited(self, conn: AsyncConnection, row, action: str, what: str) -> None:
        target = f"{self.project.name}/{row.repo} !{row.iid} {what}"
        await audit.record(conn, actor_id=None, token_id=None, action=action, target=target, project_id=self.project.id)

    async def check(self, row, stage: str, kind: str, name: str, variables: dict | None = None):
        """One step of job ``name`` of the pipeline of ``row``'s commit, which ``stage`` of it watches (lines of
        ``kind``): it plays a manual one, retries one that failed once, and waits; (result, why) once it ended."""
        follow = row.follow or {}
        pipeline = follow["pipeline"]
        job = (await self.jobs_of(pipeline)).get(name)
        retried = stage in follow.get("retried", [])
        step = post_merge.job_step(
            job.status if job else None, played=stage in follow.get("played", []), retried=retried
        )
        if step == "play":
            await self.gl.play_job(self.path, job.id, variables)
            to = (variables or {}).get(post_merge.ROLLBACK_VARIABLE)
            what = f"the hub runs manual job {name}" + (f" to put PROD back at {short(to)}" if to else "")
            await self.moved(row, stage, "played", kind, what, {"job": name, "to": to})
            return None
        if step == "retry" and stage == "deploy" and self.found.tripped:  # the breaker runs no deploy job again
            step = "failed" if job.status == "failed" else "none"
        if step == "retry":
            again = await self.gl.retry_job(self.path, job.id)
            why = f"job {name} ended {job.status}: the hub runs it again once"
            await self.moved(row, stage, "retried", "retried", why, {"job": name, "job_id": again.id})
            return None
        if step == "wait":
            return await self.late(row, f"job {name} of pipeline {pipeline} is {job.status}")
        if job is None:
            return "none", f"job {name} is not in pipeline {pipeline} of {short(row.after_sha)}"
        again = (" on its retry" if step == "passed" else " again after a retry") if retried else ""
        return step, f"job {name} of pipeline {pipeline} ended {job.status}{again}"

    async def pipeline_check(self, row):
        """The pipeline as the UAT check of a repo that names no UAT check job: (result, why) once it ended."""
        follow = row.follow or {}
        pipeline = await self.gl.pipeline(self.path, follow["pipeline"])
        step = post_merge.pipeline_step(pipeline.status, retried="pipeline" in follow.get("retried", []))
        if step == "retry":
            await self.gl.retry_pipeline(self.path, pipeline.id)
            why = f"its pipeline {pipeline.id} on {self.target} ended {pipeline.status}: the hub runs it again once"
            await self.moved(row, "pipeline", "retried", "retried", why, {"pipeline": pipeline.id})
            return None
        if step == "wait":
            return await self.late(row, f"its pipeline {pipeline.id} on {self.target} is {pipeline.status}")
        if step == "passed":
            jobs = "no job" if not any(self.jobs.values()) else "no UAT check job"
            return step, (
                f"its pipeline {pipeline.id} on {self.target} is green, and the repo names {jobs}: no check after "
                "deploy"
            )
        return step, f"its pipeline {pipeline.id} on {self.target} ended {pipeline.status}"

    # The UAT check

    async def uat(self) -> None:
        for row in await self.rows(*post_merge.UAT_STATES):
            if row.uat_result is not None:
                continue
            try:
                await self.uat_of(row)
            except GitLabRefused as exc:
                await self.refused(row.id, exc)

    async def uat_of(self, row) -> None:
        if await self.pipeline_of(row) is None:
            ended = await self.late(row, f"no pipeline of {short(row.after_sha)} on {self.target} yet")
        else:
            row = await self.row(row.id)
            name = self.jobs["uat_check"]
            ended = await (self.check(row, "uat", "uat_check", name) if name else self.pipeline_check(row))
        if ended is None:
            return
        result, why = ended
        async with self.engine.begin() as conn:
            locked = await queue.row_of(conn, row.id, lock=True)
            if locked is None or locked.state not in post_merge.UAT_STATES or locked.uat_result is not None:
                return
            reason = None if result == "passed" else merge_gate.reason_line(f"its UAT check: {why}")
            await queue.set_row(conn, row.id, uat_result=result, reason=reason)
            detail = {"result": result, "pipeline": (locked.follow or {}).get("pipeline")}
            await queue.record(
                conn, locked, "uat_check", passed=PASSED[result], reason=why, sha=row.after_sha, detail=detail
            )

    async def revert_pending(self, conn: AsyncConnection) -> bool:
        """Whether a culprit of the branch waits for its revert, or a revert has no result of its own yet."""
        q = tables.merge_queue
        culprit = exists().where(self._branch(), q.c.state == "reverting")
        unchecked = exists().where(
            self._branch(),
            q.c.source == post_merge.REVERT,
            q.c.uat_result.is_(None),
            q.c.state.in_((*merge_gate.QUEUED_STATES, *post_merge.UAT_STATES)),
        )
        return bool((await conn.execute(select(culprit | unchecked))).scalar_one())

    async def settle(self) -> None:
        """Settle the UAT checks of the branch's merges: passed or cleared, the culprit, or the suspects."""
        rows = await self.rows(*post_merge.UAT_STATES)
        if not rows:
            return
        async with self.engine.begin() as conn:
            pending = await self.revert_pending(conn)
        settled = post_merge.settle([(row.id, row.uat_result) for row in rows], revert_pending=pending)
        by_id = {row.id: row for row in rows}
        for row_id in settled.passed:
            await self.uat_passed(row_id, None)
        for row_id, good in settled.cleared:
            await self.uat_passed(row_id, by_id[good])
        if settled.culprit is not None:
            culprit = by_id[settled.culprit]
            await self.blame(culprit.id, f"its UAT check on its own commit {short(culprit.after_sha)} failed again")
        if settled.suspects:
            await self.ask_suspects([by_id[row_id] for row_id in settled.suspects], "UAT")

    async def uat_passed(self, row_id: int, cleared_by) -> None:
        """``row_id``'s UAT check passed, or the passed check of ``cleared_by``, merged after it, cleared it: PROD next,
        or nothing when the repo names no PROD deploy job."""
        async with self.engine.begin() as conn:
            row = await queue.row_of(conn, row_id, lock=True)
            if row is None or row.state not in post_merge.UAT_STATES:
                return
            if cleared_by is not None:
                why = f"cleared by the UAT check of !{cleared_by.iid}, merged after it, which passed"
                detail = {"cleared_by": cleared_by.id}
                await queue.record(conn, row, "uat_check", passed=True, reason=why, sha=row.after_sha, detail=detail)
            state = "releasing" if self.jobs["prod_deploy"] else "done"
            await queue.set_row(conn, row_id, state=state, reason=None, uat_at=func.now(), waits_since=func.now())

    # The culprit

    async def blame(self, row_id: int, why: str) -> None:
        """``row_id`` is the culprit: the hub reverts it, and each later merge not past PROD is behind it."""
        async with self.engine.begin() as conn:
            row = await queue.row_of(conn, row_id, lock=True)
            if row is None or row.state in post_merge.CULPRIT_STATES:
                return
            reason = merge_gate.reason_line(f"the culprit: {why}; the hub reverts it")
            await queue.set_row(conn, row.id, state="reverting", reason=reason)
            await mark_behind(conn, row)
        self.found.outcomes["culprit"] += 1

    async def ask_suspects(self, suspects: list, check: str, why: str | None = None) -> None:
        """The hub cannot tell which of ``suspects`` broke the ``check`` check: it reverts none and asks each admin of
        the project once, listing them."""
        oldest = suspects[0]
        if "suspects" in (oldest.follow or {}).get("asked", []):
            return
        named = suspects[: post_merge.MAX_SUSPECTS]
        listed = post_merge.iids(row.iid for row in named)
        lines = [f"- !{row.iid}: {row.title or ''} ({row.web_url or 'no link'})" for row in named]
        body = "\n".join(
            [
                f"The {check} check of {oldest.repo} on {self.target} failed, and the check of !{oldest.iid}, the "
                "oldest merge since the last good commit, has no result: the hub reverts none. "
                + (f"{why} " if why else "")
                + "Pick the one to revert on the merge queue page, or revert it on GitLab yourself.",
                "",
                *lines,
            ]
        )
        async with self.engine.begin() as conn:
            details = {"ask": "revert", "check": check, "suspects": [{"queue_id": r.id, "iid": r.iid} for r in named]}
            title = f"The {check} check of {oldest.repo} failed: which of {listed} broke {self.target}?"
            await tell_admins(conn, self.project, suspects[-1], title=title, body=body, details=details)
            for row in suspects:
                locked = await queue.row_of(conn, row.id, lock=True)
                kept = with_item(locked.follow, "asked", "suspects") if row.id == oldest.id else locked.follow
                reason = f"its {check} check failed and the hub cannot tell which of {listed} broke it: an admin picks"
                await queue.set_row(conn, row.id, follow=kept, reason=merge_gate.reason_line(reason))
        self.found.outcomes["asked"] += 1

    async def reverts(self) -> None:
        """Open the revert of each culprit of the branch that has none yet."""
        q = tables.merge_queue
        r = q.alias("revert")
        async with self.engine.begin() as conn:
            query = select(q).where(self._branch(), q.c.state == "reverting", ~exists().where(r.c.reverts_id == q.c.id))
            rows = (await conn.execute(query.order_by(q.c.merged_at, q.c.id))).all()
        for row in rows:
            if (row.follow or {}).get("revert_problem"):
                continue
            try:
                await self.open_revert(row)
            except GitLabRefused as exc:
                await self.revert_refused(row.id, str(exc), exc.code if isinstance(exc, RevertRefused) else None)

    async def open_revert(self, row) -> None:
        commits = await self.gl.merge_request_commits(self.path, row.iid)
        shas = post_merge.newest_first(commits, row.after_sha)
        if not shas:
            return await self.revert_refused(row.id, f"GitLab lists no commit of !{row.iid}", None)
        branch = post_merge.REVERT_BRANCH.format(iid=row.iid)
        why = (row.reason or "its check failed").removeprefix("the culprit: ").removesuffix("; the hub reverts it")
        mr = await self.gl.open_revert(
            self.path,
            shas=shas,
            target_branch=self.target,
            branch=branch,
            title=post_merge.revert_title(row.iid, row.title),
            description=post_merge.revert_description(self.project.name, row.iid, self.target, why, shas),
        )
        async with self.engine.begin() as conn:
            q = tables.merge_queue
            values = {
                "project_id": row.project_id,
                "repo": row.repo,
                "iid": mr.iid,
                "source": post_merge.REVERT,
                "target_branch": self.target,
                "source_branch": branch,
                "title": mr.title,
                "web_url": mr.web_url,
                "reverts_id": row.id,
            }
            revert_id = (await conn.execute(insert(q).values(values).returning(q.c.id))).scalar_one()
            revert = await queue.row_of(conn, revert_id)
            await queue.record(conn, revert, "opened", reason=f"the hub opened the revert of !{row.iid}")
            culprit = await queue.row_of(conn, row.id, lock=True)
            what = f"the hub opened !{mr.iid} from {branch}: it reverts {len(shas)} commit(s), the newest first"
            detail = {"iid": mr.iid, "branch": branch, "commits": shas}
            await queue.record(
                conn, culprit, "revert_opened", passed=True, reason=what, sha=row.after_sha, detail=detail
            )
            await self.audited(conn, culprit, audit.MERGE_REVERT, f"revert=!{mr.iid} branch={branch}")
        self.found.outcomes["reverts"] += 1

    async def revert_refused(self, row_id: int, why: str, code: str | None) -> None:
        """GitLab would not make the revert of culprit ``row_id``: a person decides, and the hub tries no more."""
        async with self.engine.begin() as conn:
            row = await queue.row_of(conn, row_id, lock=True)
            if row is None or row.state != "reverting":
                return
            reason = merge_gate.reason_line(f"GitLab would not revert it ({why}): a person decides")
            await queue.set_row(conn, row_id, reason=reason, follow={**(row.follow or {}), "revert_problem": why})
            detail = {"code": code} if code else {}
            await queue.record(conn, row, "revert_opened", passed=False, reason=why, sha=row.after_sha, detail=detail)

    # PROD

    async def prod(self) -> None:
        rows = await self.rows(*post_merge.FLIGHT_STATES)
        if not rows:
            return
        if not self.jobs["prod_deploy"]:  # the policy no longer names a deploy: the hub leaves PROD alone
            for row in rows:
                if row.state == "releasing":
                    await self.finish(row.id, "done", "the policy names no PROD deploy job any more")
            rows = [row for row in rows if row.state == "rolling_back"]  # a rollback under way ends
            if not rows:
                return
        flight = [row for row in rows if row.state == "rolling_back" or (row.follow or {}).get("deploy")]
        try:
            if flight:
                await (self.rolling_back(flight[0]) if flight[0].state == "rolling_back" else self.deployed(flight[0]))
            elif not self.found.tripped:  # the repo's breaker tripped: no deploy starts
                await self.next_deploy(rows)
        except GitLabRefused as exc:
            await self.refused((flight or rows)[0].id, exc)

    async def finish(self, row_id: int, state: str, reason: str | None, **values) -> None:
        async with self.engine.begin() as conn:
            await queue.set_row(conn, row_id, state=state, reason=reason, **values)

    async def next_deploy(self, rows: list) -> None:
        """Deploy the next merge of the branch under ``prod_release``, carrying the merges before it."""
        candidates = []
        async with self.engine.begin() as conn:
            for row in rows:
                follow = row.follow or {}
                if self.release == "after_uat":
                    hours = timedelta(hours=self.hours)
                    eligible = row.uat_at is not None and await queue.older(conn, row.uat_at, hours)
                else:
                    eligible = bool(follow.get("approved_by")) or self.release == "immediate"
                candidates.append(
                    post_merge.Candidate(row.id, row.uat_result == "passed", bool(follow.get("behind")), eligible)
                )
        if self.release == "manual":
            await self.ask_release(rows, candidates)
        chosen = post_merge.deploy_target(candidates, self.release)
        if chosen is None:
            return
        by_id = {row.id: row for row in rows}
        await self.deploy(by_id[chosen[0]], [by_id[row_id] for row_id in chosen[1]])

    async def deploy(self, target, carried: list) -> None:
        name = self.jobs["prod_deploy"]
        pipeline = (target.follow or {}).get("pipeline")
        job = (await self.jobs_of(pipeline)).get(name) if pipeline else None
        if job is None:  # nothing to deploy: PROD stays where it is
            why = f"job {name} is not in pipeline {pipeline} of {short(target.after_sha)}: nothing was deployed"
            async with self.engine.begin() as conn:
                for row in [*carried, target]:
                    locked = await queue.row_of(conn, row.id, lock=True)
                    detail = {"result": "none"}
                    await queue.record(conn, locked, "prod_deploy", reason=why, sha=row.after_sha, detail=detail)
                    await queue.set_row(
                        conn, row.id, state="done", prod_result="none", reason=merge_gate.reason_line(why)
                    )
            return
        if job.status == post_merge.MANUAL:
            await self.gl.play_job(self.path, job.id)
        names = post_merge.iids(row.iid for row in carried)
        why = f"the hub runs job {name} of {short(target.after_sha)}" + (f", which carries {names}" if names else "")
        async with self.engine.begin() as conn:
            locked = await queue.row_of(conn, target.id, lock=True)
            kept = {
                **with_item(locked.follow, "played", "deploy"),
                "deploy": job.id,
                "carried": [r.id for r in carried],
            }
            await queue.set_row(conn, target.id, follow=kept, waits_since=func.now(), reason=None)
            detail = {"job": name, "job_id": job.id, "carried": [row.id for row in carried]}
            await queue.record(conn, locked, "prod_deploy", reason=why, sha=target.after_sha, detail=detail)
            carries = f" carries={names.replace(' ', '')}" if names else ""
            await self.audited(conn, locked, audit.MERGE_DEPLOY, f"job={name} sha={target.after_sha}{carries}")
            for row in carried:
                each = await queue.row_of(conn, row.id, lock=True)
                reason = f"its PROD deploy rides with !{target.iid}'s"
                await queue.set_row(
                    conn, row.id, follow={**(each.follow or {}), "carried_by": target.id}, reason=reason
                )
        self.found.outcomes["deploys"] += 1

    async def ask_release(self, rows: list, candidates: list) -> None:
        """Ask each admin once to approve the PROD release of each merge that may be deployed (``manual``)."""
        by_id = {row.id: row for row in rows}
        for candidate in candidates:
            row = by_id[candidate.id]
            follow = row.follow or {}
            if not candidate.passed or candidate.behind or "release" in follow.get("asked", []):
                continue
            async with self.engine.begin() as conn:
                await tell_admins(
                    conn,
                    self.project,
                    row,
                    title=f"PROD release of {row.repo} waits for an admin: !{row.iid}, merged into {self.target}",
                    body=(
                        f"!{row.iid} ({row.title or ''}) passed its UAT check at {short(row.after_sha)}. The policy "
                        "releases this repo to PROD by hand: approve its release on the merge queue page, and the "
                        "hub runs its deploy job, with the merges before it."
                    ),
                    details={"ask": "release"},
                )
                locked = await queue.row_of(conn, row.id, lock=True)
                reason = "its PROD release waits for an admin to approve it (prod_release manual)"
                await queue.set_row(conn, row.id, follow=with_item(locked.follow, "asked", "release"), reason=reason)

    async def deployed(self, target) -> None:
        """The deploy of ``target`` in flight: its deploy job, then its PROD check."""
        follow = target.follow or {}
        if not follow.get("deployed"):
            ended = await self.check(target, "deploy", "prod_deploy", self.jobs["prod_deploy"])
            if ended is None:
                return
            result, why = ended
            if result != "passed":
                return await self.prod_failed(target, result, why, "prod_deploy")
            async with self.engine.begin() as conn:
                locked = await queue.row_of(conn, target.id, lock=True)
                kept = {**(locked.follow or {}), "deployed": True}
                await queue.set_row(conn, target.id, follow=kept, waits_since=func.now())
                await queue.record(conn, locked, "prod_deploy", passed=True, reason=why, sha=target.after_sha)
            target = await self.row(target.id)
        name = self.jobs["prod_check"]
        if not name:
            return await self.prod_passed(target, "its deploy passed, and the repo names no PROD check job")
        ended = await self.check(target, "prod", "prod_check", name)
        if ended is None:
            return
        result, why = ended
        if result == "passed":
            return await self.prod_passed(target, why)
        await self.prod_failed(target, result, why, "prod_check")

    async def prod_passed(self, target, why: str) -> None:
        async with self.engine.begin() as conn:
            locked = await queue.row_of(conn, target.id, lock=True)
            await queue.record(conn, locked, "prod_check", passed=True, reason=why, sha=target.after_sha)
            await queue.set_row(conn, target.id, state="done", reason=None, prod_result="passed", prod_at=func.now())
            for row_id in (locked.follow or {}).get("carried", []):
                row = await queue.row_of(conn, row_id, lock=True)
                carried = f"carried by the deploy of !{target.iid}, whose PROD check passed"
                await queue.record(conn, row, "prod_check", passed=True, reason=carried, sha=target.after_sha)
                await queue.set_row(conn, row_id, state="done", reason=None, prod_result="passed", prod_at=func.now())

    async def rollback_to(self, conn: AsyncConnection, target, carried: list[int]) -> tuple[str | None, str]:
        """(the commit PROD goes back to, why): the last of the branch whose PROD check passed, else the commit before
        the oldest merge the deploy carried."""
        q = tables.merge_queue
        query = (
            select(q.c.after_sha)
            .where(self._branch(), q.c.prod_result == "passed", _order(q) < tuple_(target.merged_at, target.id))
            .order_by(q.c.merged_at.desc(), q.c.id.desc())
            .limit(1)
        )
        good = (await conn.execute(query)).scalar_one_or_none()
        if good is not None:
            return good, f"{short(good)}, the last commit of {self.target} whose PROD check passed"
        oldest = await queue.row_of(conn, carried[0]) if carried else target
        sha = oldest.before_sha
        return sha, f"{short(sha)}, the commit before !{oldest.iid}: the hub knows no commit whose PROD check passed"

    async def prod_failed(self, target, result: str, why: str, kind: str) -> None:
        """The deploy of ``target`` failed, or its PROD check: PROD goes back first, with the rollback job."""
        name = self.jobs["rollback"]
        async with self.engine.begin() as conn:
            locked = await queue.row_of(conn, target.id, lock=True)
            follow = locked.follow or {}
            to, which = await self.rollback_to(conn, locked, follow.get("carried", []))
            detail = {"result": result}
            await queue.record(
                conn, locked, kind, passed=PASSED[result], reason=why, sha=target.after_sha, detail=detail
            )
            kept = {**follow, "rollback_to": to}
            reason = merge_gate.reason_line(f"its {kind.replace('_', ' ')} failed ({why}): PROD goes back to {which}")
            values = {"prod_result": result, "follow": kept, "waits_since": func.now(), "reason": reason}
            await queue.set_row(conn, target.id, state="rolling_back", **values)
            if not name or to is None:
                no = "the repo names no rollback job" if not name else "the hub knows no commit to put PROD back at"
                why = f"{no}: PROD stays as the failed deploy left it"
                await queue.record(conn, locked, "rollback", passed=False, reason=why, sha=target.after_sha)
        if not name or to is None:
            await self.after_rollback(await self.row(target.id))

    async def rolling_back(self, target) -> None:
        follow = target.follow or {}
        to = follow.get("rollback_to")
        variables = {post_merge.ROLLBACK_VARIABLE: to}
        ended = await self.check(target, "rollback", "rollback", self.jobs["rollback"], variables)
        if ended is None:
            return
        result, why = ended
        async with self.engine.begin() as conn:
            locked = await queue.row_of(conn, target.id, lock=True)
            detail = {"to": to, "result": result}
            await queue.record(conn, locked, "rollback", passed=result == "passed", reason=why, sha=to, detail=detail)
        await self.after_rollback(await self.row(target.id))

    async def after_rollback(self, target) -> None:
        """PROD went back, or could not: the deploy's merge is the culprit when it carried no other, else the hub
        asks an admin, listing the merges of the deploy."""
        follow = target.follow or {}
        carried = follow.get("carried", [])
        to = short(follow.get("rollback_to"))
        if post_merge.prod_culprit(target.id, carried, target.prod_result) is not None:
            return await self.blame(target.id, f"its PROD check failed again after a retry; PROD went back to {to}")
        async with self.engine.begin() as conn:
            rows = [await queue.row_of(conn, row_id) for row_id in [*carried, target.id]]
        for row in rows:
            await self.finish(row.id, "done", merge_gate.reason_line(f"PROD went back to {to}"))
        rows = [await self.row(row.id) for row in rows]
        await self.ask_suspects(rows, "PROD", f"PROD went back to {to}.")


# What an admin answers


def admin_only(user, access) -> None:
    if user.scope is not None or not has_role(access.role, "admin"):
        raise HTTPException(403, f"this needs the admin role on project {access.name}")


async def _queued(conn: AsyncConnection, access, queue_id: int):
    row = await queue.row_of(conn, queue_id, lock=True)
    if row is None or row.project_id != access.project_id:
        raise HTTPException(404, f"project {access.name} has no merge request {queue_id} in its merge queue")
    return row


async def answer_sample(conn: AsyncConnection, user, access, sample_id: int, result: str, note: str | None):
    """An admin's answer to audit sample ``sample_id``: ``faulty`` counts as a pass of the Judge found wrong."""
    admin_only(user, access)
    s = tables.merge_samples
    found = (await conn.execute(select(s).where(s.c.id == sample_id).with_for_update())).one_or_none()
    if found is None or found.project_id != access.project_id:
        raise HTTPException(404, f"project {access.name} has no audit sample {sample_id}")
    if found.result is not None:
        raise HTTPException(409, f"audit sample {sample_id} was answered already: {found.result}")
    text = " ".join((note or "").split())[:MAX_NOTE_CHARS] or None
    values = {"result": result, "note": text, "answered_by": user.user_id, "answered_at": func.now()}
    await conn.execute(update(s).values(values).where(s.c.id == sample_id))
    row = await queue.row_of(conn, found.queue_id)
    why = f"{user.login} answered the audit sample: {result}"
    detail = {"sample": sample_id, "result": result}
    await queue.record(
        conn, row, "sample_answered", passed=result == "ok", reason=why, sha=row.after_sha, detail=detail
    )
    n = tables.notifications
    sampled = n.c.details["sample_id"].astext == str(sample_id)
    await conn.execute(update(n).values(read_at=func.now()).where(sampled, n.c.read_at.is_(None)))
    target = f"{access.name}/{row.repo} !{row.iid} sample={sample_id} result={result}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.MERGE_SAMPLE,
        target=target,
        project_id=access.project_id,
    )
    return (await conn.execute(select(s).where(s.c.id == sample_id))).one()


async def approve_release(conn: AsyncConnection, user, access, queue_id: int, release: str):
    """An admin approves the PROD release of merge ``queue_id`` (``prod_release`` manual): the next pass deploys it."""
    admin_only(user, access)
    row = await _queued(conn, access, queue_id)
    if release != "manual":
        raise HTTPException(409, f"the policy releases {row.repo} to PROD {release}, not by hand")
    if row.state != "releasing" or row.uat_result != "passed":
        raise HTTPException(409, f"!{row.iid} does not wait for its PROD release: it is {row.state}")
    await queue.set_row(conn, row.id, follow={**(row.follow or {}), "approved_by": user.user_id})
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.MERGE_RELEASE,
        target=f"{access.name}/{row.repo} !{row.iid} approved",
        project_id=access.project_id,
    )
    return await queue.row_of(conn, row.id)


AFTER_MERGE = (*post_merge.UAT_STATES, "releasing", "done")


async def ask_revert(conn: AsyncConnection, user, access, queue_id: int):
    """An admin asks the hub to revert merge ``queue_id``, a suspect it could not tell, or any merge it followed: the
    next pass opens its revert, first in the queue."""
    admin_only(user, access)
    row = await _queued(conn, access, queue_id)
    retry = row.state == "reverting" and (row.follow or {}).get("revert_problem")
    if row.source == post_merge.REVERT or not (row.state in AFTER_MERGE or retry):
        raise HTTPException(409, f"!{row.iid} is {row.state}: the hub reverts a merge it followed after its merge")
    kept = {key: value for key, value in (row.follow or {}).items() if key != "revert_problem"}
    reason = f"the culprit: {user.login} asked for its revert; the hub reverts it"
    await queue.set_row(conn, row.id, state="reverting", reason=reason, follow=kept)
    await mark_behind(conn, row)
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.MERGE_REVERT,
        target=f"{access.name}/{row.repo} !{row.iid} revert=asked",
        project_id=access.project_id,
    )
    return await queue.row_of(conn, row.id)
