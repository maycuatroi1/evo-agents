"""An admin's pass of a hold of the hub's merge queue on GitLab (``evo_agents.hub.server.merge_pass``), against Postgres
and the fake GitLab of ``tests.hub.fake_gitlab``: the real GitLab is never called.

Gate 3 (score hacking) holds a merge request for a person: a plan run that adds its tests to .gitlab-ci.yml is
blocked there. An admin of the project passes the hold with a reason (POST .../merge-queue/{id}/pass, ``evo-agents hub
merge pass``): the merge request is gated again at the same head and the queue goes on from that gate; the Judge, its
turn, the pipeline at its rebased head and the merge still apply, and gate 3 at the rebased head lets the same patch
through. The ledger names the admin and the reason, the audit says merge.pass. A new push takes the pass back; a hold no
person passes (another gate, a revert) is refused, and so is a member who is no admin. The world is
``tests.hub.test_merge_gate``'s; the projects and people are fictional.
"""

from __future__ import annotations

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select, update

from evo_agents.hub import merge_gate, tables
from evo_agents.hub.server.app import create_app
from tests.hub.contract_keys import assert_json_keys
from tests.hub.fake_gitlab import sha_of
from tests.hub.live import sql
from tests.hub.test_merge_gate import (  # noqa: F401 (world: a fixture)
    CLEAN,
    PROJECT,
    REPO,
    gate_pass,
    green,
    judged,
    one,
    plan_done,
    world,
)
from tests.hub.test_merge_queue import judge_next, kinds
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

CI_FILE = {  # the plan run adds its tests to the pipeline
    "old_path": ".gitlab-ci.yml",
    "new_path": ".gitlab-ci.yml",
    "diff": "@@ -1,2 +1,4 @@\n stages: [test]\n test:\n+  script:\n+    - python -m pytest -q tests/test_report.py\n",
    "new_file": False,
    "renamed_file": False,
    "deleted_file": False,
}
WITH_CI = [*CLEAN, CI_FILE]
REASON = "The goal of plan lumen-report is the report's test job in the pipeline."


def pass_hold(world, queue_id: int, reason: str = REASON, who: str = "owner"):
    return world.client.post(
        f"/v1/projects/{PROJECT}/merge-queue/{queue_id}/pass", json={"reason": reason}, headers=world.headers[who]
    )


def held_at_gate_3(world):
    """The plan's merge request, its diff changing .gitlab-ci.yml, held at gate 3; (the merge request, its row)."""
    plan_done(world)
    assert gate_pass(world) == {"opened": 1}
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request, WITH_CI)
    gate_pass(world)
    entry = one(world)
    assert entry["state"] == "blocked" and entry["reason"].startswith("gate 3 (score hacking): the diff shows signs")
    assert "ci_changed" in entry["reason"] and ".gitlab-ci.yml" in entry["reason"]
    return merge_request, entry


def audited(world) -> list[str]:
    a = tables.audit
    return [row[0] for row in sql(world.db, select(a.c.target).where(a.c.action == "merge.pass").order_by(a.c.id))]


def test_merge_pass_an_admin_passes_a_ci_change_held_at_gate_3_and_the_queue_merges_it(world):
    merge_request, entry = held_at_gate_3(world)
    (note,) = [body for _, body in world.project.notes]
    assert "is blocked at gate 3 (score hacking)" in note and "This hold is for a person" in note
    assert "evo-agents hub merge pass" in note

    assert pass_hold(world, entry["id"], who="other").status_code == 403  # a writer passes nothing
    assert pass_hold(world, entry["id"], reason="   ").status_code == 422  # nor a pass without a reason
    passed = pass_hold(world, entry["id"])
    assert passed.status_code == 200, passed.text
    found = passed.json()
    assert (found["state"], found["reason"], found["head_sha"]) == ("gating", None, merge_request.sha)
    line = found["events"][0]
    assert (line["kind"], line["gate"], line["passed"], line["sha"]) == ("gate", 3, True, merge_request.sha)
    assert line["reason"] == f"passed by owner, an admin of the project: {REASON}"
    assert line["detail"] == {"person": "owner", "held": entry["reason"]}
    assert audited(world) == [f"{PROJECT}/{REPO} !1 gate=3 sha={merge_request.sha} reason={REASON}"]
    again = pass_hold(world, entry["id"])
    assert again.status_code == 409 and "it is gating and held at no gate" in again.json()["message"]

    # the queue goes on from gate 3 at the same head: the Judge still judges it
    assert gate_pass(world) == {"judging": 1}
    entry = one(world)
    by_gate = [event for event in entry["events"] if event["kind"] == "gate" and event["passed"]]
    gate_3 = next(event for event in by_gate if event["gate"] == 3 and "pass" in event["detail"])
    assert gate_3["reason"].startswith("the diff shows signs of score hacking (ci_changed), first at .gitlab-ci.yml")
    assert gate_3["reason"].endswith(f"; passed by owner, an admin of the project: {REASON}")
    assert gate_3["detail"] == {"pass": line["id"], "passed_by": "owner"}
    assert one(world)["risk"]["high"] is True  # a CI file is a risk path: the second Judge too
    assert judge_next(world)["reason"] == "gate 7 (second Judge): waits for the second Judge"
    assert gate_pass(world) == {"judging": 1}
    assert judged(world, head=merge_request.sha, names=merge_gate.RISK_DIMENSIONS)[1]["state"] == "ready"

    # its turn rebases it onto a dev that moved; at the rebased head the same patch passes gate 3 again, and merges
    world.project.branches["dev"] = sha_of("merged by hand")
    before = merge_request.sha
    assert gate_pass(world) == {}
    assert one(world)["state"] == "testing" and merge_request.sha != before
    green(world, merge_request, WITH_CI)
    assert gate_pass(world) == {"merged": 1}
    entry = one(world)
    assert entry["state"] == "merged" and world.project.merge_requests[1].state == "merged"
    regated = [e for e in entry["events"] if e["kind"] == "gate" and e["gate"] == 3 and e["sha"] == merge_request.sha]
    assert regated[0]["reason"].startswith("at its rebased head, the diff shows signs of score hacking")
    assert regated[0]["detail"]["passed_by"] == "owner"


def test_merge_pass_a_new_push_takes_the_pass_back(world):
    merge_request, entry = held_at_gate_3(world)
    assert pass_hold(world, entry["id"]).status_code == 200
    merge_request.sha = sha_of("pushed after the pass")  # someone pushes before the queue reads it again
    green(world, merge_request, WITH_CI)
    gate_pass(world)
    entry = one(world)
    assert entry["state"] == "blocked" and entry["reason"].startswith("gate 3 (score hacking)")
    assert entry["head_sha"] == merge_request.sha and "head" in kinds(entry)
    assert sql(world.db, select(tables.runs.c.id).where(tables.runs.c.kind == "judge")) == []
    assert pass_hold(world, entry["id"], "The new push keeps the test job.").status_code == 200  # a new pass
    assert gate_pass(world) == {"judging": 1}


def test_merge_pass_a_hold_a_person_merges_on_gitlab_leaves_the_queue_with_the_merge_in_its_ledger(world):
    merge_request, entry = held_at_gate_3(world)
    merge_request.state, merge_request.merge_commit_sha = "merged", sha_of("merged by hand on GitLab")
    assert gate_pass(world) == {"closed": 1}
    entry = one(world)
    assert entry["state"] == "closed" and entry["reason"] == "it left the queue: it is merged on GitLab"
    closed = entry["events"][0]
    assert closed["kind"] == "closed" and closed["detail"] == {
        "gitlab": "merged",
        "merge_commit_sha": sha_of("merged by hand on GitLab"),
    }


def test_merge_pass_refuses_a_hold_no_person_passes(world):
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request, status="failed")
    gate_pass(world)
    entry = one(world)
    assert entry["state"] == "blocked" and entry["reason"].startswith("gate 1 (pipeline)")
    refused = pass_hold(world, entry["id"])
    assert refused.status_code == 409
    assert refused.json()["message"] == (
        f"no person passes !1 of {REPO}: it is held at gate 1 (pipeline), which a new push moves, not a person"
    )
    q = tables.merge_queue
    revert = {"source": "revert", "reverts_id": entry["id"], "reason": "gate 3 (score hacking): a revert: it differs"}
    sql(world.db, update(q).values(revert))
    assert "a revert of the hub" in pass_hold(world, entry["id"]).json()["message"]
    assert pass_hold(world, 999_999).status_code == 404
    assert audited(world) == [] and one(world)["state"] == "blocked"


def test_merge_pass_from_the_command_line(world, hub_db, tmp_path, github, monkeypatch, capsys):
    from tests.hub.test_run_stream import serving

    project_id = sql(world.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    q = tables.merge_queue
    held = "gate 3 (score hacking): the diff shows signs of score hacking (ci_changed), first at .gitlab-ci.yml"
    row = {"project_id": project_id, "repo": REPO, "source": "label", "target_branch": "dev", "state": "blocked"}
    row |= {"source_branch": "fix/ci", "iid": 12, "head_sha": sha_of("ci"), "reason": held}
    sql(world.db, insert(q).values(row))
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url:
        homes = {
            login: write_credentials(tmp_path / login, url, login, world.headers[login]["Authorization"].split()[1])
            for login in ("owner", "other")
        }

        def merge(who: str, *args: str):
            return run_cli(monkeypatch, capsys, homes[who], "hub", "merge", *args, "--project", PROJECT)

        refused = merge("other", "pass", REPO, "!12", "--reason", REASON)
        assert refused.code == 1 and "needs the admin role on project" in refused.err
        missing = merge("owner", "pass", REPO, "13", "--reason", REASON)
        assert missing.code == 1 and f"holds no merge request !13 of {REPO}" in missing.err
        assert ok(merge("owner", "pass", REPO, "!12", "--reason", REASON)).out == (
            f"Passed the hold of !12 of {REPO} at gate 3 (score hacking) in project {PROJECT}: the queue goes on from "
            f"that gate at {sha_of('ci')[:12]}, and a new push takes the pass back.\n"
        )
        sql(world.db, update(q).values(state="blocked", reason=held))
        passed = as_json(merge("owner", "pass", REPO, "12", "--reason", REASON, "--json"))
        assert_json_keys("hub merge pass", passed)
        assert passed["state"] == "gating" and passed["events"][0]["detail"]["person"] == "owner"
