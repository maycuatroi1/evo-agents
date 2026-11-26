"""``evo-agents hub`` subcommands.

``hub serve``, ``hub worker``, ``hub migrate`` and ``hub storage`` need the hub-server extra. This module imports none
of it when loaded, so ``evo-agents`` keeps starting on a core install; each command imports the extra when it runs and
names the pip command when it is missing. These commands log JSON lines to stderr from their first line on: a
configuration error is a log line naming the variable, and an unexpected exception is a log line with its
traceback, secrets removed, rather than a bare traceback from the interpreter.

The client commands (``login``, ``logout``, ``whoami``, ``token``, ``admin``, ``project``, ``registry``, ``plan``,
``run``, ``decision``, ``notifications``, ``memory``, ``skills``, ``secret``, ``kg``, ``mcp``, ``hook``) are
attached by ``cli.session`` and the class modules beside it, ``agent`` by ``cli_agents``, ``chat`` by ``cli_chats``, ``plugin`` by
``cli.plugin``, ``srs`` by ``cli_srs``, ``docs`` by ``cli_docs``, ``task`` by ``cli_tasks``, ``canvas`` by ``cli_canvas``,
``signal`` by ``cli_signals``, ``sessions`` by ``cli_sessions``, ``email`` by ``cli.email``, ``release`` by
``cli.curator.release``, ``policy`` by ``cli.policy``, ``goal`` by the classes of ``evo_agents.hub.cli.goal``, ``problem`` by ``cli.problem``, ``opportunity`` by ``cli.opportunity``, ``intake`` by ``cli_intake``, ``merge`` by
``cli_merge``, ``loop`` by ``cli_loop``, ``org`` by ``cli.org`` and ``autonomy`` by ``cli.autonomy``; they need nothing beyond the core
package, so they work on a core install. ``hub openapi`` (``cli.openapi``) and ``hub contract print`` (``cli.contract``) need the extra for the API
document; ``hub contract check`` does not. The commands of plugins (``pm``, ``crm`` and ``marketing`` of the core
plugins installed with the CLI among them) come from their manifests, registered last by ``cli.extensions``.
"""

from __future__ import annotations

from evo_agents.hub.cli.server import (  # noqa: F401 (the names other modules import from here)
    EXTRA_HINT,
    SERVER,
    MissingExtra,
    ServerCommand,
    missing_extra,
    positive,
)

OWN_COMMANDS: set[str] = set()  # the hub commands of the CLI itself, the plugins' left out (cli_plugin_commands)


def _register_client_commands(hsub) -> None:
    """The command groups of the client, each registered by its own module, imported only when the parser is built."""
    _register_workspace_commands(hsub)
    _register_steering_commands(hsub)


def _register_workspace_commands(hsub) -> None:
    """The client's groups from hub login to hub kg."""
    from evo_agents.hub.cli.session import SESSION

    SESSION.attach(hsub)  # login, logout, whoami, token, admin, project, registry
    from evo_agents.hub.cli.plan import PLAN

    PLAN.attach(hsub)

    from evo_agents.hub.cli.run import RUN

    RUN.attach(hsub)

    from evo_agents.hub.cli.decision import DECISION
    from evo_agents.hub.cli.notifications import NOTIFICATIONS

    DECISION.attach(hsub)
    NOTIFICATIONS.attach(hsub)

    from evo_agents.hub.cli.curator import CURATOR

    CURATOR.attach(hsub)

    from evo_agents.hub.cli_memory import register_memory

    register_memory(hsub)

    from evo_agents.hub.cli_skills import register_skills

    register_skills(hsub)

    from evo_agents.hub.cli.account import ACCOUNT
    from evo_agents.hub.cli.model_catalog import MODEL_CATALOG
    from evo_agents.hub.cli.provider_account import PROVIDER_ACCOUNT
    from evo_agents.hub.cli.secret import SECRET

    SECRET.attach(hsub)
    ACCOUNT.attach(hsub)
    MODEL_CATALOG.attach(hsub)  # the providers an account is added for
    PROVIDER_ACCOUNT.attach(hsub)

    from evo_agents.hub.cli.kg import KG

    KG.attach(hsub)
    _register_team_commands(hsub)


def _register_team_commands(hsub) -> None:
    """The client's groups from hub mcp to hub emails."""
    from evo_agents.hub.cli.mcp import MCP

    MCP.attach(hsub)

    # the commands of the evo-hub plugin: its hooks, and `hub send`, which the hooks start in the background
    from evo_agents.hub.cli.hook import HOOK
    from evo_agents.hub.cli.send import SEND

    HOOK.attach(hsub)
    SEND.attach(hsub)

    from evo_agents.hub.cli_agents import register_agents

    register_agents(hsub)

    from evo_agents.hub.cli_systems import register as register_systems

    register_systems(hsub)

    from evo_agents.hub.cli_cron import register as register_cron

    register_cron(hsub)

    from evo_agents.hub.cli_chats import register_chats

    register_chats(hsub)

    from evo_agents.hub.cli.plugin import PLUGIN

    PLUGIN.attach(hsub)

    from evo_agents.hub.cli.market import MARKET

    MARKET.attach(hsub)

    from evo_agents.hub.cli_srs import register_srs

    register_srs(hsub)

    from evo_agents.hub.cli_docs import register_docs

    register_docs(hsub)

    from evo_agents.hub.cli_tasks import register_tasks

    register_tasks(hsub)

    from evo_agents.hub.cli_canvas import register_canvas

    register_canvas(hsub)

    from evo_agents.hub.cli_signals import register_signals

    register_signals(hsub)

    from evo_agents.hub.cli_context import register_context

    register_context(hsub)

    from evo_agents.hub.cli_sessions import register_sessions

    register_sessions(hsub)

    from evo_agents.hub.cli.email import EMAIL

    EMAIL.attach(hsub)


def _register_steering_commands(hsub) -> None:
    """The client's groups from hub release to hub inbox."""
    from evo_agents.hub.cli.curator.release import RELEASE

    RELEASE.attach(hsub)

    from evo_agents.hub.cli.policy import POLICY

    POLICY.attach(hsub)

    from evo_agents.hub.cli.goal import GOAL

    GOAL.attach(hsub)

    from evo_agents.hub.cli.problem import PROBLEM

    PROBLEM.attach(hsub)

    from evo_agents.hub.cli.opportunity import OPPORTUNITY

    OPPORTUNITY.attach(hsub)

    from evo_agents.hub.cli_intake import register_intake

    register_intake(hsub)

    from evo_agents.hub.cli_merge import register_merge

    register_merge(hsub)

    from evo_agents.hub.cli_loop import register_loop

    register_loop(hsub)

    from evo_agents.hub.cli.autonomy import AUTONOMY
    from evo_agents.hub.cli.org import ORG

    ORG.attach(hsub)
    AUTONOMY.attach(hsub)

    from evo_agents.hub.cli_browser import register_browser

    register_browser(hsub)
    from evo_agents.hub.cli.stop import STOP

    STOP.attach(hsub)
    from evo_agents.hub.cli.run_cause import RUN_CAUSE

    RUN_CAUSE.attach(hsub)
    from evo_agents.hub.cli.inbox import INBOX

    INBOX.attach(hsub)


def register(sub) -> None:
    hub = sub.add_parser("hub", help="team hub for memories, plans, skills and knowledge graphs")
    hsub = hub.add_subparsers(dest="hub_command", required=True)
    for node in SERVER:
        node.attach(hsub)

    _register_client_commands(hsub)

    from evo_agents.hub.cli.openapi import OPENAPI

    OPENAPI.attach(hsub)

    from evo_agents.hub.cli.contract import CONTRACT

    CONTRACT.attach(hsub)
    OWN_COMMANDS.clear()
    OWN_COMMANDS.update(hsub.choices)

    from evo_agents.hub.cli.extensions import EXTENSIONS

    EXTENSIONS.attach(hsub)  # last: a plugin whose id is a command of the CLI's own is left out
