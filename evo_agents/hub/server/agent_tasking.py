"""Whether an agent takes work, and whether a person may give it some: the rules a mention in a chat
(``chat_runs.hand_work``), an agent asking another agent (``agent_asks``, ``evo-agents worker agent ask``) and the list
of the agents a person may chat with share, so that they never disagree.

``work_home`` is the first: an agent takes work while it is not deleted and has a pool of its own
(``agent_placement.home_of``), or runs on the hub (profile ``runs_on`` hub: a job of the hub takes its turns, no worker
does, ``hub_runtime``), and ``takes_work`` reads the same for many agents at once. ``task_refusal`` is the
second: the person must hold writer on the agent's project, or any grant there for the agent of a settle chat they are
in (``attends``), and admin for an agent type a version of its plugin says takes work from the admins alone
(``admins_only``). ``role_refusal`` is that rule on a role already read. The texts are the refusals the thread shows.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import agent_placement
from evo_agents.hub.server.profile_store import Profile

HUB = "hub"  # profile runs_on of an agent whose turns the hub takes (profiles.RUNS_ON)


def deleted_refusal(profile: Profile) -> str | None:
    """Why agent ``profile`` takes no work because it was deleted; None while it was not."""
    if profile.deleted_at is not None:
        return f"@{profile.handle} was deleted: it takes no new work"
    return None


def no_pool_refusal(profile: Profile) -> str:
    return f"@{profile.handle} has no pool of its own: an admin adds one (`evo-agents worker pool`)"


async def work_home(conn: AsyncConnection, profile: Profile) -> tuple[agent_placement.Home | None, str | None]:
    """(the pool agent ``profile`` works on, None), (None, None) for an agent that runs on the hub, or (None, why it
    takes no work): it was deleted, or it has no pool of its own."""
    refusal = deleted_refusal(profile)
    if refusal is not None:
        return None, refusal
    if profile.runs_on == HUB:
        return None, None
    home = await agent_placement.home_of(conn, profile.id)
    if home is None:
        return None, no_pool_refusal(profile)
    return home, None


async def takes_work(conn: AsyncConnection, profile_ids: list[int]) -> set[int]:
    """Those of the agents ``profile_ids`` that take work now, as ``work_home`` reads it: not deleted, with a pool or
    running on the hub."""
    if not profile_ids:
        return set()
    ap, wp = tables.agent_profiles, tables.worker_pools
    query = (
        select(ap.c.id)
        .distinct()
        .outerjoin_from(ap, wp, wp.c.agent_id == ap.c.id)
        .where(ap.c.id.in_(profile_ids), ap.c.deleted_at.is_(None), or_(wp.c.id.is_not(None), ap.c.runs_on == HUB))
    )
    return set((await conn.execute(query)).scalars())


async def admins_only(conn: AsyncConnection, profile_id: int) -> bool:
    """Whether the profile ``profile_id`` is that of an agent type a version of its plugin says takes work from the
    project's admins alone (``mentioned_by: admin``); any version the hub holds counts, so it never opens up."""
    t, v = tables.project_agent_types, tables.plugin_versions
    query = select(t.c.name, v.c.manifest).join_from(t, v, v.c.plugin_id == t.c.plugin_id)
    for row in await conn.execute(query.where(t.c.profile_id == profile_id)):
        for item in (row.manifest.get("contributes") or {}).get("agent_types") or ():
            if item.get("name") == row.name and item.get("mentioned_by") == "admin":
                return True
    return False


def role_refusal(profile: Profile, login: str, role: str | None, *, attends: bool, admins: bool) -> str | None:
    """Why ``login``, who holds ``role`` (None: no grant) on the project of agent ``profile``, may not give it work:
    they are not a writer there, unless the agent is that of a settle chat they are in (``attends``), where any grant
    will do; or the agent takes work from the admins alone (``admins``) and they are not one. None when they may."""
    if role is None and attends:
        return (
            f"@{profile.handle} works for the members of project {profile.project} in this settle chat, and "
            f"{login} holds no grant there"
        )
    if not has_role(role, "writer") and not attends:
        return (
            f"@{profile.handle} works for the writers of project {profile.project}, and {login} holds "
            f"{role or 'no grant'} there"
        )
    if not has_role(role, "admin") and admins:
        return (
            f"@{profile.handle} takes work from the admins of project {profile.project} alone, and {login} holds "
            f"{role} there"
        )
    return None


async def project_role(conn: AsyncConnection, user_id: int, project_id: int) -> str | None:
    g = tables.grants
    query = select(g.c.role).where(g.c.user_id == user_id, g.c.project_id == project_id)
    return (await conn.execute(query)).scalar_one_or_none()


async def task_refusal(
    conn: AsyncConnection, profile: Profile, user_id: int, login: str, *, attends: bool = False
) -> str | None:
    """Why person ``user_id`` (``login``) may not give agent ``profile`` work: it was deleted, or ``role_refusal``
    on their grant on its project. None when they may. Whether it has a pool is ``work_home``'s."""
    refusal = deleted_refusal(profile)
    if refusal is not None:
        return refusal
    role = await project_role(conn, user_id, profile.project_id)
    admins = not has_role(role, "admin") and await admins_only(conn, profile.id)
    return role_refusal(profile, login, role, attends=attends, admins=admins)
