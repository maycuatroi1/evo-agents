import importlib.util
import os

import pytest

from tests.hub import pg

if pg.DSN:
    _missing = [name for name in pg.SERVER_STACK if importlib.util.find_spec(name) is None]
    if _missing:  # a test Postgres without the server stack is a broken setup, not a reason to skip
        raise pytest.UsageError(
            f"EVO_HUB_TEST_DSN is set but {', '.join(_missing)} is missing: "
            "python -m pip install -e '.[test,hub-server]'"
        )


@pytest.fixture(autouse=True)
def _no_worker_of_this_machine(monkeypatch):
    """No variable of a worker or a run of the machine running pytest (EVO_WORKER_*, EVO_RUN_*): ``evo-agents hub
    mcp`` sends a worker's token inside a run, and a test run inside a run of a worker must never send that worker's
    token anywhere, nor read its state."""
    for name in list(os.environ):
        if name.startswith(("EVO_WORKER_", "EVO_RUN_")):
            monkeypatch.delenv(name)


@pytest.fixture(scope="session")
def pg_server():
    """The admin DSN of a Postgres that answers, after waiting up to 30 s for it. At the end of the session
    the server's own database must still hold no hub table."""
    if not pg.DSN:
        pytest.skip(pg.SKIP_REASON)
    pg.wait_ready()
    yield pg.DSN
    from sqlalchemy import func, select

    with pg.admin_engine().connect() as conn:
        stray = conn.execute(select(func.to_regclass("public.alembic_version"))).scalar_one()
    assert stray is None, "a test migrated the database named by EVO_HUB_TEST_DSN instead of its own"


@pytest.fixture
def hub_db(pg_server):
    """A fresh database for one test, dropped afterwards. A connection the test leaves open fails it."""
    db = pg.create_database()
    try:
        yield db
        leaked = pg.wait_no_backends(db.name)
    finally:
        pg.drop_database(db)
    if leaked:
        pytest.fail(f"{leaked} connection(s) to {db.name} still open after the test: a pool or connection leaked")


@pytest.fixture
def github():
    """A fake GitHub on a local port (``tests.hub.fake_github``), stopped after the test."""
    from tests.hub.fake_github import FakeGitHub

    with FakeGitHub() as fake:
        yield fake


@pytest.fixture
def s3():
    """A fake S3 on a local port with an empty bucket of its own (``tests.hub.s3``), stopped after the test."""
    from tests.hub.s3 import fake_s3

    with fake_s3() as fake:
        yield fake
