"""``evo-agents hub`` subcommands.

``hub serve``, ``hub worker``, ``hub migrate`` and ``hub storage`` need the hub-server extra. This module imports none
of it when loaded, so ``evo-agents`` keeps starting on a core install; each command imports the extra when it runs and
names the pip command when it is missing. These commands log JSON lines to stderr from their first line on: a
configuration error is a log line naming the variable, and an unexpected exception is a log line with its
traceback, secrets removed, rather than a bare traceback from the interpreter.

The client commands (``login``, ``logout``, ``whoami``, ``token``, ``admin``, ``project``, ``registry``, ``plan``,
``run``, ``decision``, ``notifications``, ``memory``, ``skills``, ``secret``, ``kg``, ``mcp``, ``hook``) are
attached by ``cli.session`` and the class modules beside it, ``agent`` by ``cli_agents``, ``chat`` by ``cli.chat``, ``plugin`` by
``cli.plugin``, ``srs`` by ``cli.srs``, ``docs`` by ``cli.docs``, ``task`` by ``cli.task``, ``canvas`` by ``cli.canvas``,
``signal`` by ``cli.signal``, ``sessions`` by ``cli.sessions``, ``email`` by ``cli.email``, ``release`` by
``cli.curator.release``, ``policy`` by ``cli.policy``, ``goal`` by the classes of ``evo_agents.hub.cli.goal``, ``problem`` by ``cli_problems``, ``opportunity`` by ``cli_opportunities``, ``intake`` by ``cli.intake``, ``merge`` by
``cli.merge``, ``loop`` by ``cli.loop``, ``org`` by ``cli.org`` and ``autonomy`` by ``cli.autonomy``; they need nothing beyond the core
package, so they work on a core install. ``hub openapi`` (``openapi``) and ``hub contract print`` (``contract``) need the extra for the API
document; ``hub contract check`` does not. The commands of plugins (``pm``, ``crm`` and ``marketing`` of the core
plugins installed with the CLI among them) come from their manifests, registered last by ``cli.extensions``.
"""

from __future__ import annotations

from evo_agents.hub.cli.server import (  # noqa: F401 (the names other modules import from here)
    EXTRA_HINT,
    SERVER,
    missing_extra,
    positive,
    server_command,
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

    from evo_agents.hub.cli.memory import MEMORY

    MEMORY.attach(hsub)

    from evo_agents.hub.cli.skills import SKILLS

    SKILLS.attach(hsub)

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
    from evo_agents.hub.mcp_proxy import register_mcp

    register_mcp(hsub)

    # the commands of the evo-hub plugin: its hooks, and `hub send`, which the hooks start in the background
    from evo_agents.hub.cli.send import register_send
    from evo_agents.hub.hooks import register_hooks

    register_hooks(hsub)
    register_send(hsub)

    from evo_agents.hub.cli_agents import register_agents

    register_agents(hsub)

    from evo_agents.hub.cli_systems import register as register_systems

    register_systems(hsub)

    from evo_agents.hub.cli_cron import register as register_cron

    register_cron(hsub)

    from evo_agents.hub.cli.chat import CHAT

    CHAT.attach(hsub)

    from evo_agents.hub.cli.plugin import PLUGIN

    PLUGIN.attach(hsub)

    from evo_agents.hub.cli.market import MARKET

    MARKET.attach(hsub)

    from evo_agents.hub.cli.srs import SRS

    SRS.attach(hsub)

    from evo_agents.hub.cli.docs import DOCS

    DOCS.attach(hsub)

    from evo_agents.hub.cli.task import TASK

    TASK.attach(hsub)

    from evo_agents.hub.cli.canvas import CANVAS

    CANVAS.attach(hsub)

    from evo_agents.hub.cli.signal import SIGNAL

    SIGNAL.attach(hsub)

    from evo_agents.hub.cli_context import register_context

    register_context(hsub)

    from evo_agents.hub.cli.sessions import SESSIONS

    SESSIONS.attach(hsub)

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

    from evo_agents.hub.cli_problems import register_problems

    register_problems(hsub)

    from evo_agents.hub.cli_opportunities import register_opportunities

    register_opportunities(hsub)

    from evo_agents.hub.cli.intake import INTAKE

    INTAKE.attach(hsub)

    from evo_agents.hub.cli.merge import MERGE

    MERGE.attach(hsub)

    from evo_agents.hub.cli.loop import LOOP

    LOOP.attach(hsub)

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

    from evo_agents.hub.openapi import register as register_openapi

    register_openapi(hsub)

    from evo_agents.hub.contract import register as register_contract

    register_contract(hsub)
    OWN_COMMANDS.clear()
    OWN_COMMANDS.update(hsub.choices)

    from evo_agents.hub.cli.extensions import EXTENSIONS

    EXTENSIONS.attach(hsub)  # last: a plugin whose id is a command of the CLI's own is left out
