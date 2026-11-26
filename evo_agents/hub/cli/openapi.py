"""``evo-agents hub openapi``: print the hub API's OpenAPI document (``evo_agents.hub.openapi``), as a server command
(``evo_agents.hub.cli.ServerCommand``): it needs the hub-server extra, and says so in a log line when it is missing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from evo_agents.clikit import Arg, Context, Result
from evo_agents.hub import openapi
from evo_agents.hub.cli import MissingExtra, ServerCommand

log = logging.getLogger("evo_agents.hub")


class OpenApi(ServerCommand):
    name = "openapi"
    help = "print the hub API's OpenAPI document (needs the hub-server extra)"
    arguments = (Arg("--output", "-o", help="write the document to this file instead of stdout"),)

    def run(self, ctx: Context) -> Result:
        try:
            text = openapi.render(openapi.document())
        except ImportError as exc:
            raise MissingExtra(exc) from exc
        if ctx.args.output:
            Path(ctx.args.output).write_text(text, encoding="utf-8")
            log.info("openapi document written", extra={"path": ctx.args.output})
        else:
            ctx.out.write(text)
        return Result(None)


OPENAPI = OpenApi()
