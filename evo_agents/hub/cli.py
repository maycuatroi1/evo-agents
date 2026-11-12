"""``evo-agents hub`` subcommands.

``hub serve``, ``hub worker``, ``hub migrate`` and ``hub storage`` need the hub-server extra. This module imports none
of it when loaded, so ``evo-agents`` keeps starting on a core install; each command imports the extra when it runs and
names the pip command when it is missing. These commands log JSON lines to stderr from their first line on: a
configuration error is a log line naming the variable, and an unexpected exception is a log line with its
traceback, secrets removed, rather than a bare traceback from the interpreter.

The client commands (``login``, ``logout``, ``whoami``, ``token``, ``admin``, ``project``, ``registry``, ``plan``,
``run``, ``decision``, ``notifications``, ``memory``, ``skills``, ``secret``, ``kg``, ``mcp``, ``hook``) are
registered by ``cli_client``, ``agent`` by ``cli_agents``, ``chat`` by ``cli_chats``, ``plugin`` by
``cli_plugins``, ``srs`` by ``cli_srs``, ``docs`` by ``cli_docs``, ``task`` by ``cli_tasks``, ``canvas`` by ``cli_canvas``,
``signal`` by ``cli_signals``, ``sessions`` by ``cli_sessions``, ``email`` by ``cli_emails``, ``release`` by
``release_cli``, ``policy`` by ``cli_policy``, ``goal`` by ``cli_goals``, ``problem`` by ``cli_problems``, ``opportunity`` by ``cli_opportunities``, ``intake`` by ``cli_intake``, ``merge`` by
``cli_merge``, ``loop`` by ``cli_loop``, ``org`` by ``cli_org`` and ``autonomy`` by ``cli_autonomy``; they need nothing beyond the core
package, so they work on a core install. ``hub openapi`` (``openapi``) and ``hub contract print`` (``contract``) need the extra for the API
document; ``hub contract check`` does not. The commands of plugins (``pm``, ``crm`` and ``marketing`` of the core
plugins installed with the CLI among them) come from their manifests, registered last by ``cli_plugin_commands``.
"""

from __future__ import annotations

import argparse
import functools
import logging

log = logging.getLogger("evo_agents.hub")

EXTRA_HINT = "python -m pip install 'evo-ak[hub-server]'"
EXIT_FAILED = 1
EXIT_USAGE = 2
_DSN_HELP = "Postgres DSN (default: $EVO_HUB_DSN; prefer the variable, a command line is visible to ps)"
OWN_COMMANDS: set[str] = set()  # the hub commands of the CLI itself, the plugins' left out (cli_plugin_commands)


def server_command(func):
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


def missing_extra(exc: ImportError, args) -> int:
    log.error(
        f"hub {args.hub_command} needs the hub-server extra (missing module {exc.name}): {EXTRA_HINT}",
        extra={"missing_module": exc.name},
    )
    return EXIT_USAGE


@server_command
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
        return missing_extra(exc, args)
    init_sentry(config)
    # A failed start (Postgres unreachable, migration refused) makes uvicorn exit with status 3.
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        lifespan="on",
        log_config=None,  # uvicorn's loggers propagate to the JSON handler
        access_log=False,  # replaced by the app's own access line, which leaves out query strings
        forwarded_allow_ips=config.forwarded_allow_ips,  # None keeps uvicorn's default: the loopback addresses
        server_header=False,
        timeout_graceful_shutdown=10,
    )
    return 0


@server_command
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
        from evo_agents.hub.server.app import (
            entity_key,
            hub_turn,
            init_sentry,
            loop_tick,
            memory_upkeep,
            release_pass,
        )
    except ImportError as exc:
        return missing_extra(exc, args)
    init_sentry(config)
    return worker.main(
        config,
        args.concurrency,
        queues=args.queues,
        release_pass=release_pass,
        loop_tick=loop_tick,
        memory_upkeep=memory_upkeep,
        entity_key=entity_key,
        hub_turn=hub_turn,
    )


@server_command
def cmd_deliver(args) -> int:
    from evo_agents.hub.config import ConfigError, load_config

    try:
        config = load_config(dsn=args.dsn, data_dir=args.data_dir)
    except ConfigError as exc:
        return _config_error(exc)
    if config.broker_url is None:
        message = "EVO_HUB_BROKER_URL is not set: hub deliver carries what the event bus carries, and the bus is off"
        return _config_error(ConfigError("EVO_HUB_BROKER_URL", message))
    try:
        import aio_pika  # noqa: F401

        from evo_agents.hub import deliverer
        from evo_agents.hub.server.app import init_sentry
    except ImportError as exc:
        return missing_extra(exc, args)
    init_sentry(config)
    return deliverer.main(config)


@server_command
def cmd_plugin_host(args) -> int:
    try:
        from evo_agents.hub.plugin_host_service import load_host_config, run_service
    except ImportError as exc:
        return missing_extra(exc, args)
    try:
        config = load_host_config(args)
    except ValueError as exc:
        log.error(str(exc))
        return EXIT_USAGE
    return run_service(config)


def _storage_env(path: str | None) -> dict[str, str]:
    """The variables a store is read from: those of the environment file at ``path``, else the process's."""
    import os
    from pathlib import Path

    from evo_agents.hub.blob_migrate import read_env_file

    return dict(os.environ) if path is None else read_env_file(Path(path).expanduser())


def _storage_stores(args, *paths: str | None):
    """The stores the environment files at ``paths`` describe (None: the process environment), or an exit code."""
    from evo_agents.hub.config import ConfigError

    try:
        from evo_agents.hub.blob_migrate import store_from_env
    except ImportError as exc:
        return missing_extra(exc, args)
    stores = []
    try:
        for path in paths:
            stores.append(store_from_env(_storage_env(path), concurrency=getattr(args, "parallel", 8)))
    except ConfigError as exc:
        return _config_error(exc)
    except (OSError, ValueError) as exc:  # the file is missing or malformed; the error names no value
        log.error(f"hub {args.hub_command}: {exc}")
        return EXIT_USAGE
    finally:
        if len(stores) < len(paths):
            for store in stores:
                store.close()
    return stores


@server_command
def cmd_storage_check(args) -> int:
    stores = _storage_stores(args, args.env)
    if isinstance(stores, int):
        return stores
    from evo_agents.hub.blob_migrate import check, describe

    (store,) = stores
    try:
        print(f"store\t{describe(store)}", flush=True)
        steps = check(store, create_bucket=args.create_bucket)
    finally:
        store.close()
    for step in steps:
        detail = f"\t{step.detail}" if step.detail else ""
        print(f"{'ok' if step.ok else 'FAILED'}\t{step.name}\t{step.millis} ms{detail}")
    return 0 if steps and all(step.ok for step in steps) else EXIT_FAILED


@server_command
def cmd_storage_migrate(args) -> int:
    import json

    stores = _storage_stores(args, args.source, args.target)
    if isinstance(stores, int):
        return stores
    from evo_agents.hub.blob_migrate import describe, migrate
    from evo_agents.hub.blobs import BlobStoreUnavailable

    source, target = stores
    try:
        print(f"from\t{describe(source)}\nto\t{describe(target)}", flush=True)
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
        log.error(f"hub storage migrate: {exc}")
        return EXIT_USAGE
    except BlobStoreUnavailable as exc:  # the source's listing failed: nothing after it was tried
        log.error(f"hub storage migrate stopped: {exc}")
        return EXIT_FAILED
    finally:
        source.close()
        target.close()
    for sha256, problem in report.failed:
        print(f"FAILED\t{sha256}\t{problem}")
    print(json.dumps({**report.as_dict(), "dry_run": args.dry_run}))
    return EXIT_FAILED if report.failed else 0


def positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, not {value!r}")
    return number


@server_command
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

        from evo_agents.hub.migrate import MigrationError, migrate, status
    except ImportError as exc:
        return missing_extra(exc, args)
    try:
        if getattr(args, "status", False):
            return _print_status(status(dsn))
        done = migrate(dsn, only=getattr(args, "plugin", None))
        for chain in done.plugins:
            if chain.error is not None:
                log.warning(f"plugin chain left as it was: {chain.error}", extra={"plugin": chain.plugin_id})
            else:
                log.info("plugin chain migrated", extra={"plugin": chain.plugin_id, "applied": list(chain.applied)})
    except MigrationError as exc:
        log.error(f"migration refused: {exc}", extra={"db": redact_dsn(dsn)})
        return EXIT_FAILED
    except (DBAPIError, psycopg.Error) as exc:  # unreachable, refused, or a revision failed: nothing committed
        log.error(f"migration failed: {type(exc).__name__}: {exc}", extra={"db": redact_dsn(dsn)})
        return EXIT_FAILED
    return 0


def _print_status(chains) -> int:
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
        print(f"{chain.chain}\t{chain.version_table}\t{at}\t{state}")
    return EXIT_FAILED if behind else 0


def _register_client_commands(hsub) -> None:
    """The command groups of the client, each registered by its own module, imported only when the parser is built."""
    _register_workspace_commands(hsub)
    _register_steering_commands(hsub)


def _register_workspace_commands(hsub) -> None:
    """The client's groups from hub login to hub emails."""
    from evo_agents.hub.cli_client import register_client

    register_client(hsub)

    from evo_agents.hub.cli_agents import register_agents

    register_agents(hsub)

    from evo_agents.hub.cli_systems import register as register_systems

    register_systems(hsub)

    from evo_agents.hub.cli_cron import register as register_cron

    register_cron(hsub)

    from evo_agents.hub.cli_chats import register_chats

    register_chats(hsub)

    from evo_agents.hub.cli_plugins import register_plugins

    register_plugins(hsub)

    from evo_agents.hub.cli_market import register_market

    register_market(hsub)

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

    from evo_agents.hub.cli_emails import register_emails

    register_emails(hsub)


def _register_steering_commands(hsub) -> None:
    """The client's groups from hub release to hub inbox."""
    from evo_agents.hub.release_cli import register_release

    register_release(hsub)

    from evo_agents.hub.cli_policy import register_policy

    register_policy(hsub)

    from evo_agents.hub.cli_goals import register_goals

    register_goals(hsub)

    from evo_agents.hub.cli_problems import register_problems

    register_problems(hsub)

    from evo_agents.hub.cli_opportunities import register_opportunities

    register_opportunities(hsub)

    from evo_agents.hub.cli_intake import register_intake

    register_intake(hsub)

    from evo_agents.hub.cli_merge import register_merge

    register_merge(hsub)

    from evo_agents.hub.cli_loop import register_loop

    register_loop(hsub)

    from evo_agents.hub.cli_autonomy import register_autonomy
    from evo_agents.hub.cli_org import register_org

    register_org(hsub)
    register_autonomy(hsub)

    from evo_agents.hub.cli_browser import register_browser

    register_browser(hsub)
    from evo_agents.hub.cli_stop import register_stop

    register_stop(hsub)
    from evo_agents.hub.cli_run_cause import register_run_cause

    register_run_cause(hsub)
    from evo_agents.hub.cli_inbox import register_inbox

    register_inbox(hsub)


def register(sub) -> None:
    hub = sub.add_parser("hub", help="team hub for memories, plans, skills and knowledge graphs")
    hsub = hub.add_subparsers(dest="hub_command", required=True)
    _register_serve(hsub)
    _register_worker(hsub)
    _register_deliver(hsub)
    _register_migrate(hsub)
    _register_plugin_host(hsub)
    _register_storage(hsub)

    _register_client_commands(hsub)

    from evo_agents.hub.openapi import register as register_openapi

    register_openapi(hsub)

    from evo_agents.hub.contract import register as register_contract

    register_contract(hsub)
    OWN_COMMANDS.clear()
    OWN_COMMANDS.update(hsub.choices)

    from evo_agents.hub.cli_plugin_commands import register_plugin_commands

    register_plugin_commands(hsub)  # last: a plugin whose id is a command of the CLI's own is left out


def _register_serve(hsub) -> None:
    serve = hsub.add_parser("serve", help="run the hub server (needs the hub-server extra and Postgres)")
    serve.add_argument("--dsn", help=_DSN_HELP)
    serve.add_argument(
        "--data-dir",
        help="cache directory, rebuilt from Postgres and the blob store when lost "
        "(default: $EVO_HUB_DATA_DIR or ~/.evo/hub-server/cache)",
    )
    serve.add_argument("--host", help="interface to listen on (default: $EVO_HUB_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port to listen on (default: $EVO_HUB_PORT or 8080)")
    serve.set_defaults(func=cmd_serve)


def _register_worker(hsub) -> None:
    worker = hsub.add_parser(
        "worker", help="run background jobs from the queue in Postgres (needs the hub-server extra, Postgres and R2)"
    )
    worker.add_argument("--dsn", help=_DSN_HELP)
    worker.add_argument(
        "--data-dir",
        help="cache directory, rebuilt from Postgres and the blob store when lost "
        "(default: $EVO_HUB_DATA_DIR or ~/.evo/hub-server/cache)",
    )
    worker.add_argument("--concurrency", type=positive, default=1, help="jobs run at the same time (default: 1)")
    worker.add_argument(
        "--queue",
        action="append",
        dest="queues",
        choices=["default", "kg"],
        help="take the jobs of this queue alone, again for another: default (periodic and short jobs) or kg "
        "(knowledge graph builds); without it, every queue",
    )
    worker.set_defaults(func=cmd_worker)


def _register_deliver(hsub) -> None:
    deliver = hsub.add_parser(
        "deliver",
        help="deliver plugin events and notifications through the event bus within seconds of their commit "
        "(needs the hub-server extra, Postgres and EVO_HUB_BROKER_URL); several may run",
    )
    deliver.add_argument("--dsn", help=_DSN_HELP)
    deliver.add_argument(
        "--data-dir",
        help="cache directory, rebuilt from Postgres and the blob store when lost "
        "(default: $EVO_HUB_DATA_DIR or ~/.evo/hub-server/cache)",
    )
    deliver.set_defaults(func=cmd_deliver)


def _register_migrate(hsub) -> None:
    migrate = hsub.add_parser("migrate", help="bring the hub database to this release's schema, then exit")
    migrate.add_argument("--dsn", help=_DSN_HELP)
    which = migrate.add_mutually_exclusive_group()
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
    migrate.set_defaults(func=cmd_migrate)


def _register_plugin_host(hsub) -> None:
    plugin_host = hsub.add_parser(
        "plugin-host",
        help="run the backends of isolated plugins, apart from the hub, holding none of its secrets (hub-server extra)",
    )
    plugin_host.add_argument("--hub-url", help="the hub's URL to pull plugins from (default: $EVO_HUB_PUBLIC_URL)")
    plugin_host.add_argument(
        "--data-dir", help="where plugins are placed and run (default: $EVO_HUB_DATA_DIR/plugins or ~/.evo/plugin-host)"
    )
    plugin_host.add_argument("--host", default="0.0.0.0", help="interface to listen on (default: 0.0.0.0)")
    plugin_host.add_argument("--port", type=int, help="port to listen on (default: 8090)")
    plugin_host.add_argument(
        "--allow-shared-user",
        action="store_true",
        help="not as root, run backends under this user anyway; a developer's machine only (docs/plugins.md)",
    )
    plugin_host.set_defaults(func=cmd_plugin_host)


def _register_storage(hsub) -> None:
    storage = hsub.add_parser(
        "storage", help="check the blob store, or copy its blobs to another S3 store (hub-server extra)"
    )
    ssub = storage.add_subparsers(dest="storage_command", required=True)
    env_help = "an environment file of EVO_HUB_S3_* variables describing the store (default: this process's)"
    check = ssub.add_parser(
        "check", help="put, copy, read, list and delete one object of random bytes, as the hub does, and time each"
    )
    check.add_argument("--env", metavar="FILE", help=env_help)
    check.add_argument("--create-bucket", action="store_true", help="create the bucket first when it is missing")
    check.set_defaults(func=cmd_storage_check)
    move = ssub.add_parser(
        "migrate",
        help="copy every blob the target lacks from the source, each checked against its SHA-256; run again to resume",
    )
    move.add_argument("--from", dest="source", metavar="FILE", help=env_help.replace("the store", "the source"))
    move.add_argument(
        "--to",
        dest="target",
        metavar="FILE",
        required=True,
        help="an environment file of EVO_HUB_S3_* variables describing the target store",
    )
    move.add_argument("--parallel", type=positive, default=8, help="blobs copied at the same time (default: 8)")
    move.add_argument("--dry-run", action="store_true", help="count what would be copied, and copy nothing")
    move.add_argument("--verify", action="store_true", help="hash the target's copies too, and copy a wrong one again")
    move.add_argument("--scratch", metavar="DIR", help="where each blob waits on its way (default: the temp directory)")
    move.set_defaults(func=cmd_storage_migrate)
