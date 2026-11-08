"""What the plugin srs tells members in their Inbox and on their channels: a version that waits for its reviewers, and a
review that reached the author of a version.

``srs_review`` goes to the reviewers of a version in review once it may be approved, so that nobody is asked to look at
a version @ba is still reviewing or whose findings to decide are still open:

- when it enters review and the hub handed @ba no review of its round (the agent type is off or past its cap, or the
  project has none; ``auto_review``);
- when @ba's review of its round ends, whatever it ended as (``settle.review_ended``), and the document has no open
  finding of level decide;
- when the last open finding of level decide of the document is settled or waived (``findings_settled``) while the
  version is in review and @ba's review of its round is no longer queued or running.

The reviewers are the members the config's ``reviewers`` names, else every writer and admin of the project; of them,
only those who may review the version now: a grant of writer, the document's label visible through the project's hub
sink, and no version written since the one approved last unless ``self_approval`` is on. When the config names members
of whom none may review it now, every writer who may is told instead, so the version never waits for nobody. Each is
told once per version and round: the details keep the version's id and round, and a later call finds them.

``srs_reviewed`` goes to the author of a version when a reviewer asks for changes on it or on one of its use cases, and
when it is approved; a reviewer who is the author (``self_approval``) is not told of their own review.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import signals, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.srs import findings, store, use_case_reviews
from evo_agents.hub.server.srs.store import DocumentRow, Settings, VersionRow

REVIEW = "srs_review"  # evo_agents.hub.runs.NOTICE_KINDS: a version waits for its reviewers
REVIEWED = "srs_reviewed"  # evo_agents.hub.runs.NOTICE_KINDS: a review reached the version's author
READY, CHANGES, APPROVED = "ready", "changes_requested", "approved"  # the ``event`` of a notice's details
IN_REVIEW = "in_review"
LISTED_CASES = 10  # the use cases a notice's body names; the review page lists them all


def _details(doc: DocumentRow, held: VersionRow, event: str, **more) -> dict:
    return {
        "document": doc.slug,
        "document_id": doc.id,
        "version": held.number,
        "version_id": held.id,
        "round": held.round,
        "event": event,
        **more,
    }


async def _told(conn: AsyncConnection, user_id: int, held: VersionRow) -> bool:
    """Whether member ``user_id`` was told already that the current round of ``held`` waits for them."""
    n = tables.notifications
    query = (
        select(n.c.id)
        .where(
            n.c.user_id == user_id,
            n.c.notice_kind == REVIEW,
            n.c.details["version_id"].astext == str(held.id),
            n.c.details["round"].astext == str(held.round),
        )
        .limit(1)
    )
    return (await conn.execute(query)).first() is not None


async def _named(conn: AsyncConnection, logins: tuple[str, ...]) -> set[int]:
    """The ids of the members ``logins`` names (logins are case-insensitive), those with an account."""
    u = tables.users
    rows = await conn.execute(select(u.c.id).where(func.lower(u.c.login).in_([login.lower() for login in logins])))
    return set(rows.scalars())


async def reviewers(
    conn: AsyncConnection, project_id: int, doc: DocumentRow, held: VersionRow, settings: Settings
) -> list[int]:
    """The members to tell that ``held`` of ``doc`` waits for them (see the module), by id."""
    rules = await signals.project_rules(conn, project_id)
    barred = set() if settings.self_approval else await use_case_reviews.authors_since(conn, doc, held.number)
    g = tables.grants
    grants = await conn.execute(
        select(g.c.user_id, g.c.role, g.c.max_level).where(g.c.project_id == project_id).order_by(g.c.user_id)
    )
    eligible = []
    for user_id, role, max_level in grants:
        if user_id in barred or not has_role(role, "writer"):
            continue
        access = ProjectAccess(project_id, rules, role, max_level)
        through = store.sink_of(access, None)
        if through is not None and access.visible(doc.label, through):
            eligible.append(user_id)
    if not settings.reviewers:
        return eligible
    named = await _named(conn, settings.reviewers)
    return [user_id for user_id in eligible if user_id in named] or eligible


def _body(held: VersionRow, cases: list[use_case_reviews.CaseState], run, deciding_settled: bool) -> str:
    lines = [held.summary.strip()] if held.summary.strip() else []
    if cases:
        named = [f"{case.code or case.title} ({case.change})" for case in cases[:LISTED_CASES]]
        more = f" and {len(cases) - LISTED_CASES} more" if len(cases) > LISTED_CASES else ""
        lines.append(f"Use cases to review: {', '.join(named)}{more}.")
    if run is None:
        lines.append("@ba did not review this round.")
    elif run.state == "done":
        lines.append(f"@ba reviewed it (run #{run.id}).")
    else:
        lines.append(f"@ba's review (run #{run.id}) ended {run.state}: review it without @ba's findings.")
    if deciding_settled:
        lines.append("Every finding to decide is settled.")
    return "\n".join(lines)


async def tell_reviewers(
    conn: AsyncConnection, project_id: int, doc: DocumentRow, held: VersionRow, *, settled: bool = False
) -> list[int]:
    """Tell the reviewers that version ``held`` of ``doc`` waits for them, when it is in review and nothing holds its
    approval now; the ids of the members told this time. ``settled`` says the last finding to decide was just settled."""
    if held.state != IN_REVIEW or doc.archived_at is not None:
        return []
    deciding, running = await findings.holds(conn, doc, held)
    if deciding or running is not None:
        return []
    rules = await signals.project_rules(conn, project_id)
    settings = await store.project_settings(conn, project_id, rules.name)
    snap = await store.snapshot(conn, held)
    wide = use_case_reviews.round_approvals(held)
    cases = await use_case_reviews.states(conn, project_id, doc, held, snap.doc, settings, wide)
    run = await findings.review_run(conn, held)
    body = _body(held, cases, run, settled)
    title = f"v{held.number} of {doc.slug} in {rules.name} waits for your review"
    cases_named = [case.code or case.key for case in cases]
    if len(cases) == 1:  # one use case to read: its review, else the list of what waits
        link = store.case_path(rules.name, doc.slug, held.number, cases[0].key)
    else:
        link = store.queue_path(rules.name)
    told = []
    for user_id in await reviewers(conn, project_id, doc, held, settings):
        if await _told(conn, user_id, held):
            continue
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=REVIEW,
            title=title,
            body=body,
            project_id=project_id,
            details=_details(doc, held, READY, use_cases=cases_named, review_run=run.id if run else None),
            link=link,
        )
        told.append(user_id)
    return told


async def findings_settled(conn: AsyncConnection, project_id: int, doc: DocumentRow) -> list[int]:
    """After a finding of ``doc`` was settled or waived: tell the reviewers of each version of it in review that nothing
    holds any more; the ids of the members told."""
    told = []
    for held in await store.versions(conn, doc.id):
        if held.state == IN_REVIEW:
            told += await tell_reviewers(conn, project_id, doc, held, settled=True)
    return told


async def tell_author(
    conn: AsyncConnection,
    project_id: int,
    doc: DocumentRow,
    held: VersionRow,
    reviewer: tuple[int, str],
    event: str,
    *,
    use_case: str | None = None,
    case_key: str | None = None,
    note: str = "",
) -> bool:
    """Tell the author of ``held`` that ``reviewer`` (id, login) asked for changes on it, or on ``use_case``
    (``CHANGES``), or that it is approved (``APPROVED``); whether a notice went, never to the reviewer themselves."""
    reviewer_id, login = reviewer
    if held.author_id == reviewer_id:
        return False
    rules = await signals.project_rules(conn, project_id)
    where = f"v{held.number} of {doc.slug} in {rules.name}"
    if event == APPROVED:
        title = f"{where} is approved"
    elif use_case:
        title = f"{login} asked for changes on {use_case} of {where}"
    else:
        title = f"{login} returned {where} to draft"
    await notify(
        conn,
        user_id=held.author_id,
        kind="notice",
        notice_kind=REVIEWED,
        title=title,
        body=note.strip() or None,
        project_id=project_id,
        details=_details(doc, held, event, reviewer=login, use_case=use_case),
        link=(
            store.case_path(rules.name, doc.slug, held.number, case_key)
            if case_key
            else store.review_path(rules.name, doc.slug, held.number)
        ),
    )
    return True


__all__ = [
    "APPROVED",
    "CHANGES",
    "READY",
    "REVIEW",
    "REVIEWED",
    "findings_settled",
    "reviewers",
    "tell_author",
    "tell_reviewers",
]
