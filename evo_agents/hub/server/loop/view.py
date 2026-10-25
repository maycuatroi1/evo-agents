"""What one tick of the Loop reads of a project (``model.View``), in the tick's transaction: the loop section of its
policy, its goals with whether each one's metric meets its target now, its work items that are not finished and the
chains an event needs, the claims of those with their cell and their status as the newest approved baseline holds it,
the frame tasks, approvals, runs, plans and merge requests the work items wait on, when the weekly look at the goals
comes, and whether a solution goes through use cases (``loop.use_cases``: auto does when the plugin srs is on in the
project and it has an SRS document not archived).

Everything is read with SQLAlchemy Core on the hub's tables: the claims, use cases and tasks of the core plugins canvas,
srs and tasks through ``tables_canvas``, ``tables_srs`` and ``tables_tasks``, never by importing a plugin. A goal's
metric is summed up as a test card of the canvas sums up signals of a key of kind number: its ``aggregate`` over its
``window_days`` (90 when it names none), of its tenant when it names one, the percentiles the nearest rank; nothing
calls a model.
"""

from __future__ import annotations

import operator
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, time, timedelta

from sqlalchemy import Text, column, func, literal, or_, select, table
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals, tables
from evo_agents.hub.autonomy import AUTO, WEEKDAYS, repo_policy
from evo_agents.hub.chats import chat_link as chats_link
from evo_agents.hub.hub_events import BASELINE, SRS_APPROVED
from evo_agents.hub.merge_gate import GITLAB_STATE, MERGE_SHA
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.autonomy_policies import effective_policy, newest, project_repos
from evo_agents.hub.server.loop.model import (
    ACTIVE,
    ALWAYS,
    AUTO_MODE,
    BRAINSTORM,
    DELIVER,
    DISPATCHED,
    DRAFT,
    FINISHED,
    RUN_ENDED,
    USE_CASE,
    ApprovalView,
    ClaimView,
    Event,
    GoalView,
    ItemView,
    MergeView,
    RunView,
    TaskView,
    VersionView,
    View,
)
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.plugins import config as plugin_config

TASKS = "tasks"  # the core plugin whose tasks a build waits on
SRS = "srs"  # the core plugin whose SRS documents put a project in use cases (loop.use_cases auto)
NUMBER = "number"  # tables_signals: the kind of key a goal's metric names
OPEN_PROPOSAL = "open"  # canvas_proposals.status
OPEN_FINDING, DECIDE = "open", "decide"  # tables_srs.FINDING_STATUSES, FINDING_LEVELS: a finding that holds approval
REQUEST_CHANGES = "request_changes"  # tables_srs.REVIEW_DECISIONS
COMPARE = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt}
UTC_ZONE = "UTC"
PG_TIMEZONES = table("pg_timezone_names", column("name", Text))  # the zones Postgres knows
CHECK_JOBS = ("uat_check", "prod_check")  # a repo naming either waits for them after a merge
MAX_HOPS = 20  # how far a run handed out is followed through the runs that resume it or retry it
RELEASED = ("merged", "not-needed")  # the status of a repo of a plan once a person merged it, or it needs nothing
CLOSED_MERGE = "closed"  # merge_gate.STATES: it left the queue; merged by a person when its ledger says so


# Goals


async def measure(
    conn: AsyncConnection, project_id: int, metric: dict, now: datetime, *, bounded: bool = False
) -> float | None:
    """The value of ``metric`` now; None when its key is no number key of the project or no signal is in its window.
    ``bounded`` leaves out the signals measured after ``now``, so a moment in the past gets the value it had then
    (Today compares a problem's metric with its value seven days ago)."""
    k, s = tables.signal_keys, tables.signals
    key_id = await conn.scalar(
        select(k.c.id).where(k.c.project_id == project_id, k.c.name == metric.get("key"), k.c.kind == NUMBER)
    )
    if key_id is None:
        return None
    window = timedelta(days=metric.get("window_days") or goals.DEFAULT_WINDOW_DAYS)
    where = [s.c.key_id == key_id, s.c.value.is_not(None), s.c.measured_at > now - window]
    if bounded:
        where.append(s.c.measured_at <= now)
    if metric.get("tenant"):
        where.append(s.c.tenant == metric["tenant"])
    aggregate = metric.get("aggregate") or "last"
    if aggregate == "last":
        latest = select(s.c.value).where(*where).order_by(s.c.measured_at.desc(), s.c.id.desc()).limit(1)
        value = await conn.scalar(latest)
        return None if value is None else float(value)
    figures = {
        "mean": func.avg(s.c.value),
        "sum": func.sum(s.c.value),
        "count": func.count(),
        "p50": func.percentile_disc(0.5).within_group(s.c.value),
        "p90": func.percentile_disc(0.9).within_group(s.c.value),
    }
    count, value = (await conn.execute(select(func.count(), figures[aggregate]).where(*where))).one()
    return float(value) if count and value is not None else None


async def reached(conn: AsyncConnection, project_id: int, metric: dict | None, now: datetime) -> bool | None:
    """Whether goal metric ``metric`` meets its target now; None without a metric or a value."""
    if not metric or metric.get("comparator") not in COMPARE or metric.get("target") is None:
        return None
    value = await measure(conn, project_id, metric, now)
    return None if value is None else COMPARE[metric["comparator"]](value, metric["target"])


async def _goals(conn: AsyncConnection, project_id: int, now: datetime) -> dict[int, GoalView]:
    """Every goal of the project at its newest revision, the active ones with a metric measured."""
    g, r = tables.goals, tables.goal_revisions
    query = (
        select(
            g.c.id,
            g.c.number,
            r.c.revision,
            r.c.status,
            r.c.metric,
            r.c.canvas_id,
            r.c.rank,
            r.c.owner_id,
            r.c.problem_id,
            r.c.opportunity_id,
        )
        .join_from(g, r, (r.c.goal_id == g.c.id) & (r.c.revision == g.c.revision))
        .where(g.c.project_id == project_id)
    )
    found = {}
    for row in await conn.execute(query):
        measured = await reached(conn, project_id, row.metric, now) if row.status == ACTIVE else None
        found[row.id] = GoalView(
            row.id,
            goals.code(row.number),
            row.revision,
            row.status,
            row.metric is not None,
            measured,
            row.canvas_id,
            row.rank,
            row.owner_id,
            row.problem_id,
            row.opportunity_id,
        )
    return found


# Work items


def _item_query():
    i = tables.loop_items
    return select(
        i.c.id,
        i.c.kind,
        i.c.key,
        i.c.state,
        i.c.reason,
        i.c.goal_id,
        i.c.claim_id,
        i.c.srs_document_id,
        i.c.use_case,
        i.c.srs_version,
        i.c.task_id,
        i.c.plan_id,
        i.c.run_id,
        i.c.parent_id,
        i.c.detail,
        i.c.attempts,
        i.c.failures,
        i.c.problem_id,
        i.c.opportunity_id,
    )


def _wanted(project_id: int, events: Sequence[Event]):
    """Which work items a tick reads: those not finished, every one of a claim a baseline names, and every use_case
    with the work items of its claim when a version was approved."""
    i = tables.loop_items
    wanted = [i.c.state.not_in(FINISHED)]
    claims = {moved["claim_id"] for e in events if e.kind == BASELINE for moved in e.payload.get("transitions") or []}
    if claims:
        wanted.append(i.c.claim_id.in_(sorted(claims)))
    if any(e.kind == SRS_APPROVED for e in events):
        cases = select(i.c.claim_id).where(
            i.c.project_id == project_id, i.c.kind == USE_CASE, i.c.claim_id.is_not(None)
        )
        wanted.extend([i.c.kind == USE_CASE, i.c.claim_id.in_(cases.scalar_subquery())])
    return or_(*wanted)


async def _items(conn: AsyncConnection, project_id: int, events: Sequence[Event]) -> tuple[ItemView, ...]:
    i, e = tables.loop_items, tables.loop_item_events
    rows = list(await conn.execute(_item_query().where(i.c.project_id == project_id, _wanted(project_id, events))))
    runs: dict[int, set[int]] = defaultdict(set)
    if rows:
        handed = select(e.c.item_id, e.c.run_id).where(
            e.c.item_id.in_([row.id for row in rows]), e.c.kind == DISPATCHED, e.c.run_id.is_not(None)
        )
        for item_id, run_id in await conn.execute(handed):
            runs[item_id].add(run_id)
    found = []
    for row in sorted(rows, key=lambda row: row.id):
        fields = row._asdict()
        held = runs[row.id] | ({row.run_id} if row.run_id is not None else set())
        found.append(ItemView(**fields, runs=tuple(sorted(held))))
    return tuple(found)


async def _brainstorms(conn: AsyncConnection, project_id: int):
    """(the keys of the project's brainstorms, when each goal's newest was made, the open proposals of claims each
    goal's brainstorm runs made)."""
    i, e, p = tables.loop_items, tables.loop_item_events, tables.canvas.proposals
    mine = (i.c.project_id == project_id) & (i.c.kind == BRAINSTORM)
    keys = frozenset(await conn.scalars(select(i.c.key).where(mine)))
    newest_made = (
        select(i.c.goal_id, func.max(i.c.created_at)).where(mine, i.c.goal_id.is_not(None)).group_by(i.c.goal_id)
    )
    made = {goal_id: at for goal_id, at in await conn.execute(newest_made)}
    proposals = (
        select(i.c.goal_id, func.count(func.distinct(p.c.id)))
        .join_from(p, e, e.c.run_id == p.c.run_id)
        .join(i, i.c.id == e.c.item_id)
        .where(mine, e.c.kind == DISPATCHED, p.c.status == OPEN_PROPOSAL)
        .group_by(i.c.goal_id)
    )
    return keys, made, {goal_id: count for goal_id, count in await conn.execute(proposals)}


# Claims


async def _claims(conn: AsyncConnection, ids: Iterable[int]) -> dict[int, ClaimView]:
    """The claims ``ids``, each with its cell and the status the newest approved baseline of its canvas holds."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    c, k, b = tables.canvas.claims, tables.canvas.canvases, tables.canvas.baselines
    columns = (c.c.id, c.c.canvas_id, k.c.slug, c.c.number, c.c.use_cases, c.c.cell, c.c.delivery)
    query = select(*columns).join_from(c, k, k.c.id == c.c.canvas_id)
    rows = list(await conn.execute(query.where(c.c.id.in_(wanted))))
    statuses: dict[int, str] = {}
    for canvas_id in sorted({row.canvas_id for row in rows}):
        newest_approved = (
            select(b.c.snapshot)
            .where(b.c.canvas_id == canvas_id, b.c.status == "approved")
            .order_by(b.c.approved_at.desc(), b.c.id.desc())
            .limit(1)
        )
        for entry in await conn.scalar(newest_approved) or []:
            statuses[entry["claim_id"]] = entry.get("status")
    return {
        row.id: ClaimView(
            row.id,
            row.canvas_id,
            row.slug,
            f"CL-{row.number}",
            statuses.get(row.id),
            tuple(row.use_cases),
            row.cell,
            row.delivery,
        )
        for row in rows
    }


# Tasks, approvals, runs and merge requests


async def status_groups(conn: AsyncConnection, row, project_id: int) -> dict[str, str | None]:
    """status -> its group, as the config of the plugin tasks (its row of ``enabled_query``) in the project sets them."""
    stacks = await stored_stacks(conn, [row.id], project_id, None)
    merged = plugin_config.merge(row.manifest.get("config"), stacks.get(row.id, []))
    field = merged.fields.get("statuses")
    statuses = field.value if field is not None and isinstance(field.value, list) else []
    return {status["key"]: status.get("group") for status in statuses if isinstance(status, dict) and status.get("key")}


async def _tasks(conn: AsyncConnection, project_id: int, items: Sequence[ItemView], plugin) -> tuple[dict, dict]:
    """(the frame tasks of ``items`` by id, those of the builds still waiting for theirs by external key)."""
    ids = {item.task_id for item in items if item.task_id is not None}
    frames = {item.detail.get("frame") for item in items if item.task_id is None and item.detail.get("frame")}
    if plugin is None or not (ids or frames):
        return {}, {}
    groups = await status_groups(conn, plugin, project_id)
    t, sp = tables.tasks.items, tables.tasks.sprints
    query = (
        select(t.c.id, t.c.number, t.c.status, t.c.due_on, t.c.external_key, sp.c.state.label("sprint"))
        .select_from(t.outerjoin(sp, sp.c.id == t.c.sprint_id))
        .where(t.c.project_id == project_id, or_(t.c.id.in_(sorted(ids)), t.c.external_key.in_(sorted(frames))))
    )
    by_id, by_key = {}, {}
    for row in await conn.execute(query):
        task = TaskView(row.id, row.number, groups.get(row.status), row.sprint == "active", row.due_on)
        by_id[row.id] = task
        if row.external_key in frames:
            by_key[row.external_key] = task
    return by_id, by_key


async def _run_states(conn: AsyncConnection, ids: Iterable[int]) -> dict[int, str]:
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    r = tables.runs
    return {run_id: state for run_id, state in await conn.execute(select(r.c.id, r.c.state).where(r.c.id.in_(wanted)))}


async def _approvals(conn: AsyncConnection, items: Sequence[ItemView]) -> dict[int, ApprovalView]:
    """What the plugin srs did of the version each waiting build names, by the version's id."""
    versions = {item.detail.get("version_id") for item in items if item.detail.get("version_id") is not None}
    if not versions:
        return {}
    a = tables.srs.approval_tasks
    rows = list(
        await conn.execute(select(a.c.version_id, a.c.split_run_id).where(a.c.version_id.in_(sorted(versions))))
    )
    states = await _run_states(conn, (row.split_run_id for row in rows if row.split_run_id is not None))
    return {
        row.version_id: ApprovalView(
            row.version_id,
            row.split_run_id,
            row.split_run_id is None or states.get(row.split_run_id) in RUN_ENDED,
        )
        for row in rows
    }


def _steps_done(body: dict | None) -> bool | None:
    steps = (body or {}).get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    return all(isinstance(step, dict) and step.get("status") == "done" for step in steps)


async def run_chains(conn: AsyncConnection, ids: Iterable[int]) -> dict[int, list[int]]:
    """Each run of ``ids`` -> it and the runs that go on from it, oldest first: one that resumes it
    (``resume_of_run_id``) or is its next attempt (``parent_run_id``), and so on, at most MAX_HOPS deep."""
    r = tables.runs
    chains = {run_id: [run_id] for run_id in ids}
    frontier = {run_id: run_id for run_id in chains}  # a run reached -> the run handed out it goes on from
    for _ in range(MAX_HOPS):
        if not frontier:
            break
        wanted = sorted(frontier)
        query = select(r.c.id, r.c.resume_of_run_id, r.c.parent_run_id).where(
            or_(r.c.resume_of_run_id.in_(wanted), r.c.parent_run_id.in_(wanted))
        )
        reached = {}
        for row in await conn.execute(query.order_by(r.c.id)):
            origin = frontier.get(row.resume_of_run_id, frontier.get(row.parent_run_id))
            if origin is not None and row.id not in chains[origin]:
                chains[origin].append(row.id)
                reached[row.id] = origin
        frontier = reached
    return {origin: sorted(chain) for origin, chain in chains.items()}


async def _proposals(conn: AsyncConnection, ids: Sequence[int]) -> dict[int, int]:
    """Run -> the proposals of claims it made (a brainstorm's)."""
    p = tables.canvas.proposals
    query = select(p.c.run_id, func.count()).where(p.c.run_id.in_(ids)).group_by(p.c.run_id)
    return {run_id: count for run_id, count in await conn.execute(query)}


async def _run_rows(conn: AsyncConnection, project_id: int, ids: Sequence[int]) -> dict[int, RunView]:
    r, p, d = tables.runs, tables.plans, tables.decisions
    open_decisions = select(func.count()).where(d.c.run_id == r.c.id, d.c.state == "open").scalar_subquery()
    columns = (r.c.id, r.c.kind, r.c.state, r.c.plan_id, r.c.repos, r.c.finish_requested_at, open_decisions)
    rows = list(await conn.execute(select(*columns).where(r.c.id.in_(ids))))
    plan_ids = sorted({row.plan_id for row in rows if row.kind == "plan" and row.plan_id})
    bodies = {}
    if plan_ids:
        query = select(p.c.plan_id, p.c.body).where(p.c.project_id == project_id, p.c.plan_id.in_(plan_ids))
        bodies = {plan_id: body for plan_id, body in await conn.execute(query)}
    made = await _proposals(conn, [row.id for row in rows if row.kind == "task"])
    found = {}
    for row in rows:
        repos = tuple(entry["repo"] for entry in row.repos or [] if isinstance(entry, dict) and entry.get("repo"))
        found[row.id] = RunView(
            row.id,
            row.kind,
            row.state,
            row.plan_id if row.kind == "plan" else None,
            _steps_done(bodies.get(row.plan_id)) if row.kind == "plan" else None,
            repos if row.kind == "plan" else (),
            row.finish_requested_at is not None,
            int(row[6] or 0),
            made.get(row.id, 0),
        )
    return found


async def _written(conn: AsyncConnection, project_id: int, chains: Mapping[int, list[int]]) -> dict[int, str]:
    """The run handed out -> the plan an author run of its chain wrote first."""
    pr = tables.plan_revisions
    every = sorted({run_id for chain in chains.values() for run_id in chain})
    query = select(pr.c.run_id, func.min(pr.c.plan_id)).where(pr.c.project_id == project_id, pr.c.run_id.in_(every))
    by_run = {run_id: plan_id for run_id, plan_id in await conn.execute(query.group_by(pr.c.run_id))}
    return {
        origin: next(by_run[run_id] for run_id in chain if run_id in by_run)
        for origin, chain in chains.items()
        if any(run_id in by_run for run_id in chain)
    }


async def _runs(conn: AsyncConnection, project_id: int, items: Sequence[ItemView]) -> dict[int, RunView]:
    """The run in flight of each unfinished work item, as the newest run that goes on from it stands (``RunView``), by
    the id handed out; and the chat run of each work item's triage thread, by its own id. An author run's plan is the
    one a run of its chain wrote."""
    handed = {item.run_id for item in items if item.run_id is not None and not item.finished}
    chats = {item.detail["triage"].get("run_id") for item in items if isinstance(item.detail.get("triage"), dict)}
    wanted = sorted(handed | {run_id for run_id in chats if isinstance(run_id, int)})
    if not wanted:
        return {}
    chains = await run_chains(conn, wanted)
    views = await _run_rows(conn, project_id, sorted({chain[-1] for chain in chains.values()}))
    written = await _written(conn, project_id, chains)
    found = {}
    for run_id, chain in chains.items():
        view = views.get(chain[-1])
        if view is not None:
            found[run_id] = replace(view, plan_id=written.get(run_id)) if view.kind == "author" else view
    return found


async def _merged_outside(conn: AsyncConnection, ids: Sequence[int]) -> dict[int, tuple]:
    """Each merge request of ``ids`` the queue closed because GitLab says a person merged it -> (the merge commit
    GitLab named, when the queue saw it), from the newest line ``closed`` of its ledger."""
    if not ids:
        return {}
    e = tables.merge_events
    query = select(e.c.queue_id, e.c.detail, e.c.created_at).where(e.c.queue_id.in_(ids), e.c.kind == "closed")
    newest_line = {row.queue_id: row for row in await conn.execute(query.order_by(e.c.id))}
    return {
        queue_id: (row.detail.get(MERGE_SHA), row.created_at)
        for queue_id, row in newest_line.items()
        if (row.detail or {}).get(GITLAB_STATE) == "merged"
    }


async def _merges(conn: AsyncConnection, project_id: int, items: Sequence[ItemView]) -> dict[str, tuple]:
    """The merge requests of the merge queue of each deliver's plan, each with whether its repo names a check job, and
    whether a person merged it on GitLab while the queue had it (``MergeView.outside``)."""
    plans = sorted({item.plan_id for item in items if item.kind == DELIVER and item.plan_id and not item.finished})
    if not plans:
        return {}
    q = tables.merge_queue
    query = select(q.c.id, q.c.repo, q.c.state, q.c.after_sha, q.c.merged_at, q.c.plan_id).where(
        q.c.project_id == project_id, q.c.plan_id.in_(plans)
    )
    rows = list(await conn.execute(query.order_by(q.c.id)))
    outside = await _merged_outside(conn, [row.id for row in rows if row.state == CLOSED_MERGE])
    held = await newest(conn, project_id)
    body = None if held is None else held.body
    found: dict[str, list[MergeView]] = defaultdict(list)
    for row in rows:
        jobs = repo_policy(body, row.repo)["jobs"]
        checked = any(jobs.get(job) for job in CHECK_JOBS)
        merge = MergeView(row.id, row.repo, row.state, row.after_sha, row.merged_at, checked)
        if row.id in outside:
            sha, seen = outside[row.id]
            merge = replace(merge, after_sha=sha if isinstance(sha, str) else None, merged_at=seen, outside=True)
        found[row.plan_id].append(merge)
    return {plan_id: tuple(merges) for plan_id, merges in found.items()}


async def _newest_versions(conn: AsyncConnection, documents: Sequence[int]) -> dict[int, tuple]:
    """Document -> (its newest version's id, number, state and round, its slug)."""
    v, d = tables.srs.versions, tables.srs.documents
    newest = select(v.c.document_id, func.max(v.c.number).label("number")).where(v.c.document_id.in_(documents))
    newest = newest.group_by(v.c.document_id).subquery()
    query = (
        select(v.c.document_id, v.c.id, v.c.number, v.c.state, v.c.round, d.c.slug)
        .join_from(v, newest, (newest.c.document_id == v.c.document_id) & (newest.c.number == v.c.number))
        .join(d, d.c.id == v.c.document_id)
    )
    return {row.document_id: tuple(row)[1:] for row in await conn.execute(query)}


async def _review_state(conn: AsyncConnection, documents: Sequence[int], versions: Mapping[int, tuple]):
    """(document -> its open findings of level decide, version -> its settle chat's link, the versions a reviewer asked
    changes of in their round)."""
    f, sc, rv = tables.srs.findings, tables.srs.settle_chats, tables.srs.reviews
    decide: dict[int, list[int]] = defaultdict(list)
    query = select(f.c.document_id, f.c.id).where(
        f.c.document_id.in_(documents), f.c.status == OPEN_FINDING, f.c.level == DECIDE
    )
    for document_id, finding_id in await conn.execute(query.order_by(f.c.id)):
        decide[document_id].append(finding_id)
    ids = [found[0] for found in versions.values()]
    query = select(sc.c.version_id, sc.c.chat_id, sc.c.root_message_id).where(sc.c.version_id.in_(ids))
    chats = {version: chats_link(chat, root) for version, chat, root in await conn.execute(query)}
    rounds = {found[0]: found[3] for found in versions.values()}
    query = select(rv.c.version_id, rv.c.round).where(rv.c.version_id.in_(ids), rv.c.decision == REQUEST_CHANGES)
    asked = {version for version, round_ in await conn.execute(query) if rounds.get(version) == round_}
    return decide, chats, asked


async def _drafts(conn: AsyncConnection, items: Sequence[ItemView], runs: Mapping[int, RunView]) -> dict:
    """The version each unfinished use_case work item drafted (``VersionView``), by work item: the newest version of
    the document its runs drafted a version of last."""
    wanted: dict[int, set[int]] = {}
    for item in items:
        if item.kind == USE_CASE and not item.finished:
            ids = set(item.runs) | ({runs[item.run_id].id} if item.run_id in runs else set())
            if ids:
                wanted[item.id] = ids
    every = sorted({run_id for ids in wanted.values() for run_id in ids})
    if not every:
        return {}
    v = tables.srs.versions
    drafted = list(
        await conn.execute(select(v.c.id, v.c.drafted_by_run, v.c.document_id).where(v.c.drafted_by_run.in_(every)))
    )
    document_of = {}
    for item_id, ids in wanted.items():
        mine = [row for row in drafted if row.drafted_by_run in ids]
        if mine:
            document_of[item_id] = max(mine, key=lambda row: row.id).document_id
    documents = sorted(set(document_of.values()))
    if not documents:
        return {}
    versions = await _newest_versions(conn, documents)
    decide, chats, asked = await _review_state(conn, documents, versions)
    found = {}
    for item_id, document_id in document_of.items():
        version_id, number, state, _, slug = versions[document_id]
        found[item_id] = VersionView(
            version_id,
            document_id,
            slug,
            number,
            state,
            tuple(decide.get(document_id, ())),
            chats.get(version_id),
            state == DRAFT and version_id in asked,
        )
    return found


async def _delivery(conn: AsyncConnection, project_id: int, effective: dict, items: Sequence[ItemView]):
    """(the repos whose merge requests the hub's queue does not merge, the repos of each deliver's plan its ``repos``
    mark merged or not needed); nothing without a deliver waiting."""
    plans = sorted({item.plan_id for item in items if item.kind == DELIVER and item.plan_id and not item.finished})
    if not plans:
        return frozenset(), {}
    facts = await project_repos(conn, project_id)
    manual = frozenset(
        name
        for name, entry in (effective.get("repos") or {}).items()
        if entry.get("mode") != AUTO or (name in facts and facts[name].on_github)
    )
    p = tables.plans
    query = select(p.c.plan_id, p.c.body).where(p.c.project_id == project_id, p.c.plan_id.in_(plans))
    released = {}
    for plan_id, body in await conn.execute(query):
        entries = body.get("repos") if isinstance(body, dict) and isinstance(body.get("repos"), list) else []
        released[plan_id] = frozenset(
            entry["repo"]
            for entry in entries
            if isinstance(entry, dict) and entry.get("repo") and entry.get("status") in RELEASED
        )
    return manual, released


async def _versions(conn: AsyncConnection, events: Sequence[Event]) -> dict[int, int | None]:
    """The run that drafted each version an approval of ``events`` names (None: a person did)."""
    ids = sorted({e.payload["version_id"] for e in events if e.kind == SRS_APPROVED and "version_id" in e.payload})
    if not ids:
        return {}
    v = tables.srs.versions
    return {
        version_id: run
        for version_id, run in await conn.execute(select(v.c.id, v.c.drafted_by_run).where(v.c.id.in_(ids)))
    }


# Use cases


async def use_cases(conn: AsyncConnection, project_id: int, policy: Mapping) -> bool:
    """Whether a solution goes through use cases of SRS documents in project ``project_id`` (``loop.use_cases`` of
    ``policy``, its loop section): always, never, or auto (the default), which does when the plugin srs is on in the
    project and it has an SRS document not archived."""
    mode = policy.get("use_cases") or AUTO_MODE
    if mode != AUTO_MODE:
        return mode == ALWAYS
    if await enabled_in(conn, SRS, project_id) is None:
        return False
    d = tables.srs.documents
    held = select(d.c.id).where(d.c.project_id == project_id, d.c.archived_at.is_(None)).limit(1)
    return await conn.scalar(held) is not None


# The week


async def loop_zone(conn: AsyncConnection, project_id: int, policy: dict) -> str:
    """The zone of the Loop's schedule (its weekly look at the goals, the month's review of them, its brief):
    ``loop.timezone``, else the charter's window's, else UTC; a zone Postgres does not know is passed over."""
    c = tables.charters
    charter = await conn.scalar(
        select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    )
    for zone in (policy.get("timezone"), ((charter or {}).get("window") or {}).get("timezone")):
        if isinstance(zone, str) and zone:
            known = await conn.scalar(select(PG_TIMEZONES.c.name).where(PG_TIMEZONES.c.name == zone).limit(1))
            if known is not None:
                return zone
    return UTC_ZONE


def week_of(local_now: datetime, weekday: str, at: str) -> tuple[str, datetime] | None:
    """(the ISO week, its local moment) of this week's look at the goals once ``local_now`` reached it; None before."""
    hours, minutes = (int(part) for part in at.split(":"))
    monday = local_now.date() - timedelta(days=local_now.weekday())
    moment = datetime.combine(monday + timedelta(days=WEEKDAYS.index(weekday)), time(hours, minutes))
    if local_now < moment:
        return None
    year, week, _ = moment.date().isocalendar()
    return f"{year}-W{week:02d}", moment


async def _week(conn: AsyncConnection, project_id: int, policy: dict) -> tuple[str | None, datetime | None]:
    zone = await loop_zone(conn, project_id, policy)
    local_now = await conn.scalar(select(func.timezone(zone, func.now())))
    brainstorm = policy.get("brainstorm") or {}
    found = week_of(local_now, brainstorm.get("weekday", "monday"), brainstorm.get("time", "09:00"))
    if found is None:
        return None, None
    return found[0], await conn.scalar(select(func.timezone(zone, literal(found[1]))))


# The view


async def load(conn: AsyncConnection, project_id: int, events: Sequence[Event] = ()) -> View:
    """What the tick reads of project ``project_id`` for ``events`` (none: for the clock and reconciling)."""
    now = await conn.scalar(select(func.now()))
    effective = await effective_policy(conn, project_id)
    policy = effective["loop"]
    items = await _items(conn, project_id, events)
    moved = {moved["claim_id"] for e in events if e.kind == BASELINE for moved in e.payload.get("transitions") or []}
    claims = await _claims(conn, moved | {item.claim_id for item in items if item.claim_id is not None})
    keys, made, proposals = await _brainstorms(conn, project_id)
    week, week_at = await _week(conn, project_id, policy)
    plugin = await enabled_in(conn, TASKS, project_id)
    by_id, by_key = await _tasks(conn, project_id, items, plugin)
    runs = await _runs(conn, project_id, items)
    manual, released = await _delivery(conn, project_id, effective, items)
    return View(
        now=now,
        policy=policy,
        goals=await _goals(conn, project_id, now),
        claims=claims,
        items=items,
        keys=keys,
        week=week,
        week_at=week_at,
        brainstormed=made,
        proposals=proposals,
        versions=await _versions(conn, events),
        tasks_on=plugin is not None,
        tasks=by_id,
        frames=by_key,
        approvals=await _approvals(conn, items),
        runs=runs,
        merges=await _merges(conn, project_id, items),
        drafts=await _drafts(conn, items, runs),
        manual=manual,
        released=released,
        use_cases=await use_cases(conn, project_id, policy),
    )


__all__ = ["load", "loop_zone", "reached", "run_chains", "use_cases", "week_of"]
