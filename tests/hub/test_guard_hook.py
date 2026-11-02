"""The evo-hub plugin's guard of private names in a member's session (``evo_agents.hub.guard_hook``): a Bash command
that commits, pushes or writes with gh to a guarded repo is denied when its text, a file it names or what it pushes
holds a name; anything else, and anything bound elsewhere, runs. The names are fictional (northwind, lumen).
"""

from __future__ import annotations

import io
import json
import os
import subprocess

import pytest

from evo_agents.hub import guard_hook
from evo_agents.private_names import Policy, entries

POLICY = Policy(terms=entries("Northwind") + entries("lumen ops"), targets=["github.com/acme/public"])


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t"}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env={**env, "GIT_COMMITTER_EMAIL": "t@x"})


@pytest.fixture
def repo(tmp_path):
    work = tmp_path / "public"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "remote", "add", "origin", "https://github.com/acme/public.git")
    (work / "a").write_text("a\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "a")
    git(work, "update-ref", "refs/remotes/origin/main", "HEAD")
    other = tmp_path / "internal"
    other.mkdir()
    git(other, "init", "-q")
    git(other, "remote", "add", "origin", "https://github.com/acme/internal.git")
    return work


def test_a_command_bound_for_a_guarded_repo_is_denied_for_a_name_in_its_text(repo):
    message = "git commit -m \"$(cat <<'EOF'\nBrief of Northwind\nEOF\n)\""
    assert "Northwind" in guard_hook.check_command(POLICY, message, str(repo))
    pr = 'gh pr create --title "t" --body "lumen-ops images"'
    assert "lumen-ops" in guard_hook.check_command(POLICY, pr, str(repo))
    edit = "gh pr edit 3 --repo acme/public --body northwind"
    assert guard_hook.check_command(POLICY, edit, "/") is not None
    assert guard_hook.check_command(POLICY, "git commit -m 'clean'", str(repo)) is None
    assert guard_hook.check_command(POLICY, "echo northwind > x", str(repo)) is None  # neither commits nor pushes


def test_a_command_bound_elsewhere_runs_whatever_it_says(repo):
    internal = str(repo.parent / "internal")
    assert guard_hook.check_command(POLICY, "git commit -m northwind", internal) is None
    assert guard_hook.check_command(POLICY, "gh pr create --repo acme/internal --body northwind", str(repo)) is None
    assert guard_hook.check_command(POLICY, f"cd {internal} && git commit -m northwind", str(repo)) is None
    assert guard_hook.check_command(POLICY, f"cd {repo} && git commit -m northwind", internal) is not None


def test_a_body_file_and_the_commits_of_a_push_are_read_too(repo, tmp_path):
    body = tmp_path / "body.md"
    body.write_text("for Northwind\n")
    assert guard_hook.check_command(POLICY, f"gh pr create --title t --body-file {body}", str(repo)) is not None
    (repo / "b").write_text("lumen ops\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "b")
    assert "lumen ops" in guard_hook.check_command(POLICY, "git push origin main", str(repo))


def test_the_hook_denies_through_claude_codes_answer_and_never_fails_the_session(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EVO_HUB_HOME", str(tmp_path / "hub"))
    path = guard_hook.policy_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(POLICY.to_json()))
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m northwind"}, "cwd": str(repo)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert guard_hook.cmd_hook_pre_tool_use(None) == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "northwind" in out["permissionDecisionReason"]
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert guard_hook.cmd_hook_pre_tool_use(None) == 0
    assert capsys.readouterr().out == ""
    path.unlink()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert guard_hook.cmd_hook_pre_tool_use(None) == 0 and capsys.readouterr().out == ""
