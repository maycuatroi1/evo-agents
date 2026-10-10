import importlib.util
import os

import pytest


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
