"""The hub's fixtures for the worker's tests: Postgres (EVO_HUB_TEST_DSN), a database per test, a fake GitHub and a
fake S3; and no variable of a worker or a run of the machine running pytest (EVO_WORKER_*, EVO_RUN_*), so a test run
inside a run of a worker never reaches that worker's state. tests/conftest.py puts the root logger back as each test
found it."""

import os

import pytest

from tests.hub.conftest import github, hub_db, hub_template, pg_server, s3  # noqa: F401

collect_ignore_glob = ["test_*.py"] if os.name == "nt" else []


@pytest.fixture(autouse=True)
def _no_worker_of_this_machine(monkeypatch):
    """The test's processes, the daemons and commands it starts among them, see none of this machine's worker: without
    EVO_WORKER_HOME, ``~/.evo/worker`` of the test's own HOME is the state directory."""
    for name in list(os.environ):
        if name.startswith(("EVO_WORKER_", "EVO_RUN_")):
            monkeypatch.delenv(name)
