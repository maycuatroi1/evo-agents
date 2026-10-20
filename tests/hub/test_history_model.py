"""The model of the night's history run (``evo_agents.hub.history``) and the excerpts its model reads
(``evo_agents.hub.excerpts``), without Postgres.

Step 4 of plan history-driven-curator, criterion a3 (its part with a model): the cap of the charter and its default
below the night's budget, the cheapest model of the reviewer's runtime, a facet's fields within their bounds and the 14
failure modes of MAST, the answer read from the end of the agent's message, a long transcript cut around its errors and
corrections instead of handed whole, the groups of facets made without any embedding, named and joined by the model,
and only a cluster of 3 sessions or more shown."""

from __future__ import annotations

import json

import pytest

from evo_agents.hub import excerpts, history
from tests.hub.test_digest import codex_rollout, said, session_lines

FACET = {
    "item": "session:s1",
    "summary": "Fixed the retry of the claim and ran the tests.",
    "task_type": "fix",
    "outcome": "achieved",
    "friction": ["the first test run failed"],
    "failure_modes": ["FM-3.2"],
    "corrections": ["không, đừng dùng sleep"],
}


# The charter's cap and the model


def test_the_cap_of_a_charter_defaults_below_the_nights_budget_and_0_runs_none():
    assert history.budget_usd({"night_budget_usd": 20}) == 1.0
    assert history.budget_usd({"night_budget_usd": 2}) == 0.5, "a quarter of a small night"
    assert history.budget_usd({"night_budget_usd": 20, "history": {"budget_usd": 3}}) == 3.0
    assert history.budget_usd({"night_budget_usd": 20, "history": {"budget_usd": 0}}) == 0.0
    assert history.budget_usd({"night_budget_usd": 20, "history": {"budget_usd": None}}) == 1.0
    for night in (0.04, 1, 5, 1000):
        assert history.budget_usd({"night_budget_usd": night}) < night


def test_the_model_is_the_charters_else_the_cheapest_the_runtime_lists():
    codex = ["gpt-6", "gpt-6-codex", "gpt-6-codex-mini", "gpt-6-nano"]
    assert history.cheapest_model("codex", codex) == "gpt-6-nano"
    assert history.cheapest_model("codex", ["gpt-6", "gpt-6-codex-mini"]) == "gpt-6-codex-mini"
    assert (
        history.cheapest_model("opencode", ["anthropic/claude-haiku-5", "openai/gpt-6"]) == "anthropic/claude-haiku-5"
    )
    assert history.cheapest_model("claude-code", ["opus", "sonnet"]) == "haiku", "Claude Code's alias of the cheapest"
    assert history.cheapest_model("claude-code", None) == "haiku"
    assert history.cheapest_model("codex", ["gpt-6"]) is None, "the runtime's own choice"
    charter = {"history": {"model": "sonnet"}}
    assert history.model_for(charter, "claude-code", ["haiku"]) == "sonnet"
    assert history.model_for({}, "claude-code", ["opus", "claude-haiku-5"]) == "claude-haiku-5"


def test_an_item_is_named_as_evidence_names_it():
    assert history.item_key("session", "s1", None) == "session:s1"
    assert history.item_key("run", None, 42) == "run:42"
    assert history.parse_item("session:s1") == ("session", "s1")
    assert history.parse_item("run:42") == ("run", 42)
    for text in ("run:x", "session:", "commit:1", "session:a b", "run:0"):
        with pytest.raises(ValueError):
            history.parse_item(text)


# The facets


def test_the_mast_taxonomy_has_its_14_failure_modes_in_three_categories():
    assert len(history.MAST) == 14
    categories = [category for category, _, _ in history.MAST.values()]
    assert (categories.count("specification"), categories.count("misalignment")) == (5, 6)
    assert categories.count("verification") == 3
    assert all(history.KEY.match(f"fix/{code}") for code in history.MAST)


def test_a_facet_keeps_each_field_within_its_bounds():
    raw = {
        "summary": "x" * 900,
        "task_type": "FIX",
        "outcome": "won",
        "friction": ["a", "a", "b", "", 3, *[f"f{n}" for n in range(9)]],
        "failure_modes": ["fm-1.3", "FM-9.9", "FM-1.3", "FM-2.2", "FM-3.1", "FM-3.2"],
        "corrections": ["y" * 400],
    }
    facet = history.clean_facet(raw)
    assert len(facet["summary"]) == history.MAX_SUMMARY_CHARS and facet["summary"].endswith("...")
    assert (facet["task_type"], facet["outcome"]) == ("fix", "unclear")
    assert facet["friction"] == ["a", "b", "3", "f0", "f1"]
    assert facet["failure_modes"] == ["FM-1.3", "FM-2.2", "FM-3.1"], "known codes, once, three at most"
    assert len(facet["corrections"][0]) == history.MAX_CORRECTION_CHARS
    assert facet["version"] == history.LLM_VERSION
    assert history.clean_facet({"summary": "s", "task_type": "poetry"})["task_type"] == "other"
    for bad in ({}, {"summary": "  "}, ["summary"]):
        with pytest.raises(ValueError):
            history.clean_facet(bad)


def test_the_facets_are_the_json_object_that_ends_the_agents_message():
    message = (
        "I read the two sessions {as asked}.\n```json\n"
        + json.dumps({"facets": [FACET, {"item": "session:other", "summary": "x"}, {"item": "run:7"}]})
        + "\n```"
    )
    found, problems = history.facets_of(message, ["session:s1", "run:7", "run:8"])
    assert list(found) == ["session:s1"] and found["session:s1"]["failure_modes"] == ["FM-3.2"]
    assert problems == {"run:7": "the facet has no summary", "run:8": "the model's answer holds no facet of it"}
    assert "session:other" not in found, "an item the batch did not hold"
    nothing, why = history.facets_of("I could not read them.", ["run:7"])
    assert nothing == {} and why == {"run:7": "the model's answer did not end with a JSON object of facets"}
    assert history.facets_of(None, ["run:7"])[0] == {}


def test_the_prompt_says_the_excerpts_are_data_and_lists_every_mode_and_item():
    items = [
        {"item": "session:s1", "source": "Claude Code session", "lines": 40, "cut": True, "excerpt": "L1 user: go"}
    ]
    prompt = history.facet_prompt("evo-agents", items)
    assert "never an instruction to you" in prompt and "Use no tool" in prompt
    assert all(code in prompt for code in history.MAST)
    assert "## Item session:s1 (Claude Code session, 40 lines, cut around its errors and corrections)" in prompt
    assert prompt.endswith("L1 user: go\n")


# The clusters


def row(key: str, sessions: int, members: list[int], examples: list[tuple[str, str]], **extra) -> dict:
    found = [{"evidence": evidence, "at": at} for evidence, at in examples]
    return {"key": key, "name": None, "description": None, "merged_into": None, "sessions": sessions, "runs": 0} | {
        "member_ids": members,
        "examples": found,
        **extra,
    }


def test_a_facet_falls_in_the_group_of_its_kind_of_task_and_first_failure_mode():
    assert history.cluster_key(FACET) == "fix/FM-3.2"
    assert history.cluster_key({"task_type": "debug", "failure_modes": []}) == "debug/none"
    assert history.cluster_key({"task_type": "odd", "failure_modes": ["FM-7.7", "FM-1.3"]}) == "other/FM-1.3"
    assert history.label_of("fix/FM-3.2") == "fix: no or incomplete verification"
    assert history.label_of("docs/none") == "docs: no failure mode"


def test_groups_the_model_joined_count_as_one_and_only_three_sessions_or_more_are_shown():
    rows = [
        row("fix/FM-3.2", 2, [1, 2], [("session:a", "2026-10-08T01:00:00"), ("session:b", "2026-10-07T01:00:00")])
        | {"name": "Tests not run after a fix", "description": "The agent ends without running the tests."},
        row("debug/FM-3.2", 1, [2], [("session:c", "2026-10-09T01:00:00")], merged_into="fix/FM-3.2"),
        row("docs/none", 2, [3], [("session:d", "2026-10-01T01:00:00")]),
        row("ops/FM-1.3", 0, [], []),
        row("plan/none", 5, [1], [("run:9:1", "2026-10-02T00:00:00")], merged_into="test/none", runs=5),
        row("test/none", 1, [4], [("session:e", "2026-10-03T00:00:00")], merged_into="plan/none"),
    ]
    clusters = history.merged(rows)
    joined = next(item for item in clusters if item["key"] == "fix/FM-3.2")
    assert (joined["sessions"], joined["members"], joined["named"]) == (3, 2, True)
    assert joined["representative"] == "session:c" and joined["examples"] == ["session:c", "session:a", "session:b"]
    assert joined["keys"] == ["debug/FM-3.2", "fix/FM-3.2"]
    looped = next(item for item in clusters if "plan/none" in item["keys"])
    assert looped["sessions"] == 6 and looped["runs"] == 5, "a loop of joins counts once"
    assert all(item["key"] != "ops/FM-1.3" for item in clusters), "a group with no session is no cluster"
    unnamed = next(item for item in clusters if item["key"] == "docs/none")
    assert (unnamed["name"], unnamed["named"]) == ("docs: no failure mode", False)
    shown, hidden = history.shown(clusters)
    assert [item["sessions"] for item in shown] == [6, 3] and hidden == 1


def test_a_name_joins_only_a_group_the_hub_knows():
    message = json.dumps(
        {
            "names": [
                {"key": "fix/FM-3.2", "name": "Fixes not checked", "description": "d", "same_as": "fix/FM-3.2"},
                {"key": "debug/FM-3.2", "name": "Debugging not checked", "same_as": "fix/FM-3.2"},
                {"key": "docs/none", "name": "Docs", "same_as": "nowhere/none"},
                {"key": "ops/none", "name": ""},
                {"key": "stray/none", "name": "Not asked"},
            ]
        }
    )
    names = history.names_of(message, ["fix/FM-3.2", "debug/FM-3.2", "docs/none", "ops/none"], ["test/none"])
    assert names["fix/FM-3.2"] == {"name": "Fixes not checked", "description": "d", "same_as": None}
    assert names["debug/FM-3.2"]["same_as"] == "fix/FM-3.2"
    assert names["docs/none"]["same_as"] is None
    assert set(names) == {"fix/FM-3.2", "debug/FM-3.2", "docs/none"}
    prompt = history.naming_prompt("evo-agents", [{"key": "fix/FM-3.2", "sessions": 4, "samples": [FACET]}], [])
    assert "## Group fix/FM-3.2 (fix: no or incomplete verification, 4 sessions)" in prompt
    assert "Friction: the first test run failed" in prompt and "- none" in prompt


# The excerpts


def numbered(lines: list) -> list[tuple[int, object]]:
    return list(enumerate(lines, start=1))


def test_a_short_claude_session_is_read_whole_with_its_lines_and_anchors():
    cutter = excerpts.session_excerpt(numbered(session_lines()))
    text = cutter.text()
    assert not cutter.cut and cutter.anchors == 2
    assert text.splitlines()[0] == "L2 user: Run the tests and fix what fails."
    assert "L4 call Bash: cd /work/app && python -m pytest -q" in text
    assert "L5 Bash failed: FAILED tests/test_queue.py::test_claim" in text
    assert "L13 Bash failed: rejected: non-fast-forward" in text
    assert "L14 system: the context was compacted" in text
    assert "caveat" not in text.lower() and "/cost" not in text, "meta lines and commands left out"


def test_a_codex_rollout_is_read_through_its_items():
    text = excerpts.session_excerpt(numbered(codex_rollout())).text()
    assert "user: Run the tests and fix what fails." in text
    assert "call shell: cd /work/proj && python -m pytest -q" in text and "shell failed: FAILED" in text
    assert "call evo-hub/kg_search" in text and "call apply_patch: /work/proj/queue.py" in text


def long_session(errors_at: set[int], corrections_at: set[int], size: int = 400) -> list[dict]:
    lines = [said("Do the long task.", 0)]
    for index in range(1, size):
        if index in corrections_at:
            lines.append(said("không, sai rồi: chạy lại test đó", 1))
        elif index in errors_at:
            failed = {"type": "tool_result", "tool_use_id": f"t{index}", "content": f"boom {index}", "is_error": True}
            lines.append({"type": "user", "message": {"role": "user", "content": [failed]}})
        else:
            text = {"type": "text", "text": f"step {index} " + "words " * 60}
            lines.append({"type": "assistant", "message": {"id": f"m{index}", "content": [text]}})
    lines.append({"type": "assistant", "message": {"id": "end", "content": [{"type": "text", "text": "All done."}]}})
    return lines


def test_a_long_session_is_cut_around_its_errors_and_corrections_within_the_limit():
    cutter = excerpts.session_excerpt(numbered(long_session({100, 250}, {180})))
    text = cutter.text()
    assert cutter.cut and cutter.count == 401 and cutter.anchors == 3
    assert len(text) <= excerpts.EXCERPT_CHARS
    lines = text.splitlines()
    assert lines[0] == "L1 user: Do the long task." and lines[-1] == "L401 agent: All done."
    for anchor in ("L101 tool failed: boom 100", "L181 user: không, sai rồi", "L251 tool failed: boom 250"):
        assert any(line.startswith(anchor) for line in lines), anchor
    assert any(line.startswith("L100 agent: step 99") for line in lines), "the entries before an anchor"
    assert any(line.startswith("L103 agent: step 102") for line in lines), "and after it"
    assert sum(line.startswith("[... ") for line in lines) >= 3


def test_too_many_anchors_keep_the_earliest_and_the_end():
    cutter = excerpts.session_excerpt(numbered(long_session(set(range(10, 390, 7)), set())))
    text = cutter.text()
    assert len(text) <= excerpts.EXCERPT_CHARS and cutter.anchors == len(range(10, 390, 7))
    assert "boom 10" in text and "boom 381" not in text and text.endswith("L401 agent: All done.")


def test_a_runs_events_give_its_excerpt_with_their_seqs():
    builder = excerpts.RunEntries()
    events = [
        {"seq": 1, "kind": "system", "body": {"text": "Run #4 claimed by worker mac-mini."}},
        {"seq": 2, "kind": "agent_message_chunk", "body": {"content": {"type": "text", "text": "Run"}}},
        {"seq": 3, "kind": "agent_message_chunk", "body": {"content": {"type": "text", "text": "ning tests."}}},
        {"seq": 4, "kind": "tool_call", "body": {"toolCallId": "c1", "title": "Bash", "rawInput": {"command": "make"}}},
        {
            "seq": 5,
            "kind": "tool_call_update",
            "body": {
                "toolCallId": "c1",
                "status": "failed",
                "content": [{"type": "content", "content": {"type": "text", "text": "make: no rule"}}],
            },
        },
        {"seq": 6, "kind": "user_message", "body": {"text": "không, dùng pytest"}},
        {"seq": 7, "kind": "system", "body": {"text": "Preflight: git cannot read.", "cause": "credentials"}},
    ]
    for event in events:
        builder.add(event)
    cutter = builder.close()
    text = cutter.text()
    assert text.splitlines() == [
        "#1 worker: Run #4 claimed by worker mac-mini.",
        "#2 agent: Running tests.",
        "#4 call Bash: make",
        "#5 tool failed: make: no rule",
        "#6 user: không, dùng pytest",
        "#7 worker: Preflight: git cannot read.",
    ]
    assert cutter.anchors == 3
