"""A merge approval bound to a head and a scope, on the worker (plan super-board-parity-2, criterion a3): what
``pull_heads`` reads of a pull request, what ``worker ask --pull`` binds its decision to, and the autonomy hook that
sends the head and scope of the pull request a ``gh pr merge`` merges, refusing it once the hub says the answer was
given for another head. The repos are fictional."""

from __future__ import annotations

import argparse
import json
import subprocess

import pytest

from evo_agents.hub import approval_heads
from evo_agents.worker import cli, pull_heads
from evo_agents.worker.home import WorkerStateError
from tests.worker.test_autonomy_level_hook import Recorder, check, gate

HEAD = "a" * 40
NEW_HEAD = "b" * 40
VIEW = {
    "number": 12,
    "headRefOid": HEAD,
    "files": [{"path": "app/models.py"}, {"path": "app/migrations/0004_add_index.py"}],
    "body": "Adds an index.\n\nneeds-you: run the backfill on prod after the merge\n",
    "url": "https://github.com/acme/app/pull/12",
}


def test_approval_head_of_a_pull_request_as_gh_views_it():
    bound = pull_heads.of_view(VIEW)
    scope = approval_heads.scope_of(["app/models.py", "app/migrations/0004_add_index.py"], VIEW["body"])
    assert bound == {"pull": "acme/app#12", "head": HEAD, "scope": scope}
    assert pull_heads.of_view({**VIEW, "headRefOid": None}) is None
    assert pull_heads.of_view({**VIEW, "headRefOid": "not a sha"}) is None


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("gh pr merge 12 --squash", ("12", None)),
        ("EVO_DECISION=4 gh pr merge --squash 12 --repo acme/app", ("12", "acme/app")),
        (
            "gh pr merge -R acme/app --subject 'Ship it' https://github.com/acme/app/pull/12",
            (
                "https://github.com/acme/app/pull/12",
                "acme/app",
            ),
        ),
        ("gh pr merge --squash --delete-branch", (None, None)),
        ("cd app && gh pr merge 7 --match-head-commit abc1234", ("7", None)),
        ("glab mr merge 4", None),
        ("git push origin main", None),
    ],
)
def test_approval_head_reads_the_pull_request_a_merge_command_merges(command, target):
    assert pull_heads.merge_target(command) == target


def test_approval_head_read_runs_gh_in_the_run_directory_with_the_agents_environment(tmp_path, monkeypatch):
    seen = {}

    def run(args, **kwargs):
        seen.update(args=args, cwd=kwargs["cwd"], token=kwargs["env"].get("GH_TOKEN"))
        return subprocess.CompletedProcess(args, 0, json.dumps(VIEW), "")

    monkeypatch.setattr(pull_heads.subprocess, "run", run)
    bound = pull_heads.of_command("gh pr merge 12 -R acme/app", tmp_path, {"GH_TOKEN": "agent-token"})
    assert bound["head"] == HEAD and bound["pull"] == "acme/app#12"
    assert seen["args"] == ["gh", "pr", "view", "12", "--json", pull_heads.FIELDS, "--repo", "acme/app"]
    assert seen["cwd"] == tmp_path and seen["token"] == "agent-token"
    monkeypatch.setattr(pull_heads.subprocess, "run", lambda args, **_: subprocess.CompletedProcess(args, 1, "", "x"))
    assert pull_heads.of_command("gh pr merge 12", tmp_path) is None, "a pull request gh does not read binds nothing"


def test_approval_head_worker_ask_pull_binds_the_decision_to_the_head_and_scope(monkeypatch):
    monkeypatch.setattr(pull_heads, "read", lambda ref, repo, cwd, env=None: pull_heads.of_view(VIEW))
    assert cli.asked_approval("merge", "12") == pull_heads.of_view(VIEW)
    with pytest.raises(WorkerStateError, match="use --category merge"):
        cli.asked_approval("deploy", "12")
    monkeypatch.setattr(pull_heads, "read", lambda ref, repo, cwd, env=None: None)
    with pytest.raises(WorkerStateError, match="gh could not read pull request 12"):
        cli.asked_approval("merge", "12")


def test_approval_head_worker_ask_takes_pull():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    cli._register_ask(sub)
    args = parser.parse_args(["ask", "--category", "merge", "--question", "Merge #12?", "--pull", "12"])
    assert args.pull == "12"


def test_approval_head_hook_sends_the_head_and_scope_of_the_pull_request(tmp_path):
    recorder = Recorder({"mode": "ask", "told": False, "answered": True, "stale": None})
    made = gate(tmp_path, recorder, K5="ask")
    made.pull = lambda command: pull_heads.of_view(VIEW) if "gh pr merge" in command else None
    why = check(made, "gh pr merge 12")
    assert "--category merge --pull <the pull request's number>" in why, "the refusal says to bind the ask"
    assert check(made, "EVO_DECISION=31 gh pr merge 12 --squash") is None
    (act,) = recorder.acts
    assert act == {
        "klass": "K5",
        "what": "gh pr merge 12 --squash",
        "decision_id": 31,
        "head": HEAD,
        "scope": pull_heads.of_view(VIEW)["scope"],
    }


def test_approval_head_hook_refuses_an_answer_given_for_another_head_and_says_to_ask_again(tmp_path):
    stale = f"it was given for head {HEAD[:12]}, and the head is {NEW_HEAD[:12]} now"
    recorder = Recorder({"mode": "ask", "told": False, "answered": False, "stale": stale})
    made = gate(tmp_path, recorder, K5="ask")
    made.pull = lambda command: pull_heads.of_view({**VIEW, "headRefOid": NEW_HEAD})
    why = check(made, "EVO_DECISION=31 gh pr merge 12")
    assert "the answer to decision #31 no longer opens the gate" in why and stale in why
    assert "evo-agents worker ask --category merge --pull" in why and "EVO_DECISION=<the new decision's id>" in why
    assert recorder.acts[0]["head"] == NEW_HEAD


def test_approval_head_hook_sends_no_head_for_a_merge_it_cannot_read(tmp_path):
    recorder = Recorder({"mode": "ask", "told": False, "answered": False, "stale": "the head could not be read"})
    made = gate(tmp_path, recorder, K5="ask")
    made.pull = lambda command: None
    assert "no longer opens the gate" in check(made, "EVO_DECISION=31 gh pr merge 12")
    assert "head" not in recorder.acts[0], "the hub then holds a bound answer closed"
