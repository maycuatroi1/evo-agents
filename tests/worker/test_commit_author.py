"""Who a run's commits name (``runner.authorship``): a chat run, and a run under a profile that is not its project's
default, commit with the agent as author (``<handle>@<host of the hub>``) and the person who dispatched the run as
co-author, the committer staying this machine's identity; any other run under the default profile commits as before."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker import gitops  # noqa: E402
from evo_agents.worker.runner import authorship, push  # noqa: E402
from tests.worker.test_claude_home import Notes, agent  # noqa: E402

HUB = "https://agents.omelet.tech"
SANDBOX = {"handle": "evo-sandbox", "version": 3, "name": "Evo Sandbox", "instructions": "", "permissions": {}}
DEFAULT = {"handle": "claude", "version": 1, "name": "Claude", "instructions": "", "permissions": {}}
ORIGIN = {"login": "octo-member", "github_id": 4242}
TRAILER = "Co-authored-by: octo-member <4242+octo-member@users.noreply.github.com>"
MACHINE = ("Machine Owner", "owner@machine.example")


def spec(**extra) -> dict:
    return {"id": 7, "kind": "step", "runtime": "claude-code", "origin": ORIGIN, **extra}


@pytest.fixture(autouse=True)
def plain_git(monkeypatch):
    """Git configured by this test's repository alone, not by the GIT_CONFIG_* a worker run hands its commands."""
    for name in list(os.environ):
        if name.startswith("GIT_CONFIG") or name.startswith("GIT_AUTHOR") or name.startswith("GIT_COMMITTER"):
            monkeypatch.delenv(name)


def repository(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    for command in (
        ["init", "--quiet", "-b", "main"],
        ["config", "user.name", MACHINE[0]],
        ["config", "user.email", MACHINE[1]],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *command], cwd=path, check=True)
    (path / "README.md").write_text("start\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "start"], cwd=path, check=True)
    return path


def head(path: Path) -> dict:
    out = subprocess.run(
        ["git", "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    an, ae, cn, ce, body = out.split("\0")
    return {"author": (an, ae), "committer": (cn, ce), "body": body.strip()}


def fake_run(run_spec: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_spec["id"],
        title="fix the docs",
        spec=run_spec,
        note=Notes(),
        daemon=SimpleNamespace(hub=SimpleNamespace(url=HUB)),
    )


# Who is the author


def test_commit_author_of_a_chat_run_and_of_another_profile_is_the_agent_at_the_hub_host():
    chat = authorship.agent_author(spec(kind="chat", profile=DEFAULT), HUB)
    assert chat == authorship.Author("Claude", "claude@agents.omelet.tech")
    own = authorship.agent_author(spec(profile=SANDBOX, own_home=True), HUB + ":8443/")
    assert own == authorship.Author("Evo Sandbox", "evo-sandbox@agents.omelet.tech")
    assert own.flag == "Evo Sandbox <evo-sandbox@agents.omelet.tech>"
    nameless = authorship.agent_author(spec(profile={**SANDBOX, "name": "  "}, own_home=True), HUB)
    assert nameless == authorship.Author("evo-sandbox", "evo-sandbox@agents.omelet.tech")


def test_commit_author_of_any_other_default_profile_run_is_none():
    for other in (
        spec(profile=DEFAULT),
        spec(profile=DEFAULT, own_home=False, kind="plan"),
        spec(kind="chat"),  # a run from before profiles
        spec(own_home=True),
    ):
        assert authorship.agent_author(other, HUB) is None
        assert authorship.co_author_trailer(other) is None
        assert authorship.author_env(other, HUB) == {}
        agent = "\n\nEvo-Agent: claude" if other.get("profile") else ""  # the run's agent, its trailer alone
        assert authorship.commit_message(other, "run #7: x") == "run #7: x" + agent
    assert authorship.agent_author(spec(kind="chat", profile=DEFAULT), "") is None


def test_commit_coauthor_trailer_names_the_origin_with_the_github_noreply_email():
    assert authorship.co_author_trailer(spec(kind="chat", profile=DEFAULT)) == TRAILER
    no_id = spec(profile=SANDBOX, own_home=True, origin={"login": "old-user", "github_id": None})
    assert authorship.co_author_trailer(no_id) == "Co-authored-by: old-user <old-user@users.noreply.github.com>"
    assert authorship.co_author_trailer(spec(profile=SANDBOX, own_home=True, origin=None)) is None
    message = authorship.commit_message(spec(kind="chat", profile=DEFAULT), "run #7: fix")
    assert message == f"run #7: fix\n\n{TRAILER}\nEvo-Agent: claude"


# The daemon's commit


def test_commit_of_the_daemon_for_an_agent_run_has_the_agent_author_the_machine_committer_and_the_coauthor(tmp_path):
    path = repository(tmp_path)
    (path / "README.md").write_text("changed\n", encoding="utf-8")
    run = fake_run(spec(profile=SANDBOX, own_home=True))
    assert asyncio.run(push.commit(run, path)) is True
    made = head(path)
    assert made["author"] == ("Evo Sandbox", "evo-sandbox@agents.omelet.tech")
    assert made["committer"] == MACHINE
    assert made["body"] == f"run #7: fix the docs\n\n{TRAILER}\nEvo-Agent: evo-sandbox"
    trailers = subprocess.run(
        ["git", "log", "-1", "--format=%(trailers:key=Co-authored-by,valueonly)"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert trailers == "octo-member <4242+octo-member@users.noreply.github.com>"


def test_commit_of_the_daemon_for_a_default_profile_run_is_as_before_no_author_no_coauthor(tmp_path):
    path = repository(tmp_path)
    (path / "README.md").write_text("changed\n", encoding="utf-8")
    assert asyncio.run(push.commit(fake_run(spec(profile=DEFAULT)), path)) is True
    made = head(path)
    assert made["author"] == MACHINE and made["committer"] == MACHINE
    assert made["body"] == "run #7: fix the docs\n\nEvo-Agent: claude"


def test_commit_run_without_an_author_keeps_the_machine_identity(tmp_path):
    path = repository(tmp_path)
    (path / "new.txt").write_text("x\n", encoding="utf-8")
    assert asyncio.run(gitops.commit_run(path, "plain")).made
    assert head(path)["author"] == MACHINE


# The agent's own commits


def test_commit_author_of_the_agent_itself_comes_from_its_environment(tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    daemon_hub = SimpleNamespace(url=HUB)
    own = agent(tmp_path, env, profile=SANDBOX, own_home=True, origin=ORIGIN)
    own.run.daemon.hub = daemon_hub
    chat = agent(tmp_path, env, kind="chat", profile=DEFAULT, origin=ORIGIN)
    chat.run.daemon.hub = daemon_hub
    default = agent(tmp_path, env, profile=DEFAULT, origin=ORIGIN)
    default.run.daemon.hub = daemon_hub
    assert {k: own.env()[k] for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL")} == {
        "GIT_AUTHOR_NAME": "Evo Sandbox",
        "GIT_AUTHOR_EMAIL": "evo-sandbox@agents.omelet.tech",
    }
    assert chat.env()["GIT_AUTHOR_EMAIL"] == "claude@agents.omelet.tech"
    assert "GIT_AUTHOR_NAME" not in default.env() and "GIT_AUTHOR_EMAIL" not in default.env()
    assert not any(name.startswith("GIT_COMMITTER") for name in own.env())
    # a commit the agent makes with that environment names it as author, the machine as committer
    path = repository(tmp_path)
    (path / "agent.txt").write_text("by the agent\n", encoding="utf-8")
    agent_env = {**os.environ, **own.env()}
    subprocess.run(["git", "add", "agent.txt"], cwd=path, check=True, env=agent_env)
    subprocess.run(["git", "commit", "--quiet", "-m", "agent work"], cwd=path, check=True, env=agent_env)
    made = head(path)
    assert made["author"] == ("Evo Sandbox", "evo-sandbox@agents.omelet.tech") and made["committer"] == MACHINE
