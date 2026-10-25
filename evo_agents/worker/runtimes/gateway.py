"""A run on a provider account's model, through the hub's gateway route (plan model-providers; docs/credentials.md).

The hub leases such a run one lease of kind gateway (``evo_agents.hub.gateway_runs``): the hub's route ``base_url``
(``<hub>/v1``), the run's token as its value, the run's model (``<prefix>/<model>``, as the gateway reads it) and the
API format of the run's runtime; never a provider's key nor a key of the gateway. ``agent_env`` is what that lease adds
to the agent's environment (``credentials.RunCredentials``), by its format:

- ``anthropic`` (Claude Code): ANTHROPIC_BASE_URL the hub (Claude Code adds ``/v1/messages``), ANTHROPIC_AUTH_TOKEN
  the token, the run's model in ANTHROPIC_MODEL and in each variable of CLAUDE_MODELS (the small and fast model, the
  default haiku, sonnet and opus models, the subagents' model), and CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC. The
  agent gets none of CLAUDE_DROPPED: no env lease of them is handed over, the daemon's own are left out of its
  environment (``runner.agent``) and unset by the launcher (``claude_code.dropped_env``), and on this machine it runs
  in a config dir of its own that holds no login (``claude_home.prepare``), so no Claude login of the owner or of a
  lease ever goes to the hub's route, and the token of the run goes nowhere else.
- ``openai`` (opencode): OPENCODE_CONFIG_CONTENT gains provider PROVIDER (``@ai-sdk/openai-compatible``, baseURL the
  route, apiKey ``{env:EVO_GATEWAY_TOKEN}``, which opencode reads from the environment, so no key is written to disk)
  with the run's model alone; the session's model is ``PROVIDER/<model>`` (``opencode_model``).
- ``openai-responses`` (Codex): the thread's config, and the terminal UI's ``-c`` options, set ``model_provider`` to
  PROVIDER and ``model_providers.PROVIDER`` to the route with ``wire_api`` responses and ``env_key`` EVO_GATEWAY_TOKEN
  (``codex_config``, ``codex_args``).

Every format gets MODEL and URL, and the openai ones TOKEN. MODEL among the variables the leases set is how a runtime
knows it runs through the gateway (``active``). The token is a lease's value, so the daemon masks it in its log and in
the run's events as it does every lease's (``credentials.remember``), and a pane's script never holds it.
Standard library only: ``credentials`` imports it, and git's credential helper starts without the worker extra.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from evo_agents.hub.credentials import Lease
from evo_agents.hub.gateway_runs import GATEWAY

if TYPE_CHECKING:
    from evo_agents.worker.adapter import RunContext

PROVIDER = "evo_gateway"  # the provider of opencode and of Codex that is the hub's gateway route
NAME = "evo-agents gateway"
TOKEN = "EVO_GATEWAY_TOKEN"  # the run's token, for opencode's apiKey and Codex's env_key
MODEL = "EVO_GATEWAY_MODEL"  # the run's model, <prefix>/<model>
URL = "EVO_GATEWAY_URL"  # the hub's gateway route, <hub>/v1
CONFIG_CONTENT = "OPENCODE_CONFIG_CONTENT"  # profiles.CONFIG_CONTENT
ANTHROPIC, OPENAI, RESPONSES = "anthropic", "openai", "openai-responses"
# What a Claude Code agent of a gateway run never gets: a Claude login, an Anthropic key, another backend, headers.
CLAUDE_DROPPED = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_CUSTOM_HEADERS",
)
CLAUDE_MODELS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


def lease_of(leases: Iterable[Lease]) -> Lease | None:
    """The run's gateway lease: kind gateway, with a route, a token and a model; None for a run without one."""
    for lease in leases:
        if lease.kind == GATEWAY and lease.base_url and lease.value and lease.model:
            return lease
    return None


def anthropic_root(base_url: str) -> str:
    """ANTHROPIC_BASE_URL for the route ``base_url``: the hub, since Claude Code adds ``/v1/messages`` itself."""
    root = base_url.rstrip("/")
    return root.removesuffix("/v1")


def agent_env(lease: Lease, base_env: Mapping[str, str]) -> dict[str, str]:
    """What gateway lease ``lease`` adds to the agent's environment (see the module); ``base_env`` is the daemon's,
    whose OPENCODE_CONFIG_CONTENT the provider is merged into."""
    model, url = str(lease.model), str(lease.base_url)
    env = {MODEL: model, URL: url}
    if lease.api_format == ANTHROPIC:
        env.update({name: model for name in CLAUDE_MODELS})
        env.update(
            {
                "ANTHROPIC_BASE_URL": anthropic_root(url),
                "ANTHROPIC_AUTH_TOKEN": lease.value,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )
    elif lease.api_format in (OPENAI, RESPONSES):
        env[TOKEN] = lease.value
    if lease.api_format == OPENAI:
        env[CONFIG_CONTENT] = opencode_content(url, model, base_env.get(CONFIG_CONTENT) or "")
    return env


def drops(lease: Lease | None, env_var: str | None) -> bool:
    """Whether an env lease of ``env_var`` stays out of the agent's environment beside gateway lease ``lease``."""
    return lease is not None and lease.api_format == ANTHROPIC and env_var in CLAUDE_DROPPED


def active(context: RunContext) -> bool:
    """Whether the run of ``context`` runs through the gateway: a lease set MODEL in its agent's environment."""
    return MODEL in context.leased and bool(context.env.get(MODEL))


def opencode_content(url: str, model: str, own: str) -> str:
    """OPENCODE_CONFIG_CONTENT with provider PROVIDER, merged into ``own`` (the daemon's, when it is a JSON object)."""
    try:
        content = json.loads(own) if own.strip() else {}
    except ValueError:
        content = {}
    content = content if isinstance(content, dict) else {}
    providers = content.get("provider") if isinstance(content.get("provider"), dict) else {}
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": NAME,
        "options": {"baseURL": url, "apiKey": "{env:" + TOKEN + "}"},
        "models": {model: {"name": model}},
    }
    return json.dumps({**content, "provider": {**providers, PROVIDER: provider}})


def opencode_model(context: RunContext) -> str | None:
    """The model of an opencode session of a gateway run, ``PROVIDER/<model>``; None for any other run."""
    return f"{PROVIDER}/{context.env[MODEL]}" if active(context) else None


def codex_provider(context: RunContext) -> dict:
    return {"name": NAME, "base_url": context.env.get(URL) or "", "env_key": TOKEN, "wire_api": "responses"}


def codex_config(context: RunContext) -> dict:
    """What the Codex thread's config gains for a gateway run: its model provider; nothing for any other run."""
    if not active(context):
        return {}
    return {"model_provider": PROVIDER, "model_providers": {PROVIDER: codex_provider(context)}}


def codex_args(context: RunContext) -> list[str]:
    """``codex_config`` as ``-c`` options of the terminal UI, each value TOML."""
    if not active(context):
        return []
    table = ", ".join(f"{key} = {json.dumps(value)}" for key, value in codex_provider(context).items())
    return ["-c", f"model_provider={json.dumps(PROVIDER)}", "-c", f"model_providers.{PROVIDER}={{{table}}}"]
