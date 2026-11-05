"""EVO_HUB_TEST_REQUIRE_DSN, which CI's Linux jobs set: there a hub test fails rather than skips when EVO_HUB_TEST_DSN
is missing, so a run whose Postgres went missing cannot pass with its hub tests skipped (tests/hub/pg.py). Each case
runs pytest on a hub test module in a process of its own, without the database."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.hub import pg

ROOT = Path(__file__).resolve().parents[2]

MODULE = "tests/hub/test_guard.py"  # skips at module level without the database


def run_pytest(**env: str) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if k not in ("EVO_HUB_TEST_DSN", pg.REQUIRE_ENV, "EVO_TEST_SHARD")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", MODULE],
        cwd=ROOT,
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_missing_db_fails():
    done = run_pytest(**{pg.REQUIRE_ENV: "1"})
    assert done.returncode not in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED), done.stdout + done.stderr
    assert "EVO_HUB_TEST_DSN is not" in done.stdout + done.stderr


def test_missing_db_skips_without_require():
    done = run_pytest()
    # the module skips as a whole, so pytest collects no test
    assert done.returncode in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED), done.stdout + done.stderr
    assert "skipped" in done.stdout
