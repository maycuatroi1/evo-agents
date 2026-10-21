"""A project's forge secrets: the GitLab tokens with which the hub's own process drives the merge requests of the
project's repos, and that no run ever holds (``docs/credentials.md``, section Forge secrets).

A member who holds the admin role on a project writes one with PUT /v1/secrets/{name}, kind ``forge``
(``evo_agents.hub.server.secrets``): a url_prefix as a git secret has, an end, no username and no worker. The lease
route reads kinds env and git alone (``credentials.LEASED_KINDS``), so a worker never gets one; GET /v1/secrets shows
its owner its name and target, never its value; no route of the worker protocol, /mcp or the API reads it.

``forge_token`` is the one place that opens one, for the hub's job worker right before it calls GitLab: of the forge
secrets bound to the project, not deleted, not past their end, whose owner still holds the admin role on the project,
it takes the one whose url_prefix covers the origin the longest (then the one written last, then the newest), opens
it, and hands back a ``ForgeToken`` whose repr leaves the value out. A secret that does not open with the hub's key is
passed over with a warning naming its id. None means the project has no forge secret for that origin, or the hub has
no key: the hub then leaves the project's merge requests there as they are, open for a person, as before
(criterion a3). ``ForgeToken.gitlab`` opens the client of ``evo_agents.hub.server.gitlab`` with it; the caller keeps
the token no longer than its calls, and nothing writes its value to a log line, an error, an event or the audit.
``has_forge`` says whether such a secret covers an origin without opening one, for a caller that only decides whether
the merge queue takes a merge request (``evo_agents.hub.server.merge_gate``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credentials import FORGE_KIND, matches, normalize_origin
from evo_agents.hub.server.gitlab import GitLab, GitLabProject, project_of
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, secret_aad

log = logging.getLogger(__name__)

FORGE_ROLE = "admin"  # the role on a project a forge secret's owner holds, when they write it and when it is used


@dataclass(frozen=True)
class ForgeToken:
    """An opened forge secret and the GitLab project of the origin it was opened for; ``repr`` leaves the value out."""

    secret_id: int
    name: str
    url_prefix: str
    expires_at: datetime
    project: GitLabProject
    value: str = field(repr=False)

    def gitlab(self, transport: httpx.AsyncBaseTransport | None = None) -> GitLab:
        """The GitLab client of the token's host, holding the token; use it in an ``async with`` block."""
        return GitLab(self.project.api_url, self.value, transport=transport)


def _usable(project_id: int):
    """The forge secrets bound to ``project_id``, live, not past their end, of an owner who holds FORGE_ROLE on it."""
    s, b, g = tables.secrets, tables.secret_bindings, tables.grants
    bound = exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id)
    admin = exists().where(g.c.user_id == s.c.owner_id, g.c.project_id == project_id, g.c.role == FORGE_ROLE)
    return select(
        s.c.id,
        s.c.owner_id,
        s.c.name,
        s.c.url_prefix,
        s.c.sealed,
        s.c.nonce,
        s.c.key_id,
        s.c.expires_at,
        s.c.updated_at,
    ).where(
        s.c.kind == FORGE_KIND,
        s.c.deleted_at.is_(None),
        s.c.expires_at > func.now(),
        bound,
        admin,
    )


def _preference(row) -> tuple:
    """The longest url_prefix first, then the secret written last, then the newest."""
    return (-len(normalize_origin(row.url_prefix)), -row.updated_at.timestamp(), -row.id)


async def has_forge(conn: AsyncConnection, project_id: int, origin: str | None) -> bool:
    """Whether a forge secret ``forge_token`` would consider covers ``origin`` of project ``project_id``; none is
    opened, so a secret that would not open still counts."""
    if not origin or project_of(origin) is None:
        return False
    return any(matches(row.url_prefix, origin) for row in await conn.execute(_usable(project_id)))


async def forge_token(conn: AsyncConnection, sealer: Sealer | None, project_id: int, origin: str) -> ForgeToken | None:
    """The forge secret of project ``project_id`` for ``origin``, opened; None when there is none the hub can use."""
    project = project_of(origin)
    if sealer is None or project is None:
        return None
    rows = [row for row in await conn.execute(_usable(project_id)) if matches(row.url_prefix, origin)]
    for row in sorted(rows, key=_preference):
        sealed = Sealed(row.sealed, row.nonce, row.key_id)
        try:
            value = sealer.open(sealed, secret_aad(row.owner_id, row.name, FORGE_KIND))
        except Unsealable as exc:
            log.warning("a forge secret does not open; it is passed over", extra={"secret_id": row.id, "why": str(exc)})
            continue
        return ForgeToken(row.id, row.name, row.url_prefix, row.expires_at, project, value)
    return None
