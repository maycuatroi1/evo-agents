"""The worker and its queue: ``evo-agents hub migrate`` creates procrastinate's schema from the vendored SQL of the
pinned release and a second run changes nothing; ``evo-agents hub worker`` runs jobs the api deferred, lets a running
job finish on SIGTERM before it exits, and refuses to start without the blob store or Postgres; kg jobs of a project
take its lock and queue at most one.

The worker runs as a process of its own, as in production; the fake S3 runs in this process (``tests.hub.s3``)."""

import asyncio
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import procrastinate
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from procrastinate.manager import JobManager
from procrastinate.schema import SchemaManager
from sqlalchemy import Boolean, Text, cast, column, func, literal, select, table, union_all
from sqlalchemy.dialects.postgresql import REGCLASS
from sqlalchemy.types import UserDefinedType

from evo_agents.hub import jobs
from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import make_engine, open_pool
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.migrate import alembic_config, migrate
from evo_agents.hub.server.app import create_app
from evo_agents.hub.worker import queue

ROOT = Path(__file__).parents[2]
REVISION = ScriptDirectory.from_config(alembic_config()).get_revision("0004").module


class Catalog(UserDefinedType):
    """A reg* type of Postgres's catalogs, to cast an oid to the name it stands for."""

    cache_ok = True

    def __init__(self, name: str):
        self.name = name

    def get_col_spec(self, **kw):
        return self.name


# The catalogs and procrastinate's tables the tests read, described where they are read.
pg_tables = table("pg_tables", column("schemaname"), column("tablename"))
schema_columns = table(
    "columns",
    column("table_schema"),
    column("table_name", Text),
    column("column_name", Text),
    column("data_type", Text),
    column("is_nullable", Text),
    schema="information_schema",
)
pg_indexes = table("pg_indexes", column("schemaname"), column("indexdef"))
pg_trigger = table("pg_trigger", column("tgrelid"), column("tgname"), column("tgisinternal", Boolean))
pg_proc = table("pg_proc", column("oid"), column("proname", Text), column("prosrc"), column("pronamespace"))
pg_type = table("pg_type", column("typname", Text), column("typnamespace"), column("typtype"))
alembic_version = table("alembic_version", column("version_num"))
procrastinate_jobs = table(
    "procrastinate_jobs",
    column("id"),
    column("task_name"),
    column("status"),
    column("lock"),
    column("queueing_lock"),
    column("args"),
)
procrastinate_events = table("procrastinate_events", column("job_id"), column("type"), column("at"))
procrastinate_workers = table("procrastinate_workers", column("id"))


def _snapshot():
    """Every table, column, index, trigger, function, type and the revision of the public schema, as sorted rows."""
    public = cast("public", Catalog("regnamespace"))
    trigger = cast(cast(pg_trigger.c.tgrelid, REGCLASS), Text) + " " + pg_trigger.c.tgname
    function = cast(cast(pg_proc.c.oid, Catalog("regprocedure")), Text) + " " + func.md5(pg_proc.c.prosrc)
    c = schema_columns.c
    parts = union_all(
        select(literal("table").label("what"), pg_tables.c.tablename.label("detail")).where(
            pg_tables.c.schemaname == "public"
        ),
        select(literal("column"), c.table_name + "." + c.column_name + " " + c.data_type + " " + c.is_nullable).where(
            c.table_schema == "public"
        ),
        select(literal("index"), pg_indexes.c.indexdef).where(pg_indexes.c.schemaname == "public"),
        select(literal("trigger"), trigger).where(~pg_trigger.c.tgisinternal),
        select(literal("function"), function).where(pg_proc.c.pronamespace == public),
        select(literal("type"), pg_type.c.typname).where(
            pg_type.c.typnamespace == public, pg_type.c.typtype.in_(["e", "c"])
        ),
        select(literal("revision"), alembic_version.c.version_num),
    )
    return parts.order_by(parts.selected_columns.what, parts.selected_columns.detail)


SNAPSHOT = _snapshot()


def query(db, statement):
    return live.sql(db, statement)


def make_config(db, tmp_path, s3=None) -> HubConfig:
    return HubConfig(
        dsn=db.dsn,
        data_dir=tmp_path / "cache",
        pool_min_size=1,
        pool_max_size=4,
        pool_timeout=5.0,
        **(s3.config() if s3 else {}),
    )


def job(db, job_id: int) -> dict:
    j = procrastinate_jobs.c
    (row,) = query(db, select(j.task_name, j.status, j.lock, j.queueing_lock, j.args).where(j.id == job_id))
    return dict(zip(("task", "status", "lock", "queueing_lock", "args"), row, strict=True))


def wait_for(condition, timeout: float = 60.0, what: str = "the condition"):
    deadline = time.monotonic() + timeout
    while not (found := condition()):
        assert time.monotonic() < deadline, f"{what} did not happen within {timeout:.0f}s"
        time.sleep(0.2)
    return found


@dataclass
class LiveWorker:
    proc: subprocess.Popen
    log_path: Path

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8")

    def messages(self) -> list[str]:
        return [line["msg"] for line in pg.log_lines(self.log())]

    def stop(self, timeout: float = 60.0) -> int:
        self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(timeout=timeout)


@contextmanager
def running_worker(db, tmp_path: Path, s3, *args: str):
    """``hub worker ARGS`` with the test database and bucket; ready once it says so. Killed if still running."""
    log_path = tmp_path / "hub-worker.log"
    env = pg.clean_env(EVO_HUB_DSN=db.dsn, EVO_HUB_DATA_DIR=str(tmp_path / "worker-cache"), **s3.env())
    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "evo_agents", "hub", "worker", *args],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    worker = LiveWorker(proc, log_path)
    try:
        wait_for(lambda: proc.poll() is not None or "worker ready" in worker.log(), what="worker ready")
        assert proc.poll() is None, worker.log()
        yield worker
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


async def with_queue(db, func):
    """``func(JobQueue, pool)`` over a pool of its own, as the api defers."""
    pool = await open_pool(HubConfig(dsn=db.dsn, data_dir=Path("."), pool_min_size=1, pool_max_size=2))
    try:
        return await func(await JobQueue.open(pool), pool)
    finally:
        await pool.close()


# The schema


def test_hub_migrate_creates_the_procrastinate_tables_and_a_second_run_changes_nothing(hub_db):
    env = pg.clean_env(EVO_HUB_DSN=hub_db.dsn)
    first = pg.cli(["hub", "migrate"], env=env)
    assert first.returncode == 0, first.stderr
    applied = [line for line in pg.log_lines(first.stderr) if line["msg"] == "migrations applied"]
    assert applied and "0004" in applied[0]["applied"]
    tables = {row[0] for row in query(hub_db, select(pg_tables.c.tablename).where(pg_tables.c.schemaname == "public"))}
    assert pg.QUEUE_TABLES | pg.BLOB_TABLES <= tables
    types = {row[0] for row in query(hub_db, select(pg_type.c.typname).where(pg_type.c.typname.like("procrastinate%")))}
    assert {"procrastinate_job_status", "procrastinate_job_event_type", "procrastinate_job_to_defer_v1"} <= types
    functions = query(hub_db, select(func.count()).where(pg_proc.c.proname.like("procrastinate%")))[0][0]
    assert functions >= 15
    before = query(hub_db, SNAPSHOT)

    second = pg.cli(["hub", "migrate"], env=env)
    assert second.returncode == 0, second.stderr
    messages = [line["msg"] for line in pg.log_lines(second.stderr)]
    assert "schema already at head" in messages and not any(m.startswith("Running upgrade") for m in messages)
    assert query(hub_db, SNAPSHOT) == before

    async def schema_found(job_queue, pool):  # procrastinate's own check of its schema
        return await JobManager(job_queue._manager.connector).check_connection_async()

    assert asyncio.run(with_queue(hub_db, schema_found)) is True


def test_the_vendored_schema_is_the_one_of_the_pinned_procrastinate_release():
    assert procrastinate.__version__ == REVISION.PROCRASTINATE_VERSION
    pin = re.search(r'"procrastinate==([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pin and pin.group(1) == REVISION.PROCRASTINATE_VERSION
    assert REVISION.PROCRASTINATE_SQL.read_text(encoding="utf-8") == SchemaManager.get_schema()
    assert (REVISION.PROCRASTINATE_SQL.parent / "procrastinate-LICENSE.txt").is_file()


# Running jobs


def test_jobs_the_api_deferred_are_run_to_completion_by_the_worker(hub_db, tmp_path, s3):
    app = create_app(make_config(hub_db, tmp_path, s3))
    with TestClient(app) as client:
        defer = app.state.jobs.defer
        ping = client.portal.call(partial(defer, jobs.PING, note="from the api"))
        cleanup = client.portal.call(partial(defer, jobs.CLEANUP_UPLOADS))
        prune = client.portal.call(partial(defer, jobs.PRUNE_JOBS))
        # two jobs under one lock: the worker runs them one after the other although it may run three at once
        locked = [client.portal.call(partial(defer, jobs.PING, lock="kg:alpha", seconds=1)) for _ in range(2)]
    ids = [ping, cleanup, prune, *locked]
    assert all(isinstance(job_id, int) for job_id in ids)
    assert {job(hub_db, job_id)["status"] for job_id in ids} == {"todo"}

    with running_worker(hub_db, tmp_path, s3, "--concurrency", "3") as worker:
        wait_for(lambda: all(job(hub_db, job_id)["status"] == "succeeded" for job_id in ids), what="the jobs")
        assert worker.stop() == 0, worker.log()
    e = procrastinate_events.c
    events = query(
        hub_db,
        select(e.job_id, cast(e.type, Text), e.at)
        .where(e.job_id.in_(locked), cast(e.type, Text).in_(["started", "succeeded"]))
        .order_by(e.at),
    )
    first, second = locked
    started_second = next(at for job_id, kind, at in events if job_id == second and kind == "started")
    done_first = next(at for job_id, kind, at in events if job_id == first and kind == "succeeded")
    started_first = next(at for job_id, kind, at in events if job_id == first and kind == "started")
    done_second = next(at for job_id, kind, at in events if job_id == second and kind == "succeeded")
    assert started_second >= done_first or started_first >= done_second  # never both running

    log = worker.log()
    for secret in (hub_db.password, s3.secret_access_key, s3.access_key_id):
        assert secret not in log
    messages = worker.messages()
    for expected in (
        "worker starting",
        "worker ready",
        "ping",
        "stale uploads removed",
        "worker stopped, connection pool closed",
    ):
        assert expected in messages, messages
    (starting,) = [line for line in pg.log_lines(log) if line["msg"] == "worker starting"]
    assert starting["db"] == hub_db.dsn.replace(hub_db.password, "***") and starting["concurrency"] == 3
    assert (tmp_path / "worker-cache").is_dir()


def test_sigterm_lets_the_running_job_finish_before_the_worker_exits(hub_db, tmp_path, s3):
    migrate(hub_db.dsn)
    job_id = asyncio.run(with_queue(hub_db, lambda job_queue, pool: job_queue.defer(jobs.PING, seconds=3)))
    with running_worker(hub_db, tmp_path, s3) as worker:
        wait_for(lambda: job(hub_db, job_id)["status"] == "doing", what="the job starting")
        stopped_at = time.monotonic()
        assert worker.stop() == 0, worker.log()
        assert time.monotonic() - stopped_at > 1  # it waited for the job instead of dropping it
    assert job(hub_db, job_id)["status"] == "succeeded"
    messages = worker.messages()
    assert messages.index("ping") < messages.index("worker stopped, connection pool closed")
    assert query(hub_db, select(func.count()).select_from(procrastinate_workers)) == [(0,)]  # it unregistered itself


def test_a_worker_that_loses_postgres_exits_nonzero_so_it_is_restarted(hub_db, tmp_path, s3):
    with running_worker(hub_db, tmp_path, s3) as worker:
        pg.set_reachable(hub_db, False)
        try:
            code = worker.proc.wait(timeout=90)
        finally:
            pg.set_reachable(hub_db, True)
    assert code == 1, worker.log()
    lines = pg.log_lines(worker.log())
    assert any(line["level"] == "error" for line in lines)
    assert "worker stopped, connection pool closed" not in [line["msg"] for line in lines]
    for secret in (hub_db.password, s3.secret_access_key, s3.access_key_id):
        assert secret not in worker.log()


def test_kg_jobs_of_one_project_take_its_lock_and_queue_at_most_one(hub_db):
    migrate(hub_db.dsn)

    async def defer(job_queue, pool):
        first = await job_queue.defer_kg(jobs.PING, "alpha", note="first")
        again = await job_queue.defer_kg(jobs.PING, "alpha", note="again")
        other = await job_queue.defer_kg(jobs.PING, "beta")
        async with make_engine(pool).connect() as conn:  # a job deferred in a transaction that rolls back never exists
            await job_queue.defer_kg(jobs.PING, "gamma", connection=conn)
            await conn.rollback()
        return first, again, other

    first, again, other = asyncio.run(with_queue(hub_db, defer))
    assert isinstance(first, int) and again is None and isinstance(other, int)
    assert job(hub_db, first) == {
        "task": jobs.PING,
        "status": "todo",
        "lock": "kg:alpha",
        "queueing_lock": "kg:alpha",
        "args": {"note": "first"},
    }
    assert job(hub_db, other)["lock"] == job(hub_db, other)["queueing_lock"] == "kg:beta"
    assert query(hub_db, select(func.count()).select_from(procrastinate_jobs)) == [(2,)]
    assert jobs.kg_lock("alpha") == "kg:alpha"


def test_the_cleanup_and_the_pruning_are_periodic_and_queue_at_most_one():
    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.CLEANUP_UPLOADS].cron == "17 * * * *"  # hourly
    assert periodic[jobs.PRUNE_JOBS].cron == "43 3 * * *"  # daily
    assert periodic[jobs.PRUNE_KG_ARTIFACTS].cron == "31 * * * *"  # hourly
    for name in (jobs.CLEANUP_UPLOADS, jobs.PRUNE_JOBS, jobs.PRUNE_KG_ARTIFACTS):
        assert queue.tasks[name].queueing_lock == name
    assert {jobs.PING, jobs.CLEANUP_UPLOADS, jobs.PRUNE_JOBS, jobs.PRUNE_KG_ARTIFACTS} <= set(queue.tasks)


# Refusing to start


def test_the_worker_refuses_to_start_without_the_blob_store_or_postgres(tmp_path, s3):
    dsn = f"postgresql://hub:Worker-Secret-8@127.0.0.1:{pg.free_port()}/hub"
    without = pg.cli(["hub", "worker"], env=pg.clean_env(EVO_HUB_DSN=dsn))
    assert without.returncode == 2
    (line,) = pg.log_lines(without.stderr)
    assert line["variable"] == "EVO_HUB_S3_ENDPOINT" and "needs the blob store" in line["msg"]

    partial_env = {**s3.env()}
    del partial_env["EVO_HUB_S3_SECRET_ACCESS_KEY"]
    some = pg.cli(["hub", "worker"], env=pg.clean_env(EVO_HUB_DSN=dsn, **partial_env))
    assert some.returncode == 2
    assert pg.log_lines(some.stderr)[0]["variable"] == "EVO_HUB_S3_SECRET_ACCESS_KEY"

    env = pg.clean_env(EVO_HUB_DSN=dsn, EVO_HUB_DATA_DIR=str(tmp_path / "cache"), **s3.env())
    unreachable = pg.cli(["hub", "worker"], env=env, timeout=120)
    assert unreachable.returncode == 1
    (failure,) = [line for line in pg.log_lines(unreachable.stderr) if line["msg"] == "worker cannot start"]
    assert failure["db"] == dsn.replace("Worker-Secret-8", "***")
    for secret in ("Worker-Secret-8", s3.secret_access_key, s3.access_key_id):
        assert secret not in unreachable.stderr

    assert pg.cli(["hub", "worker", "--concurrency", "0"], env=env).returncode == 2


def test_the_core_cli_loads_neither_boto3_nor_procrastinate():
    code = (
        "import sys; import evo_agents.cli as cli; cli.build_parser(); "
        "print(','.join(sorted(m for m in sys.modules if m.split('.')[0] in "
        "('boto3', 'botocore', 'procrastinate', 'moto'))))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
