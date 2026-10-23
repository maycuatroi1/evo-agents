"""The plugin's effective config in a project: the monthly budget, the dry-run time, the rules a project changes for a
channel, and the credential of each API channel and the login session of each browser channel.

The credentials are x-secret fields: sealed on the hub and opened here, at the moment of a post, with the hub's key
(``core_plugin_api.plugin_config``). They are held in a ``Config`` whose repr hides them and handed to one adapter;
they never go into a lease, an environment, a prompt, a log or an answer of a route. A login session reaches one place
besides: the worker's script, in the answer to its claim (``browser``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.marketing import registry
from evo_agents.core_plugins.marketing.store import PLUGIN
from evo_agents.hub.config import HubConfig
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server import core_plugin_api as hub

TOKENS = {  # channel -> the secret field that holds its credential
    "github": "github_token",
    "x": "x_token",
    "linkedin": "linkedin_token",
    "reddit": "reddit_token",
    "facebook_page": "facebook_page_token",
    "tiktok": "tiktok_token",
}
SESSIONS = {  # browser channel -> the secret field that holds the sealed login session of its account
    "hackernews": "hackernews_session",
    "reddit": "reddit_session",
    "linkedin": "linkedin_session",
    "viblo": "viblo_session",
    "facebook_profile": "facebook_profile_session",
    "tiktok": "tiktok_session",
}


@dataclass(frozen=True)
class Config:
    budget_usd: float = registry.DEFAULT_BUDGET_USD
    dry_run_hours: float = registry.DEFAULT_DRY_RUN_HOURS
    rules: dict = field(default_factory=dict)  # channel -> the fields of its rules the project changes
    replies_per_day: int = registry.DEFAULT_REPLIES_PER_DAY  # reply work items the Loop makes in 24 hours
    tokens: dict = field(default_factory=dict, repr=False)  # channel -> credential; hidden from repr
    sessions: dict = field(default_factory=dict, repr=False)  # channel -> login session of the browser; hidden

    def token(self, channel: str) -> str | None:
        return self.tokens.get(channel) or None

    def session(self, channel: str) -> str | None:
        """The sealed login session (Playwright's storage state, as JSON) of a browser channel, or None."""
        return self.sessions.get(channel) or None


def _number(value, default: float) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    return float(value) if ok else default


async def config(conn: AsyncConnection, hub_config: HubConfig | None, project: str) -> Config:
    """The effective config of the plugin in ``project``. A credential this process cannot open is none."""
    found = await hub.plugin_config(conn, hub.plugin(__name__), hub_config, project=project)
    values = found.values
    tokens = {channel: values.get(name) for channel, name in TOKENS.items() if isinstance(values.get(name), str)}
    sessions = {channel: values.get(name) for channel, name in SESSIONS.items() if isinstance(values.get(name), str)}
    rules = values.get("rules")
    return Config(
        budget_usd=_number(values.get("budget_usd"), registry.DEFAULT_BUDGET_USD),
        dry_run_hours=_number(values.get("dry_run_hours"), registry.DEFAULT_DRY_RUN_HOURS),
        rules=rules if isinstance(rules, dict) else {},
        replies_per_day=int(_number(values.get("replies_per_day"), registry.DEFAULT_REPLIES_PER_DAY)),
        tokens=tokens,
        sessions=sessions,
    )


async def enabled(conn: AsyncConnection, project_id: int):
    """The ``enabled_in`` row of the plugin in the project, or None when it is not enabled there."""
    return await enabled_in(conn, PLUGIN, project_id)
