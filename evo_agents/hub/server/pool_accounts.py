"""The env accounts of an agent's account pool, as a run of the agent in pool mode is leased them
(``credentials.survey_run``; ``account_pools``)."""

from __future__ import annotations

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.account_providers import MODEL_ENV_VARS
from evo_agents.hub.credentials import PROVIDER_KIND

NEEDS_POOL = (
    "agent @{handle} has no account pool, so a run of it has no account to use: ask an admin of "
    "project {project} to attach one (`evo-agents hub agent account-pool {handle} POOL`)"
)


def needs_pool(profile, project: str) -> str | None:
    """Why a run of ``profile`` (an agent with no account pool) cannot be dispatched: an admin must attach an account
    pool; None for an agent that has one."""
    if profile.account_pool_id is not None:
        return None
    return NEEDS_POOL.format(handle=profile.handle, project=project)


def chat_needs_pool(profile, project: str) -> str | None:
    """``needs_pool`` for a chat turn: an agent that runs on the hub takes its model through the hub's gateway with the
    project's provider account, not an account pool (``hub_runtime``), so it needs none."""
    if profile.runs_on == "hub":
        return None
    return needs_pool(profile, project)


def pool_env_accounts(pool_id: int, *, lock: bool):
    """The live env accounts of account pool ``pool_id``, past their end or not, by name. With ``lock`` their rows are
    shared-locked. Each row carries its owner (a member) or its profile (an agent's account), which its sealing names."""
    s, m = tables.secrets, tables.account_pool_members
    statement = (
        select(
            s.c.id,
            s.c.name,
            s.c.kind,
            s.c.env_var,
            s.c.sealed,
            s.c.nonce,
            s.c.key_id,
            s.c.expires_at,
            s.c.environment,
            s.c.owner_id,
            s.c.profile_id,
            func.coalesce(s.c.expires_at <= func.now(), False).label("ended"),
        )
        .join(m, m.c.secret_id == s.c.id)
        .where(m.c.pool_id == pool_id, s.c.kind == "env", s.c.deleted_at.is_(None))
        .order_by(s.c.name)
    )
    return statement.with_for_update(read=True, of=s) if lock else statement


def usable_member():
    """A member of the account pool of the run being claimed (``tables.runs``, correlated) a run can take now: its
    secret live, not past its end (an OAuth account whose access token ended is, as the lease refreshes it first:
    ``account_refresh``; one whose refresh the endpoint refused is cooling), not paused and not cooling, an env account
    of a model variable or a provider account."""
    m, s, a, ap, r = (
        tables.account_pool_members,
        tables.secrets,
        tables.accounts,
        tables.agent_profiles.alias("of_run"),
        tables.runs,
    )
    return exists().where(
        ap.c.id == r.c.profile_id,
        m.c.pool_id == ap.c.account_pool_id,
        s.c.id == m.c.secret_id,
        s.c.deleted_at.is_(None),
        or_(
            s.c.expires_at.is_(None),
            s.c.expires_at > func.now(),
            exists().where(a.c.secret_id == s.c.id, a.c.oauth_sealed.is_not(None)),
        ),
        or_(s.c.kind == PROVIDER_KIND, and_(s.c.kind == "env", s.c.env_var.in_(MODEL_ENV_VARS))),
        ~exists().where(
            a.c.secret_id == s.c.id,
            or_(a.c.paused_at.is_not(None), a.c.cooldown_until > func.now()),
        ),
    )


def claim_gate():
    """The condition on the run being claimed that account pools hold: a run of an agent in pool mode is taken only
    while the agent's account pool has an account the run can use, or when the run took an account already (a resumed
    run, a run that failed over). The Curator's runs and a browser login take their accounts as before."""
    from evo_agents.hub.server import agent_placement  # it reads this module's neighbours: imported when used

    r, ra = tables.runs, tables.run_accounts
    took = exists().where(ra.c.run_id == r.c.id)
    return or_(
        ~agent_placement.pooled_run(),
        r.c.kind.in_((*runs.CURATOR_KINDS, "browser")),
        took,
        usable_member(),
    )


async def is_pooled(conn: AsyncConnection, run_id: int) -> bool:
    """Whether run ``run_id`` is one of an agent in pool mode whose model account is its account pool's alone: not a run
    of the Curator nor a browser login (``claim_gate``)."""
    r, ap = tables.runs, tables.agent_profiles
    query = (
        select(ap.c.id)
        .join_from(r, ap, ap.c.id == r.c.profile_id)
        .where(r.c.id == run_id, r.c.kind.not_in((*runs.CURATOR_KINDS, "browser")))
    )
    return (await conn.execute(query)).scalar_one_or_none() is not None
