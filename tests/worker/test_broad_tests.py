"""The broad test runs a plan run's hook refuses (``evo_agents.worker.broad_tests``), from the commands of runs #1642
and #1675 of evo-agents, which spent most of their time in tests the Judge and the release CI run again."""

from __future__ import annotations

import pytest

from evo_agents.hub import record_rule
from evo_agents.worker import broad_tests
from evo_agents.worker.wait_guard import WaitGuard

WT = "/home/evo/.evo/worker-evo-agents/worktrees/evo-agents-1642/evo-agents"
HUB_FILES = " ".join(f"tests/hub/test_kg_{name}.py" for name in "abcdefghijk")  # 11 files
REFUSED = [
    # run #1675: the whole suite against a fresh Postgres, killed at its timeout after 28 minutes
    "docker run -d --name evo1675-pg -e POSTGRES_PASSWORD=test -p 55675:5432 postgres:16 >/dev/null && sleep 1; "
    f"cd {WT} && EVO_HUB_TEST_DSN=postgresql://postgres:test@localhost:55675/postgres timeout 1700 python -m pytest "
    "-q -n auto -x -p no:randomly 2>&1 | tail -25",
    "export EVO_HUB_TEST_DSN=x; timeout 580 python -m pytest -q -n auto --ignore=tests/hub 2>&1 | tail -8",
    # run #1642: a directory, and a selection of 19 hub files under a 25-minute timeout
    "timeout 1200 python -m pytest -q -n auto tests/kg 2>&1 | tail -8",
    f"timeout 1500 python -m pytest -q -n 8 -p no:cacheprovider {HUB_FILES} > /tmp/pg1642.log 2>&1",
    f"python -m pytest -q {HUB_FILES}",
    "pytest",
    "uv run pytest -q tests/",
    ".venv/bin/python -m pytest -q 'tests/hub/test_kg_*.py'",
    "bash -c 'cd evo-agents && python -m pytest -q'",
    "timeout 30m python -m pytest -q tests/hub/test_kg_answers.py",
]
ALLOWED = [
    "python -m pytest -q -n auto tests/hub/test_kg_answers.py tests/hub/test_kg_shared_pool.py tests/hub/test_contract.py "
    "tests/test_no_raw_sql.py tests/test_structure.py && ruff check . && lint-imports",
    "timeout 600 python -m pytest -q -n auto tests/hub/test_migrate.py -k 0207",
    "python -m pytest -q 'tests/worker/test_plan_runs.py::test_session_per_step_waits_for_a_decision' 2>/dev/null",
    "timeout 600 python -m pytest --collect-only -q 2>&1 | tail -15",
    "python -m pytest -q --lf",
    "python -m pytest -q $targets",
    "evo-agents worker step 6 verify --repo evo-agents --verify 'python -m pytest -q tests/kg'",
    "ruff check . && lint-imports",
    "cd web && pnpm vitest run src/components/kg/upload",
]


@pytest.mark.parametrize("command", REFUSED)
def test_a_broad_test_run_is_refused(command):
    assert broad_tests.broad(command) is not None, command


@pytest.mark.parametrize("command", ALLOWED)
def test_a_run_of_named_tests_goes_through(command):
    assert broad_tests.broad(command) is None, command


def test_the_plan_runs_hook_refuses_a_broad_run_and_records_it():
    recorded = []
    guard = WaitGuard(record=lambda text, body: recorded.append((text, body)), run_id=1675)
    why = guard.check("Bash", {"command": REFUSED[0], "run_in_background": True})
    assert why is not None and "the whole suite" in why and "evo-agents worker step KEY verify" in why
    ((text, body),) = recorded
    assert "broad test selection" in text and (body["rule"], body["verdict"], body["run"]) == ("tests", "refused", 1675)
    assert guard.check("Bash", {"command": ALLOWED[0]}) is None and len(recorded) == 1


def test_the_plan_runs_prompt_names_the_same_limits():
    rule = record_rule.record_rule("6", "the step")
    assert f"at most {broad_tests.MAX_FILES} files a command" in rule
    assert f"a `timeout` of at most {broad_tests.MAX_SECONDS} s" in rule
