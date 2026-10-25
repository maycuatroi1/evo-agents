"""How contacts come in: what the hub holds of a project's people, and what GitHub shows of those who found it.

From the hub: an invite link of the project that names a login (made for it, ``for_login``) or was accepted by one
(``accepted_by``), and a login with a grant on the project that is not an admin of it (the team is no contact). From
GitHub (``github``): the author of an issue, a pull request or a discussion who is no member of the repo; an issue
the invite form opened is an invite request. A person who only replied, an admin of the project and a bot are no
contacts.

Each source is read from its cursor (``crm_cursors``) on: the newest time it had read last pass. A contact made
earlier is found again by its login and gets nothing twice (a touchpoint is one row per kind and reference); one
that was deleted is not made again by what lies before the cursor, only by something new it does after.

A new touchpoint from GitHub (an invite request, an issue, a pull request or a discussion) of the last 7 days records
the event ``plugin.crm.touch``: the Loop's rule ``growth`` (``loop``) gives @community its work from it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.crm import github, rules, store
from evo_agents.hub.server import core_plugin_api as hub

log = logging.getLogger(__name__)

HUB, GITHUB = "hub", "github"  # the sources of crm_cursors
TOUCH_EVENT = "touch"  # the plugin's event of a new touch, which emit_event records as TOUCH
TOUCH = f"plugin.{store.PLUGIN}.{TOUCH_EVENT}"  # what the rule growth of the Loop reads (``loop``)
FRESH = timedelta(days=7)  # a touch older than this when first read wakes no agent: history is not answered
JOIN = "join"  # the kind of an invite link of a project


@dataclass(frozen=True)
class Seen:
    login: str
    kind: str
    ref: str
    url: str | None
    at: datetime


async def _team(conn: AsyncConnection, project: str) -> set[str]:
    """The logins that hold an admin grant on the project: its team, never contacts."""
    grants = await hub.read_grants(conn, store.handle(), project)
    return {grant.login.lower() for grant in grants if grant.role == "admin"}


async def hub_touches(conn: AsyncConnection, project: str, since: datetime | None) -> list[Seen]:
    """Invites and grants of the project that happened after ``since`` (all when None)."""
    plugin = store.handle()
    found: list[Seen] = []
    invites = await hub.read_invites(conn, plugin, project, kinds=[JOIN], since=since)
    for invite in invites:
        if invite.for_login is not None and (since is None or invite.created_at > since):
            found.append(Seen(invite.for_login, "invite_link", f"invite:{invite.id}", None, invite.created_at))
    for invite in invites:
        taken = invite.accepted_at
        if invite.accepted_by is not None and taken is not None and (since is None or taken > since):
            found.append(Seen(invite.accepted_by, "invite_link", f"accepted:{invite.id}", None, taken))
    for grant in await hub.read_grants(conn, plugin, project):
        if grant.role != "admin" and (since is None or grant.granted_at > since):
            found.append(Seen(grant.login, "grant", "grant", None, grant.granted_at))
    return found


async def apply(conn: AsyncConnection, project_id: int, label, touches: list[Seen]) -> dict[str, int]:
    """Make the contacts ``touches`` name, with ``label``, and add their touchpoints, oldest first; counts of what was
    new."""
    project = (await hub.read_project(conn, store.handle(), project_id)).name
    team = await _team(conn, project)
    done = {"contacts": 0, "touchpoints": 0}
    for touch in sorted(touches, key=lambda t: t.at):
        login = rules.login_of(touch.login)
        if login is None or login in team:
            continue
        contact_id, made = await store.ensure_contact(conn, project_id, login, touch.kind, dict(label), touch.at)
        done["contacts"] += made
        new = await store.add_touchpoint(conn, contact_id, touch.kind, touch.ref, touch.at, url=touch.url)
        done["touchpoints"] += new
        if new and touch.kind in github.GITHUB_KINDS and touch.at >= datetime.now(UTC) - FRESH:
            payload = {"v": 1, "contact_id": contact_id, "kind": touch.kind, "ref": touch.ref, "url": touch.url}
            await hub.emit_event(
                conn, store.handle(), project, TOUCH_EVENT, payload
            )  # no login: a deleted one leaves none
    return done


async def from_hub(conn: AsyncConnection, project: hub.EnabledProject) -> dict[str, int]:
    """Read the invites and grants after the hub cursor and move it."""
    project_id, label = project.id, project.default_label
    since = await store.cursor(conn, project_id, HUB)
    touches = await hub_touches(conn, project.name, since)
    done = await apply(conn, project_id, label, touches)
    if touches:
        await store.move_cursor(conn, project_id, HUB, max(touch.at for touch in touches))
    return done


async def from_github(
    conn: AsyncConnection, project: hub.EnabledProject, repo: str, fetcher: github.Fetcher
) -> dict[str, int]:
    """Read the issues, pull requests and discussions of ``repo`` after the GitHub cursor and move it. A read that
    fails is logged and changes nothing: the next pass reads the same span again."""
    project_id, label = project.id, project.default_label
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
