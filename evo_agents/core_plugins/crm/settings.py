"""The plugin's effective config in a project: the repo whose issues, pull requests and discussions it reads, and the
Policy of the invite links @community makes (at most ``invite_links_per_week`` in 7 days, only for a GitHub account at
least ``invite_min_github_age_days`` old)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.crm import github, store
from evo_agents.hub.server import core_plugin_api as hub

DEFAULT_LINKS_PER_WEEK = 3
DEFAULT_MIN_AGE_DAYS = 30


@dataclass(frozen=True)
class Config:
    repo: str | None  # owner/name of the repo to read; None reads the hub's own data alone
    invite_links_per_week: int = DEFAULT_LINKS_PER_WEEK  # links @community makes in 7 days at most
    invite_min_github_age_days: int = DEFAULT_MIN_AGE_DAYS  # how old a GitHub account a link is made for is at least


def _count(value, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


async def config(conn: AsyncConnection, project: str) -> Config:
    """The config of the plugin in ``project``, which enables it (404 otherwise); a repo that is no owner/name is left
    out. The plugin has no secret field, so it unseals nothing."""
    found = await hub.plugin_config(conn, store.handle(), None, project=project)
    values = found.values
    repo = values.get("repo")
    return Config(
        repo if isinstance(repo, str) and re.fullmatch(github.REPO, repo) else None,
        _count(values.get("invite_links_per_week"), DEFAULT_LINKS_PER_WEEK),
        _count(values.get("invite_min_github_age_days"), DEFAULT_MIN_AGE_DAYS),
    )
