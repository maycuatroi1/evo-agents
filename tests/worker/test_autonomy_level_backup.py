"""``evo-agents worker backup`` (plan autonomy-levels, criterion a9) and the floor S3 at a push (criterion a10, its S3
part) on the worker.

The agent of a run records a backup: the command sends it to the hub (the fake hub keeps it) and keeps it in the run's
``backups.jsonl``, where the hook of autonomy reads it; a time in the future and one without its zone are refused, and
nothing is kept. Every push of the worker (``gitops._send``) refuses a commit whose message or added lines hold the
value of one of the run's leases, in the daemon with the values it holds and in an agent's command through the run's
socket (``op leaks``), and the refusal names the commit, never the value.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from evo_agents.worker import credentials, gitops, lease_leaks
from evo_agents.worker.autonomy_hook import backups_path, fresh_backup
from evo_agents.worker.home import WorkerHome

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from tests.worker.test_plan_runs import (  # noqa: E402
    PLAN,
    machine,  # noqa: F401 (a fixture)
    plan_body,
    run_repos,
    with_daemon,
)

VALUE = "ghs_autonomyLevelLeaseValue0123456789"  # a lease value of the tests, of no account
RUN = 9101


def test_autonomy_level_backup_command_records_on_the_hub_and_for_the_hook(machine):  # noqa: F811
    made = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    backup = ["backup", "--target", "db app", "--location", "s3://backups/app.dump", "--restore", "pg_restore x"]
    machine.scenarios(
        {
            f"plan:{PLAN}": [
                {"cli": [*backup, "--made-at", made]},
                {"cli": ["backup", "--target", "db", "--location", "/b", "--made-at", future]},
                {"cli": ["backup", "--target", "db", "--location", "/b", "--made-at", "2026-10-23T09:00:00"]},
                {"result": {"summary": "Backed up."}},
            ]
        }
    )
    body = plan_body()
    found = {}

    async def run_it(hub, daemon):
        run_id = hub.queue_plan_run(PLAN, run_repos(body))
        assert await hub.wait_state(run_id, "done", "failed") in ("done", "failed")
        found.update(run=run_id, backups=list(hub.backups))
        found["kept"] = backups_path(machine.home, run_id).read_text(encoding="utf-8").splitlines()

    with_daemon(machine, run_it, plan=body)
    run_id = found["run"]
    first, future_one, zoneless = [c for c in machine.commands() if c["run"] == run_id]
    assert first["exit"] == 0 and f"recorded for run #{run_id}" in first["stdout"], first
    assert future_one["exit"] == 1 and "is in the future" in future_one["stderr"]
    assert zoneless["exit"] == 1 and "needs its time zone" in zoneless["stderr"]
    (sent,) = found["backups"]
    assert sent == {
        "run_id": run_id,
        "target": "db app",
        "location": "s3://backups/app.dump",
        "made_at": made,
        "restore": "pg_restore x",
    }
    (kept,) = [json.loads(line) for line in found["kept"]]
    assert kept["location"] == "s3://backups/app.dump" and kept["seq"] == 1


def test_autonomy_level_backup_refuses_outside_a_run(tmp_path, monkeypatch, capsys):
    from evo_agents.cli import main as cli_main

    monkeypatch.delenv("EVO_RUN_ID", raising=False)
    monkeypatch.setenv("EVO_WORKER_HOME", str(tmp_path / "worker"))
    assert cli_main(["worker", "backup", "--target", "db", "--location", "/b"]) == 1
    assert "EVO_RUN_ID is not set" in capsys.readouterr().err
    assert fresh_backup(backups_path(WorkerHome(tmp_path / "worker"), 1)) is None


# The floor S3 at a push


def _git(cwd, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "A", "GIT_COMMITTER_EMAIL": "a@x"}
    import os

    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env={**os.environ, **env})
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--quiet", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "--quiet", str(origin), str(work))
    (work / "README.md").write_text("# app\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "--quiet", "-m", "first")
    _git(work, "push", "--quiet", "origin", "HEAD:refs/heads/main")
    _git(work, "fetch", "--quiet", "origin")
    return work


def _commit(work, name: str, text: str, message: str) -> str:
    (work / name).write_text(text, encoding="utf-8")
    _git(work, "add", name)
    _git(work, "commit", "--quiet", "-m", message)
    return _git(work, "rev-parse", "HEAD")


def test_autonomy_level_a_push_that_carries_a_lease_value_is_refused_without_the_value(repo, monkeypatch):
    monkeypatch.delenv("EVO_RUN_ID", raising=False)
    leaked = _commit(repo, "config.env", f"TOKEN={VALUE}\n", "Add the config")
    _commit(repo, "later.txt", "later\n", "Later")
    credentials.remember(VALUE, RUN)
    try:
        with pytest.raises(gitops.PushRefused) as refused:
            asyncio.run(gitops._send(repo, "HEAD", "feat/x", "origin", env=None))
    finally:
        credentials.forget(RUN)
    message = str(refused.value)
    assert leaked[:12] in message and "floor S3" in message and VALUE not in message
    assert _git(repo, "ls-remote", "origin", "refs/heads/feat/x") == "", "nothing was pushed"
    asyncio.run(gitops._send(repo, "HEAD", "feat/x", "origin", env=None)), "no value held: the push goes"


def test_autonomy_level_a_commit_message_with_a_lease_value_is_refused_and_a_removed_line_is_not(repo):
    _commit(repo, "a.txt", "a\n", f"Use {VALUE} for now")
    head = _git(repo, "rev-parse", "HEAD")
    assert asyncio.run(lease_leaks.leaked_commit(repo, "HEAD", "origin", [VALUE])) == head
    assert asyncio.run(lease_leaks.leaked_commit(repo, "HEAD", "origin", ["short"])) is None, "too short to look for"
    log = f"{lease_leaks.MARK}abc\nDrop it\n\ndiff --git a/x b/x\n-TOKEN={VALUE}\n+TOKEN=\n"
    assert lease_leaks.leaked(log, [VALUE]) is None, "a commit that takes the value out sends nothing of it"


def test_autonomy_level_the_runs_socket_answers_the_commit_alone_for_an_agents_push(repo, tmp_path, monkeypatch):
    leaked = _commit(repo, "config.env", f"TOKEN={VALUE}\n", "Add the config")
    home = WorkerHome(tmp_path / "worker")
    held = credentials.RunCredentials(RUN, home, {}, lambda *a, **k: None)
    credentials.remember(VALUE, RUN)
    try:
        request = {"op": "leaks", "path": str(repo), "source": "HEAD", "remote": "origin"}
        answer = asyncio.run(held.answer(request))
    finally:
        credentials.forget(RUN)
    assert answer == {"commit": leaked} and VALUE not in json.dumps(answer)
    monkeypatch.setenv("EVO_RUN_ID", str(RUN))
    monkeypatch.setenv("EVO_WORKER_HOME", str(home.root))
    asyncio.run(lease_leaks.check(repo, "HEAD", "origin")), "a run without leases has no socket: nothing to look for"
