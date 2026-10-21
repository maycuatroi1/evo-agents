"""The breaker of each repo of the hub's merge queue on GitLab (plan gitlab-merge-queue, criterion a7; the rules are
``evo_agents.hub.merge_breaker``'s), on the model of the Curator's circuit breaker (``outcomes.check_circuit``) but
for one repo, so a repo that fails stops no other.

``tripped`` reads, at the start of a pass of the job hub.merge_queue over a repo (``merge_gate.RepoPass``), whether
its breaker tripped: the gate then keeps the repo's merge requests waiting at their gates (``merge_gate.scope``), the
turn takes none but a revert (``merge_turn``), and what follows a merge starts no deploy (``post_merge``). ``watch``
runs in that pass once the checks of the merges are settled, before PROD and the turn: it reads the newest pipeline of
the target branch, the ledger of the repo's merge requests since the breaker's last reset and its merges of the last
days, and trips the breaker when the rules say so (``trip``): its row in merge_breakers, each merge request ready or in
its turn ``paused`` with a line ``paused`` of its ledger, a notice merge_paused to each admin of the project (the
Inbox, and Telegram or any channel they linked), and the audit merge.breaker. While it stays tripped, each pass pauses
what got ready since.

``resume`` is an admin's reset (POST /v1/projects/{p}/merge-queue/repos/{repo}/resume, ``evo-agents hub merge
resume``): the breaker is armed again, what trips it counts from then, its paused merge requests are ready again at
their place, its notices are read, and the audit says merge.resume. ``breakers`` lists them for the queue's view.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import and_, distinct, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import merge_breaker as rules
from evo_agents.hub import merge_gate, post_merge, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server import merge_gate as queue
from evo_agents.hub.server import post_merge as after_merge
from evo_agents.hub.server.gitlab import GitLabRefused

log = logging.getLogger(__name__)

NOTICE = "merge_paused"  # runs.NOTICE_KINDS
MAX_FAILURES = 20  # lines of the ledger that tripped it, named in its reason
BLOCKING = ("gate", "pipeline", "rebased", "merged")  # the lines of a revert's merge request blocked
MISSED = ("revert_opened", "rollback")  # a merge the Judge passed, then found the culprit or rolled back


def _repo(q, project_id: int, repo: str):
    return (q.c.project_id == project_id) & (q.c.repo == repo)


async def row_of(conn: AsyncConnection, project_id: int, repo: str, *, lock: bool = False):
    b = tables.merge_breakers
    query = select(b).where(b.c.project_id == project_id, b.c.repo == repo)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def tripped(engine: AsyncEngine, project_id: int, repo: str) -> bool:
    """Whether the breaker of ``repo`` tripped."""
    async with engine.begin() as conn:
        found = await row_of(conn, project_id, repo)
    return found is not None and found.state == rules.TRIPPED


# Watching


async def watch(found, *, read_target: bool = True) -> None:
    """Trip the breaker of the repo of ``found``, a pass over it (``queue.RepoPass``), when the rules say so; pause
    what got ready while it stays tripped. ``read_target`` False reads not the target branch on GitLab again, only
    how long the hub has seen it red."""
    project, repo = found.project, found.repo
    b = tables.merge_breakers
    async with found.engine.begin() as conn:
        await conn.execute(
            insert(b)
            .values(project_id=project.id, repo=repo)
            .on_conflict_do_nothing(index_elements=["project_id", "repo"])
        )
        breaker = await row_of(conn, project.id, repo)
    if breaker.state == rules.TRIPPED:
        found.tripped = True
        async with found.engine.begin() as conn:
            await pause(conn, project.id, repo, breaker.reason)
        return
    red = await red_minutes(found, breaker) if read_target else await _minutes(found.engine, breaker.red_since)
    async with found.engine.begin() as conn:
        facts = await facts_of(conn, found, breaker.resumed_at, red)
    causes = rules.causes(facts, project.policy["breaker"])
    if causes:
        await trip(found, causes)


async def red_minutes(found, breaker) -> float | None:
    """How long the target branch has been red, as the hub saw it; None while it is not. The newest pipeline of its
    head failing turns it red, one passing green, and any other leaves it as it was."""
    try:
        head = await found.gl.branch_head(found.path, found.target)
        pipelines = await found.gl.pipelines(found.path, head, ref=found.target) if head else []
    except GitLabRefused as exc:
        log.warning("the breaker could not read the target branch", extra={"repo": found.repo, "why": str(exc)})
        pipelines = None
    state = rules.target_state(pipelines[0].status if pipelines else None)
    b = tables.merge_breakers
    async with found.engine.begin() as conn:
        if state == "red" and breaker.red_since is None:
            await conn.execute(
                update(b).values(red_since=func.now(), updated_at=func.now()).where(b.c.id == breaker.id)
            )
            return 0.0
        if state == "green" and breaker.red_since is not None:
            await conn.execute(update(b).values(red_since=None, updated_at=func.now()).where(b.c.id == breaker.id))
            return None
    return await _minutes(found.engine, breaker.red_since)


async def _minutes(engine: AsyncEngine, since) -> float | None:
    """The minutes since ``since`` by the database's clock; None when it is None."""
    if since is None:
        return None
    async with engine.begin() as conn:
        minutes = await conn.execute(select(func.extract("epoch", func.now() - since) / 60))
    return float(minutes.scalar_one())


async def facts_of(conn: AsyncConnection, found, since, red: float | None) -> rules.Facts:
    """What trips the breaker of the repo of ``found``, counted since its last reset ``since`` (None: never reset)."""
    q = tables.merge_queue
    project, repo = found.project, found.repo
    merges = _repo(q, project.id, repo) & (q.c.source != post_merge.REVERT) & q.c.merged_at.is_not(None)
    query = select(q.c.state, q.c.uat_at.is_not(None), q.c.prod_result).where(merges)
    if since is not None:
        query = query.where(q.c.merged_at > since)
    in_a_row = rules.in_a_row([tuple(row) for row in await conn.execute(query.order_by(q.c.merged_at, q.c.id))])
    counted = select(func.count(), func.count().filter(q.c.state.in_(rules.CULPRIT_STATES))).where(merges)
    days = timedelta(days=rules.RATE_DAYS)
    window = tuple((await conn.execute(counted.where(q.c.merged_at > func.now() - days))).one())
    baseline = None
    auto = merge_gate.auto_since(project.revisions, repo)
    if auto is not None and await queue.older(conn, auto, days):
        first = counted.where(q.c.merged_at >= auto, q.c.merged_at < auto + days)
        baseline = tuple((await conn.execute(first)).one())
    misses, new_miss = await _misses(conn, project.id, repo, since)
    opened = tables.merge_events.c.kind == "revert_opened"  # a merge found the culprit, its revert opened or refused
    return rules.Facts(
        red_minutes=red,
        in_a_row=in_a_row,
        window=window,
        baseline=baseline,
        new_culprit=since is None or await _since(conn, project.id, repo, since, opened),
        misses=misses,
        new_miss=new_miss,
        failures=await _failures(conn, project.id, repo, since),
    )


def _ledger(project_id: int, repo: str, *columns):
    """The lines of the ledger of the repo's merge requests, with ``columns`` of theirs and of their merge requests."""
    q, e = tables.merge_queue, tables.merge_events
    return select(*columns).join_from(e, q, q.c.id == e.c.queue_id).where(_repo(q, project_id, repo))


async def _since(conn: AsyncConnection, project_id: int, repo: str, since, *where) -> bool:
    e = tables.merge_events
    query = _ledger(project_id, repo, e.c.id).where(e.c.created_at > since, *where).limit(1)
    return bool((await conn.execute(select(query.exists()))).scalar_one())


async def _misses(conn: AsyncConnection, project_id: int, repo: str, since) -> tuple[int, bool]:
    """(merges the Judge passed found wrong in the last MISS_DAYS, whether one was found wrong since ``since``)."""
    q, e = tables.merge_queue, tables.merge_events
    missed = and_(
        q.c.source != post_merge.REVERT,
        q.c.verdict[("first", "passed")].as_boolean().is_(True),
        or_(e.c.kind.in_(MISSED), and_(e.c.kind == "sample_answered", e.c.passed.is_(False))),
    )
    recent = e.c.created_at > func.now() - timedelta(days=rules.MISS_DAYS)
    count = await conn.execute(_ledger(project_id, repo, func.count(distinct(e.c.queue_id))).where(missed, recent))
    misses = count.scalar_one()
    if since is None:
        return misses, misses > 0
    return misses, await _since(conn, project_id, repo, since, missed)


async def _failures(conn: AsyncConnection, project_id: int, repo: str, since) -> tuple[tuple[str, str], ...]:
    """(cause, why) of each line of the repo's ledger since ``since`` that trips the breaker by itself: a check that
    failed or has no result, a rollback, a revert GitLab would not make, a revert's merge request blocked."""
    q, e = tables.merge_queue, tables.merge_events
    result = e.c.detail["result"].astext
    waits = func.coalesce(e.c.detail["waits"].as_boolean(), False)
    revert = q.c.source == post_merge.REVERT
    failing = or_(
        and_(e.c.kind.in_(("uat_check", "prod_deploy", "prod_check")), result.in_(rules.FAILED)),
        e.c.kind == "rollback",
        and_(e.c.kind == "revert_opened", e.c.passed.is_(False)),
        and_(revert, e.c.kind.in_(BLOCKING), e.c.passed.is_(False), waits.is_(False)),
    )
    query = _ledger(project_id, repo, e.c.kind, result.label("result"), e.c.reason, q.c.iid, revert.label("revert"))
    query = query.where(failing)
    if since is not None:
        query = query.where(e.c.created_at > since)
    found: list[tuple[str, str]] = []
    for row in await conn.execute(query.order_by(e.c.id).limit(MAX_FAILURES)):
        item = rules.failure_of(
            row.kind, row.result, row.iid, row.reason, revert=bool(row.revert) and row.kind in BLOCKING
        )
        if item not in found:
            found.append(item)
    return tuple(found)


# Tripping


async def pause(conn: AsyncConnection, project_id: int, repo: str, reason: str | None) -> list[int]:
    """Each merge request of ``repo`` ready or in its turn, a revert aside, waits for an admin to reset the breaker
    (``paused``), with a line of its ledger; their iids."""
    q = tables.merge_queue
    query = (
        select(q)
        .where(_repo(q, project_id, repo), q.c.state.in_(rules.PAUSED_FROM), q.c.source != post_merge.REVERT)
        .order_by(q.c.id)
        .with_for_update()
    )
    why = merge_gate.reason_line(f"the breaker of {repo} tripped: it waits for an admin to reset it ({reason})")
    paused = []
    for row in (await conn.execute(query)).all():
        await queue.set_row(conn, row.id, state=rules.PAUSED, reason=why)
        await queue.record(conn, row, "paused", reason=why, sha=row.head_sha)
        paused.append(row.iid)
    return paused


async def trip(found, causes: list[tuple[str, str]]) -> None:
    """Trip the breaker of the repo of ``found`` for ``causes`` (``rules.causes``): its row, the merge requests it
    pauses, a notice to each admin of the project, the audit."""
    project, repo = found.project, found.repo
    b = tables.merge_breakers
    cause, reason = causes[0][0], rules.reason_of(causes)
    named = list(dict.fromkeys(item[0] for item in causes))
    async with found.engine.begin() as conn:
        breaker = await row_of(conn, project.id, repo, lock=True)
        if breaker.state == rules.TRIPPED:
            found.tripped = True
            return
        values = {"state": rules.TRIPPED, "cause": cause, "reason": reason, "tripped_at": func.now()}
        await conn.execute(
            update(b).values(trips=b.c.trips + 1, updated_at=func.now(), **values).where(b.c.id == breaker.id)
        )
        paused = await pause(conn, project.id, repo, reason)
        await tell(conn, found, cause, named, reason, paused)
        what = f"{project.name}/{repo} cause={cause} causes={','.join(named)}"
        if paused:
            what += f" paused={post_merge.iids(paused).replace(' ', '')}"
        await audit.record(
            conn, actor_id=None, token_id=None, action=audit.MERGE_BREAKER, target=what, project_id=project.id
        )
    found.tripped = True
    found.outcomes["tripped"] += 1
    log.warning("merge breaker tripped", extra={"project": project.name, "repo": repo, "causes": named})


async def tell(conn: AsyncConnection, found, cause: str, named: list[str], reason: str, paused: list[int]) -> None:
    """The notice merge_paused, in the Inbox of each admin of the project and on the channels they linked."""
    from evo_agents.hub.server.notifications import notify  # it reaches the plugins' channels

    project, repo = found.project, found.repo
    lines = [
        f"The breaker of {repo} in project {project.name} tripped: {reason}.",
        "",
        f"The hub rebases, merges and deploys nothing of {repo} until an admin resets it, with `evo-agents hub merge "
        f"resume {repo} --project {project.name}` or on the merge queue page. The rollback of PROD and the revert of "
        "the failure still run, and the hub goes on following the checks of what it merged.",
    ]
    if paused:
        lines += ["", f"Paused: {post_merge.iids(paused)}."]
    for user_id in await after_merge.admins(conn, project.id):
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=NOTICE,
            project_id=project.id,
            title=f"The merge queue of {repo} stopped: {rules.CAUSE_WORDS[cause]}",
            body="\n".join(lines),
            details={"repo": repo, "branch": found.target, "cause": cause, "causes": named, "paused": paused},
            link=after_merge.link(project.name),
        )


# An admin's reset


async def resume(conn: AsyncConnection, user, access, repo: str) -> list[int]:
    """An admin of the project resets the breaker of ``repo``; the iids of the merge requests ready again."""
    after_merge.admin_only(user, access)
    b, q, n = tables.merge_breakers, tables.merge_queue, tables.notifications
    breaker = await row_of(conn, access.project_id, repo, lock=True)
    if breaker is None or breaker.state != rules.TRIPPED:
        raise HTTPException(409, f"the breaker of {repo} in project {access.name} has not tripped")
    values = {"state": rules.ARMED, "resumed_by": user.user_id, "resumed_at": func.now(), "red_since": None}
    await conn.execute(update(b).values(updated_at=func.now(), **values).where(b.c.id == breaker.id))
    query = select(q).where(_repo(q, access.project_id, repo), q.c.state == rules.PAUSED).order_by(q.c.id)
    why = f"{user.login} reset the breaker of {repo}: it waits for its turn again, at its place"
    resumed = []
    for row in (await conn.execute(query.with_for_update())).all():
        await queue.set_row(conn, row.id, state="ready", reason=None)
        await queue.record(conn, row, "queued", reason=why, sha=row.head_sha, detail={"resumed": True})
        resumed.append(row.iid)
    told = (n.c.project_id == access.project_id) & (n.c.notice_kind == NOTICE) & (n.c.details["repo"].astext == repo)
    await conn.execute(update(n).values(read_at=func.now()).where(told, n.c.read_at.is_(None)))
    target = f"{access.name}/{repo}" + (f" resumed={post_merge.iids(resumed).replace(' ', '')}" if resumed else "")
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.MERGE_RESUME,
        target=target,
        project_id=access.project_id,
    )
    log.info("merge breaker reset", extra={"project": access.name, "repo": repo, "login": user.login})
    return resumed


# The view


async def breakers(conn: AsyncConnection, project_id: int, repo: str | None = None) -> list:
    """The breakers of the project (``repo``'s alone when given), by repo, with the login of who reset each last."""
    b, u = tables.merge_breakers, tables.users
    query = (
        select(b, u.c.login.label("resumed_login"))
        .join_from(b, u, u.c.id == b.c.resumed_by, isouter=True)
        .where(b.c.project_id == project_id)
    )
    if repo is not None:
        query = query.where(b.c.repo == repo)
    return (await conn.execute(query.order_by(b.c.repo))).all()
