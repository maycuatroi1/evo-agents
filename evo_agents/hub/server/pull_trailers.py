"""The trailers of a run in the body of a pull request or a merge request the hub opens for its work (plan
autonomy-levels, criterion a10): ``Evo-Principal: <login>``, the person the run acts for, and ``Evo-Agent: <handle>``,
the agent profile that did the work, each when the run has one; never the runtime or the model. The worker puts the
same two trailers on the run's commits (``evo_agents.worker.runner.authorship``).

The hub opens a draft pull request for a chat run and a task run (``chat_agents``, ``task_dispatch``), a pull request
for a Curator change (``changes``, its Builder's run) and a release (``release_land``), and a merge request for a plan's
or a task run's work on GitLab (``merge_gate``); each body ends with ``block`` of its run.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables

PRINCIPAL_TRAILER, AGENT_TRAILER = "Evo-Principal", "Evo-Agent"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


async def lines(conn: AsyncConnection, run_id: int | None) -> list[str]:
    """``Evo-Principal: <login>`` and ``Evo-Agent: <handle>`` of run ``run_id``, each when it has one."""
    if run_id is None:
        return []
    r, u, ap = tables.runs, tables.users, tables.agent_profiles
    query = (
        select(u.c.login, ap.c.handle)
        .select_from(r.outerjoin(u, u.c.id == r.c.principal_id).outerjoin(ap, ap.c.id == r.c.profile_id))
        .where(r.c.id == run_id)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return []
    found = []
    for key, value in ((PRINCIPAL_TRAILER, row.login), (AGENT_TRAILER, row.handle)):
        if value and not _UNSAFE.search(value):
            found.append(f"{key}: {value}")
    return found


async def block(conn: AsyncConnection, run_id: int | None) -> str:
    """What ends a body of run ``run_id``: a blank line and its trailers, or nothing when it has none."""
    found = await lines(conn, run_id)
    return "\n\n" + "\n".join(found) if found else ""
