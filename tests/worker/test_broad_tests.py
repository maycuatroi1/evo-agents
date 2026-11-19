"""The broad test runs a plan run's hook refuses (``evo_agents.worker.broad_tests``), from the commands of runs #1642
and #1675 of evo-agents, which spent most of their time in tests the Judge and the release CI run again, and the checks
of a whole JavaScript workspace it refuses (``evo_agents.worker.broad_checks``)."""

from __future__ import annotations

import asyncio
import json

import pytest

from evo_agents.hub import record_rule
from evo_agents.worker import broad_checks, broad_tests, wait_guard
from evo_agents.worker.command_classes import without_heredocs
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


# The checks of a whole JavaScript workspace (``evo_agents.worker.broad_checks``), from two plan runs on a worker of
# 14 GB that started `pnpm check` in the background and stalled every run of the machine for close to an hour; the
# workspace has the shape of theirs: `check` calls `typecheck`, which runs turbo in every package.
ROOT_SCRIPTS = {
    "boundaries": "node tools/boundaries.mjs",
    "check": "pnpm boundaries && pnpm lint && pnpm typecheck && pnpm test",
    "lint": "biome check .",
    "typecheck": "TURBO_TELEMETRY_DISABLED=1 turbo run typecheck --concurrency=4 --output-logs=new-only",
    "test": "vitest run --root tools && TURBO_TELEMETRY_DISABLED=1 turbo run test --concurrency=4",
    "verify:all": "run-s boundaries typecheck",
    "build:ts": "tsc -b",
}
WORKSPACE_REFUSED = [
    # the command of the second run, started with run_in_background
    "pnpm install --frozen-lockfile > ../.evo-run/logs/check.log 2>&1 && pnpm check >> ../.evo-run/logs/check.log "
    '2>&1; echo "exit $?" >> ../.evo-run/logs/check.log',
    "timeout 3000 pnpm check 2>&1 | tail -30",
    "pnpm typecheck",
    "pnpm run test",
    "npm run typecheck",
    "yarn check",
    "pnpm verify:all",
    "pnpm build:ts",
    "TURBO_TELEMETRY_DISABLED=1 turbo run typecheck --concurrency=4 --output-logs=new-only",
    "npx turbo typecheck",
    "pnpm exec turbo run test --filter '!@lumen/web'",
    "pnpm -r typecheck",
    "pnpm --recursive run test",
    "pnpm -r exec tsc --noEmit",
    "npm test --workspaces",
    "yarn workspaces foreach -A run typecheck",
    "nx run-many -t typecheck",
    "lerna run test",
    "(cd packages/eval && pnpm typecheck); pnpm check",
    "cd packages/eval && cd ../.. && pnpm check",
    "bash -c 'pnpm check'",
    "bash <<'EOF'\npnpm check\nEOF",
]
WORKSPACE_ALLOWED = [
    "free -g | head -2; (cd packages/eval && timeout 600 pnpm typecheck 2>&1 | tail -3; echo done)",
    "cd packages/eval && pnpm typecheck && pnpm exec vitest run test/sre.test.ts",
    "pnpm --filter @lumen/eval typecheck",
    "pnpm -C packages/eval typecheck",
    "turbo run typecheck --filter=@lumen/eval",
    "turbo run typecheck --affected",
    "pnpm -r --filter ./packages/eval typecheck",
    "pnpm install --frozen-lockfile && pnpm -r install",
    "pnpm lint && pnpm boundaries",
    "nx affected -t typecheck",
    "turbo --version",
    "yarn workspace @lumen/eval typecheck",
    "git commit -qm 'leave pnpm check to CI'",
    "evo-agents worker step 3 verify --repo lumen --verify 'pnpm check'",
    # a pull request's body in a heredoc is text: the plan run's hook refused one that named a pytest run
    "cat > /tmp/pr-body.md <<'EOF'\n- `uv run pytest tests/test_library_import.py tests/test_overlays.py`: 75 passed.\n"
    "pnpm check\nturbo run typecheck\nEOF\ngh pr create --body-file /tmp/pr-body.md",
]


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - packages/*\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"name": "lumen", "scripts": ROOT_SCRIPTS}), encoding="utf-8")
    package = tmp_path / "packages" / "eval"
    package.mkdir(parents=True)
    scripts = {"typecheck": "tsc --noEmit", "test": "vitest run"}
    (package / "package.json").write_text(json.dumps({"name": "@lumen/eval", "scripts": scripts}), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("command", WORKSPACE_REFUSED)
def test_a_check_of_the_whole_workspace_is_refused(workspace, command):
    assert broad_checks.whole_workspace(without_heredocs(command), workspace) is not None, command


@pytest.mark.parametrize("command", WORKSPACE_ALLOWED)
def test_a_check_of_the_packages_changed_goes_through(workspace, command):
    command = without_heredocs(command)
    assert broad_checks.whole_workspace(command, workspace) is None, command
    assert broad_tests.broad(command) is None, command


def test_a_script_is_read_where_the_command_runs(workspace):
    package = workspace / "packages" / "eval"
    assert broad_checks.whole_workspace("pnpm typecheck", package) is None
    assert broad_checks.whole_workspace("pnpm -w check", package) is not None
    assert broad_checks.whole_workspace("pnpm check", None) is None  # no directory: nothing to read
    found = broad_checks.whole_workspace("pnpm check", workspace)
    assert "script check runs pnpm typecheck" in found and "turbo run typecheck" in found


def test_the_plan_runs_hook_refuses_a_workspace_check_from_the_sessions_directory(workspace):
    recorded = []
    hook = wait_guard.claude_wait_hook(WaitGuard(record=lambda text, body: recorded.append((text, body)), run_id=7))
    call = {"tool_name": "Bash", "tool_input": {"command": WORKSPACE_REFUSED[0], "run_in_background": True}}
    answer = asyncio.run(hook({**call, "cwd": str(workspace)}, "toolu_1", None))
    why = answer["hookSpecificOutput"]["permissionDecisionReason"]
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny" and "--filter" in why
    ((text, body),) = recorded
    assert "every package" in text and (body["rule"], body["run"]) == ("workspace", 7)
    assert asyncio.run(hook({**call, "cwd": str(workspace / "packages" / "eval")}, "toolu_2", None)) == {}
