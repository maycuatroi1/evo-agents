"""``evo-agents kg`` subcommands."""

from __future__ import annotations

from evo_agents.clikit import Group


def tree() -> Group:
    """The ``kg`` group, every command in the order ``evo-agents kg --help`` lists them. Its modules are imported
    here, so ``kg hook`` (``evo_agents.kg.cli.hook``) loads none of them."""
    from evo_agents.kg.cli.bench import BENCH
    from evo_agents.kg.cli.bind import BIND
    from evo_agents.kg.cli.build import BUILD
    from evo_agents.kg.cli.claude_entities import CLAUDE_ENTITIES
    from evo_agents.kg.cli.connector import CONNECTOR
    from evo_agents.kg.cli.eval import EVAL
    from evo_agents.kg.cli.eval_questions import EVAL_QUESTIONS
    from evo_agents.kg.cli.hook import HOOK
    from evo_agents.kg.cli.init import INIT
    from evo_agents.kg.cli.links import LINKS
    from evo_agents.kg.cli.profile import PROFILE
    from evo_agents.kg.cli.prune import PRUNE
    from evo_agents.kg.cli.query import QUERY
    from evo_agents.kg.cli.rank import RANK
    from evo_agents.kg.cli.schedule import SCHEDULE
    from evo_agents.kg.cli.scrub import SCRUB
    from evo_agents.kg.cli.semantic import SEMANTIC
    from evo_agents.kg.cli.serve import SERVE
    from evo_agents.kg.cli.settings import SETTINGS
    from evo_agents.kg.cli.status import STATUS
    from evo_agents.kg.cli.strategy import STRATEGY
    from evo_agents.kg.cli.sync import SYNC

    return Group(
        "kg",
        "multi-source knowledge graph",
        [
            SYNC,
            SCHEDULE,
            STATUS,
            BIND,
            CONNECTOR,
            HOOK,
            BUILD,
            PROFILE,
            QUERY,
            SERVE,
            EVAL,
            EVAL_QUESTIONS,
            SEMANTIC,
            SETTINGS,
            INIT,
            STRATEGY,
            SCRUB,
            PRUNE,
            BENCH,
            RANK,
            LINKS,
            CLAUDE_ENTITIES,
        ],
        dest="command_name",
    )


def register(sub) -> None:
    """``evo-agents kg`` on the subparsers of ``evo-agents``."""
    tree().attach(sub)
