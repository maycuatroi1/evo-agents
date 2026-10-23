"""The trailers and the ledger of a run on the worker (plan autonomy-levels, criterion a10): every commit of a run names
its principal and its agent (``Evo-Principal: <login>``, ``Evo-Agent: <handle>``), the daemon's commit in its message
and the agent's own through the run's hooks (``runner.authorship``), never the runtime or the model; the hook's event
of each classed command names the run, its principal and its agent; a lease value never reaches what the daemon keeps
of an event. Who authors the commits: test_commit_author."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from evo_agents.worker import credentials
from evo_agents.worker.credentials import config_env
from evo_agents.worker.runner import authorship
from tests.worker.test_autonomy_level_hook import Recorder, check, modes
from tests.worker.test_commit_author import (  # noqa: F401 (plain_git is a fixture)
    SANDBOX,
    TRAILER,
    plain_git,
    repository,
    spec,
)

CARD = {"card": "# Your autonomy", "modes": {}, "categories": [], "principal": "ivan"}


def run_spec(**extra) -> dict:
    return spec(**{"profile": SANDBOX, "own_home": True, "autonomy": CARD, "model": "claude-opus-x", **extra})


def made(path: Path) -> Path:
    path.mkdir()
    return repository(path)


def body(path: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(path: Path, env: dict, name: str, message: str) -> None:
    (path / name).write_text(name + "\n", encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "--quiet", "-m", message], cwd=path, check=True, env=env)


def test_autonomy_level_trailer_of_the_daemons_commit_names_principal_and_agent_never_runtime_or_model():
    assert authorship.run_trailers(run_spec()) == ["Evo-Principal: ivan", "Evo-Agent: evo-sandbox"]
    message = authorship.commit_message(run_spec(), "run #7: fix")
    assert message == f"run #7: fix\n\n{TRAILER}\nEvo-Principal: ivan\nEvo-Agent: evo-sandbox"
    assert "claude-code" not in message and "claude-opus-x" not in message
    odd = run_spec(autonomy={**CARD, "principal": "ivan\nEvo-Agent: root"})
    assert authorship.run_trailers(odd)[0] == "Evo-Principal: ivanEvo-Agentroot", "no line of its own"
    assert authorship.run_trailers(spec()) == [], "a run without a card or a profile has none"


def test_autonomy_level_trailer_hook_adds_them_to_the_agents_own_commit_under_the_worktree_alone(tmp_path):
    work = made(tmp_path / "inside")
    outside = made(tmp_path / "outside")
    hooks = authorship.trailer_hooks(run_spec(), tmp_path / "hooks", work.parent)
    assert hooks is not None and all(os.access(hooks / name, os.X_OK) for name in authorship.HOOK_NAMES)
    assert authorship.trailer_hooks(spec(), tmp_path / "none", work.parent) is None
    env = {**os.environ, **config_env(os.environ, [("core.hooksPath", str(hooks))])}
    commit(work, env, "agent.txt", "agent work")
    assert body(work) == "agent work\n\nEvo-Principal: ivan\nEvo-Agent: evo-sandbox"
    commit(work, env, "again.txt", "more\n\nEvo-Agent: evo-sandbox")
    assert body(work).count("Evo-Agent: evo-sandbox") == 1, "a trailer the message has already is not added twice"
    commit(outside, env, "else.txt", "elsewhere")
    assert body(outside) == "elsewhere", "a checkout outside the run's worktree is not the run's"


def test_autonomy_level_trailer_hook_runs_the_checkouts_own_hook_after_its_own(tmp_path):
    work = made(tmp_path / "inside")
    own = work / ".git" / "hooks" / "commit-msg"
    own.write_text('#!/bin/sh\necho "Reviewed-by: repo-hook" >> "$1"\n', encoding="utf-8")
    own.chmod(0o755)
    refuse = work / ".git" / "hooks" / "pre-commit"
    hooks = authorship.trailer_hooks(run_spec(), tmp_path / "hooks", work.parent)
    env = {**os.environ, **config_env(os.environ, [("core.hooksPath", str(hooks))])}
    commit(work, env, "agent.txt", "agent work")
    text = body(work)
    assert "Evo-Principal: ivan" in text and "Reviewed-by: repo-hook" in text
    refuse.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    refuse.chmod(0o755)
    (work / "blocked.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "blocked.txt"], cwd=work, check=True, env=env)
    blocked = subprocess.run(["git", "commit", "--quiet", "-m", "blocked"], cwd=work, env=env, capture_output=True)
    assert blocked.returncode != 0, "the checkout's own pre-commit still decides"


def test_autonomy_level_ledger_of_the_hook_names_the_run_its_principal_and_its_agent(tmp_path):
    from evo_agents.worker.autonomy_hook import Gate

    recorder = Recorder({"mode": "notify", "told": True, "answered": False})
    autonomy = {"modes": modes(K10="notify"), "sources": {}}
    made = Gate.of(
        autonomy,
        tmp_path,
        tmp_path / "backups.jsonl",
        record=recorder.record,
        act=recorder.act,
        run_id=7,
        principal="ivan",
        agent="evo-sandbox",
    )
    assert check(made, "gh issue create --repo other/lib --title Bug --body x") is None
    ((_, event),) = recorder.events
    assert {key: event[key] for key in ("run", "principal", "agent", "class", "mode", "verdict")} == {
        "run": 7,
        "principal": "ivan",
        "agent": "evo-sandbox",
        "class": "K10",
        "mode": "notify",
        "verdict": "allowed",
    }


def test_autonomy_level_secret_leak_the_daemon_masks_a_lease_value_in_what_it_keeps_of_an_event():
    value = "ghs_" + "a1b2c3d4" * 4
    credentials.remember(value, 991)
    try:
        kept = credentials.scrub({"text": f"pushed with {value}", "list": [value]})
    finally:
        credentials.forget(991)
    assert kept == {"text": "pushed with ***", "list": ["***"]}
    assert credentials.scrub(value) == value, "a value no run holds stays"
