"""What the chain of a project's Loop waits on a person for, on its decision board (plan loop-engine, criterion a8; the
report's sections Trang Quyết định and Người vào cuộc ngoài ba cổng).

- ``exception``: each decision of category loop still open, asked of the person the work item's last run stands for
  (the goal's owner gets no notice of it: the board shows it to every member); each decision still open of a run the
  Loop handed out that a work item waits on (``run_decision``: a plan run at a checkpoint, an author run's), and an
  author run of the Loop asking its owner in its chat with no decision open; each merge request of a work item deliver
  held for a person (blocked or paused; one blocked at a gate says how it goes on: an admin passes a hold at gate 3,
  ``merge_gate.pass_refusal``, and a new push moves any other) and the repos of its plan the queue does not merge, to
  merge by hand and mark merged in the plan; each audit sample of the merge queue not answered.
- ``srs``: each version of an SRS document a work item use_case drafted that waits for its review, for the writers
  who may approve it (who wrote none of the versions since the one approved last, unless the plugin's self_approval is
  on; the approval policy of the plugin may ask roles too); each settle chat of an open finding of level decide that
  holds such a version, for the chat's members. Neither is a fourth gate: the rules of the plugin srs stay as they are.

A decision and a run show under the rule of GET .../decisions (its run's plan readable, or the runs of no plan), a
plan to merge by hand under the same rule, an SRS document under its label. Nothing here writes.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals, merge_gate, runs, tables
from evo_agents.hub.server.loop.board_items import (
    ADMIN,
    CHAT_MEMBER,
    RUN_OWNER,
    SRS_REVIEWER,
    TITLE_CHARS,
    WRITER,
    BoardItem,
    People,
    Reading,
    document_path,
    item,
    line,
    project_path,
    self_approval,
)
from evo_agents.hub.server.loop.model import KINDS, WAITING_HUMAN
from evo_agents.hub.server.loop.view import run_chains
from evo_agents.hub.server.notifications import decision_link

OPEN = "open"  # runs.DECISION_STATES
ASKING = ("run_decision", "author_question")  # the reasons of a work item whose run's agent asks its owner
HELD = ("blocked", "paused")  # merge_gate.STATES of a merge request that waits for a person
IN_REVIEW = "in_review"  # tables_srs.VERSION_STATES
SRS = "srs"  # the core plugin whose config says self_approval
MIN_WHY_CHARS = 40  # of why a merge request is held at a gate, in its title


async def _waiting(conn: AsyncConnection, project_id: int) -> list:
    """The project's work items waiting for a person, with the code of their goal."""
    i, g = tables.loop_items, tables.goals
    query = (
        select(i.c.id, i.c.kind, i.c.reason, i.c.detail, i.c.plan_id, i.c.run_id, i.c.use_case, i.c.updated_at)
        .add_columns(g.c.number.label("goal"))
        .join_from(i, g, g.c.id == i.c.goal_id, isouter=True)
        .where(i.c.project_id == project_id, i.c.state == WAITING_HUMAN, i.c.kind.in_(KINDS))
        .order_by(i.c.id)
    )
    return list(await conn.execute(query))


def _refs(work, **more) -> dict:
    found = {key: value for key, value in more.items() if value is not None}
    return {**found, "goal": goals.code(work.goal)} if work is not None and work.goal is not None else found


# Exceptions: decisions and questions of runs


def _decisions():
    """The open decisions of runs, each with the person it is asked of (``asked``: decisions.asked_of, or its run's
    owner for one asked before schema 0115)."""
    d, r = tables.decisions, tables.runs
    asked = func.coalesce(d.c.asked_of, r.c.dispatched_by).label("asked")
    return (
        select(d.c.id, d.c.run_id, d.c.category, d.c.question, d.c.asked_at, asked)
        .add_columns(r.c.plan_id.label("run_plan"), d.c.plan_id)
        .join_from(d, r, r.c.id == d.c.run_id)
        .where(d.c.state == OPEN, r.c.kind != "local")  # a local run's decisions are its owner's alone
        .order_by(d.c.id)
    )


async def _loop_decisions(conn: AsyncConnection, reading: Reading, crowd: People, waiting: Sequence) -> list:
    d, i = tables.decisions, tables.loop_items
    asking = select(i.c.id).where(i.c.project_id == reading.project_id)
    asking = asking.where(i.c.detail["decision"]["id"].astext == cast(d.c.id, Text)).limit(1).scalar_subquery()
    query = _decisions().add_columns(asking.label("item_id"))
    query = query.where(d.c.project_id == reading.project_id, d.c.category == runs.LOOP_DECISION_CATEGORY)
    works = {work.id: work for work in waiting}
    found = []
    for row in await conn.execute(query):
        if not reading.run(row.plan_id or row.run_plan):
            continue
        title = f"Decision #{row.id} on run #{row.run_id}: {line(row.question)}"
        refs = _refs(works.get(row.item_id), decision=row.id, run=row.run_id)
        deciders, link = crowd.logins([row.asked]), decision_link(row.id)
        found.append(
            item("loop_decision", title, row.asked_at, RUN_OWNER, deciders, link, work_item=row.item_id, refs=refs)
        )
    return found


async def _asking(conn: AsyncConnection, reading: Reading, crowd: People, waiting: Sequence) -> list[BoardItem]:
    """The decisions open on the runs work items wait on, and an author run asking in its chat with none open."""
    asked = {work.id: work.detail.get("asked_in") or work.run_id for work in waiting if work.reason in ASKING}
    asked = {item_id: run_id for item_id, run_id in asked.items() if isinstance(run_id, int)}
    if not asked:
        return []
    chains = await run_chains(conn, set(asked.values()))
    newest = {item_id: chains[run_id][-1] for item_id, run_id in asked.items()}
    d, r = tables.decisions, tables.runs
    query = _decisions().where(
        d.c.run_id.in_(sorted(set(newest.values()))), d.c.category != runs.LOOP_DECISION_CATEGORY
    )
    open_on: dict[int, list] = {}
    for row in await conn.execute(query):
        open_on.setdefault(row.run_id, []).append(row)
    query = select(r.c.id, r.c.plan_id, r.c.dispatched_by).where(r.c.id.in_(sorted(set(newest.values()))))
    run_rows = {row.id: row for row in await conn.execute(query)}
    found = []
    for work in waiting:
        run = run_rows.get(newest.get(work.id))
        if run is None or not reading.run(run.plan_id):
            continue
        for row in open_on.get(run.id, []):
            title = f"Decision #{row.id} of run #{run.id}, work item #{work.id} ({work.kind}): {line(row.question)}"
            link, refs = decision_link(row.id), _refs(work, decision=row.id, run=run.id)
            deciders = crowd.logins([row.asked])
            found.append(
                item("run_decision", title, row.asked_at, RUN_OWNER, deciders, link, work_item=work.id, refs=refs)
            )
        if work.reason == "author_question" and not open_on.get(run.id):
            title = f"Author run #{run.id} of work item #{work.id} asks its owner in its chat"
            link = project_path(reading.project, "runs", str(run.id)) + "?tab=chat"  # author_chat.chat_link
            refs, deciders = _refs(work, run=run.id), crowd.logins([run.dispatched_by])
            found.append(
                item("author_question", title, work.updated_at, RUN_OWNER, deciders, link, work_item=work.id, refs=refs)
            )
    return found


# Exceptions: merges


async def _merges(conn: AsyncConnection, reading: Reading, crowd: People, waiting: Sequence) -> list[BoardItem]:
    """The merge requests of a deliver held for a person, and its repos to merge by hand."""
    q = tables.merge_queue
    held = {
        merge_id: work
        for work in waiting
        if work.reason == "merge_blocked"
        for merge_id in work.detail.get("merges") or []
    }
    found = []
    if held:
        query = select(q.c.id, q.c.repo, q.c.iid, q.c.state, q.c.reason, q.c.plan_id, q.c.updated_at, q.c.source)
        query = query.where(q.c.project_id == reading.project_id, q.c.id.in_(sorted(held)), q.c.state.in_(HELD))
        admins, queue = crowd.holding(ADMIN), project_path(reading.project, "merge-queue")
        for row in await conn.execute(query.order_by(q.c.id)):
            work = held[row.id]
            title = _held_title(row, row.plan_id or work.plan_id)
            refs = _refs(work, merge_request=row.id, repo=row.repo, plan=row.plan_id or work.plan_id)
            found.append(item("merge_held", title, row.updated_at, ADMIN, admins, queue, work_item=work.id, refs=refs))
    writers = crowd.holding(WRITER)
    for work in waiting:
        if work.reason != "merge_manual" or not work.plan_id or work.plan_id not in reading.plans:
            continue
        repos = [repo for repo in work.detail.get("by_hand") or work.detail.get("repos") or [] if isinstance(repo, str)]
        title = (
            f"Plan {work.plan_id}: merge {', '.join(repos) or 'its repos'} by hand, then mark them merged in the plan"
        )
        link, refs = project_path(reading.project, "plans", work.plan_id), _refs(work, plan=work.plan_id)
        found.append(item("merge_manual", title, work.updated_at, WRITER, writers, link, work_item=work.id, refs=refs))
    return found


def _held_title(row, plan: str | None) -> str:
    """The title of a merge request held for a person. One blocked at a gate names the gate, why in the room left, and
    how it goes on: an admin passes a hold for a person (``merge_gate.pass_refusal``), a new push moves any other."""
    what = f"Merge request !{row.iid or '?'} of {row.repo} {row.state}"
    gate = merge_gate.held_at(row.reason) if row.state == "blocked" else None
    if gate is None:
        return f"{what}, plan {plan}" + (f": {line(row.reason)}" if row.reason else "")
    head = f"{what} at gate {gate} ({merge_gate.GATES[gate]}): "
    how = "; a new push gates it again"
    if merge_gate.pass_refusal(row.state, row.reason, row.source) is None:
        how = f"; an admin passes it: `evo-agents hub merge pass {row.repo} {row.iid} --reason ...`"
    why = row.reason.split(": ", 1)[-1]
    return head + line(why, max(TITLE_CHARS - len(head) - len(how), MIN_WHY_CHARS)) + how


async def _samples(conn: AsyncConnection, reading: Reading, crowd: People) -> list[BoardItem]:
    s, q = tables.merge_samples, tables.merge_queue
    query = (
        select(s.c.id, s.c.created_at, q.c.repo, q.c.iid)
        .join_from(s, q, q.c.id == s.c.queue_id)
        .where(s.c.project_id == reading.project_id, s.c.result.is_(None))
        .order_by(s.c.id)
    )
    admins, queue = crowd.holding(ADMIN), project_path(reading.project, "merge-queue")
    return [
        item(
            "merge_sample",
            f"Audit sample #{row.id}: was merge request !{row.iid or '?'} of {row.repo} right?",
            row.created_at,
            ADMIN,
            admins,
            queue,
            refs={"sample": row.id, "repo": row.repo},
        )
        for row in await conn.execute(query)
    ]


# SRS in the chain


async def _documents(conn: AsyncConnection, project_id: int, slugs: set[str]) -> tuple[dict, dict]:
    """(slug -> its document's row, (document id, number) -> its version's row)."""
    d, v = tables.srs.documents, tables.srs.versions
    query = select(d.c.id, d.c.slug, d.c.label, d.c.approved_version).where(
        d.c.project_id == project_id, d.c.slug.in_(sorted(slugs))
    )
    documents = {row.slug: row for row in await conn.execute(query)}
    query = select(v.c.id, v.c.document_id, v.c.number, v.c.state, v.c.author_id, v.c.submitted_at)
    query = query.where(v.c.document_id.in_([row.id for row in documents.values()]))
    return documents, {(row.document_id, row.number): row for row in await conn.execute(query)}


async def _chat_members(conn: AsyncConnection, version_ids: list[int]) -> dict[int, tuple[int, list[int]]]:
    """Version -> (its settle chat, the ids of the people in it)."""
    sc, m = tables.srs.settle_chats, tables.chat_members
    query = (
        select(sc.c.version_id, sc.c.chat_id, m.c.user_id)
        .join_from(sc, m, (m.c.chat_id == sc.c.chat_id) & m.c.user_id.is_not(None) & m.c.removed_at.is_(None))
        .where(sc.c.version_id.in_(version_ids))
    )
    found: dict[int, tuple[int, list[int]]] = {}
    for version_id, chat_id, user_id in await conn.execute(query):
        found.setdefault(version_id, (chat_id, []))[1].append(user_id)
    return found


def _reviewers(crowd: People, versions: dict, document, number: int, anyone: bool) -> list[str]:
    """The writers who may approve version ``number`` of ``document``: none of its authors since the one approved last,
    unless ``anyone`` (self_approval)."""
    since = document.approved_version or 0
    authors = (
        []
        if anyone
        else [row.author_id for (doc, at), row in versions.items() if doc == document.id and since < at <= number]
    )
    return crowd.holding(WRITER, but=authors)


async def srs_items(conn: AsyncConnection, reading: Reading, crowd: People, waiting: Sequence) -> list[BoardItem]:
    """The SRS group: a version under review, or its settle chat, each a use_case of the Loop waits on."""
    wanted = [work for work in waiting if work.reason in ("srs_review", "srs_finding")]
    wanted = [work for work in wanted if isinstance(work.detail.get("srs"), dict)]
    if not wanted:
        return []
    documents, versions = await _documents(
        conn, reading.project_id, {work.detail["srs"].get("document") for work in wanted}
    )
    anyone = await self_approval(conn, SRS, reading.project_id)  # the author of a version may approve it
    chats = await _chat_members(conn, [row.id for row in versions.values()])
    found = []
    for work in wanted:
        srs = work.detail["srs"]
        document = documents.get(srs.get("document"))
        version = versions.get((document.id, srs.get("version"))) if document is not None else None
        if version is None or version.state != IN_REVIEW or not reading.label(document.label):
            continue
        refs = _refs(work, document=document.slug, version=version.number, use_case=work.use_case)
        named = f"SRS {document.slug} v{version.number}" + (f" ({work.use_case})" if work.use_case else "")
        if work.reason == "srs_review":
            deciders = _reviewers(crowd, versions, document, version.number, anyone)
            link = document_path(reading.project, document.slug)
            title = f"{named} waits for its review"
            found.append(
                item(
                    "srs_review",
                    title,
                    version.submitted_at,
                    SRS_REVIEWER,
                    deciders,
                    link,
                    work_item=work.id,
                    refs=refs,
                )
            )
            continue
        chat_id, people = chats.get(version.id, (None, []))
        findings = [finding for finding in srs.get("findings") or [] if isinstance(finding, int)]
        title = f"{named}: {len(findings)} finding(s) of level decide to settle in its chat"
        link = srs.get("chat") if isinstance(srs.get("chat"), str) else None
        refs |= {"chat": chat_id} if chat_id is not None else {}
        found.append(
            item(
                "srs_finding",
                title,
                work.updated_at,
                CHAT_MEMBER,
                crowd.logins(people),
                link,
                work_item=work.id,
                refs=refs,
            )
        )
    return found


async def chain_items(conn: AsyncConnection, reading: Reading, crowd: People) -> list[BoardItem]:
    """The exceptions and the SRS of the project's Loop."""
    waiting = await _waiting(conn, reading.project_id)
    return [
        *await _loop_decisions(conn, reading, crowd, waiting),
        *await _asking(conn, reading, crowd, waiting),
        *await _merges(conn, reading, crowd, waiting),
        *await _samples(conn, reading, crowd),
        *await srs_items(conn, reading, crowd, waiting),
    ]


__all__ = ["chain_items", "srs_items"]
