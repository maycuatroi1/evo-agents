"""The pure parts of what the Curator's Reviewer remembers and cites (``evo_agents.hub.playbook``, the evidence of a line
of a transcript in ``evo_agents.hub.review``, the status of earlier findings in ``evo_agents.hub.server.earlier``, the
ledger's reading of that evidence in ``evo_agents.hub.ledger``), without Postgres. Step 5 of plan
history-driven-curator, criteria a4 and a11."""

from __future__ import annotations

from datetime import date

import pytest

from evo_agents.hub import ledger, playbook, review
from evo_agents.hub.runs import MAX_PROMPT_BYTES
from evo_agents.hub.server import earlier

SESSION = "0199b1c2-7d3e-7a10-9c41-00000000a001"


# Evidence of a line of a transcript


def test_transcript_evidence_reads_and_writes_the_line_of_a_file_of_a_session():
    assert review.parse_evidence(f"transcript:{SESSION}:12") == {
        "kind": "transcript",
        "session_id": SESSION,
        "file": "main",
        "line": 12,
    }
    sub = review.parse_evidence(f"transcript:{SESSION}:subagents/agent-a1.jsonl:3")
    assert sub == {"kind": "transcript", "session_id": SESSION, "file": "subagents/agent-a1.jsonl", "line": 3}
    for item in (review.parse_evidence(f"transcript:{SESSION}:12"), sub):
        assert review.parse_evidence(review.evidence_text(item)) == item
    assert review.evidence_text({"kind": "transcript", "session_id": SESSION, "line": 4}) == f"transcript:{SESSION}:4"
    for bad, why in (
        (f"transcript:{SESSION}", "transcript:ID:LINE or transcript:ID:FILE:LINE"),
        (f"transcript:{SESSION}:0", "the line must be a whole number from 1"),
        (f"transcript:{SESSION}:other/file:3", "a transcript's file is main or subagents/..."),
        ("transcript:bad id:3", "transcript:ID:LINE"),
        ("line:x:3", "transcript:ID[:FILE]:LINE"),
    ):
        with pytest.raises(review.EvidenceProblem, match=why.replace("[", r"\[").replace("]", r"\]")):
            review.parse_evidence(bad)
    assert "transcript" in review.EVIDENCE_KINDS


def test_a_line_of_a_transcript_points_the_ledger_at_what_its_session_does():
    figures = {
        "environment": [{"cause": "network", "count": 3, "evidence": [f"session:{SESSION}:errors:0"], "runs": []}],
        "repeated_commands": [{"command": "sleep 5", "times": 4, "evidence": [f"session:{SESSION}"]}],
        "corrections": [{"evidence": f"session:{SESSION}:user_turns:2", "text": "no"}],
    }
    keys = ledger.evidence_metrics(figures, [f"transcript:{SESSION}:40"])
    assert keys == ["environment:network", "repeated:sleep 5", "corrections"]
    assert ledger.evidence_metrics(figures, ["transcript:another-session:40"]) == []
    assert ledger.evidence_metrics(figures, [f"session:{SESSION}"]) == ["repeated:sleep 5"]  # as before


# The playbook


def test_the_playbook_ranks_the_live_entries_of_the_nights_lenses_the_most_helpful_first():
    entries = [
        {"id": 1, "lens": "cost", "text": "Count cache reads.", "helpful": 1, "harmful": 0},
        {"id": 2, "lens": None, "text": "Numbers first.", "helpful": 3, "harmful": 1},
        {"id": 3, "lens": "environment", "text": "Pull before a push.", "helpful": 2, "harmful": 0},
        {"id": 4, "lens": "environment", "text": "Old.", "helpful": 9, "harmful": 0, "retired_at": "2026-10-01"},
        {"id": 5, "lens": "environment", "text": "Tie, newer.", "helpful": 2, "harmful": 0},
    ]
    assert [entry["id"] for entry in playbook.ranked(entries, ["environment"])] == [2, 3, 5]
    assert [entry["id"] for entry in playbook.ranked(entries)] == [2, 3, 5, 1]
    assert playbook.prompt_lines(entries, ["cost"]) == [
        "- pb-2 (every lens, helpful 3, harmful 1): Numbers first.",
        "- pb-1 (cost, helpful 1, harmful 0): Count cache reads.",
    ]
    assert playbook.prompt_lines(entries, ["cost"], most=1) == [
        "- pb-2 (every lens, helpful 3, harmful 1): Numbers first."
    ]
    assert playbook.parse_ref("pb-12") == playbook.parse_ref("12") == 12
    for bad in ("pb-0", "pb-x", "", "12a"):
        with pytest.raises(playbook.PlaybookProblem):
            playbook.parse_ref(bad)
    assert playbook.clean_text("  a\n lesson  ") == "a lesson"
    with pytest.raises(playbook.PlaybookProblem, match="at most 500 characters"):
        playbook.clean_text("x" * 501)
    assert playbook.OPS == ("add", "helpful", "harmful", "retire")


# Earlier findings


def test_a_finding_takes_its_status_from_its_proposals_and_their_merged_changes():
    status = earlier.proposal_status
    assert status("open", None, None, None) == "proposed"
    assert status("accepted", "judging", None, None) == "proposed"
    assert status("rejected", None, None, None) == status("dropped", None, None, None) == "rejected"
    assert status("accepted", "merged", "2026-10-10", None) == "merged"
    assert status("accepted", "merged", "2026-10-10", "unclear") == "merged"
    assert status("accepted", "merged", "2026-10-10", "keep") == "kept"
    assert status("accepted", "merged", "2026-10-10", "revert") == "reverted"
    assert earlier.finding_status([]) == "open"
    assert earlier.finding_status(["rejected", "proposed"]) == "proposed"
    assert earlier.finding_status(["rejected", "reverted"]) == "reverted"
    assert earlier.finding_status(["merged", "kept"]) == "kept"
    assert earlier.CLOSED == ("rejected", "merged", "kept")
    assert earlier.similar("The push to origin is rejected", "Push to origin rejected")
    assert not earlier.similar("The push to origin is rejected", "Tests flake on the queue")
    assert not earlier.similar("", "anything")


# The prompt


def test_the_prompt_holds_earlier_nights_rejected_proposals_and_the_playbook_within_its_bounds():
    found = {
        "id": 7,
        "night": "2026-10-08",
        "severity": "high",
        "status": "rejected",
        "title": "The push is rejected",
        "proposals": [{"id": 3, "state": "rejected"}],
    }
    remembered = {
        "findings": {"environment": [found], "cost": []},
        "rejected": [{"id": 3, "lens": "environment", "title": "A retry", "note": "not now"}],
    }
    text = review.build_review_prompt(
        "demo",
        date(2026, 10, 11),
        ["environment", "cost"],
        [],
        {},
        earlier=remembered,
        playbook=["- pb-1 (environment, helpful 2, harmful 0): Pull before a push."],
    )
    assert "- environment #7 (2026-10-08, high, rejected: proposal #3 rejected): The push is rejected" in text
    assert "- #3 (environment): A retry; the owner's note: not now" in text
    assert "- pb-1 (environment, helpful 2, harmful 0): Pull before a push." in text
    bare = review.build_review_prompt("demo", None, ["cost"], [], {})
    assert "- nothing yet" in bare and "- empty: add what you learn tonight" in bare
    assert "Proposals the owner rejected" not in bare
    many = {"findings": {"cost": [{**found, "title": "x" * 190, "id": n} for n in range(400)]}, "rejected": []}
    lines = [f"- pb-{n} (cost, helpful 0, harmful 0): " + "y" * 450 for n in range(40)]
    big = review.build_review_prompt("demo", None, ["cost"], [], {}, earlier=many, playbook=lines)
    assert len(big.encode()) <= MAX_PROMPT_BYTES and "# The night's figures" in big
