"""A step run waits in the queue while another run of its plan and repo edits a file the step names
(plan super-board-parity-2, a5): the path reading and the match, which need no database."""

from evo_agents.hub import overlap


def edit(call: str, path: str) -> dict:
    body = {"toolCallId": call, "title": "Edit", "kind": "edit", "rawInput": {"file_path": path}}
    return {"seq": int(call), "at": "2026-10-30T10:00:00Z", "kind": "tool_call", "body": body}


def test_overlap_wait_reads_the_paths_a_step_names():
    what = "Change `evo_agents/hub/claims.py` and the tests under tests/hub/, see https://example.org/a/b.py"
    assert overlap.step_paths(what, "also docs/workers.md.") == [
        "evo_agents/hub/claims.py",
        "tests/hub/",
        "docs/workers.md",
    ]
    assert overlap.step_paths(None, "nothing to see, e.g. this") == []


def test_overlap_wait_meets_an_edited_file_at_a_directory_boundary():
    edited = ["/w/run/evo_agents/hub/claims.py", "/w/run/evo_agents/hub/other.py", "/w/run/tests/hub/test_a.py"]
    assert overlap.overlapping(["hub/claims.py"], edited) == [edited[0]]
    assert overlap.overlapping(["laims.py"], edited) == []
    assert overlap.overlapping(["tests/hub/"], edited) == [edited[2]]
    assert overlap.overlapping([], edited) == []


def test_overlap_wait_reads_the_edited_files_from_tool_call_events():
    events = [edit("1", "/w/x/evo_agents/a.py"), edit("2", "/w/x/docs/b.md")]
    assert overlap.edited_paths("claude-code", events) == ["/w/x/evo_agents/a.py", "/w/x/docs/b.md"]
