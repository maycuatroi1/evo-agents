"""The playbook's maintenance: entries that hold until a proposal lands, and entries shown often and never helpful."""

from evo_agents.hub import playbook


def test_the_proposal_an_entry_holds_until_is_read_from_its_text():
    assert playbook.until_proposal("Hold builds until proposal #61 lands") == 61
    assert playbook.until_proposal("skip the probe; #62 lands next week") == 62
    assert playbook.until_proposal("until #7 lands") == 7
    assert playbook.until_proposal("see #4 for why") is None
    assert playbook.until_of({"until_proposal_id": 9, "text": "until proposal #3 lands"}) == 9
    assert playbook.until_of({"until_proposal_id": None, "text": "until proposal #3 lands"}) == 3


def test_the_maintenance_retires_entries_whose_proposal_merged_or_that_never_helped():
    entries = [
        {"id": 3, "text": "until proposal #61 lands"},
        {"id": 4, "text": "until proposal #62 lands"},
        {"id": 5, "text": "x", "until_proposal_id": 61, "retired_at": "2026-11-01"},
        {"id": 6, "text": "x", "prompt_exposures": 30, "helpful": 0, "harmful": 2},
        {"id": 7, "text": "x", "prompt_exposures": 30, "helpful": 1},
        {"id": 8, "text": "x", "prompt_exposures": 29, "helpful": 0},
    ]
    due = playbook.due_retires(entries, {61: "merged", 62: "open"})
    assert [entry_id for entry_id, _ in due] == [3, 6]
    assert "#61" in due[0][1] and "30 review runs" in due[1][1]
    assert [entry_id for entry_id, _ in playbook.due_retires(entries, {}, exposure_limit=29)] == [6, 8]
    assert playbook.due_retires(entries, {}, exposure_limit=0) == []
