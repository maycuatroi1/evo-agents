"""``evo-agents worker run``: the daemon in the foreground (``evo_agents.worker.daemon``). It needs the worker extra
(aiohttp). On a machine that is not a worker, such as one revoked with ``revoke``, it exits ``home.EXIT_REVOKED`` (3,
or what EVO_WORKER_REVOKED_EXIT says), so a service does not start it again and again."""

from __future__ import annotations

import asyncio
import logging
import os

from evo_agents.clikit import Arg, Result, Stderr, Text
from evo_agents.hub.client import HubError
from evo_agents.worker.cli.base import EXIT_FAILED, MissingExtra, WorkerCommand, WorkerContext
from evo_agents.worker.home import NotJoined, PidLock, revoked_exit
from evo_agents.worker.membership import member_hub, refresh_repos


class Run(WorkerCommand):
    name = "run"
    help = "run the daemon in the foreground: claim runs, run them, report; SIGTERM lets held runs end"
    arguments = (Arg("--quiet", action="store_true", help="log to ~/.evo/worker/worker.log only, not to stderr"),)

    def run(self, ctx: WorkerContext) -> Result:
        home = ctx.home
        try:
            config = home.load_config()
            token = home.load_token()
        except NotJoined as exc:  # not a worker, or not any more: starting the daemon again changes nothing
            return Result(None, Stderr(Text(f"error: {exc}")), revoked_exit(os.environ))
        try:
            import aiohttp  # noqa: F401

            from evo_agents.worker.daemon import Daemon
        except ImportError as exc:
            raise MissingExtra(f"missing module {exc.name}") from None
        from evo_agents.worker import logs
        from evo_agents.worker.adapter import load_adapters

        home.ensure()
        lock = PidLock(home.pid_path)
        if not lock.acquire():
            raise HubError(f"a worker daemon runs here already (pid {home.read_pid() or 'unknown'})")
        try:
            secrets = [token]
            member = member_hub(config.url)
            if member is not None and member.token:
                secrets.append(member.token)
            logs.configure(home.log_path, stderr=not ctx.args.quiet, secrets=secrets)
            if member is not None:
                refresh_repos(config, member)
                home.save(config)
            adapters = load_adapters()
            return Result(None, status=asyncio.run(Daemon(home, config, token, adapters=adapters).run()))
        except Exception:
            logging.getLogger("evo_agents.worker").exception("the worker daemon failed")
            return Result(None, status=EXIT_FAILED)
        finally:
            lock.release()


RUN = Run()
