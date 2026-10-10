"""The decisions of a run on the worker that need no I/O (``evo_agents.worker.runner.transitions``): how it ends,
what it reports, which branch it works on, when the watchdog stops it, what the inbox holds for its agent, and what
its result file says.

They need neither a hub nor the worker extra, so they run on every job, macOS among them; the run's whole life,
through the daemon and a hub, is in tests/worker.
"""

import pytest

from evo_agents.worker.runner.common import RunFailed
from evo_agents.worker.runner.transitions import (
    MAX_ERROR_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_USAGE_BYTES,
    MAX_VERIFY,
    Ending,
    answer_note,
    bare_report,
    cap_passed,
    end_fields,
    failure_ending,
    final_state,
    handed,
    protected_hit,
    report_body,
    result_commands,
    run_record,
    running_from,
    start_refs,
    stop_ending,
    summary_of,
    unread,
    verify_failure,
    waits_for_owner,
    why_stopped,
    working_branch,
)


def stop(reason: str, **extra) -> Ending | None:
    args = {"timeout_s": 3600, "state": "running", "worker": "mac", "watchdog": None, "verify": []} | extra
    return stop_ending(reason, **args)


def test_a_run_the_hub_let_go_of_ends_without_a_report():
    assert stop("gone") is None


def test_a_cancelled_run_ends_cancelled_with_nothing_else():
    assert stop("cancel") == Ending("Cancelled by the owner.", "cancelled")


def test_a_run_past_its_timeout_fails_with_its_verify_results_and_the_cause_timeout():
    verify = [{"command": "true", "exit_code": 0, "duration_ms": 3}]
    ending = stop("timeout", timeout_s=1800, verify=verify)
    error = "it ran past its timeout of 30 minutes"
    assert ending == Ending(
        f"Run failed: {error}.", "failed", {"error": error, "verify": verify, "failure_cause": "timeout"}
    )
    assert stop("timeout").fields["verify"] is None, "no verify ran: the report carries none"


def test_a_run_the_watchdog_stopped_fails_naming_why_in_its_log():
    why = "app:.github/workflows/ci.yml is protected by the charter (.github/**)"
    ending = stop("watchdog", watchdog=why)
    assert ending.state == "failed"
    assert ending.fields == {"error": f"the watchdog of the Curator's runs stopped it: {why}"}
    assert ending.extra == {"watchdog": why}


@pytest.mark.parametrize("reason", ["shutdown", "anything else"])
def test_a_run_stopped_with_its_worker_fails_naming_the_worker_and_the_state(reason):
    ending = stop(reason, state="verifying", worker="mac")
    error = "the worker mac was stopped while the run was verifying"
    assert ending == Ending(f"Run failed: {error}.", "failed", {"error": error, "failure_cause": "worker_stopped"})


def test_a_failure_keeps_its_cause_and_otherwise_takes_the_cause_of_the_cap_that_stopped_the_agent():
    usage = {"cost": {"amount": 0.5}}
    capped = failure_ending(RunFailed("claude-code stopped at the run's cost cap", usage=usage, cap="cost"))
    assert capped.note == "Run failed: claude-code stopped at the run's cost cap"
    assert capped.extra == {"cap": "cost"}
    assert capped.fields == {
        "error": "claude-code stopped at the run's cost cap",
        "verify": None,
        "usage": usage,
        "failure_cause": "cost_cap",
    }
    assert failure_ending(RunFailed("git fetch failed", cause="credentials", cap="time")).fields["failure_cause"] == (
        "credentials"
    )
    plain = failure_ending(RunFailed("the agent did not write .evo-run/result.json"))
    assert plain.extra == {}
    assert plain.fields["failure_cause"] is None


def test_why_the_agent_is_stopped_reads_each_reason_and_passes_an_unknown_one_through():
    assert why_stopped("timeout") == "the run reached its timeout"
    assert why_stopped("gone") == "the hub no longer holds the run for this worker"
    assert why_stopped("something") == "something"


def test_the_last_report_cuts_its_error_drops_a_usage_too_large_and_names_the_agents_session():
    fields = end_fields({"error": "x" * (MAX_ERROR_CHARS + 10), "usage": {"blob": "y" * MAX_USAGE_BYTES}}, "s-1")
    assert len(fields["error"]) == MAX_ERROR_CHARS
    assert fields["error"].endswith("...")
    assert fields["usage"] is None
    assert fields["session_id"] == "s-1"
    assert end_fields({"session_id": "s-0", "usage": {"turns": 3}}, "s-1") == {
        "session_id": "s-0",
        "usage": {"turns": 3},
    }


def test_a_report_sends_the_fields_that_have_a_value_and_its_bare_form_keeps_the_move_and_the_error():
    body = report_body("failed", {"error": "boom", "verify": None, "usage": {"turns": 1}})
    assert body == {"state": "failed", "error": "boom", "usage": {"turns": 1}}
    assert bare_report(body) == {"state": "failed", "error": "boom"}
    assert bare_report({"state": "done", "summary": "ok"}) == {"state": "done"}


def test_a_step_run_that_pushed_ends_done_only_when_the_plan_approves_it_automatically():
    assert final_state("auto") == "done"
    assert final_state("review") == "review"


def test_the_first_verify_command_that_fails_fails_the_run_and_a_missing_program_is_a_missing_tool():
    ok = {"command": "pytest", "exit_code": 0, "duration_ms": 10}
    assert verify_failure([ok], "/w") is None
    missing = {"command": "ruff check .", "exit_code": 127, "duration_ms": 1}
    error, cause = verify_failure([ok, missing, {"command": "false", "exit_code": 1, "duration_ms": 1}], "/w")
    assert error == "verify command `ruff check .` exited 127; nothing was pushed, the work stays in /w"
    assert cause == "missing_tool"
    assert verify_failure([{"command": "false", "exit_code": 1, "duration_ms": 1}], "/w")[1] == "verify_failed"


def test_the_report_of_running_leaves_interactive_or_waiting_when_the_run_is_there_else_leased():
    assert running_from("interactive") == ("interactive",)
    assert running_from("waiting") == ("waiting",)
    assert running_from("leased") == ("leased",)
    assert running_from("running") == ("leased",)


def test_the_inbox_holds_for_the_agent_what_it_has_not_had_and_handing_it_over_answers_its_decisions():
    messages = [{"id": 3, "text": "a"}, {"id": 5, "text": "b", "decision_id": 9}, {"id": "x"}, "junk", {"id": 2}]
    assert unread(messages, 2) == [{"id": 3, "text": "a"}, {"id": 5, "text": "b", "decision_id": 9}]
    assert unread(messages, 5) == []
    assert handed(unread(messages, 2), 2) == (5, {9})
    assert handed([{"text": "no id"}], 4) == (4, set())


def test_a_plan_run_waits_for_its_owner_while_a_decision_is_open():
    assert waits_for_owner({4}, 0)
    assert waits_for_owner(set(), 1)
    assert not waits_for_owner(set(), 0)


def test_the_note_of_an_answer_names_the_decisions_it_answers_and_the_session_that_goes_on():
    messages = [{"id": 1, "decision_id": 7}, {"id": 2, "decision_id": 3}, {"id": 3}]
    assert (
        answer_note(messages, "s-9") == "The owner wrote answering decision #3, #7: the agent goes on in session s-9."
    )
    assert answer_note([{"id": 4}], None) == "The owner wrote: the agent goes on in session (new)."


def test_a_worktree_starts_from_origins_branch_then_the_local_one_then_the_default_branch():
    assert start_refs("feat/x") == (
        "refs/remotes/origin/feat/x",
        "refs/heads/feat/x",
        "refs/remotes/origin/HEAD",
        "HEAD",
    )


def test_a_branch_checked_out_elsewhere_or_ahead_here_sends_the_worktree_to_the_side_branch():
    assert working_branch("feat/x", True, set(), "evo-run/7") == ("feat/x", None)
    assert working_branch("feat/x", False, {"feat/x"}, "evo-run/7") == (
        "evo-run/7",
        "is checked out in another worktree",
    )
    assert working_branch("feat/x", False, set(), "evo-run/7") == ("evo-run/7", "has commits here that it lacks")


def test_the_watchdog_stops_a_run_whose_worktree_changed_a_protected_path_of_its_repo():
    globs = ["app:.github/**", "docs/charter.md"]
    assert protected_hit("app", ["src/a.py", ".github/workflows/ci.yml"], globs) == (
        "app:.github/workflows/ci.yml is protected by the charter (app:.github/**)"
    )
    assert protected_hit("lib", [".github/workflows/ci.yml"], globs) is None, "a glob of another repo"
    assert (
        protected_hit("lib", ["docs/charter.md"], globs)
        == "lib:docs/charter.md is protected by the charter (docs/charter.md)"
    )
    assert protected_hit("app", [], globs) is None


def test_the_watchdog_stops_a_run_past_its_time_cap_first_then_past_its_cost_cap():
    budget = {"max_seconds": 600, "max_usd": 0.5}
    assert cap_passed(budget, 599, 0.4) is None
    assert cap_passed(budget, 601, 0.9) == "the run used more than its time cap of 10 minutes of agent time"
    assert cap_passed(budget, 10, 0.51) == "the run cost more than its cost cap of $0.50"
    assert cap_passed({}, 10**6, 10**6) is None, "a run without caps"


def test_a_result_file_lists_the_verify_commands_a_run_of_one_step_runs_again():
    assert result_commands({"verify_commands": ["pytest -q", "ruff check ."]}) == ["pytest -q", "ruff check ."]
    for data, said in [
        ({}, "lists no verify_commands"),
        ([], "lists no verify_commands"),
        ({"verify_commands": []}, "lists no verify_commands"),
        ({"verify_commands": ["true"] * (MAX_VERIFY + 1)}, f"at most {MAX_VERIFY} are run"),
        ({"verify_commands": ["  "]}, "is a shell command of 1 to"),
        ({"verify_commands": [3]}, "is a shell command of 1 to"),
    ]:
        with pytest.raises(RunFailed, match=said):
            result_commands(data)


def test_the_agents_summary_is_trimmed_and_cut_and_missing_when_blank():
    assert summary_of({"summary": "  Done.  "}) == "Done."
    assert len(summary_of({"summary": "z" * (MAX_SUMMARY_CHARS + 1)})) == MAX_SUMMARY_CHARS
    assert summary_of({"summary": "   "}) is None
    assert summary_of({"summary": 3}) is None
    assert summary_of(["not", "an", "object"]) is None


def test_the_record_of_a_claimed_run_names_its_step_and_for_the_curator_what_its_pushes_go_with():
    spec = {"id": "7", "project": "demo", "repo": "app", "runtime": "codex", "plan_id": "p", "step_key": "2"}
    record = run_record(spec, "2026-10-10T00:00:00+00:00", None)
    assert record == {
        "id": 7,
        "kind": "step",
        "project": "demo",
        "plan_id": "p",
        "step_key": "2",
        "repo": "app",
        "branch": None,
        "runtime": "codex",
        "claimed_at": "2026-10-10T00:00:00+00:00",
        "finished_at": None,
        "state": "leased",
    }
    role = {"role": "builder", "branch": "curator/x", "forge": "github", "change_id": 4, "protected_paths": ["a"]}
    built = run_record(spec | {"kind": "plan"}, "t", role)
    assert built["kind"] == "plan"
    assert built["curator"] == {"role": "builder", "branch": "curator/x", "forge": "github", "change_id": 4}
