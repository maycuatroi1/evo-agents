"""``evo-agents worker join --url URL --code CODE``: trade a pairing code from the web, or a pool's join token
(``--token``, or EVO_JOIN_TOKEN), for a worker token. It needs the worker extra (aiohttp)."""

from __future__ import annotations

import asyncio

from evo_agents.clikit import Arg, Result, Text
from evo_agents.hub.client import check_url
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext, hubapi
from evo_agents.worker.membership import config_of, host_facts, joined, refuse_rejoin
from evo_agents.worker.pool_boot import JOIN_TOKEN_ENV, join_secret


class Join(WorkerCommand):
    name = "join"
    help = "join a hub with a pairing code from its web, or a pool's join token"
    arguments = (
        Arg("--url", required=True, help="the hub, such as https://hub.example.org"),
        Arg("--code", help="the pairing code, XXXX-XXXX; it works once, for 10 minutes"),
        Arg("--token", help=f"instead of a code: a pool's join token, evj_... (or {JOIN_TOKEN_ENV})"),
        Arg("--replace", action="store_true", help="join even when this machine is a worker already"),
    )

    def run(self, ctx: WorkerContext) -> Result:
        refuse_rejoin(ctx.home, ctx.args.replace)
        url = check_url(ctx.args.url)
        code, token = join_secret(ctx.args)
        api = hubapi()

        async def join() -> dict:
            async with api.new_session() as session:
                return await api.WorkerHub(url, None, session).join(code, host_facts(), token=token)

        try:
            answer = asyncio.run(join())
        except api.HubProblem as exc:
            if exc.status == 429 and exc.retry_after is not None:
                raise exc.hub_error(f"{exc} (the hub says to wait {int(exc.retry_after)} seconds)") from None
            raise exc.hub_error() from None
        config, worker_token = config_of(url, answer)
        return Result(None, Text(*joined(ctx.home, config, worker_token, "Joined")))


JOIN = Join()
