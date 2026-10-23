"""The plugin's effective config in a project: the monthly budget, the dry-run time, the rules a project changes for a
channel, and the credential of each API channel.

The credentials are x-secret fields: sealed on the hub and opened here, at the moment of a post, with the hub's key
(``core_plugin_api.plugin_config``). They are held in a ``Config`` whose repr hides them and handed to one adapter;
they never go into a lease, an environment, a prompt, a log or an answer of a route.
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


@dataclass(frozen=True)
class Config:
    budget_usd: float = registry.DEFAULT_BUDGET_USD
    dry_run_hours: float = registry.DEFAULT_DRY_RUN_HOURS
    rules: dict = field(default_factory=dict)  # channel -> the fields of its rules the project changes
    tokens: dict = field(default_factory=dict, repr=False)  # channel -> credential; hidden from repr

    def token(self, channel: str) -> str | None:
        return self.tokens.get(channel) or None


def _number(value, default: float) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    return float(value) if ok else default


async def config(conn: AsyncConnection, hub_config: HubConfig | None, project: str) -> Config:
    """The effective config of the plugin in ``project``. A credential this process cannot open is none."""
    found = await hub.plugin_config(conn, hub.plugin(__name__), hub_config, project=project)
    values = found.values
    tokens = {channel: values.get(name) for channel, name in TOKENS.items() if isinstance(values.get(name), str)}
    rules = values.get("rules")
    return Config(
        budget_usd=_number(values.get("budget_usd"), registry.DEFAULT_BUDGET_USD),
        dry_run_hours=_number(values.get("dry_run_hours"), registry.DEFAULT_DRY_RUN_HOURS),
        rules=rules if isinstance(rules, dict) else {},
        tokens=tokens,
    )


async def enabled(conn: AsyncConnection, project_id: int):
    """The ``enabled_in`` row of the plugin in the project, or None when it is not enabled there."""
    return await enabled_in(conn, PLUGIN, project_id)
