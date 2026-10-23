"""How contacts come in: what the hub holds of a project's people, and what GitHub shows of those who found it.

From the hub: an invite link of the project that names a login (made for it, ``for_login``) or was accepted by one
(``accepted_by``), and a login with a grant on the project that is not an admin of it (the team is no contact). From
GitHub (``github``): the author of an issue, a pull request or a discussion who is no member of the repo; an issue
the invite form opened is an invite request. A person who only replied, an admin of the project and a bot are no
contacts.

Each source is read from its cursor (``crm_cursors``) on: the newest time it had read last pass. A contact made
earlier is found again by its login and gets nothing twice (a touchpoint is one row per kind and reference); one
that was deleted is not made again by what lies before the cursor, only by something new it does after.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.crm import github, rules, store
from evo_agents.hub import tables

log = logging.getLogger(__name__)

HUB, GITHUB = "hub", "github"  # the sources of crm_cursors


@dataclass(frozen=True)
class Seen:
    login: str
    kind: str
    ref: str
    url: str | None
    at: datetime


async def _team(conn: AsyncConnection, project_id: int) -> set[str]:
    """The logins that hold an admin grant on the project: its team, never contacts."""
    g, u = tables.grants, tables.users
    found = await conn.execute(
        select(func.lower(u.c.login))
        .join_from(g, u, u.c.id == g.c.user_id)
        .where(g.c.project_id == project_id, g.c.role == "admin")
    )
    return {login for (login,) in found}


async def hub_touches(conn: AsyncConnection, project_id: int, since: datetime | None) -> list[Seen]:
    """Invites and grants of the project that happened after ``since`` (all when None)."""
    i, g, u = tables.invites, tables.grants, tables.users
    found: list[Seen] = []
    made = select(i.c.id, i.c.for_login, i.c.created_at).where(
        i.c.project_id == project_id, i.c.kind == "join", i.c.for_login.is_not(None)
    )
    for row in await conn.execute(made if since is None else made.where(i.c.created_at > since)):
        found.append(Seen(row.for_login, "invite_link", f"invite:{row.id}", None, row.created_at))
    taken = (
        select(i.c.id, u.c.login, i.c.accepted_at)
        .join_from(i, u, u.c.id == i.c.accepted_by)
        .where(i.c.project_id == project_id, i.c.kind == "join", i.c.accepted_at.is_not(None))
    )
    for row in await conn.execute(taken if since is None else taken.where(i.c.accepted_at > since)):
        found.append(Seen(row.login, "invite_link", f"accepted:{row.id}", None, row.accepted_at))
    given = (
        select(u.c.login, g.c.granted_at)
        .join_from(g, u, u.c.id == g.c.user_id)
        .where(g.c.project_id == project_id, g.c.role != "admin")
    )
    for row in await conn.execute(given if since is None else given.where(g.c.granted_at > since)):
        found.append(Seen(row.login, "grant", "grant", None, row.granted_at))
    return found


async def apply(conn: AsyncConnection, project_id: int, label: dict, touches: list[Seen]) -> dict[str, int]:
    """Make the contacts ``touches`` name and add their touchpoints, oldest first; counts of what was new."""
    team = await _team(conn, project_id)
    done = {"contacts": 0, "touchpoints": 0}
    for touch in sorted(touches, key=lambda t: t.at):
        login = rules.login_of(touch.login)
        if login is None or login in team:
            continue
        contact_id, made = await store.ensure_contact(conn, project_id, login, touch.kind, label, touch.at)
        done["contacts"] += made
        done["touchpoints"] += await store.add_touchpoint(
            conn, contact_id, touch.kind, touch.ref, touch.at, url=touch.url
        )
    return done


async def from_hub(conn: AsyncConnection, project_id: int, label: dict) -> dict[str, int]:
    """Read the invites and grants after the hub cursor and move it."""
    since = await store.cursor(conn, project_id, HUB)
    touches = await hub_touches(conn, project_id, since)
    done = await apply(conn, project_id, label, touches)
    if touches:
        await store.move_cursor(conn, project_id, HUB, max(touch.at for touch in touches))
    return done


async def from_github(
    conn: AsyncConnection, project_id: int, label: dict, repo: str, fetcher: github.Fetcher
) -> dict[str, int]:
    """Read the issues, pull requests and discussions of ``repo`` after the GitHub cursor and move it. A read that
    fails is logged and changes nothing: the next pass reads the same span again."""
    since = await store.cursor(conn, project_id, GITHUB)
    try:
        touches = github.parse_issues(await fetcher.issues(repo, since))
        touches += github.parse_discussions(await fetcher.discussions(repo, since))
    except Exception as exc:  # GitHub is out of our hands: the hub's own sources still count
        log.warning("crm cannot read github", extra={"repo": repo, "error": type(exc).__name__})
        return {"contacts": 0, "touchpoints": 0, "failed": 1}
    if since is not None:  # GitHub's ``since`` is inclusive: what the cursor already passed is not read again
        touches = [t for t in touches if t.updated_at > since]
    seen = [Seen(t.login, t.kind, t.ref, t.url, t.happened_at) for t in touches]
    done = await apply(conn, project_id, label, seen)
    if touches:
        await store.move_cursor(conn, project_id, GITHUB, max(touch.updated_at for touch in touches))
    return done
