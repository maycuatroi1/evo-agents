import importlib.util
import logging
import os
import tempfile

import pytest

from evo_agents.worker.logs import QUIET

# No test asks GitHub for the pull requests of a repo: a git source whose origin is on github.com reads none
# (evo_agents.kg.connectors.github). The tests of that reading turn it on and stand in for GitHub.
os.environ["EVO_KG_PULLS"] = "0"

if os.name == "nt":
    _setenv, _delenv = pytest.MonkeyPatch.setenv, pytest.MonkeyPatch.delenv

    def _setenv_home(self, name, value, prepend=None):
        _setenv(self, name, value, prepend)
        if name == "HOME":
            _setenv(self, "USERPROFILE", value)

    def _delenv_home(self, name, raising=True):
        _delenv(self, name, raising)
        if name == "HOME":
            _delenv(self, "USERPROFILE", raising=False)

    pytest.MonkeyPatch.setenv = _setenv_home
    pytest.MonkeyPatch.delenv = _delenv_home
    os.environ["HOME"] = os.environ["USERPROFILE"] = tempfile.mkdtemp(prefix="evo-home-")


@pytest.fixture(autouse=True)
def _no_hub_home_of_this_machine(monkeypatch):
    """No EVO_HUB_HOME of the shell running pytest: a test that points HOME at a temporary directory expects the
    hub login under it, and must never read, overwrite or delete the login of the machine somewhere else. Nor the
    EVO_HUB_TOKEN and EVO_HUB_URL of a run that runs as its owner, which come before any login."""
    for name in ("EVO_HUB_HOME", "EVO_HUB_TOKEN", "EVO_HUB_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_exploration(monkeypatch):
    """Tests never explore the ranking of kg_search (``evo_agents.kg.explore``): a hub agent's search would put a
    random candidate at rank 4 or 5 one time in twenty, and a test of what it finds would fail by chance. A test of
    exploration gives its explorer a rate of its own."""
    from evo_agents.kg import explore

    monkeypatch.setattr(explore, "RATE", 0.0)


@pytest.fixture(autouse=True)
def _logging_as_found():
    """Put the root logger back as the test found it. ``configure_logging`` of a hub server command
    (``cmd_migrate`` in tests/hub/test_plugin_migrations.py) and ``evo_agents.worker.logs.configure`` of the worker's
    CLI and daemon, called in the test's process, set the root logger's level to INFO and add a handler on the
    stderr of that moment: the test's capsys stream, closed when the test ends. Left so, every INFO record of a later
    test in the session, the access lines of a hub served from a thread among them, fails to write to the closed
    stream, and the "--- Logging error ---" report lands on that later test's stderr; the kg builds of tests/hub,
    which count INFO records with caplog, would also see records they never asked for."""
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


def pytest_addoption(parser):
    # pyproject.toml sets asyncio_default_fixture_loop_scope for pytest-asyncio, which some environments have
    # installed; where it is missing, declare the key so pytest does not warn that it is unknown.
    if importlib.util.find_spec("pytest_asyncio") is None:
        parser.addini("asyncio_default_fixture_loop_scope", "read by pytest-asyncio when it is installed")


def _shard():
    """``(index, total)`` from EVO_TEST_SHARD=<index>/<total>, or None when it is unset."""
    value = os.environ.get("EVO_TEST_SHARD", "").strip()
    if not value:
        return None
    try:
        index, total = (int(part) for part in value.split("/"))
    except ValueError:
        raise pytest.UsageError(f"EVO_TEST_SHARD must be <index>/<total>, such as 1/2, not {value!r}") from None
    if not 1 <= index <= total:
        raise pytest.UsageError(f"EVO_TEST_SHARD {value!r}: the index must be between 1 and {total}")
    return index, total


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    # CI splits the Linux suite into jobs that run in parallel: with EVO_TEST_SHARD=i/n a run keeps every n-th test of
    # the collection, starting at the i-th, so the n shards together run each collected test exactly once and every
    # file's tests spread over the shards. Collection order is the same in every job (pytest-xdist already needs it
    # to be), and so is the split. Runs after -k and -m have deselected theirs.
    shard = _shard()
    if shard is None:
        return
    index, total = shard
    kept = [item for position, item in enumerate(items) if position % total == index - 1]
    dropped = [item for position, item in enumerate(items) if position % total != index - 1]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept
