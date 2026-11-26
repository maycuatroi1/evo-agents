"""The keys of an agent profile as ``evo-agents hub agent`` commands print it with --json: the contract of the schema
Agent (``evo-agents hub contract print``), which ``evo_agents.hub.cli.agent.profiles`` and
``evo_agents.hub.cli.account_pool`` name. A key the hub adds to Agent is added here, or the contract test fails.
Standard library only, like the rest of the client."""

from __future__ import annotations

AGENT_KEYS = (
    "handle",
    "project",
    "name",
    "description",
    "instructions",
    "color",
    "icon_url",
    "avatar_sha256",
    "starters",
    "runs_on",
    "is_default",
    "permissions",
    "restricted",
    "packs",
    "home_pool",
    "account_pool",
    "version",
    "created_by",
    "created_at",
    "updated_at",
    "deleted_at",
)
