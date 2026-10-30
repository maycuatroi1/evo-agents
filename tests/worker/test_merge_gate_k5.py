"""The merge gate of K5 in the worker's hook (plan super-board-parity-2, step 2): a merge command (``gh pr merge``, or
``gh api`` of ``repos/OWNER/NAME/pulls/N/merge``) in a repo with the hub's merge check on goes only when the hub holds
a passed verdict for the pull request's head, and the command pins that head. The hub's word is a fake here; a repo
the hub does not gate, and a hub that is silent, leave the command as the modes said. Fictional repos and people.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from evo_agents.hub.autonomy_levels import CLASSES
from evo_agents.worker import pull_heads
from evo_agents.worker.autonomy_hook import Gate
from evo_agents.worker.command_classes import classify

HEAD = "a1" * 20
OTHER = "b2" * 20
WORKTREE = Path("/work/evo-run/9")


def facts(how: str = "pr", pin: str | None = None, head: str = HEAD) -> dict:
    return {"how": how, "repo": "acme/app", "number": 7, "head": head, "pin": pin}


class Hub:
    def __init__(self, answer: dict | None):
        self.answer, self.asked = answer, []

    async def merge_gate(self, body: dict) -> dict | None:
        self.asked.append(body)
        return self.answer


def gate(hub: Hub, found: dict | None, **modes: str) -> Gate:
    autonomy = {"modes": {**dict.fromkeys(CLASSES, "auto"), **modes}, "sources": {}}
    made = Gate.of(
        autonomy, WORKTREE, WORKTREE / "backups.jsonl", merge_facts=lambda command: found, merge_gate=hub.merge_gate
    )
    assert made is not None
    return made


def run(made: Gate, command: str) -> str | None:
    return asyncio.run(made.check("Bash", {"command": command}))


PASSED = {
    "gated": True,
    "passed": True,
    "head_sha": HEAD,
    "reason": None,
    "command": "evo-agents worker merge-check app",
}
HELD = {**PASSED, "passed": False, "reason": "the hub has no merge check of pull request #7"}


def test_merge_gate_k5_refuses_a_merge_the_hub_holds_and_names_the_command_that_asks():
    hub = Hub(HELD)
    why = run(gate(hub, facts()), "gh pr merge 7")
    assert why is not None and "no merge check of pull request #7" in why
    assert "`evo-agents worker merge-check app`" in why
    assert hub.asked == [{"repo": "acme/app", "number": 7, "head": HEAD}]


def test_merge_gate_k5_lets_a_passed_head_through_when_the_command_pins_it():
    hub = Hub(PASSED)
    assert run(gate(hub, facts(pin=HEAD)), f"gh pr merge 7 --match-head-commit {HEAD}") is None
    assert run(gate(hub, facts("api", pin=HEAD)), f"gh api -X PUT repos/acme/app/pulls/7/merge -f sha={HEAD}") is None


def test_merge_gate_k5_demands_the_pin_of_the_head_the_hub_passed():
    hub = Hub(PASSED)
    unpinned = run(gate(hub, facts()), "gh pr merge 7")
    assert unpinned is not None and f"--match-head-commit={HEAD}" in unpinned and "pins no head" in unpinned
    wrong = run(gate(hub, facts(pin=OTHER)), f"gh pr merge 7 --match-head-commit {OTHER}")
    assert wrong is not None and f"pins {OTHER[:12]}" in wrong
    api = run(gate(hub, facts("api")), "gh api -X PUT repos/acme/app/pulls/7/merge")
    assert api is not None and f"-f sha={HEAD}" in api


def test_merge_gate_k5_holds_in_every_mode_but_deny_and_ask_stay_their_own():
    for mode in ("auto", "notify"):
        assert run(gate(Hub(HELD), facts(), K5=mode), "gh pr merge 7") is not None
    denied = run(gate(Hub(PASSED), facts(pin=HEAD), K5="deny"), f"gh pr merge 7 --match-head-commit {HEAD}")
    assert denied is not None and "is deny" in denied


def test_merge_gate_k5_leaves_a_repo_the_hub_does_not_gate_a_silent_hub_and_other_commands():
    off = {"gated": False, "passed": True, "head_sha": None, "reason": None, "command": "x"}
    assert run(gate(Hub(off), facts()), "gh pr merge 7") is None
    assert run(gate(Hub(None), facts()), "gh pr merge 7") is None, "the hub's silence leaves the command through"
    hub = Hub(HELD)
    assert run(gate(hub, None), "gh pr merge 7") is None, "a pull request gh does not read"
    assert run(gate(hub, facts()), "git push origin feat/x") is None
    assert hub.asked == []


@pytest.mark.parametrize(
    "command",
    [
        "gh api -X PUT repos/acme/app/pulls/7/merge -f sha=abc",
        "gh api repos/acme/app/pulls/7/merge --method PUT",
        "gh api /repos/acme/app/pulls/12/merge/ -X PUT",
    ],
)
def test_merge_gate_k5_reads_the_api_merge_as_k5(command):
    assert [hit.klass for hit in classify(command, WORKTREE)] == ["K5"]
    assert [hit.klass for hit in classify("gh api repos/acme/app/pulls/7/files", WORKTREE)] == []


def test_merge_gate_k5_reads_the_pull_request_and_its_pin_from_the_command():
    pr = pull_heads.merge_ref(f"gh pr merge 7 --repo acme/app --squash --match-head-commit {HEAD}")
    assert pr == {"how": "pr", "ref": "7", "repo": "acme/app", "pin": HEAD}
    equal = pull_heads.merge_ref(f"gh pr merge 7 --match-head-commit={HEAD}")
    assert equal is not None and equal["pin"] == HEAD
    assert pull_heads.merge_ref("gh pr merge")["ref"] is None
    api = pull_heads.merge_ref(f"gh api -X PUT repos/acme/app/pulls/7/merge -f sha={HEAD}")
    assert api == {"how": "api", "ref": "7", "repo": "acme/app", "pin": HEAD}
    assert pull_heads.merge_ref("gh api repos/acme/app/pulls/7/merge --field=sha=" + HEAD)["pin"] == HEAD
    assert pull_heads.merge_ref("gh api repos/acme/app/pulls/7/merge")["pin"] is None
    assert pull_heads.merge_ref("glab mr merge 7") is None


def test_merge_gate_k5_reads_the_head_of_the_pull_request_as_gh_shows_it(tmp_path):
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    shown = f'{{"number": 7, "headRefOid": "{HEAD.upper()}", "url": "https://github.com/Acme/App/pull/7"}}'
    fake.write_text(f"#!/bin/sh\necho '{shown}'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}"}
    found = pull_heads.facts_of_command(f"gh pr merge 7 --match-head-commit {HEAD}", tmp_path, env)
    assert found == {"how": "pr", "repo": "acme/app", "number": 7, "head": HEAD, "pin": HEAD}
    assert pull_heads.facts_of_command("git status", tmp_path, env) is None
