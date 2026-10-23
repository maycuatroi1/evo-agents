"""The plugin's effective config in a project: the repo whose issues, pull requests and discussions it reads, and the
Policy of the invite links @community makes (at most ``invite_links_per_week`` in 7 days, only for a GitHub account at
least ``invite_min_github_age_days`` old)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.crm import github
from evo_agents.core_plugins.crm.store import PLUGIN
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.plugins import config as plugin_config

DEFAULT_LINKS_PER_WEEK = 3
DEFAULT_MIN_AGE_DAYS = 30


@dataclass(frozen=True)
class Config:
    repo: str | None  # owner/name of the repo to read; None reads the hub's own data alone
    invite_links_per_week: int = DEFAULT_LINKS_PER_WEEK  # links @community makes in 7 days at most
    invite_min_github_age_days: int = DEFAULT_MIN_AGE_DAYS  # how old a GitHub account a link is made for is at least


def _count(value, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


async def config(conn: AsyncConnection, row, project_id: int) -> Config:
    """The config of the plugin in the project (``row`` is its ``enabled_in`` row); a repo that is no owner/name is
    left out."""
    stacks = await stored_stacks(conn, [row.id], project_id, None)
    merged = plugin_config.merge(row.manifest.get("config"), stacks.get(row.id, []))
    values = {name: item.value for name, item in merged.fields.items() if not item.secret}
    repo = values.get("repo")
    return Config(
        repo if isinstance(repo, str) and re.fullmatch(github.REPO, repo) else None,
        _count(values.get("invite_links_per_week"), DEFAULT_LINKS_PER_WEEK),
        _count(values.get("invite_min_github_age_days"), DEFAULT_MIN_AGE_DAYS),
    )


async def enabled(conn: AsyncConnection, project_id: int):
    """The ``enabled_in`` row of the plugin in the project, or None when it is not enabled there."""
    return await enabled_in(conn, PLUGIN, project_id)
