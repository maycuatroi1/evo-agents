"""The history of a project in the database: the facets of session_facets the cheap model reads and what it wrote
of them, the groups of facet_clusters, the history runs of history_runs, the words of history_lines and of the chat
messages, and the transcripts with their parts."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import and_, exists, false, func, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import excerpts, history, history_index, tables
from evo_agents.hub.server.history.models import MAX_RUNS_LISTED, SCAN_HITS, SCAN_ROWS

HEADLINE = "MaxWords=35, MinWords=12, MaxFragments=2, FragmentDelimiter= ... , StartSel=«, StopSel=»"


async def now(conn: AsyncConnection) -> datetime:
    """The database's time of the transaction."""
    return (await conn.execute(select(func.now()))).scalar_one()


async def member_and_project(conn: AsyncConnection, user_id: int, project_id: int) -> tuple[str, str]:
    """(the login of member ``user_id``, the name of project ``project_id``)."""
    u, p = tables.users, tables.projects
    login = (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()
    name = (await conn.execute(select(p.c.name).where(p.c.id == project_id))).scalar_one()
    return login, name


async def newest_charter_body(conn: AsyncConnection, project_id: int) -> dict | None:
    ch = tables.charters
    query = select(ch.c.body).where(ch.c.project_id == project_id).order_by(ch.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none()


# The facets the model reads


def _current(f):
    """A facet of the model of LLM_VERSION written from the content as it is now; never NULL."""
    fresh = f.c.llm_fingerprint.is_not_distinct_from(f.c.fingerprint)
    return and_(f.c.llm.is_not(None), f.c.llm_version == history.LLM_VERSION, fresh)


def _eligible(f, r):
    """A facet the model reads: of a session whose own transcript the hub holds, or of a run that is not local."""
    t = tables.session_transcripts
    live = exists().where(
        t.c.project_id == f.c.project_id,
        t.c.session_id == f.c.session_id,
        t.c.file == excerpts.MAIN,
        t.c.deleted_at.is_(None),
        t.c.parts > 0,
    )
    session = and_(f.c.kind == "session", f.c.source == "transcript", live)
    run = and_(f.c.kind == "run", r.c.kind.not_in(("local", "history", "probe", "evolve")))
    return and_(f.c.body.is_not(None), or_(session, run))


def _facets(project_id: int, *where):
    f, r = tables.session_facets, tables.runs
    columns = (f.c.id, f.c.project_id, f.c.kind, f.c.session_id, f.c.run_id, f.c.label, f.c.user_id, f.c.at)
    return (
        select(*columns, f.c.fingerprint, f.c.runtime, f.c.llm_attempts, r.c.kind.label("run_kind"), r.c.plan_id)
        .add_columns(r.c.log_sha256)
        .add_columns(_current(f).label("current"))
        .select_from(f.outerjoin(r, r.c.id == f.c.run_id))
        .where(f.c.project_id == project_id, _eligible(f, r), *where)
    )


async def eligible_facets(conn: AsyncConnection, project_id: int) -> list:
    """The project's facets the model reads, each with whether the model's facet of it is current."""
    return list((await conn.execute(_facets(project_id))).all())


async def waiting_facets(conn: AsyncConnection, project_id: int, now: datetime, scanned: int) -> list:
    """At most ``scanned`` of the project's facets that wait for one of the model now (no current one, and no retry
    to wait for), the latest first."""
    f = tables.session_facets
    due = or_(f.c.llm_retry_at.is_(None), f.c.llm_retry_at <= now)
    query = _facets(project_id, ~_current(f), due).order_by(f.c.at.desc().nulls_last(), f.c.id.desc())
    return list((await conn.execute(query.limit(scanned))).all())


async def session_parts(conn: AsyncConnection, project_id: int, session_id: str) -> list:
    """The parts of the main transcript file of session ``session_id``, in order."""
    t, p = tables.session_transcripts, tables.transcript_parts
    query = (
        select(p.c.first_line, p.c.sha256.label("blob"), p.c.size)
        .join_from(t, p, p.c.transcript_id == t.c.id)
        .where(t.c.project_id == project_id, t.c.session_id == session_id, t.c.file == excerpts.MAIN)
        .where(t.c.deleted_at.is_(None))
        .order_by(p.c.seq)
    )
    return list((await conn.execute(query)).all())


async def attempts_of(conn: AsyncConnection, facet_id: int) -> int:
    f = tables.session_facets
    return (await conn.execute(select(f.c.llm_attempts).where(f.c.id == facet_id))).scalar_one()


async def set_facet(conn: AsyncConnection, facet_id: int, values: dict) -> None:
    f = tables.session_facets
    await conn.execute(update(f).values(**values).where(f.c.id == facet_id))


async def facet_of_item(conn: AsyncConnection, project_id: int, kind: str, ident):
    """The id and fingerprint of the facet of session or run ``ident``, locked; None without one."""
    f = tables.session_facets
    key = f.c.run_id == ident if kind == "run" else f.c.session_id == ident
    query = select(f.c.id, f.c.fingerprint).where(f.c.project_id == project_id, f.c.kind == kind, key)
    return (await conn.execute(query.with_for_update())).one_or_none()


# The groups of facets


async def llm_facets(conn: AsyncConnection, project_id: int) -> list:
    """The project's facets whose facet of the model is current, the latest first."""
    f, r = tables.session_facets, tables.runs
    columns = (f.c.kind, f.c.session_id, f.c.run_id, f.c.label, f.c.user_id, f.c.at, f.c.llm)
    query = (
        select(*columns, r.c.kind.label("run_kind"), r.c.plan_id)
        .select_from(f.outerjoin(r, r.c.id == f.c.run_id))
        .where(f.c.project_id == project_id, _current(f))
        .order_by(f.c.at.desc().nulls_last(), f.c.id.desc())
    )
    return list((await conn.execute(query)).all())


async def store_counts(conn: AsyncConnection, project_id: int, counted: dict[str, dict], now: datetime) -> None:
    """Write the counts of each group of ``counted`` (by key) into facet_clusters; a group not in it counts nothing."""
    c = tables.facet_clusters
    for key, values in sorted(counted.items()):
        values = {**values, "counted_at": now}
        statement = pg_insert(c).values(project_id=project_id, key=key, **values)
        changes = {name: statement.excluded[name] for name in values}
        await conn.execute(
            statement.on_conflict_do_update(
                index_elements=[c.c.project_id, c.c.key], set_={**changes, "updated_at": func.now()}
            )
        )
    gone = {"sessions": 0, "runs": 0, "member_ids": [], "examples": [], "counted_at": now, "updated_at": func.now()}
    await conn.execute(
        update(c).values(**gone).where(c.c.project_id == project_id, c.c.key.not_in(list(counted) or [""]))
    )


async def cluster_rows(conn: AsyncConnection, project_id: int) -> list:
    c = tables.facet_clusters
    return list((await conn.execute(select(c).where(c.c.project_id == project_id).order_by(c.c.key))).all())


async def has_unnamed(conn: AsyncConnection, project_id: int) -> bool:
    """Whether a group of the project's facets that holds sessions has no name yet."""
    c = tables.facet_clusters
    unnamed = exists().where(c.c.project_id == project_id, c.c.name.is_(None), c.c.sessions > 0)
    return bool((await conn.execute(select(unnamed))).scalar())


async def name_group(conn: AsyncConnection, project_id: int, key: str, values: dict) -> bool:
    """Give group ``key`` of the project its name while it has none; whether it got it."""
    c = tables.facet_clusters
    named = await conn.execute(
        update(c)
        .values(**values, named_at=func.now(), updated_at=func.now())
        .where(c.c.project_id == project_id, c.c.key == key, c.c.name.is_(None))
        .returning(c.c.id)
    )
    return named.first() is not None


# The history runs


async def reviewed(conn: AsyncConnection, project_id: int, night: date) -> bool:
    """Whether the night's figures of the project were counted."""
    cf = tables.curator_figures
    query = select(cf.c.run_id).where(cf.c.project_id == project_id, cf.c.night == night)
    return (await conn.execute(query)).scalar_one_or_none() is not None


async def had_history_run(conn: AsyncConnection, schedule_id: int, night: date) -> bool:
    r = tables.runs
    one = exists().where(r.c.schedule_id == schedule_id, r.c.schedule_night == night, r.c.kind == "history")
    return bool((await conn.execute(select(one))).scalar())


async def add_history_run(conn: AsyncConnection, run_id: int, project_id: int) -> None:
    await conn.execute(pg_insert(tables.history_runs).values(run_id=run_id, project_id=project_id))


async def keep_key(conn: AsyncConnection, run_id: int, digest: str) -> None:
    """Keep ``digest``, the SHA-256 of the run's new key, as history run ``run_id``'s."""
    r, h = tables.runs, tables.history_runs
    project_id = (await conn.execute(select(r.c.project_id).where(r.c.id == run_id))).scalar_one()
    statement = pg_insert(h).values(run_id=run_id, project_id=project_id, key_digest=digest)
    await conn.execute(
        statement.on_conflict_do_update(
            index_elements=[h.c.run_id], set_={"key_digest": statement.excluded.key_digest, "updated_at": func.now()}
        )
    )


async def run_locked(conn: AsyncConnection, run_id: int):
    """The state, worker, kind, project and dispatcher of run ``run_id``, locked; None without one."""
    r = tables.runs
    columns = (r.c.state, r.c.worker_id, r.c.kind, r.c.project_id, r.c.dispatched_by)
    return (await conn.execute(select(*columns).where(r.c.id == run_id).with_for_update())).one_or_none()


async def history_run_locked(conn: AsyncConnection, run_id: int):
    h = tables.history_runs
    return (await conn.execute(select(h).where(h.c.run_id == run_id).with_for_update())).one_or_none()


async def add_counts(conn: AsyncConnection, run_id: int, counts: dict[str, int]) -> None:
    """Add what one report kept, left and named to history run ``run_id``'s sums."""
    h = tables.history_runs
    sums = {"analyzed": h.c.analyzed + counts["stored"], "failed": h.c.failed + counts["failed"]}
    sums |= {"stale": h.c.stale + counts["stale"], "named": h.c.named + counts["named"]}
    await conn.execute(update(h).values(**sums, updated_at=func.now()).where(h.c.run_id == run_id))


async def history_runs(conn: AsyncConnection, project_id: int) -> list:
    """The project's latest history runs, newest first, with what each wrote."""
    r, h = tables.runs, tables.history_runs
    columns = (r.c.id, r.c.schedule_night.label("night"), r.c.state, r.c.model, r.c.usage, r.c.queued_at)
    query = (
        select(*columns, r.c.finished_at, h.c.analyzed, h.c.failed, h.c.stale, h.c.named)
        .join_from(h, r, r.c.id == h.c.run_id)
        .where(h.c.project_id == project_id)
        .order_by(r.c.id.desc())
        .limit(MAX_RUNS_LISTED)
    )
    return list((await conn.execute(query)).all())


# The sessions and runs, and a search


def runs_clause(user_id: int, mine: bool, unplanned: bool, plans: frozenset, replays: bool):
    """The runs a caller reads, on the runs table: their own local runs when ``mine``, the runs on no plan when
    ``unplanned``, the runs of ``plans``, and the replay, probe and evolve runs only with ``replays``."""
    r = tables.runs
    local_run = and_(r.c.kind == "local", r.c.dispatched_by == user_id, true() if mine else false())
    no_plan = and_(r.c.kind != "local", r.c.plan_id.is_(None), true() if unplanned else false())
    planned = and_(r.c.kind != "local", r.c.plan_id.in_(sorted(plans) or [""]))
    kept = [] if replays else [r.c.kind.not_in(("replay", "probe", "evolve"))]
    return and_(or_(local_run, no_plan, planned), *kept)


async def facet_rows(conn: AsyncConnection, project_id: int, kind, since, until, login, runtime) -> list:
    """At most SCAN_ROWS of the project's facets of ``kind`` between ``since`` and ``until``, of member ``login`` and
    ``runtime`` (each when given), the latest first, with their run, their member and whether the hub holds a
    transcript of the session."""
    f, r, u, t = tables.session_facets, tables.runs, tables.users, tables.session_transcripts
    held = (
        select(t.c.id)
        .where(
            t.c.project_id == f.c.project_id,
            t.c.session_id == f.c.session_id,
            t.c.deleted_at.is_(None),
            t.c.parts > 0,
        )
        .exists()
    )
    query = (
        select(
            f.c.kind,
            f.c.session_id,
            f.c.run_id,
            f.c.source,
            f.c.label,
            f.c.runtime,
            f.c.at,
            f.c.summary,
            f.c.llm,
            r.c.kind.label("run_kind"),
            r.c.state,
            r.c.plan_id,
            r.c.dispatched_by,
            u.c.login,
            held.label("held"),
        )
        .select_from(f.outerjoin(r, r.c.id == f.c.run_id).outerjoin(u, u.c.id == f.c.user_id))
        .where(f.c.project_id == project_id, f.c.body.is_not(None))
    )
    if kind is not None:
        query = query.where(f.c.kind == kind)
    if since is not None:
        query = query.where(f.c.at >= since)
    if until is not None:
        query = query.where(f.c.at < until)
    if login is not None:
        query = query.where(func.lower(u.c.login) == login.lower())
    if runtime is not None:
        query = query.where(f.c.runtime == runtime)
    return list((await conn.execute(query.order_by(f.c.at.desc().nulls_last(), f.c.id.desc()).limit(SCAN_ROWS))).all())


def _headline(text_column, query):
    return func.ts_headline(history_index.CONFIG, text_column, query, HEADLINE)


def _between(column, since, until):
    found = []
    if since is not None:
        found.append(column >= since)
    if until is not None:
        found.append(column < until)
    return found


async def transcript_hits(conn: AsyncConnection, project_id: int, query, since, until, login, runtime) -> list:
    """The best SCAN_HITS lines of the project's transcripts that match ``query``, each with its words around the
    match (``snippet``), its rank, its transcript's label and runtime and its member."""
    lines, t, u = tables.history_lines, tables.session_transcripts, tables.users
    rank = func.ts_rank(lines.c.search, query)
    where = [
        lines.c.project_id == project_id,
        lines.c.source == "transcript",
        lines.c.search.bool_op("@@")(query),
        t.c.deleted_at.is_(None),
        *_between(lines.c.at, since, until),
    ]
    if login is not None:
        where.append(func.lower(u.c.login) == login.lower())
    if runtime is not None:
        where.append(t.c.runtime == runtime)
    statement = (
        select(
            lines.c.session_id,
            lines.c.file,
            lines.c.line,
            lines.c.at,
            lines.c.role,
            _headline(lines.c.text, query).label("snippet"),
            rank.label("rank"),
            t.c.label,
            t.c.runtime,
            u.c.login,
        )
        .join_from(lines, t, t.c.id == lines.c.transcript_id)
        .join(u, u.c.id == t.c.user_id)
        .where(*where)
        .order_by(rank.desc(), lines.c.at.desc().nulls_last(), lines.c.id.desc())
        .limit(SCAN_HITS)
    )
    return list((await conn.execute(statement)).all())


async def run_hits(conn: AsyncConnection, project_id: int, readable, query, since, until, login, runtime) -> list:
    """The best SCAN_HITS lines of the logs of the project's runs that match ``query`` among the runs ``readable``
    (a clause of ``runs_clause``) keeps, each with its words around the match, its rank, its run and its member."""
    lines, r, u = tables.history_lines, tables.runs, tables.users
    rank = func.ts_rank(lines.c.search, query)
    where = [
        lines.c.project_id == project_id,
        lines.c.source == "run",
        lines.c.search.bool_op("@@")(query),
        readable,
        *_between(lines.c.at, since, until),
    ]
    if login is not None:
        where.append(func.lower(u.c.login) == login.lower())
    if runtime is not None:
        where.append(r.c.runtime == runtime)
    statement = (
        select(
            lines.c.run_id,
            lines.c.line,
            lines.c.at,
            lines.c.role,
            _headline(lines.c.text, query).label("snippet"),
            rank.label("rank"),
            r.c.kind,
            r.c.plan_id,
            r.c.dispatched_by,
            r.c.runtime,
            u.c.login,
        )
        .join_from(lines, r, r.c.id == lines.c.run_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .where(*where)
        .order_by(rank.desc(), lines.c.at.desc().nulls_last(), lines.c.id.desc())
        .limit(SCAN_HITS)
    )
    return list((await conn.execute(statement)).all())


async def chat_hits(conn: AsyncConnection, user_id: int, project: str, query, since, until, login) -> list:
    """The best SCAN_HITS messages that match ``query`` of the chats member ``user_id`` is in, whose label names
    project ``project``, each with its words around the match, its rank, its chat's title and its author."""
    m, cm, c, u, ap = (tables.chat_messages, tables.chat_members, tables.chats, tables.users, tables.agent_profiles)
    vector = history_index.vector(m.c.body)
    rank = func.ts_rank(vector, query)
    member = and_(cm.c.chat_id == m.c.chat_id, cm.c.user_id == user_id, cm.c.removed_at.is_(None))
    author = func.coalesce(u.c.login, ap.c.handle, m.c.guest_name)
    where = [vector.bool_op("@@")(query), m.c.label.contains([{"projects": [project]}])]
    where += _between(m.c.created_at, since, until)
    if login is not None:
        where.append(func.lower(u.c.login) == login.lower())
    statement = (
        select(
            m.c.chat_id,
            m.c.seq,
            m.c.created_at,
            m.c.label,
            m.c.author_profile_id,
            m.c.guest_name,
            c.c.title,
            author.label("author"),
            _headline(m.c.body, query).label("snippet"),
            rank.label("rank"),
        )
        .select_from(
            m.join(cm, member)
            .join(c, c.c.id == m.c.chat_id)
            .outerjoin(u, u.c.id == m.c.author_user_id)
            .outerjoin(ap, ap.c.id == m.c.author_profile_id)
        )
        .where(*where)
        .order_by(rank.desc(), m.c.created_at.desc(), m.c.id.desc())
        .limit(SCAN_HITS)
    )
    return list((await conn.execute(statement)).all())


# The lines of a transcript


async def transcript_of(conn: AsyncConnection, project_id: int, session_id: str, file: str):
    """Transcript file ``file`` of session ``session_id`` of the project with its member's login; None without one."""
    t, u = tables.session_transcripts, tables.users
    query = (
        select(t.c.id, t.c.label, t.c.lines, t.c.runtime, t.c.deleted_at, u.c.login)
        .join_from(t, u, u.c.id == t.c.user_id)
        .where(t.c.project_id == project_id, t.c.session_id == session_id, t.c.file == file)
    )
    return (await conn.execute(query)).one_or_none()


async def parts_between(conn: AsyncConnection, transcript_id: int, start: int, end: int) -> list:
    """The parts of transcript ``transcript_id`` that hold a line from ``start`` to ``end``, in order."""
    p = tables.transcript_parts
    query = (
        select(p.c.first_line, p.c.sha256.label("blob"), p.c.size)
        .where(p.c.transcript_id == transcript_id, p.c.first_line <= end, p.c.first_line + p.c.lines > start)
        .order_by(p.c.seq)
    )
    return list((await conn.execute(query)).all())


async def transcript_head(conn: AsyncConnection, project_id: int, session_id: str, file: str):
    """The label, lines and deletion of transcript file ``file`` of session ``session_id``; None without one."""
    t = tables.session_transcripts
    query = select(t.c.label, t.c.lines, t.c.deleted_at).where(
        t.c.project_id == project_id, t.c.session_id == session_id, t.c.file == file
    )
    return (await conn.execute(query)).one_or_none()
