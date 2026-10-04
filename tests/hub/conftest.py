import importlib.util

import pytest

from tests.hub import pg

if pg.DSN:
    _missing = [name for name in pg.SERVER_STACK if importlib.util.find_spec(name) is None]
    if _missing:  # a test Postgres without the server stack is a broken setup, not a reason to skip
        raise pytest.UsageError(
            f"EVO_HUB_TEST_DSN is set but {', '.join(_missing)} is missing: "
            "python -m pip install -e '.[test,hub-server]'"
        )


@pytest.fixture(scope="session")
def pg_server():
    """The admin DSN of a Postgres that answers, after waiting up to 30 s for it. At the end of the session
    the server's own database must still hold no hub table."""
    if not pg.DSN:
        pytest.skip(pg.SKIP_REASON)
    pg.wait_ready()
    yield pg.DSN
    with pg.admin() as conn:
        stray = conn.execute("SELECT to_regclass('public.alembic_version')").fetchone()[0]
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
