"""The hub's merge queue on GitLab, up to its gate before merge (plan gitlab-merge-queue, criterion a4; the rules are
``evo_agents.hub.merge_gate``'s): the job that reads the merge requests of each repo an autonomy policy has in mode
auto, opens those of the hub's runs, runs the gates on each one and queues its Judges; the hand-off of the Curator's
merge requests its Judge passed. ``evo_agents.hub.server.merge_judge`` holds the judge runs.

``advance`` is one pass of the job ``hub.merge_queue``. For each project with a policy (or with merge requests still
in its queue), and each repo of it: a repo in mode off has its merge requests leave the queue (``closed``), open for a
person again. A repo in mode auto that no forge secret of the project covers is left as it is: the hub cannot read
GitLab for it, and nothing of it moves. Otherwise the hub opens the forge secret (``forge.forge_token``) and, through
the GitLab client, for as long as the pass needs it:

- opens the merge request of each plan whose steps in the repo are all done, from the branch the plan names for it,
  once a plan run or step run of the plan ended done since the repo went to mode auto (``merge_gate.auto_since``), and
  of each task run that ended done since then and pushed its branch of the repo; or takes the merge request open of
  that branch already. GitLab refusing is kept as the row's reason, and tried again after OPEN_RETRY. Each opening is
  audited (``merge.open``).
- takes each merge request labeled ``merge.opt_in_label`` by a member of role Developer or above (resource label
  events), and finds the merge request of each Curator's change handed to it (``curator_judged``) by its branch.
- reads each merge request of the queue again: out of scope, it leaves the queue; a new head sends it back to the
  first gate; in ``gating`` it runs gates 0 to 3 and computes the risk (gate 6) with the knowledge graph, read as the
  admin who wrote the policy; in ``judging`` it queues the next judge run (``merge_judge.queue_judge``) or, after
  JUDGE_ATTEMPTS runs without a verdict, blocks it.
- moves the turn of the repo's target branch on: the merge requests ``ready`` are rebased, tested and merged one at a
  time (``evo_agents.hub.server.merge_turn``).

A gate that does not pass leaves the merge request ``gating`` with why when it waits for something on GitLab (its
pipeline, the project's settings, the end of its draft), or ``blocked`` with why; a blocked one moves only after a
new push. Each new reason, and each gate passed at a head, is a line of the ledger (merge_events), and the hub
comments on the merge request once for each reason that blocks it or each setting it misses. GitLab failing leaves
the repo for the next pass. A merge request out of scope, or of a project without a policy, is never written to, as
before the policy (criterion a9). ``evo_agents.hub.server.merge_view`` shows the queue.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import autonomy, judge, merge_gate, runs, tables, task_runs
from evo_agents.hub.server import audit, change_tiers
from evo_agents.hub.server.autonomy_policies import project_repos
from evo_agents.hub.server.forge import forge_token, has_forge
from evo_agents.hub.server.gitlab import GitLab, GitLabRefused, GitLabUnavailable, MergeRequest, project_of

log = logging.getLogger(__name__)

OPEN_RETRY = timedelta(hours=1)  # a merge request GitLab would not open is asked for again after this
MAX_REVISIONS = 500  # of a policy, read for when a repo went to mode auto


# The rows and their ledger


async def record(
    conn: AsyncConnection,
    row,
    kind: str,
    *,
    gate: int | None = None,
    passed: bool | None = None,
    reason: str | None = None,
    sha: str | None = None,
    run_id: int | None = None,
    detail: dict | None = None,
) -> None:
    """Add a line to the ledger of merge request ``row``."""
    await conn.execute(
        insert(tables.merge_events).values(
            project_id=row.project_id,
            queue_id=row.id,
            kind=kind,
            gate=gate,
            passed=passed,
            reason=merge_gate.reason_line(reason) if reason else None,
            sha=sha,
            run_id=run_id,
            detail=detail or {},
        )
    )


async def set_row(conn: AsyncConnection, row_id: int, **values) -> None:
    q = tables.merge_queue
    await conn.execute(update(q).values(updated_at=func.now(), **values).where(q.c.id == row_id))


async def row_of(conn: AsyncConnection, row_id: int, *, lock: bool = False):
    q = tables.merge_queue
    query = select(q).where(q.c.id == row_id)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def charter_of(conn: AsyncConnection, project_id: int) -> dict:
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none() or {}


def protected_of(charter: dict) -> list[str]:
    return [item for item in charter.get("protected_paths") or [] if isinstance(item, str)]


async def plan_body(conn: AsyncConnection, project_id: int, plan_id: str | None) -> dict | None:
    if not plan_id:
        return None
    p = tables.plans
    query = select(p.c.body).where(p.c.project_id == project_id, p.c.plan_id == plan_id)
    found = (await conn.execute(query)).scalar_one_or_none()
    return found if isinstance(found, dict) else None


async def hold(conn: AsyncConnection, row, gate: int, why: str, *, blocked: bool = False, sha: str | None = None):
    """Keep merge request ``row`` at ``gate``: ``gating`` while it waits, ``blocked`` when it is; a reason it did not
    have before is a line of the ledger."""
    reason = merge_gate.gate_reason(gate, why)
    state = "blocked" if blocked else "gating"
    if reason == row.reason and state == row.state:
        return
    await set_row(conn, row.id, state=state, reason=reason)
    detail = {} if blocked else {"waits": True}
    await record(conn, row, "gate", gate=gate, passed=False, reason=reason, sha=sha or row.head_sha, detail=detail)


async def wait(conn: AsyncConnection, row, why: str) -> None:
    """Keep ``row`` where it is, with why it waits for something on GitLab that is no gate (its draft, GitLab)."""
    reason = merge_gate.reason_line(merge_gate.WAITS + why)
    if reason != row.reason:
        await set_row(conn, row.id, reason=reason)


async def close(conn: AsyncConnection, row, why: str) -> None:
    """``row`` leaves the queue: its merge request is a person's again, or it is merged or closed on GitLab."""
    reason = merge_gate.reason_line(why)
    await set_row(conn, row.id, state="closed", reason=reason, judge_run_id=None, judge_key=None)
    await record(conn, row, "closed", reason=reason, sha=row.head_sha)


async def ready(conn: AsyncConnection, row, why: str) -> None:
    """Every gate passed at the head of ``row``: it waits for its turn on the target branch (the queue's), at the place
    it had when its turn sent it back to its gates (``queued_at`` kept), else last."""
    q = tables.merge_queue
    await set_row(
        conn,
        row.id,
        state="ready",
        reason=None,
        queued_at=func.coalesce(q.c.queued_at, func.now()),
        rebases=0,
        judge_run_id=None,
        judge_key=None,
    )
    await record(conn, row, "queued", reason=why, sha=row.head_sha)


# What the queue takes


async def takes(conn: AsyncConnection, project_id: int, repo: str, origin: str | None) -> tuple[bool, str | None]:
    """(whether the queue takes the merge requests of ``repo``, its target branch): its policy has it in mode auto
    and a forge secret of the project covers its origin on GitLab."""
    repos = await project_repos(conn, project_id)
    a = tables.autonomy_policies
    query = select(a.c.body).where(a.c.project_id == project_id).order_by(a.c.revision.desc()).limit(1)
    body = (await conn.execute(query)).scalar_one_or_none()
    policy = autonomy.repo_policy(body, repo, repos.get(repo))
    if policy["mode"] != autonomy.AUTO or not origin or project_of(origin) is None:
        return False, policy[autonomy.TARGET_BRANCH]
    return await has_forge(conn, project_id, origin), policy[autonomy.TARGET_BRANCH]


async def _builder(conn: AsyncConnection, run_id: int | None) -> tuple[str | None, str | None]:
    """(the agent profile as @handle, the model) of run ``run_id``."""
    if run_id is None:
        return None, None
    r, ap = tables.runs, tables.agent_profiles
    query = select(r.c.model, ap.c.handle).join_from(r, ap, ap.c.id == r.c.profile_id, isouter=True)
    found = (await conn.execute(query.where(r.c.id == run_id))).one_or_none()
    if found is None:
        return None, None
    return (f"@{found.handle}" if found.handle else None), found.model


async def curator_judged(engine: AsyncEngine, change) -> tuple[str, str]:
    """(outcome, reason) of a judged change of the Curator on GitLab, for the job curator.changes: the merge queue
    takes it when the Judge passed it and its repo is in mode auto with a forge secret (``judge.merge_decision``), as
    a row of source builder whose merge request the job finds by its branch; else it stays open, as before."""
    if not change.passed:
        return "open", "the Judge did not pass it: " + "; ".join((change.verdict or {}).get("failures") or [])
    async with engine.begin() as conn:
        queue, target = await takes(conn, change.project_id, change.repo, change.origin)
        facts = judge.MergeFacts(
            forge=change.forge or "gitlab",
            tier=change.tier,
            auto_merge=(),
            repo_checked=False,
            passed=bool(change.passed),
            judged_sha=change.head_sha,
            pr_state=None,
            pr_merged=False,
            pr_head=None,
            pr_base=None,
            default_branch=None,
            signs=(),
            ci="none",
            queue=queue and bool(target),
        )
        decided, reasons = judge.merge_decision(facts)
        if decided != "queue":
            return "open", "; ".join(reasons)
        q = tables.merge_queue
        known = (await conn.execute(select(q.c.id).where(q.c.change_id == change.id))).first()
        if known is None:
            profile, model = await _builder(conn, change.builder_run_id)
            body = await plan_body(conn, change.project_id, change.plan_id) or {}
            steps = [str(step.get("id")) for step in body.get("steps") or [] if isinstance(step, dict)]
            row_id = (
                await conn.execute(
                    insert(q)
                    .values(
                        project_id=change.project_id,
                        repo=change.repo,
                        source="builder",
                        target_branch=target,
                        source_branch=change.branch,
                        run_id=change.builder_run_id,
                        plan_id=change.plan_id,
                        steps=steps,
                        change_id=change.id,
                        profile=profile,
                        model=model,
                        reason=f"waits for the merge request of {change.branch} into {target}",
                    )
                    .returning(q.c.id)
                )
            ).scalar_one()
            row = await row_of(conn, row_id)
            await record(conn, row, "queued", reason=f"the Curator's change #{change.id}, its Judge passed it")
    return "queued", f"handed to the merge queue of {change.repo}: {reasons[0]}"


# The job


@dataclass(frozen=True)
class Project:
    """What a pass reads of a project once."""

    id: int
    name: str
    policy: dict  # autonomy.effective
    repos: dict[str, autonomy.RepoFacts]
    revisions: list[tuple[datetime, dict | None]]  # the newest first
    owner_id: int | None  # who wrote the newest revision: the judge runs and the graph's reads are theirs
    charter: dict


async def _project_ids(engine: AsyncEngine) -> list[int]:
    a, q = tables.autonomy_policies, tables.merge_queue
    async with engine.begin() as conn:
        with_policy = {row[0] for row in await conn.execute(select(a.c.project_id).distinct())}
        queued = select(q.c.project_id).where(q.c.state.in_(merge_gate.QUEUED_STATES)).distinct()
        with_rows = {row[0] for row in await conn.execute(queued)}
    return sorted(with_policy | with_rows)


async def _load(conn: AsyncConnection, project_id: int) -> Project:
    a, p = tables.autonomy_policies, tables.projects
    name = (await conn.execute(select(p.c.name).where(p.c.id == project_id))).scalar_one()
    query = (
        select(a.c.created_at, a.c.body, a.c.created_by)
        .where(a.c.project_id == project_id)
        .order_by(a.c.revision.desc())
        .limit(MAX_REVISIONS)
    )
    rows = (await conn.execute(query)).all()
    repos = await project_repos(conn, project_id)
    newest = rows[0].body if rows else None
    return Project(
        id=project_id,
        name=name,
        policy=autonomy.effective(newest, repos),
        repos=repos,
        revisions=[(row.created_at, row.body) for row in rows],
        owner_id=rows[0].created_by if rows else None,
        charter=await charter_of(conn, project_id),
    )


async def advance(engine: AsyncEngine, sealer, *, blobs=None, data_dir=None, transport=None) -> dict:
    """One pass of the job hub.merge_queue up to the gate (see the module's docstring); how many merge requests ended
    in each outcome. ``transport`` is the GitLab client's, for the tests."""
    outcomes: Counter = Counter()
    reader = change_tiers.GraphReader.at(blobs, data_dir)
    for project_id in await _project_ids(engine):
        try:
            async with engine.begin() as conn:
                project = await _load(conn, project_id)
            for repo, settings in project.policy[autonomy.REPOS].items():
                await _repo(engine, sealer, project, repo, settings, outcomes, reader, transport)
        except Exception:  # one project failing leaves the others their pass
            log.exception("a project's merge queue failed", extra={"project_id": project_id})
            outcomes["failed"] += 1
    return dict(sorted(outcomes.items()))


async def _repo(engine, sealer, project: Project, repo: str, settings: dict, outcomes, reader, transport) -> None:
    facts = project.repos.get(repo)
    origin = facts.origin if facts is not None else None
    target = settings[autonomy.TARGET_BRANCH]
    if settings["mode"] != autonomy.AUTO or not origin or project_of(origin) is None or not target:
        q = tables.merge_queue
        async with engine.begin() as conn:
            query = select(q).where(
                q.c.project_id == project.id, q.c.repo == repo, q.c.state.in_(merge_gate.QUEUED_STATES)
            )
            for row in (await conn.execute(query.with_for_update())).all():
                await close(conn, row, f"the policy has {repo} in mode {settings['mode']}: a person merges it")
                outcomes["closed"] += 1
        return
    async with engine.begin() as conn:
        token = await forge_token(conn, sealer, project.id, origin)
    if token is None:  # nothing of the repo moves, and its merge requests stay as they are on GitLab
        outcomes["no_forge"] += 1
        return
    try:
        async with token.gitlab(transport=transport) as gl:
            found = RepoPass(engine, project, repo, settings, token.project.path, gl, reader, outcomes)
            await found.run()
    except GitLabUnavailable as exc:
        log.warning("a repo of the merge queue waits for GitLab", extra={"project": project.name, "why": str(exc)})
        outcomes["gitlab"] += 1


@dataclass
class Candidate:
    """A merge request the hub opens for a run's work."""

    source: str  # plan or task
    branch: str
    title: str
    description: str
    run_id: int | None
    plan_id: str | None
    steps: list[str]
    row_id: int | None  # its row, when an earlier pass could not open it


class RepoPass:
    """One pass over one repo of the queue, with its GitLab project open."""

    def __init__(self, engine, project: Project, repo: str, settings: dict, path: str, gl: GitLab, reader, outcomes):
        self.engine, self.project, self.repo, self.settings = engine, project, repo, settings
        self.path, self.gl, self.reader, self.outcomes = path, gl, reader, outcomes
        self.target: str = settings[autonomy.TARGET_BRANCH]
        self.label: str = project.policy["merge"]["opt_in_label"]
        self._checked: list[str] | None = None
        self._project_id: int | None = None  # the GitLab project's id

    async def run(self) -> None:
        listed = {mr.iid: mr for mr in await self.gl.merge_requests(self.path, target_branch=self.target)}
        await self.open_runs(listed)
        await self.take_labeled(listed)
        await self.find_builders(listed)
        q = tables.merge_queue
        async with self.engine.begin() as conn:
            query = select(q.c.id).where(
                q.c.project_id == self.project.id,
                q.c.repo == self.repo,
                q.c.state.in_(merge_gate.OPEN_STATES),
                q.c.iid.is_not(None),
            )
            ids = [row.id for row in await conn.execute(query.order_by(q.c.id))]
        for row_id in ids:
            try:
                await self.move(row_id)
            except GitLabRefused as exc:
                await self.refused(row_id, exc)
        from evo_agents.hub.server import merge_turn  # it moves the rows through this module

        await merge_turn.take_turns(self)

    async def refused(self, row_id: int, exc: GitLabRefused) -> None:
        """GitLab refused a call for merge request ``row_id``: gone (404), it leaves the queue; else it waits."""
        log.warning("a merge request of the queue was refused", extra={"row": row_id, "why": str(exc)})
        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id, lock=True)
            if row is not None and exc.status == 404:
                await close(conn, row, f"GitLab no longer has it: {exc}")
            elif row is not None:
                await wait(conn, row, f"GitLab refused the hub: {exc}")
        self.outcomes["refused"] += 1

    # Taking merge requests

    async def _insert(self, conn: AsyncConnection, values: dict, why: str, kind: str):
        q = tables.merge_queue
        row_id = (
            await conn.execute(
                insert(q)
                .values(project_id=self.project.id, repo=self.repo, target_branch=self.target, **values)
                .returning(q.c.id)
            )
        ).scalar_one()
        row = await row_of(conn, row_id)
        await record(conn, row, kind, reason=why, sha=row.head_sha)
        return row

    async def _plan_candidates(self, conn: AsyncConnection, since: datetime) -> list[Candidate]:
        r, q = tables.runs, tables.merge_queue
        latest = (
            select(r.c.plan_id, func.max(r.c.id).label("run_id"))
            .where(
                r.c.project_id == self.project.id,
                r.c.kind.in_(("plan", "step")),
                r.c.state == "done",
                r.c.plan_id.is_not(None),
                r.c.finished_at > since,
            )
            .group_by(r.c.plan_id)
        )
        found = []
        for plan_id, run_id in (await conn.execute(latest)).all():
            if judge.is_curator_plan_id(plan_id):
                continue  # the Curator's merge request opens with its push, and comes through curator_judged
            body = await plan_body(conn, self.project.id, plan_id)
            if body is None or not merge_gate.plan_ready(body, self.repo, self.target):
                continue
            known = (
                await conn.execute(
                    select(q.c.id, q.c.iid, q.c.updated_at).where(
                        q.c.project_id == self.project.id,
                        q.c.repo == self.repo,
                        q.c.source == "plan",
                        q.c.plan_id == plan_id,
                    )
                )
            ).one_or_none()
            if known is not None and (known.iid is not None or not await older(conn, known.updated_at)):
                continue
            steps = merge_gate.steps_of(body, self.repo)
            goal = " ".join(str(body.get("goal") or "").split())
            titles = "\n".join(f"- step {step.get('id')}: {' '.join(str(step.get('title')).split())}" for step in steps)
            found.append(
                Candidate(
                    source="plan",
                    branch=merge_gate.plan_branch(body, self.repo),
                    title=f"{plan_id}: {goal or 'the work of the plan'}"[:255],
                    description=(
                        f"Opened by the evo-agents hub for plan {plan_id} of project {self.project.name}, whose "
                        f"steps in {self.repo} are done (run #{run_id} last).\n\n{goal}\n\n{titles}"
                    ),
                    run_id=run_id,
                    plan_id=plan_id,
                    steps=[str(step.get("id")) for step in steps],
                    row_id=None if known is None else known.id,
                )
            )
        return found

    async def _task_candidates(self, conn: AsyncConnection, since: datetime) -> list[Candidate]:
        r, q, e = tables.runs, tables.merge_queue, tables.run_events
        pushed = exists().where(
            e.c.run_id == r.c.id, e.c.kind == "system", e.c.body["pull"]["repo"].astext == self.repo
        )
        taken = exists().where(
            q.c.project_id == self.project.id, q.c.repo == self.repo, q.c.source == "task", q.c.run_id == r.c.id
        )
        query = select(r.c.id, r.c.branch, r.c.title, r.c.request).where(
            r.c.project_id == self.project.id,
            r.c.kind == "task",
            r.c.state == "done",
            r.c.finished_at > since,
            pushed,
            ~taken,
        )
        found = []
        for row in (await conn.execute(query.order_by(r.c.id))).all():
            branch = row.branch if task_runs.is_task_branch(row.branch) else task_runs.branch_of(row.id)
            title = " ".join(str(row.title or f"task run #{row.id}").split())
            found.append(
                Candidate(
                    source="task",
                    branch=branch,
                    title=title[:255],
                    description=f"Opened by the evo-agents hub for task run #{row.id} of project {self.project.name}."
                    + (f"\n\n{row.request.strip()}" if isinstance(row.request, str) and row.request.strip() else ""),
                    run_id=row.id,
                    plan_id=None,
                    steps=[],
                    row_id=None,
                )
            )
        return found

    async def open_runs(self, listed: dict[int, MergeRequest]) -> None:
        """Open the merge request of each plan and task run whose work in the repo is done (see the docstring)."""
        since = merge_gate.auto_since(self.project.revisions, self.repo)
        if since is None:
            return
        async with self.engine.begin() as conn:
            candidates = await self._plan_candidates(conn, since) + await self._task_candidates(conn, since)
        by_branch = {mr.source_branch: mr for mr in listed.values()}
        for candidate in candidates:
            mr, opened, problem = by_branch.get(candidate.branch), False, None
            if mr is None:
                try:
                    mr = await self.gl.create_merge_request(
                        self.path,
                        source_branch=candidate.branch,
                        target_branch=self.target,
                        title=candidate.title,
                        description=candidate.description,
                    )
                    opened = True
                except GitLabRefused as exc:
                    problem = str(exc)
            await self._keep_candidate(candidate, mr, opened, problem)

    async def _keep_candidate(self, candidate: Candidate, mr: MergeRequest | None, opened: bool, problem) -> None:
        async with self.engine.begin() as conn:
            profile, model = await _builder(conn, candidate.run_id)
            values = {
                "source": candidate.source,
                "source_branch": candidate.branch,
                "run_id": candidate.run_id,
                "plan_id": candidate.plan_id,
                "steps": candidate.steps,
                "profile": profile,
                "model": model,
            }
            if mr is None:
                reason = merge_gate.reason_line(
                    f"GitLab did not open the merge request of {candidate.branch}: {problem}"
                )
                if candidate.row_id is None:
                    await self._insert(conn, {**values, "state": "blocked", "reason": reason}, reason, "gate")
                else:
                    await set_row(conn, candidate.row_id, reason=reason)
                self.outcomes["not_opened"] += 1
                return
            mr_values = {"iid": mr.iid, "title": mr.title, "web_url": mr.web_url, "state": "gating", "reason": None}
            why = f"the hub opened !{mr.iid}" if opened else f"!{mr.iid} of {candidate.branch} was open already"
            try:
                if candidate.row_id is None:
                    row = await self._insert(conn, {**values, **mr_values}, why, "opened" if opened else "queued")
                else:
                    await set_row(conn, candidate.row_id, **mr_values)
                    row = await row_of(conn, candidate.row_id)
                    await record(conn, row, "opened" if opened else "queued", reason=why)
            except IntegrityError:  # the merge request is in the queue already, under another source
                return
            if opened:
                target = f"{self.project.name}/{self.repo} !{mr.iid} source={candidate.source}"
                plan = f" plan={candidate.plan_id}" if candidate.plan_id else ""
                await audit.record(
                    conn,
                    actor_id=None,
                    token_id=None,
                    action=audit.MERGE_OPEN,
                    target=f"{target}{plan} run:{candidate.run_id}",
                    project_id=self.project.id,
                )
        self.outcomes["opened" if opened else "taken"] += 1

    async def take_labeled(self, listed: dict[int, MergeRequest]) -> None:
        """Take each merge request labeled ``merge.opt_in_label`` by a Developer or above, or take it back."""
        q = tables.merge_queue
        async with self.engine.begin() as conn:
            query = select(q.c.iid, q.c.id, q.c.state, q.c.source).where(
                q.c.project_id == self.project.id, q.c.repo == self.repo, q.c.iid.in_(list(listed))
            )
            known = {row.iid: row for row in await conn.execute(query)}
        for iid, mr in listed.items():
            row = known.get(iid)
            if self.label not in mr.labels or (row is not None and (row.source != "label" or row.state != "closed")):
                continue
            user_id = merge_gate.labeler(await self.gl.label_events(self.path, iid), self.label)
            level = await self.gl.access_level(self.path, user_id) if user_id is not None else None
            if level is None or level < merge_gate.DEVELOPER:
                continue
            why = f"labeled {self.label} by a member of role {level} on GitLab"
            async with self.engine.begin() as conn:
                values = {"iid": iid, "title": mr.title, "web_url": mr.web_url, "state": "gating", "reason": None}
                if row is None:
                    await self._insert(
                        conn, {"source": "label", "source_branch": mr.source_branch, **values}, why, "queued"
                    )
                else:
                    await set_row(conn, row.id, head_sha=None, **values)
                    await record(conn, await row_of(conn, row.id), "queued", reason=why)
            self.outcomes["taken"] += 1

    async def find_builders(self, listed: dict[int, MergeRequest]) -> None:
        """Give each Curator's change handed to the queue its merge request, found by its branch."""
        q = tables.merge_queue
        async with self.engine.begin() as conn:
            query = select(q.c.id, q.c.source_branch).where(
                q.c.project_id == self.project.id,
                q.c.repo == self.repo,
                q.c.source == "builder",
                q.c.iid.is_(None),
                q.c.state == "gating",
            )
            waiting = (await conn.execute(query)).all()
            by_branch = {mr.source_branch: mr for mr in listed.values()}
            for row in waiting:
                mr = by_branch.get(row.source_branch)
                if mr is None:
                    continue
                try:
                    async with conn.begin_nested():
                        await set_row(conn, row.id, iid=mr.iid, title=mr.title, web_url=mr.web_url, reason=None)
                except IntegrityError:
                    continue

    # Moving a merge request on

    def scope_of(self, row, mr: MergeRequest) -> tuple[str, str | None]:
        """Whether merge request ``mr`` of ``row`` is still the queue's (``merge_gate.scope``)."""
        facts = merge_gate.Scope(
            mode=autonomy.AUTO,
            forge=True,
            target_branch=self.target,
            state=mr.state,
            target=mr.target_branch,
            fork=mr.source_project_id is not None and mr.source_project_id != mr.target_project_id,
            draft=mr.draft,
            source=row.source if row.source != "label" or self.label in mr.labels else None,
            labeler_ok=True,  # checked when the queue took it (take_labeled)
        )
        return merge_gate.scope(facts)

    async def move(self, row_id: int) -> None:
        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id)
        mr = await self.gl.merge_request(self.path, row.iid)
        verdict, why = self.scope_of(row, mr)
        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id, lock=True)
            if row is None or row.state not in merge_gate.OPEN_STATES:
                return
            if verdict == "out":
                await close(conn, row, f"it left the queue: {why}")
                self.outcomes["closed"] += 1
                return
            if mr.sha and mr.sha != row.head_sha:
                await self.new_head(conn, row, mr)
                row = await row_of(conn, row_id)
            if verdict == "wait":
                await wait(conn, row, why)
                return
            if row.state == "ready" and (row.reason or "").startswith(merge_gate.WAITS):
                await set_row(conn, row.id, reason=None)  # what it waited for on GitLab is over: its turn may come
        if row.state == "gating":
            await self.gate(row_id, mr)
        elif row.state == "judging":
            await self.judging(row_id)
        await self.comment(row_id)

    async def new_head(self, conn: AsyncConnection, row, mr: MergeRequest) -> None:
        """A head the gates have not read, pushed by someone other than the queue: they start again, a judge run of
        the head before no longer counts, and the merge request waits for a turn after those queued before it."""
        await set_row(
            conn,
            row.id,
            head_sha=mr.sha,
            head_at=func.now(),
            state="gating",
            reason=None,
            queued_at=None,
            rebases=0,
            before_sha=None,
            title=mr.title,
            description=merge_gate.kept_description(mr.description),
            web_url=mr.web_url,
            judge_run_id=None,
            judge_key=None,
            judge_attempts=0,
        )
        if row.head_sha is not None:
            await record(conn, row, "head", reason=f"a new head {mr.sha[:12]}: the gates start again", sha=mr.sha)

    async def repo_check(self) -> list[str]:
        """Gate 0, once a pass: what the GitLab project lacks for the queue."""
        if self._checked is None:
            settings = await self.gl.project(self.path)
            branch = await self.gl.protected_branch(self.path, self.target)
            self._project_id = settings.id
            self._checked = merge_gate.repo_checked(
                merge_method=settings.merge_method,
                pipeline_must_succeed=settings.only_allow_merge_if_pipeline_succeeds,
                skipped_is_success=settings.allow_merge_on_skipped_pipeline,
                branch=self.target,
                push_levels=branch.push_access_levels if branch is not None else None,
                merge_levels=branch.merge_access_levels if branch is not None else None,
            )
        return self._checked

    async def head_message(self, mr: MergeRequest) -> str | None:
        commits = await self.gl.merge_request_commits(self.path, mr.iid)
        found = next((commit for commit in commits if commit.id == mr.sha), None)
        return (found.message or found.title) if found is not None else None

    async def expected_branch(self, conn: AsyncConnection, row) -> tuple[str | None, dict | None]:
        """(the branch the work of ``row`` is on, its plan's body)."""
        if row.source == "plan":
            body = await plan_body(conn, self.project.id, row.plan_id)
            return (merge_gate.plan_branch(body, self.repo) if body else None), body
        if row.source == "builder":
            return row.source_branch, await plan_body(conn, self.project.id, row.plan_id)
        return row.source_branch, None

    async def gate(self, row_id: int, mr: MergeRequest) -> None:
        """Gates 0 to 3 at the head of the merge request, then the risk; a judge run when they pass."""
        missing = await self.repo_check()
        if missing:
            async with self.engine.begin() as conn:
                await hold(conn, await row_of(conn, row_id), 0, "the GitLab project " + "; ".join(missing))
            return
        message = await self.head_message(mr)
        pipeline = mr.head_pipeline
        state, why = merge_gate.pipeline_state(
            mr.sha, pipeline.sha if pipeline else None, pipeline.status if pipeline else None, message
        )
        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id, lock=True)
            if state == "pending":
                minutes = self.project.policy["merge"]["pipeline_minutes"]
                if row.head_at is not None and await older(conn, row.head_at, timedelta(minutes=minutes)):
                    await hold(conn, row, 1, f"{why} after {minutes} minutes", blocked=True)
                else:
                    await hold(conn, row, 1, why)
                return
            if state == "red":
                await hold(conn, row, 1, why, blocked=True)
                return
            branch, body = await self.expected_branch(conn, row)
            mr_project = self.path if self._project_id in (None, mr.project_id) else f"project {mr.project_id}"
            problem = merge_gate.plan_repo_problem(
                row.source, project=self.path, mr_project=mr_project, source_branch=mr.source_branch, branch=branch
            )
            if problem is not None:
                await hold(conn, row, 2, problem, blocked=True)
                return
        files = merge_gate.files_of(await self.gl.merge_request_diffs(self.path, mr.iid))
        hacked = self.hacked(files, body)
        if hacked is not None:
            async with self.engine.begin() as conn:
                row = await row_of(conn, row_id, lock=True)
                await hold(conn, row, 3, hacked, blocked=True)
            return
        risk = await self.risk(judge.changed_paths(files))
        await self.passed(row_id, mr, merge_gate.patch_fingerprint(files), risk)

    def hacked(self, files: list[judge.FileDiff], body: dict | None) -> str | None:
        """Gate 3: why the diff ``files`` of a merge request on plan ``body`` shows signs of score hacking; None when
        it shows none."""
        signs = judge.hack_signs(
            files,
            repo=self.repo,
            protected=protected_of(self.project.charter),
            verify_commands=merge_gate.verify_of(body, self.repo),
        )
        if not signs:
            return None
        kinds = ", ".join(sorted({item["kind"] for item in signs}))
        where = signs[0]["path"] + (f":{signs[0]['line']}" if signs[0].get("line") else "")
        return f"the diff shows signs of score hacking ({kinds}), first at {where}"

    async def risk(self, paths: list[str]) -> dict:
        """Gate 6: the risk of the diff's ``paths``, with what the project's graph says they reach."""
        named = change_tiers.named_paths(self.repo, paths)
        targets = [f"{repo}:{path}" for repo, path in named]
        graph = await change_tiers.impact_as_owner(
            self.engine, self.project.id, targets, self.reader, owner_id=self.project.owner_id
        )
        async with self.engine.begin() as conn:
            unknown = await change_tiers.unknown_paths(conn, self.project.id, named, graph)
        return merge_gate.risk_of(
            self.repo,
            [path for _, path in named],
            risk_paths=self.project.policy["merge"]["risk_paths"],
            protected=protected_of(self.project.charter),
            impacted=graph.impacted if graph is not None else (),
            unknown=unknown,
        )

    async def passed(self, row_id: int, mr: MergeRequest, fingerprint: str, risk: dict) -> None:
        """Gates 0 to 3 passed at the head: the Judges next, unless their verdicts hold for this patch."""
        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id, lock=True)
            if row.state != "gating" or row.head_sha != mr.sha:
                return
            for gate in range(4):
                await record(conn, row, "gate", gate=gate, passed=True, sha=mr.sha)
            verdict = row.verdict if (row.verdict or {}).get("fingerprint") == fingerprint else None
            await set_row(
                conn,
                row.id,
                state="judging",
                reason=None,
                fingerprint=fingerprint,
                risk=risk,
                verdict=verdict,
                title=mr.title,  # the Judge reads them as they are now
                description=merge_gate.kept_description(mr.description),
            )
            row = await row_of(conn, row_id)
            if verdict is not None and next_round(verdict, risk) == "ready":
                for gate in (4, 5, 6, 7):
                    why = f"the patch is the one judged before (fingerprint {fingerprint[:12]}): its verdict holds"
                    await record(conn, row, "gate", gate=gate, passed=True, reason=why, sha=mr.sha)
                await ready(conn, row, "every gate passed, the Judges' verdicts kept for the same patch")
                self.outcomes["ready"] += 1
                return
        await self.judging(row_id)

    async def judging(self, row_id: int) -> None:
        """Queue the next judge run of ``row_id``, or block it once JUDGE_ATTEMPTS runs ended without a verdict."""
        from evo_agents.hub.server.merge_judge import queue_judge  # it reads the rows through this module

        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id, lock=True)
            if row is None or row.state != "judging":
                return
            step = next_round(row.verdict, row.risk)
            if step == "ready":
                await ready(conn, row, "every gate passed")
                self.outcomes["ready"] += 1
                return
            if step == "failed":
                await hold(conn, row, 5, "the Judges' verdict for this patch did not pass it", blocked=True)
                return
            second = step == "second"
            gate = 7 if second else 5
            if row.judge_run_id is not None:
                r = tables.runs
                ended = (await conn.execute(select(r.c.state).where(r.c.id == row.judge_run_id))).scalar_one_or_none()
                if ended not in runs.TERMINAL_STATES:
                    return
                attempts = row.judge_attempts + 1
                why = f"judge run #{row.judge_run_id} ended {ended} without a verdict"
                if attempts >= judge.JUDGE_ATTEMPTS:
                    await set_row(conn, row.id, judge_run_id=None, judge_key=None, judge_attempts=attempts)
                    await hold(
                        conn, row, gate, f"{attempts} judge runs ended without a verdict; the last: {why}", blocked=True
                    )
                    return
                await set_row(conn, row.id, judge_run_id=None, judge_key=None, judge_attempts=attempts)
                row = await row_of(conn, row_id)
            why = await queue_judge(conn, self.project, row, second=second)
            if why is not None:
                await hold_judging(conn, row, gate, why)
            else:
                self.outcomes["judging"] += 1

    async def comment(self, row_id: int) -> None:
        """Say once on GitLab why the queue blocks the merge request, or which setting the project misses."""
        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id)
        if row is None or not row.reason or row.reason == row.commented:
            return
        if row.state != "blocked" and not (row.state == "gating" and row.reason.startswith("gate 0 ")):
            return
        try:
            await self.gl.comment(self.path, row.iid, merge_gate.comment(row.reason, waits=row.state != "blocked"))
        except GitLabRefused as exc:
            log.warning("the merge queue could not comment", extra={"row": row_id, "why": str(exc)})
            return
        async with self.engine.begin() as conn:
            await set_row(conn, row_id, commented=row.reason)


async def hold_judging(conn: AsyncConnection, row, gate: int, why: str) -> None:
    """Keep ``row`` judging, with why its judge run waits; a new reason is a line of the ledger."""
    reason = merge_gate.gate_reason(gate, f"waits: {why}")
    if reason == row.reason:
        return
    await set_row(conn, row.id, reason=reason)
    await record(conn, row, "gate", gate=gate, passed=False, reason=reason, sha=row.head_sha, detail={"waits": True})


async def judge_run_title(conn: AsyncConnection, run_id: int, plan_id: str | None) -> str:
    """The title of the notice a failed judge run sends its owner: the Curator's names its plan, the queue's the merge
    request it judged (``changes.judge_ended``)."""
    if plan_id:
        return f"Judge run #{run_id} of {plan_id} failed"
    q = tables.merge_queue
    found = (await conn.execute(select(q.c.repo, q.c.iid).where(q.c.judge_run_id == run_id))).first()
    what = f"merge request !{found.iid} of {found.repo}" if found is not None else "a merge request of the merge queue"
    return f"Judge run #{run_id} of {what} failed"


def next_round(verdict: dict | None, risk: dict | None) -> str:
    """What the Judges' ``verdict`` for a patch whose risk is ``risk`` calls for next: ``first`` (the Judge),
    ``second`` (the second Judge, for a high risk), ``ready`` (both said what they had to), or ``failed``."""
    first, second = (verdict or {}).get("first"), (verdict or {}).get("second")
    if first is None:
        return "first"
    if not first.get("passed"):
        return "failed"
    if not (risk or {}).get("high"):
        return "ready"
    if second is None:
        return "second"
    return "ready" if second.get("passed") else "failed"


async def older(conn: AsyncConnection, moment: datetime, span: timedelta = OPEN_RETRY) -> bool:
    """Whether ``moment`` is more than ``span`` ago, by the database's clock."""
    return bool((await conn.execute(select(func.now() - moment > span))).scalar_one())
