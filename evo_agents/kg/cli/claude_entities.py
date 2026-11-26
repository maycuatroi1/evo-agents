"""``evo-agents kg claude-entities``: the entities step's model command (``evo_agents.kg.claude_code``)."""

from __future__ import annotations

from evo_agents.clikit import Result
from evo_agents.kg import claude_code
from evo_agents.kg.cli.base import KgCommand, KgContext, done


class KgClaudeEntities(KgCommand):
    name = claude_code.COMMAND[2]
    help = "the entities step's model command: one request on stdin, Claude Code headless, one answer on stdout"

    def run(self, ctx: KgContext) -> Result:
        return done(claude_code.main())


CLAUDE_ENTITIES = KgClaudeEntities()
