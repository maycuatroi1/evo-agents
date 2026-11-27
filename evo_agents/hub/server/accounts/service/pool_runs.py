"""Whether a run of an agent can be dispatched for want of an account pool (``pools``): a run of an agent in pool mode
is leased the env accounts of its pool alone (``credentials.survey_run``; ``repository.pool_runs``)."""

from __future__ import annotations

from evo_agents.hub.server.accounts.models.pools import NEEDS_POOL


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
