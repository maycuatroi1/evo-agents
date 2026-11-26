"""``evo-agents hub serve|worker|deliver|migrate|plugin-host|storage``: the commands that run a part of the hub server.

They need the hub-server extra, which this module imports only when a command runs: ``evo-agents`` keeps starting
on a core install, and a command whose extra is missing names the pip command. :class:`ServerCommand` is their
Template Method: it logs JSON lines to stderr from the first line on, so a configuration error is a log line naming
the variable (exit status 2) and an unexpected exception a log line with its traceback, secrets removed (exit
status 1), rather than a bare traceback from the interpreter. ``hub openapi`` and ``hub contract print`` are server
commands too, for the API document.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from evo_agents.clikit import Arg, Command, Context, Group, Result

log = logging.getLogger("evo_agents.hub")

EXTRA_HINT = "python -m pip install 'evo-ak[hub-server]'"
EXIT_FAILED = 1
EXIT_USAGE = 2
DSN = Arg("--dsn", help="Postgres DSN (default: $EVO_HUB_DSN; prefer the variable, a command line is visible to ps)")
DATA_DIR = Arg(
    "--data-dir",
    help="cache directory, rebuilt from Postgres and the blob store when lost "
    "(default: $EVO_HUB_DATA_DIR or ~/.evo/hub-server/cache)",
)


class MissingExtra(Exception):
    """A module of the hub-server extra failed to import: ``error`` is the ImportError."""

    def __init__(self, error: ImportError):
        super().__init__(str(error))
        self.error = error


class Refused(Exception):
    """A failure the command explains in one log line, and its exit status."""

    def __init__(self, message: str, status: int = EXIT_USAGE):
        super().__init__(message)
        self.status = status


def config_error(exc) -> int:
    log.error(str(exc), extra={"variable": exc.variable})
    return EXIT_USAGE


def missing_extra(exc: ImportError, args) -> int:
    log.error(
        f"hub {args.hub_command} needs the hub-server extra (missing module {exc.name}): {EXTRA_HINT}",
        extra={"missing_module": exc.name},
    )
    return EXIT_USAGE


def positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, not {value!r}")
    return number


class ServerCommand(Command[Context]):
    """A command of the hub server: JSON logging configured first, and every failure a log line and an exit status.

    ``run`` raises :class:`MissingExtra` when the extra is missing, a ``ConfigError`` for a variable, and
    :class:`Refused` for anything else it explains; it writes its own lines to ``ctx.out`` as it goes."""

    def context(self, args: argparse.Namespace) -> Context:
        return Context(args)

    def __call__(self, args: argparse.Namespace) -> int:
        from evo_agents.hub.config import ConfigError, load_log_level
        from evo_agents.hub.log import configure_logging

        configure_logging("INFO")
        try:
            configure_logging(load_log_level())
            return super().__call__(args)
        except ConfigError as exc:
            return config_error(exc)
        except MissingExtra as exc:
            return missing_extra(exc.error, args)
        except Refused as exc:
            log.error(str(exc))
            return exc.status
        except Exception:
            log.exception(f"hub {args.hub_command} failed")
            return EXIT_FAILED


def config(args: argparse.Namespace, **options):
    """The hub's configuration from ``--dsn`` and ``--data-dir`` and the environment."""
    from evo_agents.hub.config import load_config

    return load_config(dsn=args.dsn, data_dir=args.data_dir, **options)


class Serve(ServerCommand):
    name = "serve"
    help = "run the hub server (needs the hub-server extra and Postgres)"
    arguments = (
        DSN,
        DATA_DIR,
        Arg("--host", help="interface to listen on (default: $EVO_HUB_HOST or 127.0.0.1)"),
        Arg("--port", type=int, help="port to listen on (default: $EVO_HUB_PORT or 8080)"),
    )

    def run(self, ctx: Context) -> Result:
        settings = config(ctx.args, host=ctx.args.host, port=ctx.args.port)
        try:
            import uvicorn

            from evo_agents.hub.server.app import create_app, init_sentry
        except ImportError as exc:
            raise MissingExtra(exc) from exc
        init_sentry(settings)
        # A failed start (Postgres unreachable, migration refused) makes uvicorn exit with status 3.
        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            lifespan="on",
            log_config=None,  # uvicorn's loggers propagate to the JSON handler
            access_log=False,  # replaced by the app's own access line, which leaves out query strings
            forwarded_allow_ips=settings.forwarded_allow_ips,  # None keeps uvicorn's default: the loopback addresses
            server_header=False,
            timeout_graceful_shutdown=10,
        )
        return Result(None)


class Worker(ServerCommand):
    name = "worker"
    help = "run background jobs from the queue in Postgres (needs the hub-server extra, Postgres and R2)"
    arguments = (
        DSN,
        DATA_DIR,
        Arg("--concurrency", type=positive, default=1, help="jobs run at the same time (default: 1)"),
        Arg(
            "--queue",
            action="append",
            dest="queues",
            choices=["default", "kg"],
            help="take the jobs of this queue alone, again for another: default (periodic and short jobs) or kg "
            "(knowledge graph builds); without it, every queue",
        ),
    )

    def run(self, ctx: Context) -> Result:
        from evo_agents.hub.config import ConfigError

        settings = config(ctx.args)
        missing = settings.blob_store_missing()
        if missing:
            raise ConfigError(
                missing[0], f"{missing[0]} is not set: hub worker needs the blob store ({', '.join(missing)})"
            )
        try:
            from evo_agents.hub import worker
            from evo_agents.hub.server.app import (
                cron_pass,
                entity_key,
                hub_turn,
                init_sentry,
                loop_tick,
                memory_upkeep,
                release_pass,
            )
        except ImportError as exc:
            raise MissingExtra(exc) from exc
        init_sentry(settings)
        status = worker.main(
            settings,
            ctx.args.concurrency,
            queues=ctx.args.queues,
            release_pass=release_pass,
            loop_tick=loop_tick,
            memory_upkeep=memory_upkeep,
            entity_key=entity_key,
            hub_turn=hub_turn,
            cron_pass=cron_pass,
        )
        return Result(None, status=status)


class Deliver(ServerCommand):
    name = "deliver"
    help = (
        "deliver plugin events and notifications through the event bus within seconds of their commit "
        "(needs the hub-server extra, Postgres and EVO_HUB_BROKER_URL); several may run"
    )
    arguments = (DSN, DATA_DIR)

    def run(self, ctx: Context) -> Result:
        from evo_agents.hub.config import ConfigError

        settings = config(ctx.args)
        if settings.broker_url is None:
            message = (
                "EVO_HUB_BROKER_URL is not set: hub deliver carries what the event bus carries, and the bus is off"
            )
            raise ConfigError("EVO_HUB_BROKER_URL", message)
        try:
            import aio_pika  # noqa: F401

            from evo_agents.hub import deliverer
            from evo_agents.hub.server.app import init_sentry
        except ImportError as exc:
            raise MissingExtra(exc) from exc
        init_sentry(settings)
        return Result(None, status=deliverer.main(settings))


class Migrate(ServerCommand):
    name = "migrate"
    help = "bring the hub database to this release's schema, then exit"
    arguments = (DSN,)

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        which = parser.add_mutually_exclusive_group()
        which.add_argument(
            "--plugin",
            metavar="ID",
            help="run the chain of migrations of core plugin ID alone, on a hub database already at this release's head",
        )
        which.add_argument(
            "--status",
            action="store_true",
            help="print where the hub's chain and each core plugin's stand against their heads, and change nothing",
        )

    def run(self, ctx: Context) -> Result:
        from evo_agents.hub.config import load_dsn
        from evo_agents.hub.log import redact_dsn

        dsn = load_dsn(dsn=ctx.args.dsn)
        try:
            import psycopg
            from sqlalchemy.exc import DBAPIError

            from evo_agents.hub.migrate import MigrationError, migrate, status
        except ImportError as exc:
            raise MissingExtra(exc) from exc
        try:
            if getattr(ctx.args, "status", False):
                return Result(None, status=self.print_status(ctx, status(dsn)))
            done = migrate(dsn, only=getattr(ctx.args, "plugin", None))
            for chain in done.plugins:
                if chain.error is not None:
                    log.warning(f"plugin chain left as it was: {chain.error}", extra={"plugin": chain.plugin_id})
                else:
                    log.info("plugin chain migrated", extra={"plugin": chain.plugin_id, "applied": list(chain.applied)})
        except MigrationError as exc:
            log.error(f"migration refused: {exc}", extra={"db": redact_dsn(dsn)})
            return Result(None, status=EXIT_FAILED)
        except (DBAPIError, psycopg.Error) as exc:  # unreachable, refused, or a revision failed: nothing committed
            log.error(f"migration failed: {type(exc).__name__}: {exc}", extra={"db": redact_dsn(dsn)})
            return Result(None, status=EXIT_FAILED)
        return Result(None)

    @staticmethod
    def print_status(ctx: Context, chains) -> int:
        """Each chain's recorded revision against its head, one line each: 0 when every chain is at its head."""
        behind = 0
        for chain in chains:
            at = ",".join(chain.recorded) or "-"
            if chain.error is not None:
                state, behind = f"error: {chain.error}", behind + 1
            elif chain.head in chain.recorded:
                state = "at head"
            else:
                state, behind = f"behind, head {chain.head}", behind + 1
            print(f"{chain.chain}\t{chain.version_table}\t{at}\t{state}", file=ctx.out)
        return EXIT_FAILED if behind else 0


class PluginHost(ServerCommand):
    name = "plugin-host"
    help = "run the backends of isolated plugins, apart from the hub, holding none of its secrets (hub-server extra)"
    arguments = (
        Arg("--hub-url", help="the hub's URL to pull plugins from (default: $EVO_HUB_PUBLIC_URL)"),
        Arg(
            "--data-dir",
            help="where plugins are placed and run (default: $EVO_HUB_DATA_DIR/plugins or ~/.evo/plugin-host)",
        ),
        Arg("--host", default="0.0.0.0", help="interface to listen on (default: 0.0.0.0)"),
        Arg("--port", type=int, help="port to listen on (default: 8090)"),
        Arg(
            "--allow-shared-user",
            action="store_true",
            help="not as root, run backends under this user anyway; a developer's machine only (docs/plugins.md)",
        ),
    )

    def run(self, ctx: Context) -> Result:
        try:
            from evo_agents.hub.plugin_host_service import load_host_config, run_service
        except ImportError as exc:
            raise MissingExtra(exc) from exc
        try:
            settings = load_host_config(ctx.args)
        except ValueError as exc:
            raise Refused(str(exc)) from exc
        return Result(None, status=run_service(settings))


# hub storage

ENV_HELP = "an environment file of EVO_HUB_S3_* variables describing the store (default: this process's)"


class StorageCommand(ServerCommand):
    """A command over blob stores, each described by an environment file of EVO_HUB_S3_* variables."""

    @staticmethod
    def env(path: str | None) -> dict[str, str]:
        """The variables a store is read from: those of the environment file at ``path``, else the process's."""
        from evo_agents.hub.blob_migrate import read_env_file

        return dict(os.environ) if path is None else read_env_file(Path(path).expanduser())

    def stores(self, args: argparse.Namespace, *paths: str | None) -> list:
        """The stores the environment files at ``paths`` describe (None: the process environment)."""
        try:
            from evo_agents.hub.blob_migrate import store_from_env
        except ImportError as exc:
            raise MissingExtra(exc) from exc
        stores = []
        try:
            for path in paths:
                stores.append(store_from_env(self.env(path), concurrency=getattr(args, "parallel", 8)))
        except (OSError, ValueError) as exc:  # the file is missing or malformed; the error names no value
            raise Refused(f"hub {args.hub_command}: {exc}") from exc
        finally:
            if len(stores) < len(paths):
                for store in stores:
                    store.close()
        return stores


class StorageCheck(StorageCommand):
    name = "check"
    help = "put, copy, read, list and delete one object of random bytes, as the hub does, and time each"
    arguments = (
        Arg("--env", metavar="FILE", help=ENV_HELP),
        Arg("--create-bucket", action="store_true", help="create the bucket first when it is missing"),
    )

    def run(self, ctx: Context) -> Result:
        (store,) = self.stores(ctx.args, ctx.args.env)
        from evo_agents.hub.blob_migrate import check, describe

        try:
            print(f"store\t{describe(store)}", file=ctx.out, flush=True)
            steps = check(store, create_bucket=ctx.args.create_bucket)
        finally:
            store.close()
        for step in steps:
            detail = f"\t{step.detail}" if step.detail else ""
            print(f"{'ok' if step.ok else 'FAILED'}\t{step.name}\t{step.millis} ms{detail}", file=ctx.out)
        return Result(None, status=0 if steps and all(step.ok for step in steps) else EXIT_FAILED)


class StorageMigrate(StorageCommand):
    name = "migrate"
    help = "copy every blob the target lacks from the source, each checked against its SHA-256; run again to resume"
    arguments = (
        Arg("--from", dest="source", metavar="FILE", help=ENV_HELP.replace("the store", "the source")),
        Arg(
            "--to",
            dest="target",
            metavar="FILE",
            required=True,
            help="an environment file of EVO_HUB_S3_* variables describing the target store",
        ),
        Arg("--parallel", type=positive, default=8, help="blobs copied at the same time (default: 8)"),
        Arg("--dry-run", action="store_true", help="count what would be copied, and copy nothing"),
        Arg("--verify", action="store_true", help="hash the target's copies too, and copy a wrong one again"),
        Arg("--scratch", metavar="DIR", help="where each blob waits on its way (default: the temp directory)"),
    )

    def run(self, ctx: Context) -> Result:
        args = ctx.args
        source, target = self.stores(args, args.source, args.target)
        from evo_agents.hub.blob_migrate import describe, migrate
        from evo_agents.hub.blobs import BlobStoreUnavailable

        try:
            print(f"from\t{describe(source)}\nto\t{describe(target)}", file=ctx.out, flush=True)
            report = migrate(
                source,
                target,
                scratch=args.scratch,
                parallel=args.parallel,
                dry_run=args.dry_run,
                verify=args.verify,
                progress=lambda so_far: log.info("blobs migrated so far", extra=so_far.as_dict()),
            )
        except ValueError as exc:
            raise Refused(f"hub storage migrate: {exc}") from exc
        except BlobStoreUnavailable as exc:  # the source's listing failed: nothing after it was tried
            raise Refused(f"hub storage migrate stopped: {exc}", EXIT_FAILED) from exc
        finally:
            source.close()
            target.close()
        for sha256, problem in report.failed:
            print(f"FAILED\t{sha256}\t{problem}", file=ctx.out)
        print(json.dumps({**report.as_dict(), "dry_run": args.dry_run}), file=ctx.out)
        return Result(None, status=EXIT_FAILED if report.failed else 0)


SERVE, WORKER, DELIVER, MIGRATE, PLUGIN_HOST = Serve(), Worker(), Deliver(), Migrate(), PluginHost()
STORAGE = Group(
    "storage",
    "check the blob store, or copy its blobs to another S3 store (hub-server extra)",
    (StorageCheck(), StorageMigrate()),
)
SERVER = (SERVE, WORKER, DELIVER, MIGRATE, PLUGIN_HOST, STORAGE)
