"""Whether a merge check failed for a reason of the hub's own machinery (``evo_agents.hub.merge_infra``), as a verdict
of the Judge reads (``merge_judge.Judged.infra_causes``), and what the hold of such a check keeps in its verdict
(``evo_agents.hub.server.merge_holds``), with no database.

On 2026-11-05 eight plan runs of one pull request were woken with failures of the hub one after the other (a judge's
git, the scanner of the diff's signs, a flaky test of the base, the judge watchdog), and one asked its owner whether to
merge. A failure is the hub's only when nothing of the pull request fails the check too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from evo_agents.hub import judge, merge_gate, merge_infra
from evo_agents.hub.server import merge_holds
from evo_agents.hub.server.merge_judge import Judged, MergeVerdictIn

HEAD = "a1" * 20
VERIFY = "python -m pytest -q"
SECURITY = {"verdict": "pass", "reason": "none found", "findings": [], "candidates": 0, "turns": 1}


def judged(**body) -> Judged:
    """The first Judge's verdict on HEAD: every dimension passed, the verify passed, no hidden check, unless ``body``
    says otherwise."""
    said = {name: {"verdict": "pass", "reason": "fine"} for name in merge_gate.DIMENSIONS}
    values = {
        "verdict": "pass",
        "reasons": "read the diff",
        "head_sha": HEAD,
        "verify": [{"command": VERIFY, "exit_code": 0}],
        "hidden": [],
        "dimensions": said,
        "security": SECURITY,
        **body,
    }
    return Judged(
        body=MergeVerdictIn(**values),
        second=False,
        expected=[VERIFY],
        hidden_count=len(values["hidden"]),
        head=HEAD,
        signs_weighed=True,
    )


FAILED_ON_BOTH = [{"command": VERIFY, "exit_code": 1, "base_exit_code": 1}]


@pytest.mark.parametrize(
    ("body", "causes"),
    [
        ({}, []),  # it passed
        ({"verify": FAILED_ON_BOTH}, [merge_infra.BASE_FAILS]),
        ({"hidden": [{"index": 1, "exit_code": 2, "base_exit_code": 2}]}, [merge_infra.BASE_FAILS]),
        ({"verdict": None, "dimensions": None, "security": None}, [merge_infra.JUDGE_SILENT]),
        ({"security": None}, [merge_infra.NO_SECURITY]),
        (
            {"verify": FAILED_ON_BOTH, "verdict": None, "dimensions": None, "security": None},
            [merge_infra.BASE_FAILS, merge_infra.JUDGE_SILENT],
        ),
        # The pull request's: a check failing on the merge result alone, or before the worker ran it on the base
        ({"verify": [{"command": VERIFY, "exit_code": 1, "base_exit_code": 0}]}, []),
        ({"verify": [{"command": VERIFY, "exit_code": 1}]}, []),
        ({"verify": []}, []),  # the plan's verify did not run
        # ... a dimension the Judge failed, with or without a failure of the hub besides
        ({"verdict": "fail", "dimensions": {"correctness": {"verdict": "fail", "reason": "wrong"}}}, []),
        ({"verdict": "fail", "verify": FAILED_ON_BOTH}, []),
        ({"verdict": "fail", "security": None}, []),
        ({"security": {**SECURITY, "verdict": "fail", "reason": "a token in a log"}}, []),
        ({"head_sha": "b2" * 20, "security": None}, []),  # the Judge read another head
    ],
)
def test_merge_infra_a_verdict_is_the_hubs_only_when_nothing_of_the_pull_request_fails_it(body, causes):
    found = judged(**body)
    assert found.passed is (not causes and not body)
    assert found.infra_causes() == causes


def test_merge_infra_reads_a_check_failing_on_the_base_and_the_cause_of_judge_runs_without_a_verdict():
    assert merge_infra.base_failed({"exit_code": 1, "base_exit_code": 2})
    assert not merge_infra.base_failed({"exit_code": 1, "base_exit_code": 0})
    assert not merge_infra.base_failed({"exit_code": 0, "base_exit_code": 1})
    assert not merge_infra.base_failed({"exit_code": 1, "base_exit_code": True})
    assert merge_infra.without_base([{"exit_code": 1, "base_exit_code": 1}, {"exit_code": 1}]) == [
        {"exit_code": 0, "base_exit_code": 1},
        {"exit_code": 1},
    ]
    assert merge_infra.no_verdict_cause("missing_tool") == merge_infra.JUDGE_SETUP
    assert merge_infra.no_verdict_cause("checkout") == merge_infra.JUDGE_SETUP
    assert merge_infra.no_verdict_cause(None) == merge_infra.NO_VERDICT
    assert merge_infra.no_verdict_cause("timeout") == merge_infra.NO_VERDICT
    assert (
        merge_infra.verdict_causes(
            passed=False,
            unweighed_signs=True,  # the queue on GitLab, whose signs fail a change unjudged
            head_matches=True,
            checks_ok=True,
            checks_ok_without_base=True,
            agent=None,
            judge_ok=False,
            judge_ok_with_security=False,
        )
        == []
    )


def test_merge_infra_the_judge_reads_what_a_failed_check_did_on_the_base_branch():
    text = judge.results_text(
        [{"command": VERIFY, "exit_code": 1, "base_exit_code": 1}],
        [{"index": 1, "exit_code": 2, "base_exit_code": 0}, {"index": 2, "exit_code": 0}],
        [],
    )
    assert f"verify `{VERIFY}` exited 1, and exited 1 on the base branch alone too: the base fails it" in text
    assert "hidden check 1 exited 2, and exited 0 on the base branch alone: the change fails it" in text
    assert "- hidden check 2 exited 0\n" in text
    rules = "\n".join(merge_gate._rules("origin/main", HEAD, merge_gate.gate_names()))
    assert "fails on the base branch alone too is the base's" in rules


def row(verdict, **values):
    return SimpleNamespace(verdict=verdict, **values)


def test_merge_holds_keeps_the_hold_and_the_thread_in_the_verdict():
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    first = {"passed": False, "run_id": 9}
    ledger = [{"id": "F1", "status": "open", "dimension": "correctness"}]
    hold = {"causes": ["base_fails"], "reason": "1 verify failed", "rounds": 1, "chat_id": 3, "root_id": 4}
    held = row({"first": first, "paths": ["a"], "findings": ledger, "triage": {**hold, "held_at": now.isoformat()}})
    assert merge_holds.held_at(held) == now
    assert not merge_holds.expired(held, now + timedelta(hours=merge_infra.HOLD_HOURS - 1))
    assert merge_holds.expired(held, now + timedelta(hours=merge_infra.HOLD_HOURS))
    assert merge_holds.thread_of(held).root_id == 4

    again = merge_holds.released(held, "the worker has git 2.43 now")
    assert (again["state"], again["judge_attempts"], again["risk"]) == ("judging", 0, None)
    assert "first" not in again["verdict"] and "paths" not in again["verdict"], "judged again from its first Judge"
    assert again["verdict"]["findings"] == ledger
    assert again["verdict"]["triage"] == hold, "the rounds and the thread stay, the hold ends"
    assert merge_holds.held_at(row(again["verdict"])) is None

    settled = merge_holds.settled(held.verdict, ["no_security"])
    assert merge_holds.failed_by(row(settled)) == ["no_security"] and merge_holds.held_at(row(settled)) is None
    assert merge_holds.failed_by(row(merge_holds.settled(settled, []))) == []

    fresh = merge_holds.at_new_head(held.verdict)
    assert fresh == {"findings": ledger, "triage": {"chat_id": 3, "root_id": 4}}, "a new head: no round held yet"
    assert merge_holds.at_new_head({"first": first}) is None
    assert merge_holds.held_at(row({"triage": {"held_at": "not a time"}})) is None
