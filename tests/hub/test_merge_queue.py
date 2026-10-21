"""The turn of the hub's merge queue on GitLab (``evo_agents.hub.server.merge_turn``; plan gitlab-merge-queue, criterion
a5), against Postgres and the fake GitLab of ``tests.hub.fake_gitlab``: the real GitLab is never called.

The checks step 5 names: each target branch has one queue and one merge request of it has the turn at a time; its turn
rebases it onto the branch, waits for the pipeline of the new head, reads the cheap gates there again and merges it
with that head's sha; the branch moving, or a push of someone else, sends it back, first in line or to its gates; a
rebase that keeps the patch keeps the Judges' verdicts; the next turn starts once the merge before reached
merge.hold_until (merged by default); every move is a line of the ledger; the queue lives in Postgres, so a pass that
did not hear GitLab's answer, or two passes at once, never merge two merge requests of a branch at once. The world is
``tests.hub.test_merge_gate``'s; the projects and people are fictional.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial

import httpx
import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from psycopg import errors
from sqlalchemy import func, insert, select, update

from evo_agents.hub import merge_gate, tables
from evo_agents.hub.server import merge_gate as queue
from tests.hub.fake_gitlab import sha_of
from tests.hub.live import sql
from tests.hub.test_merge_gate import (  # noqa: F401 (world: a fixture)
    CLEAN,
    PATH,
    REPO,
    auto,
    dimensions,
    gate_pass,
    green,
    judge_headers,
    listed,
    plan_done,
    put_policy,
    world,
)
from tests.hub.test_runs import PROJECT, claim, moved

ENCODED = PATH.replace("/", "%2F")
DEVELOPER = 41  # a member of the GitLab project of role Developer
CHANGED = [  # the patch of CLEAN after a rebase that had to change it
    {**CLEAN[0], "diff": "@@ -0,0 +1,2 @@\n+def total():\n+    return 4\n"},
    CLEAN[1],
]


def labeled(world, branch: str, title: str):
    """A person's merge request into dev, labeled evo-merge by a Developer, its pipeline green at its head."""
    world.project.members[DEVELOPER] = 30
    mr = world.fake.add_merge_request(world.project, branch, target_branch="dev", title=title)
    world.fake.label(mr, "evo-merge", DEVELOPER)
    green(world, mr)
    return mr


def judge_next(world) -> dict:
    """The judge run the queue queued, claimed by mac-mini, passing the head it was given; the verdict's answer."""
    spec = claim(world.client, world.worker)
    assert spec["kind"] == "judge", spec
    headers = judge_headers(world, spec)
    inputs = world.client.get(f"/v1/worker/runs/{spec['id']}/merge-judge", headers=headers)
    assert inputs.status_code == 200, inputs.text
    moved(world.client, world.worker, spec["id"], "running")
    body = {
        "verdict": "pass",
        "reasons": "It does what its description says.",
        "head_sha": spec["curator"]["head_sha"],
        "verify": [{"command": item, "exit_code": 0} for item in inputs.json()["verify"]],
        "dimensions": dimensions(),
    }
    answer = world.client.post(f"/v1/worker/runs/{spec['id']}/merge-verdict", json=body, headers=headers)
    assert answer.status_code == 200, answer.text
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    return answer.json()


def by_iid(world) -> dict[int, dict]:
    return {entry["iid"]: entry for entry in listed(world)}


def kinds(entry: dict) -> list[str]:
    """The kinds of the ledger lines of ``entry``, the oldest first."""
    return [event["kind"] for event in reversed(entry["events"])]


def rebases(world) -> dict[int, int]:
    q = tables.merge_queue
    return dict(sql(world.db, select(q.c.iid, q.c.rebases)))


def judge_runs(world) -> int:
    r = tables.runs
    return sql(world.db, select(func.count()).select_from(r).where(r.c.kind == "judge"))[0][0]


def merges(world) -> list[str]:
    """The sha of each merge the hub asked GitLab for, in order."""
    return [
        json.loads(request.content)["sha"]
        for request in world.fake.requests
        if request.method == "PUT" and request.url.path.endswith("/merge")
    ]


def two_ready(world):
    """Two merge requests ready, the first in its turn: rebased onto a dev that moved, waiting for its pipeline."""
    first, second = labeled(world, "fix/one", "Fix one"), labeled(world, "fix/two", "Fix two")
    assert gate_pass(world) == {"taken": 2, "judging": 1}
    assert judge_next(world)["state"] == "ready"
    world.project.branches["dev"] = sha_of("merged by hand")  # dev moves: the first needs a rebase
    assert gate_pass(world) == {"judging": 1}
    assert judge_next(world)["state"] == "ready"
    return first, second


# One at a time, each with the head its pipeline passed on


def test_merge_queue_merges_one_merge_request_at_a_time_rebased_and_tested_at_its_new_head(world):
    first, second = labeled(world, "fix/one", "Fix one"), labeled(world, "fix/two", "Fix two")
    assert gate_pass(world) == {"taken": 2, "judging": 1}  # one judge run of the project at a time
    assert judge_next(world)["state"] == "ready"
    world.project.branches["dev"] = sha_of("merged by hand")
    before = first.sha
    assert gate_pass(world) == {"judging": 1}  # the second's Judge; the first's turn rebased it
    entry = by_iid(world)[1]
    assert entry["state"] == "testing" and entry["head_sha"] == first.sha != before
    assert entry["reason"] == "its turn (pipeline): no pipeline ran on its head yet"
    assert world.project.rebased == [1] and merges(world) == []

    assert judge_next(world)["state"] == "ready"
    assert gate_pass(world) == {}  # the first has the turn of dev: the second waits, ready
    assert by_iid(world)[2]["state"] == "ready" and world.project.rebased == [1]

    green(world, first)  # its pipeline at the rebased head
    assert gate_pass(world) == {"merged": 1}  # and the second's turn rebases it onto the first at once
    entries = by_iid(world)
    assert entries[1]["state"] == "merged" and world.project.merge_requests[1].state == "merged"
    assert entries[2]["state"] == "testing" and world.project.rebased == [1, 2]
    green(world, second)
    assert gate_pass(world) == {"merged": 1}
    assert merges(world) == [first.sha, second.sha]  # each with the head its pipeline passed on
    assert world.project.branches["dev"] == second.sha  # fast-forward

    entry = by_iid(world)[2]
    assert entry["state"] == "merged" and entry["merged_at"] is not None
    turn = kinds(entry)[kinds(entry).index("queued", 2) :]
    assert turn == ["queued", "rebased", "pipeline", "gate", "gate", "gate", "merged"]
    merged = next(event for event in entry["events"] if event["kind"] == "merged")
    assert merged["detail"] == {"before_sha": first.sha, "after_sha": second.sha, "rebases": 1}
    assert merged["sha"] == second.sha and merged["reason"].startswith("merged into dev, which moved from")
    rebased = next(event for event in entry["events"] if event["kind"] == "rebased")
    assert rebased["detail"]["onto"] == first.sha and rebased["reason"].startswith("rebased onto dev at")
    regated = [event["gate"] for event in entry["events"] if event["kind"] == "gate" and event["sha"] == second.sha]
    assert sorted(regated) == [0, 1, 3]  # the cheap gates, read again at the rebased head
    assert judge_runs(world) == 2  # each rebase kept its patch: no Judge ran again

    audit = tables.audit
    (row,) = sql(world.db, select(audit.c.target).where(audit.c.action == "merge.merge").order_by(audit.c.id.desc()))[
        :1
    ]
    assert row[0] == f"{PROJECT}/{REPO} !2 into=dev sha={second.sha} before={first.sha}"
    assert world.token not in str(listed(world))


def test_merge_queue_a_target_branch_that_moves_sends_it_back_first_in_line_then_blocks_it(world):
    first, second = two_ready(world)
    assert by_iid(world)[1]["state"] == "testing" and rebases(world) == {1: 1, 2: 0}
    for count in (2, 3, 4):
        world.project.branches["dev"] = sha_of(f"merged by hand {count}")
        assert gate_pass(world) == {}
        entries = by_iid(world)
        assert entries[1]["state"] == "testing" and entries[2]["state"] == "ready"  # the first keeps its place
        assert rebases(world) == {1: count, 2: 0} and world.project.rebased == [1] * count
    back = [event for event in by_iid(world)[1]["events"] if event["kind"] == "queued" and event["detail"]]
    assert len(back) == 3 and back[0]["detail"] == {"turn": "back"}
    assert "dev moved to" in back[0]["reason"] and back[0]["reason"].endswith("it keeps its place, first")

    world.project.branches["dev"] = sha_of("merged by hand 5")
    assert gate_pass(world) == {}
    entries = by_iid(world)
    assert entries[1]["state"] == "blocked" and entries[1]["reason"].startswith("its turn (rebase): dev moved to")
    assert entries[1]["reason"].endswith("during its turn, after 4 rebases in a row")
    assert entries[2]["state"] == "testing" and world.project.rebased == [1, 1, 1, 1, 2]  # the turn went on
    gate_pass(world)
    gate_pass(world)
    notes = [body for iid, body in world.project.notes if iid == 1]
    assert len(notes) == 1 and "is blocked at its turn (rebase)" in notes[0]  # once


def test_merge_queue_a_push_during_its_turn_sends_it_to_its_gates_and_the_back_of_the_queue(world):
    first, second = two_ready(world)
    first.sha = sha_of("pushed during its turn")
    world.project.branches["fix/one"] = first.sha
    green(world, first, CHANGED)
    assert gate_pass(world) == {}  # the second takes the turn and waits for its rebased head's pipeline
    entries = by_iid(world)
    assert entries[1]["state"] == "gating" and entries[1]["queued_at"] is None and entries[1]["head_sha"] == first.sha
    assert entries[2]["state"] == "testing"
    assert gate_pass(world) == {"judging": 1}  # a new patch: its gates, then its Judge again
    assert by_iid(world)[1]["state"] == "judging" and judge_runs(world) == 3


def test_merge_queue_a_rebase_that_changes_the_patch_judges_it_again_and_it_keeps_its_place(world):
    first, second = two_ready(world)
    queued_at = by_iid(world)[1]["queued_at"]
    green(world, first, CHANGED)  # the rebase had to change the patch
    assert gate_pass(world) == {"judging": 1}  # its Judge again; the turn goes to the second meanwhile
    entries = by_iid(world)
    assert entries[1]["state"] == "judging" and entries[1]["verdict"] is None and entries[2]["state"] == "testing"
    head = next(event for event in entries[1]["events"] if event["kind"] == "head")
    assert "changed its patch: its gates and Judges start again, at its place" in head["reason"]
    assert judge_next(world)["state"] == "ready"
    assert by_iid(world)[1]["queued_at"] == queued_at  # its place: before the second, who has the turn now
    green(world, second)
    assert gate_pass(world) == {"merged": 1}
    entries = by_iid(world)
    assert entries[2]["state"] == "merged" and entries[1]["state"] == "testing"  # the first's turn came next
    assert judge_runs(world) == 3


# What blocks it in its turn


def test_merge_queue_a_pipeline_red_after_its_rebase_runs_again_once_then_blocks_it_and_the_turn_moves_on(world):
    first, second = two_ready(world)
    green(world, first, status="failed")
    assert gate_pass(world) == {}
    entry = by_iid(world)[1]
    pipeline = first.pipeline["id"]
    assert entry["state"] == "testing" and world.project.retried == [pipeline]
    assert entry["reason"] == "its turn (pipeline): its head pipeline ended failed: the hub runs it again once"
    retried = next(event for event in entry["events"] if event["kind"] == "retried")
    assert retried["detail"] == {"pipeline": pipeline} and retried["sha"] == first.sha
    assert gate_pass(world) == {}  # running again: it waits
    assert by_iid(world)[1]["reason"] == "its turn (pipeline): its head pipeline is running"

    first.pipeline["status"] = "failed"
    assert gate_pass(world) == {}
    entries = by_iid(world)
    assert entries[1]["state"] == "blocked"
    assert (
        entries[1]["reason"] == "gate 1 (pipeline): at its rebased head, its head pipeline ended failed after a retry"
    )
    assert world.project.retried == [pipeline]  # once
    assert entries[2]["state"] == "testing" and world.project.rebased == [1, 2]  # the next one's turn
    assert merges(world) == []
    gate_pass(world)
    assert sum(iid == 1 for iid, _ in world.project.notes) == 1


def test_merge_queue_a_rebase_waits_while_gitlab_runs_it_and_one_gitlab_cannot_make_blocks_it(world):
    first, second = labeled(world, "fix/one", "Fix one"), labeled(world, "fix/two", "Fix two")
    first.rebase_reads = 1  # one read after the rebase still finds GitLab at it
    second.rebase_error = "Rebase failed: Rebase locally, resolve all conflicts, then push the branch."
    gate_pass(world)
    judge_next(world)
    world.project.branches["dev"] = sha_of("merged by hand")
    gate_pass(world)
    entry = by_iid(world)[1]
    assert entry["state"] == "rebasing" and entry["reason"] == "its turn (rebase): GitLab is rebasing it"
    judge_next(world)
    gate_pass(world)
    assert by_iid(world)[1]["state"] == "testing"
    green(world, first)
    assert gate_pass(world) == {"merged": 1}
    entry = by_iid(world)[2]
    assert entry["state"] == "blocked" and entry["reason"] == (
        "its turn (rebase): GitLab could not rebase it onto dev: Rebase failed: Rebase locally, resolve all "
        "conflicts, then push the branch."
    )
    failed = next(event for event in entry["events"] if event["kind"] == "rebased")
    assert failed["passed"] is False and merges(world) == [first.sha]


def test_merge_queue_a_push_between_its_test_and_its_merge_is_gated_not_merged(world):
    first = labeled(world, "fix/one", "Fix one")
    gate_pass(world)
    judge_next(world)
    handle = world.fake.handle

    def pushing(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path.endswith("/merge") and first.sha != sha_of("late push"):
            first.sha = sha_of("late push")  # someone pushes as the hub merges
            world.project.branches["fix/one"] = first.sha
        return handle(request)

    world.fake.handle = pushing
    assert gate_pass(world) == {}
    entry = by_iid(world)[1]
    assert entry["state"] == "gating" and entry["head_sha"] == sha_of("late push")
    assert world.project.merge_requests[1].state == "opened" and world.project.branches["dev"] == sha_of("dev")


def test_merge_queue_a_merge_gitlab_made_while_the_hub_did_not_hear_is_read_back(world):
    first = labeled(world, "fix/one", "Fix one")
    gate_pass(world)
    judge_next(world)
    handle = world.fake.handle

    def deaf(request: httpx.Request) -> httpx.Response:
        answer = handle(request)
        if request.method == "PUT" and request.url.path.endswith("/merge"):
            raise httpx.ReadTimeout("the answer was lost", request=request)  # GitLab merged it all the same
        return answer

    world.fake.handle = deaf
    assert gate_pass(world) == {"gitlab": 1}  # the repo waits for GitLab
    assert by_iid(world)[1]["state"] == "merging" and world.project.merge_requests[1].state == "merged"
    world.fake.handle = handle
    assert gate_pass(world) == {"merged": 1}  # a pass of the job after, as after a restart, reads what GitLab did
    entry = by_iid(world)[1]
    assert entry["state"] == "merged" and merges(world) == [first.sha]
    assert [event["sha"] for event in entry["events"] if event["kind"] == "merged"] == [first.sha]


# The turn of a branch, whatever runs the queue


def test_merge_queue_two_passes_at_once_never_give_two_merge_requests_of_a_branch_the_turn(world):
    first, second = two_ready(world)
    third = labeled(world, "fix/three", "Fix three")
    assert gate_pass(world) == {"taken": 1, "judging": 1}
    assert judge_next(world)["state"] == "ready"
    green(world, first)  # the first may merge now; the second and the third wait, ready
    state = world.client.app.state

    async def both():
        advance = partial(queue.advance, state.engine, state.sealer, transport=world.fake.transport())
        return await asyncio.gather(advance(), advance())

    passes = world.client.portal.call(both)
    assert all("failed" not in found and "gitlab" not in found for found in passes), passes
    assert sum(found.get("merged", 0) for found in passes) == 1  # one of the two merged the first, once
    entries = by_iid(world)
    in_turn = [iid for iid, entry in entries.items() if entry["state"] in merge_gate.TURN_STATES]
    assert in_turn == [2] and entries[1]["state"] == "merged" and entries[3]["state"] == "ready"
    assert merges(world).count(first.sha) >= 1 and set(merges(world)) == {first.sha}
    assert world.project.merge_requests[3].state == "opened" and world.project.branches["dev"] == first.sha
    assert third.sha not in merges(world)
    assert [event["kind"] for event in entries[1]["events"]].count("merged") == 1
    q = tables.merge_queue
    with pytest.raises(errors.UniqueViolation, match="merge_queue_turn_key"):  # the index refuses a second turn of dev
        sql(world.db, update(q).values(state="testing").where(q.c.iid == 3))


def test_merge_queue_hold_until_uat_check_waits_for_the_merge_before_to_pass_its_check(world):
    put_policy(world.client, world.headers["owner"], {**auto(), "merge": {"hold_until": "uat_check"}})
    labeled(world, "fix/one", "Fix one"), labeled(world, "fix/two", "Fix two")
    gate_pass(world)
    judge_next(world)
    gate_pass(world)  # the first merges at once
    judge_next(world)
    assert gate_pass(world) == {}
    entries = by_iid(world)
    assert entries[1]["state"] == "merged" and entries[2]["state"] == "ready"
    assert entries[2]["reason"] == "its turn waits for !1, merged before it, to pass its uat_check (merge.hold_until)"
    assert world.project.rebased == [1]
    e = tables.merge_events
    project_id = sql(world.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    check = {"project_id": project_id, "queue_id": entries[1]["id"], "kind": "uat_check", "passed": True}
    sql(world.db, insert(e).values(check))  # as what follows a merge records it (step 6 of the plan)
    assert gate_pass(world) == {}
    assert by_iid(world)[2]["state"] == "testing" and world.project.rebased == [1, 2]


def test_merge_queue_hold_until_merged_starts_the_next_turn_once_the_merge_before_is_merged(world):
    labeled(world, "fix/one", "Fix one"), labeled(world, "fix/two", "Fix two")
    gate_pass(world)
    judge_next(world)
    assert gate_pass(world) == {"judging": 1, "merged": 1}  # the first merges at once, dev not having moved
    judge_next(world)
    assert gate_pass(world) == {}
    assert by_iid(world)[2]["state"] == "testing" and by_iid(world)[2]["reason"].startswith("its turn (pipeline)")


# Out of the queue mid-turn


def test_merge_queue_a_draft_gives_its_turn_back_and_takes_it_again_once_ready(world):
    first, second = two_ready(world)
    first.draft = True
    assert gate_pass(world) == {}
    entries = by_iid(world)
    assert entries[1]["state"] == "ready" and entries[1]["reason"] == "waits: it is a draft"
    assert entries[2]["state"] == "testing"  # the turn went on to the second
    second.draft = True
    assert gate_pass(world) == {}
    assert world.project.rebased == [1, 2]  # neither draft takes the turn
    first.draft = False
    assert gate_pass(world) == {}
    entries = by_iid(world)
    assert entries[1]["state"] == "testing" and world.project.rebased == [1, 2, 1]


def test_merge_queue_turning_the_repo_off_mid_turn_hands_its_merge_requests_back(world):
    two_ready(world)
    put_policy(world.client, world.headers["owner"], {"repos": {REPO: {"mode": "off"}}})
    assert gate_pass(world) == {"closed": 2}
    assert {entry["state"] for entry in listed(world)} == {"closed"}
    assert world.project.merge_requests[1].state == "opened"


def test_merge_queue_a_failed_judge_run_names_its_merge_request_in_its_notice(world):
    plan_done(world)
    gate_pass(world)
    (merge_request,) = world.project.merge_requests.values()
    green(world, merge_request)
    gate_pass(world)
    spec = claim(world.client, world.worker)
    moved(world.client, world.worker, spec["id"], "running")
    moved(world.client, world.worker, spec["id"], "failed", error="the agent stopped")
    n = tables.notifications
    titles = [row[0] for row in sql(world.db, select(n.c.title).where(n.c.notice_kind == "run_failed"))]
    assert titles == [f"Judge run #{spec['id']} of merge request !1 of {REPO} failed"]
