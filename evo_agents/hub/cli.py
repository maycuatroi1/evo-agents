"""``evo-agents hub`` subcommands.

``hub serve``, ``hub worker`` and ``hub migrate`` need the hub-server extra. This module imports none of it when
loaded, so ``evo-agents`` keeps starting on a core install; each command imports the extra when it runs and names
the pip command when it is missing. These commands log JSON lines to stderr from their first line on: a
configuration error is a log line naming the variable, and an unexpected exception is a log line with its
traceback, secrets removed, rather than a bare traceback from the interpreter.

The client commands (``login``, ``logout``, ``whoami``, ``token``, ``admin``, ``project``, ``registry``, ``plan``,
``memory``, ``skills``, ``kg``, ``mcp``, ``hook``) are registered by ``cli_client`` and need nothing beyond the core
package, so they work on a core install. ``hub openapi`` (``openapi``) and ``hub contract print`` (``contract``) need
the extra for the API document; ``hub contract check`` does not.
"""

from __future__ import annotations

import argparse
import functools
import logging

log = logging.getLogger("evo_agents.hub")

EXTRA_HINT = "python -m pip install 'evo-ak[hub-server]'"
EXIT_FAILED = 1
EXIT_USAGE = 2


def _server_command(func):
    """Configure JSON logging first, and turn every failure of ``func`` into a log line and an exit code."""

    @functools.wraps(func)
    def run(args) -> int:
        from evo_agents.hub.config import ConfigError, load_log_level
        from evo_agents.hub.log import configure_logging

        configure_logging("INFO")
        try:
            configure_logging(load_log_level())
        except ConfigError as exc:
            return _config_error(exc)
        try:
            return func(args)
        except Exception:
            log.exception(f"hub {args.hub_command} failed")
            return EXIT_FAILED

    return run


def _config_error(exc) -> int:
    log.error(str(exc), extra={"variable": exc.variable})
    return EXIT_USAGE


def _missing_extra(exc: ImportError, args) -> int:
    log.error(
        f"hub {args.hub_command} needs the hub-server extra (missing module {exc.name}): {EXTRA_HINT}",
        extra={"missing_module": exc.name},
    )
    return EXIT_USAGE


@_server_command
def cmd_serve(args) -> int:
    from evo_agents.hub.config import ConfigError, load_config

    try:
        config = load_config(dsn=args.dsn, data_dir=args.data_dir, host=args.host, port=args.port)
    except ConfigError as exc:
        return _config_error(exc)
    try:
        import uvicorn

        from evo_agents.hub.server.app import create_app, init_sentry
    except ImportError as exc:
        return _missing_extra(exc, args)
    init_sentry(config)
    # A failed start (Postgres unreachable, migration refused) makes uvicorn exit with status 3.
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        lifespan="on",
        log_config=None,  # uvicorn's loggers propagate to the JSON handler
        access_log=False,  # replaced by the app's own access line, which leaves out query strings
        server_header=False,
        timeout_graceful_shutdown=10,
    )
    return 0


@_server_command
def cmd_worker(args) -> int:
    from evo_agents.hub.config import ConfigError, load_config

    try:
        config = load_config(dsn=args.dsn, data_dir=args.data_dir)
    except ConfigError as exc:
        return _config_error(exc)
    missing = config.blob_store_missing()
    if missing:
        message = f"{missing[0]} is not set: hub worker needs the blob store ({', '.join(missing)})"
        return _config_error(ConfigError(missing[0], message))
    try:
        from evo_agents.hub import worker
        from evo_agents.hub.server.app import init_sentry
    except ImportError as exc:
        return _missing_extra(exc, args)
    init_sentry(config)
    return worker.main(config, args.concurrency)


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, not {value!r}")
    return number


@_server_command
def cmd_migrate(args) -> int:
    from evo_agents.hub.config import ConfigError, load_dsn
    from evo_agents.hub.log import redact_dsn

    try:
        dsn = load_dsn(dsn=args.dsn)
    except ConfigError as exc:
        return _config_error(exc)
    try:
        import psycopg
        from sqlalchemy.exc import DBAPIError

        from evo_agents.hub.migrate import MigrationError, migrate
    except ImportError as exc:
        return _missing_extra(exc, args)
    try:
        migrate(dsn)
    except MigrationError as exc:
        log.error(f"migration refused: {exc}", extra={"db": redact_dsn(dsn)})
        return EXIT_FAILED
    except (DBAPIError, psycopg.Error) as exc:  # unreachable, refused, or a revision failed: nothing committed
        log.error(f"migration failed: {type(exc).__name__}: {exc}", extra={"db": redact_dsn(dsn)})
        return EXIT_FAILED
    return 0


def register(sub) -> None:
    hub = sub.add_parser("hub", help="team hub for memories, plans, skills and knowledge graphs")
    hsub = hub.add_subparsers(dest="hub_command", required=True)
    dsn_help = "Postgres DSN (default: $EVO_HUB_DSN; prefer the variable, a command line is visible to ps)"

    serve = hsub.add_parser("serve", help="run the hub server (needs the hub-server extra and Postgres)")
    serve.add_argument("--dsn", help=dsn_help)
    serve.add_argument(
        "--data-dir",
        help="cache directory, rebuilt from Postgres and the blob store when lost "
        "(default: $EVO_HUB_DATA_DIR or ~/.evo/hub-server/cache)",
    )
    serve.add_argument("--host", help="interface to listen on (default: $EVO_HUB_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port to listen on (default: $EVO_HUB_PORT or 8080)")
    serve.set_defaults(func=cmd_serve)

    worker = hsub.add_parser(
        "worker", help="run background jobs from the queue in Postgres (needs the hub-server extra, Postgres and R2)"
    )
    worker.add_argument("--dsn", help=dsn_help)
    worker.add_argument(
        "--data-dir",
        help="cache directory, rebuilt from Postgres and the blob store when lost "
        "(default: $EVO_HUB_DATA_DIR or ~/.evo/hub-server/cache)",
    )
    worker.add_argument("--concurrency", type=_positive, default=1, help="jobs run at the same time (default: 1)")
    worker.set_defaults(func=cmd_worker)

    migrate = hsub.add_parser("migrate", help="bring the hub database to this release's schema, then exit")
    migrate.add_argument("--dsn", help=dsn_help)
    migrate.set_defaults(func=cmd_migrate)

    from evo_agents.hub.cli_client import register_client

    register_client(hsub)

    from evo_agents.hub.openapi import register as register_openapi

    register_openapi(hsub)

    from evo_agents.hub.contract import register as register_contract

    register_contract(hsub)
