"""What a claim links (``Content.use_cases``, ``Content.signal_keys``, ``Content.problems``), checked against the
project when a write adds a link (``resolved``), or kept to what the project holds when a canvas is imported
(``kept``).

- A use case is named by any code or alias an item of the plugin srs ever held (compared as ``srs_codes.code_key``
  holds them) while the project enables srs. The item must be a use case, not dropped from its document, in a
  document the member sees through the sink. The link keeps the code the SRS names the item by, so an alias the member
  typed reads as that code, which is the id of the use case's node in the knowledge graph (``kg``). The plugin reads
  the SRS through the hub's tables (``evo_agents.hub.tables.srs``), never through the plugin srs.
- A signal key is a key of the project's registry (``signal_keys``) that is not archived.
- A problem is one of the project's problems (``evo_agents.hub.problems``), by its code P-<number>, that is not
  dropped: open or solved.
- An opportunity is one of the project's opportunities (``evo_agents.hub.opportunities``), by its code O-<number>,
  that is not dropped: open, captured or missed (plan opportunity-intake).

Only what a write adds is checked: a link the claim holds already stays though its use case was dropped, its key
archived or its problem dropped since, and an undo puts back what the rev before held with no check, as that link was
checked once.
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import opportunities as opportunity_model
from evo_agents.hub import problems as problem_model
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.canvas import store
from evo_agents.hub.server.canvas.content import (
    CanvasError,
    Content,
    checked_opportunities,
    checked_problems,
    checked_signal_keys,
    checked_use_cases,
)
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.tables_srs import code_key

SRS = "srs"  # the plugin whose use cases a claim links
USE_CASE = "use_case"  # srs_items.kind of a use case


async def srs_enabled(conn: AsyncConnection, project_id: int) -> bool:
    """Whether the project enables the plugin srs, whose use cases a claim links."""
    return await enabled_in(conn, SRS, project_id) is not None


async def _use_cases(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, codes: list[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """For each of ``codes``: the code the SRS names its use case by, or why it names none."""
    if not codes:
        return {}, {}
    if not await srs_enabled(conn, access.project_id):
        why = f"project {access.name} does not enable plugin srs: a claim links no use case there"
        return {}, dict.fromkeys(codes, why)
    c, i, d = tables.srs.codes, tables.srs.items, tables.srs.documents
    query = (
        select(c.c.code_key, i.c.kind, i.c.code, i.c.retired_in, d.c.label)
        .select_from(c.join(i, i.c.id == c.c.item_id).join(d, d.c.id == i.c.document_id))
        .where(c.c.project_id == access.project_id, c.c.code_key.in_([code_key(code) for code in codes]))
    )
    rows = {row.code_key: row for row in await conn.execute(query)}
    found, refused = {}, {}
    for given in codes:
        row = rows.get(code_key(given))
        if row is None or not store.visible(access, row.label, sink):
            refused[given] = f"no use case {given} in the SRS of project {access.name} that you can see"
        elif row.kind != USE_CASE:
            refused[given] = f"{row.code} is a {row.kind.replace('_', ' ')} of the SRS, not a use case"
        elif row.retired_in is not None:
            refused[given] = f"use case {row.code} was dropped in version {row.retired_in}: link a current one"
        else:
            found[given] = row.code
    return found, refused


async def _signal_keys(conn: AsyncConnection, access: ProjectAccess, keys: list[str]) -> dict[str, str]:
    """For each of ``keys`` the project's registry lacks or archived, why a claim cannot link it."""
    if not keys:
        return {}
    k = tables.signal_keys
    query = select(k.c.name, k.c.archived_at).where(k.c.project_id == access.project_id, k.c.name.in_(keys))
    rows = {row.name: row for row in await conn.execute(query)}
    refused = {}
    for key in keys:
        if key not in rows:
            refused[key] = (
                f"project {access.name} declares no signal key {key}: an admin adds it with "
                "`evo-agents hub signal keys add`"
            )
        elif rows[key].archived_at is not None:
            refused[key] = f"signal key {key} of project {access.name} is archived"
    return refused


async def _problems(conn: AsyncConnection, access: ProjectAccess, codes: list[str]) -> dict[str, str]:
    """For each of ``codes`` the project lacks or dropped, why a claim cannot link it."""
    if not codes:
        return {}
    p, r = tables.problems, tables.problem_revisions
    numbers = {code: problem_model.number_of(code) for code in codes}
    query = (
        select(p.c.number, r.c.status)
        .join_from(p, r, and_(r.c.problem_id == p.c.id, r.c.revision == p.c.revision))
        .where(p.c.project_id == access.project_id, p.c.number.in_(list(numbers.values())))
    )
    held = {row.number: row.status for row in await conn.execute(query)}
    refused = {}
    for code, number in numbers.items():
        if number not in held:
            refused[code] = f"project {access.name} has no problem {code}: see `evo-agents hub problem list`"
        elif held[number] == problem_model.DROPPED:
            refused[code] = f"problem {code} of project {access.name} is dropped: link one that is open or solved"
    return refused


async def _opportunities(conn: AsyncConnection, access: ProjectAccess, codes: list[str]) -> dict[str, str]:
    """For each of ``codes`` the project lacks or dropped, why a claim cannot link it."""
    if not codes:
        return {}
    o, r = tables.opportunities, tables.opportunity_revisions
    numbers = {code: opportunity_model.number_of(code) for code in codes}
    query = (
        select(o.c.number, r.c.status)
        .join_from(o, r, and_(r.c.opportunity_id == o.c.id, r.c.revision == o.c.revision))
        .where(o.c.project_id == access.project_id, o.c.number.in_(list(numbers.values())))
    )
    held = {row.number: row.status for row in await conn.execute(query)}
    refused = {}
    for code, number in numbers.items():
        if number not in held:
            refused[code] = f"project {access.name} has no opportunity {code}: see `evo-agents hub opportunity list`"
        elif held[number] == opportunity_model.DROPPED:
            refused[code] = (
                f"opportunity {code} of project {access.name} is dropped: link one that is open, captured or missed"
            )
    return refused


async def _check(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, before: Content | None, after: Content
) -> tuple[Content, dict[str, str]]:
    """``after`` with each use case named as the claim or the SRS names it, and why each link it adds is none."""
    held_cases = {code_key(code): code for code in (before.use_cases if before else ())}
    held_keys = set(before.signal_keys if before else ())
    added_cases = [code for code in after.use_cases if code_key(code) not in held_cases]
    found, refused = await _use_cases(conn, access, sink, added_cases)
    refused |= await _signal_keys(conn, access, [key for key in after.signal_keys if key not in held_keys])
    held_problems = set(before.problems if before else ())
    refused |= await _problems(conn, access, [code for code in after.problems if code not in held_problems])
    held_opportunities = set(before.opportunities if before else ())
    refused |= await _opportunities(
        conn, access, [code for code in after.opportunities if code not in held_opportunities]
    )
    named = [held_cases.get(code_key(code)) or found.get(code, code) for code in after.use_cases if code not in refused]
    content = replace(
        after,
        use_cases=checked_use_cases(named),
        signal_keys=checked_signal_keys([key for key in after.signal_keys if key not in refused]),
        problems=checked_problems([code for code in after.problems if code not in refused]),
        opportunities=checked_opportunities([code for code in after.opportunities if code not in refused]),
    )
    return content, refused


async def resolved(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, before: Content | None, after: Content
) -> Content:
    """``after``, the content a write gives a claim that held ``before`` (None for a new one), with each link it adds
    checked and each use case named as the SRS names it; 422 naming the first link the project does not hold."""
    content, refused = await _check(conn, access, sink, before, after)
    if refused:
        raise CanvasError(422, next(iter(refused.values())))
    return content


async def kept(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, before: Content | None, after: Content
) -> tuple[Content, list[str]]:
    """``after`` without the links it adds that the project does not hold, each use case named as the SRS names it,
    and why each link was left out: what an import of a canvas from elsewhere keeps."""
    content, refused = await _check(conn, access, sink, before, after)
    return content, [f"link {link} left out: {why}" for link, why in refused.items()]


__all__ = ["kept", "resolved", "srs_enabled"]
