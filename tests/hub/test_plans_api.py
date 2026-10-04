"""The read side of the plans API that the hub web uses (step 26 of the agent-hub plan).

The checks: the list counts the steps done of each plan, a step's evidence comes back verbatim, the diff between
two revisions names exactly the lines that changed (numbered as in the git copy), and reading needs a grant: a hub
admin without one gets 403 on every read, a stranger the 404 of a project it cannot see, and a member whose level
stops below a plan's label the same 404 as for a plan that does not exist. The line diff itself runs without
Postgres and is checked against ``difflib.unified_diff``; the routes skip without EVO_HUB_TEST_DSN.
"""

import copy
import difflib
import random

import pytest

from evo_agents.hub.mirror import plan_text, read_plan
from evo_agents.hub.plan_diff import MAX_CONTEXT, diff_lines, plan_diff
from tests.hub import live
from tests.hub.test_plans import COMPLETED, PROJECT, draft, needs_pg, plan_url, put, setup_project

EVIDENCE = (
    "evo-agents@0960c9b: pnpm test 41 passed; playwright 26 passed.\n"
    "  Dòng thụt lề giữ nguyên, và ký tự đặc biệt: <script>, `code`, ${x}, 100%."
)


@pytest.fixture
def client(hub_db, tmp_path, github):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app

    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github))) as client:
        yield client


def fixture_body(stem: str) -> dict:
    return read_plan(next(path for path in COMPLETED if path.stem == stem)).body


def unified(hunks) -> list[str]:
    """Hunks written as ``diff -u`` writes them, without the two file header lines."""
    out = []
    for hunk in hunks:
        old = f"{hunk.old_start},{hunk.old_lines}" if hunk.old_lines != 1 else f"{hunk.old_start}"
        new = f"{hunk.new_start},{hunk.new_lines}" if hunk.new_lines != 1 else f"{hunk.new_start}"
        out.append(f"@@ -{old} +{new} @@")
        out += [{"context": " ", "added": "+", "removed": "-"}[line.kind] + line.text for line in hunk.lines]
    return out


# The line diff


@pytest.mark.parametrize("seed", range(40))
@pytest.mark.parametrize("context", [0, 1, 3])
def test_diff_lines_is_unified_diff_with_numbered_lines(seed, context):
    rng = random.Random(seed)
    old = [rng.choice("abcdef") for _ in range(rng.randint(0, 30))]
    new = list(old)
    for _ in range(rng.randint(0, 6)):
        where = rng.randint(0, len(new))
        action = rng.choice(["insert", "delete", "replace"])
        if action == "insert" or not new:
            new.insert(where, rng.choice("uvwxyz"))
        elif action == "delete":
            del new[min(where, len(new) - 1)]
        else:
            new[min(where, len(new) - 1)] = rng.choice("uvwxyz")

    found = diff_lines(old, new, context)
    expected = list(difflib.unified_diff(old, new, n=context, lineterm=""))[2:]
    assert unified(found.hunks) == expected
    assert found.added == sum(1 for line in expected if line.startswith("+"))
    assert found.removed == sum(1 for line in expected if line.startswith("-"))
    for hunk in found.hunks:
        olds = [line.old for line in hunk.lines if line.kind != "added"]
        news = [line.new for line in hunk.lines if line.kind != "removed"]
        assert olds == list(range(hunk.old_start, hunk.old_start + hunk.old_lines))
        assert news == list(range(hunk.new_start, hunk.new_start + hunk.new_lines))
        for line in hunk.lines:
            assert (line.old is None) == (line.kind == "added") and (line.new is None) == (line.kind == "removed")
            if line.old is not None:
                assert old[line.old - 1] == line.text
            if line.new is not None:
                assert new[line.new - 1] == line.text


def test_diff_lines_of_equal_texts_has_no_hunk_and_bounds_its_context():
    assert diff_lines(["a", "b"], ["a", "b"]).hunks == []
    assert diff_lines([], []).hunks == []
    with pytest.raises(ValueError, match="context"):
        diff_lines(["a"], ["b"], MAX_CONTEXT + 1)


def test_a_step_marked_done_changes_only_its_own_lines_in_the_copy():
    old = fixture_body("kg-prototype")
    new = copy.deepcopy(old)
    step = new["steps"][2]
    del step["evidence"], step["done_at"]
    step["status"] = "in_progress"
    found = plan_diff(new, old)  # from in_progress without evidence to done with it

    changed = [(line.kind, line.text.strip()) for hunk in found.hunks for line in hunk.lines if line.kind != "context"]
    assert ("removed", "status: in_progress") in changed and ("added", "status: done") in changed
    assert ("added", f"done_at: '{old['steps'][2]['done_at']}'") in changed
    assert sum(1 for kind, _ in changed if kind == "removed") == 1
    assert len(found.hunks) == 1
    text = plan_text(old).splitlines()
    for hunk in found.hunks:
        for line in hunk.lines:
            if line.new is not None:
                assert text[line.new - 1] == line.text  # numbered as in the copy git keeps


def test_plan_text_is_the_copy_without_its_header_and_hub_key():
    body = fixture_body("evo-lms-migration")
    text = plan_text(body)
    assert text.startswith(f"id: {body['id']}\n") and "\nhub:" not in text and "Mirror of" not in text
    assert plan_text(dict(reversed(list(body.items())))) == text


# The routes


def patch_step(hub, plan_id: str, step, updates: dict, revision: int):
    body = {"section": "steps", "step": step, "updates": updates, "if_revision": revision}
    response = hub.client.patch(plan_url(plan_id), json=body, headers=hub.headers)
    assert response.status_code == 200, response.text
    return response.json()


def get(hub, *path: str, **params):
    return hub.client.get(plan_url(*path), params=params, headers=hub.headers)


@needs_pg
def test_the_list_counts_done_steps_and_a_step_keeps_its_evidence_verbatim(client, github, hub_db):
    hubs = setup_project(client, github)
    alice = hubs["alice"]
    fixture = fixture_body("kg-prototype")
    assert put(alice, fixture).status_code == 200
    assert put(alice, draft("rollout", steps=4)).status_code == 200
    patch_step(alice, "rollout", 2, {"status": "done", "done_at": "2026-10-04", "evidence": EVIDENCE}, 1)

    listed = {p["plan_id"]: p for p in get(hubs["reader"]).json()}
    done = sum(1 for step in fixture["steps"] if step.get("status") == "done")
    assert (listed["kg-prototype"]["steps_done"], listed["kg-prototype"]["steps_total"]) == (done, 13)
    assert (listed["rollout"]["steps_done"], listed["rollout"]["steps_total"]) == (1, 4)
    assert listed["rollout"]["revision"] == 2 and listed["rollout"]["updated_by"] == "alice"

    step = get(hubs["reader"], "rollout").json()["body"]["steps"][1]
    assert step["evidence"] == EVIDENCE and step["status"] == "done"


@needs_pg
def test_the_diff_between_two_revisions_names_the_changed_lines(client, github, hub_db):
    hubs = setup_project(client, github)
    alice, reader = hubs["alice"], hubs["reader"]
    assert put(alice, draft("rollout", steps=3)).status_code == 200
    patch_step(alice, "rollout", 2, {"status": "done", "evidence": "commit abc123"}, 1)
    changed = {**draft("rollout", steps=3), "goal": "Ship the rollout to every team."}
    changed["steps"][1] |= {"status": "done", "evidence": "commit abc123"}
    assert put(alice, changed, if_revision=2).status_code == 200

    answer = get(reader, "rollout", "diff", **{"from": 1, "to": 2})
    assert answer.status_code == 200, answer.text
    found = answer.json()
    assert (found["from_revision"]["revision"], found["to_revision"]["revision"]) == (1, 2)
    assert found["to_revision"]["summary"] == "step 2: status pending -> done; set evidence"
    assert found["to_revision"]["actor"] == "alice" and found["context"] == 3
    lines = [(line["kind"], line["text"]) for hunk in found["hunks"] for line in hunk["lines"]]
    assert [entry for entry in lines if entry[0] != "context"] == [
        ("removed", "    status: pending"),
        ("added", "    status: done"),
        ("added", "    evidence: commit abc123"),
    ]
    assert (found["added"], found["removed"]) == (2, 1)
    old_text = plan_text(draft("rollout", steps=3)).splitlines()
    for hunk in found["hunks"]:
        for line in hunk["lines"]:
            if line["old"] is not None:
                assert old_text[line["old"] - 1] == line["text"]

    whole = get(reader, "rollout", "diff", **{"from": 1, "to": 3, "context": 0}).json()
    removed = [line["text"] for hunk in whole["hunks"] for line in hunk["lines"] if line["kind"] == "removed"]
    assert removed[0].startswith("goal: Ship the rollout.") and len(removed) == 2
    assert all(line["kind"] != "context" for hunk in whole["hunks"] for line in hunk["lines"])

    backwards = get(reader, "rollout", "diff", **{"from": 2, "to": 1}).json()
    assert (backwards["added"], backwards["removed"]) == (1, 2)
    same = get(reader, "rollout", "diff", **{"from": 2, "to": 2}).json()
    assert same["hunks"] == [] and same["added"] == same["removed"] == 0

    assert get(reader, "rollout", "diff", **{"from": 1, "to": 9}).status_code == 404
    assert get(reader, "rollout", "diff", **{"from": 0, "to": 1}).status_code == 422
    assert get(reader, "rollout", "diff", **{"from": 1}).status_code == 422
    too_wide = get(reader, "rollout", "diff", **{"from": 1, "to": 2, "context": MAX_CONTEXT + 1})
    assert too_wide.status_code == 422
    assert get(reader, "no-such-plan", "diff", **{"from": 1, "to": 2}).status_code == 404


@needs_pg
def test_reading_needs_a_grant_and_a_level_that_reaches_the_plan(client, github, hub_db):
    hubs = setup_project(client, github)
    alice = hubs["alice"]
    assert put(alice, draft("rollout")).status_code == 200
    patch_step(alice, "rollout", 1, {"status": "done"}, 1)
    reads = [
        ("",),
        ("rollout",),
        ("rollout", "revisions"),
        ("rollout", "revisions", "1"),
        ("rollout", "diff"),
    ]
    params = {"from": 1, "to": 2}

    for path in reads:
        assert get(hubs["reader"], *filter(None, path), **params).status_code == 200, path
        refused = get(hubs["admin"], *filter(None, path), **params)  # a hub admin without a grant
        assert refused.status_code == 403, path
        assert refused.json()["error"] == "forbidden"
        assert refused.json()["message"] == f"reading the plans of project {PROJECT} needs a grant on it"
        assert get(hubs["stranger"], *filter(None, path), **params).status_code == 404, path
        if path != ("",):  # an internal plan is not there for a member whose grant stops at public
            assert get(hubs["public-reader"], *path, **params).status_code == 404, path
    assert get(hubs["public-reader"]).json() == []

    other = client.get("/v1/projects/no-such-project/plans", headers=hubs["admin"].headers)
    assert other.status_code == 404  # a project that is not registered stays a 404, even for a hub admin


@needs_pg
def test_reads_change_nothing(client, github, hub_db):
    hubs = setup_project(client, github)
    assert put(hubs["alice"], draft("rollout")).status_code == 200
    before = [live.sql(hub_db, f"SELECT count(*) FROM {table}")[0][0] for table in ("plans", "plan_revisions", "audit")]
    reader = hubs["reader"]
    for path in [(), ("rollout",), ("rollout", "revisions"), ("rollout", "revisions", "1")]:
        assert get(reader, *path).status_code == 200
    assert get(reader, "rollout", "diff", **{"from": 1, "to": 1}).status_code == 200
    after = [live.sql(hub_db, f"SELECT count(*) FROM {table}")[0][0] for table in ("plans", "plan_revisions", "audit")]
    assert after == before
