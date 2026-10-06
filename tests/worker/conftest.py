"""The hub's fixtures for the worker's tests: Postgres (EVO_HUB_TEST_DSN), a database per test, a fake GitHub and a
fake S3; and the root logger put back as each test found it."""

import logging

import pytest

from evo_agents.worker.logs import QUIET
from tests.hub.conftest import github, hub_db, pg_server, s3  # noqa: F401


@pytest.fixture(autouse=True)
def _logging_as_found():
    """Put the root logger back as the test found it. ``evo_agents.worker.logs.configure``, which the CLI and the
    daemon call in the test's process, sets the root logger's level to INFO and adds the worker's handlers; left so,
    a later test in the same session (the kg builds of tests/hub count INFO records with caplog) sees records of its
    own that it never asked for."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    quiet = {name: logging.getLogger(name).level for name in QUIET}
    yield
    for handler in list(root.handlers):
        if handler not in handlers and type(handler).__module__.startswith("evo_agents."):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    for name, value in quiet.items():
        logging.getLogger(name).setLevel(value)
