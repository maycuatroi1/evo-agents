"""The audit samples of the hub's merge queue on GitLab (``evo_agents.hub.server.post_merge``; plan gitlab-merge-queue,
criterion a6), against Postgres and the fake GitLab of ``tests.hub.fake_gitlab``.

The checks step 6 names: a merge request the queue merged is drawn at the policy's ``audit.sample_rate`` and goes to
the Inbox of each admin of the project, with its link and the Judge's dimensions; a sample waiting for an answer never
holds the queue; an admin alone answers it, ok or faulty with a note, once; faulty counts as a pass of the Judge found
wrong in the figures; a revert of the hub is never drawn. The draw is fixed at 0.5 by the world of
``tests.hub.test_merge_gate``; the projects and people are fictional.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select

from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_merge_gate import PROJECT, REPO, gate_pass, green, put_policy, world  # noqa: F401 (world)
from tests.hub.test_merge_queue import by_iid, kinds
from tests.hub.test_post_merge import UNDONE, ends, fail_twice, merged, notices, on_dev


def rated(world, rate: float, **jobs) -> None:
    repo = {"mode": "auto", "target_branch": "dev", "jobs": jobs}
    body = {"repos": {REPO: repo}, "judge": {"worker": "mac-mini"}, "audit": {"sample_rate": rate}}
    put_policy(world.client, world.headers["owner"], body)


def queue_view(world, who: str = "owner") -> dict:
    response = world.client.get(f"/v1/projects/{PROJECT}/merge-queue", headers=world.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


def answer(world, sample_id: int, who: str = "owner", **body):
    return world.client.post(
        f"/v1/projects/{PROJECT}/merge-queue/samples/{sample_id}", json=body, headers=world.headers[who]
    )


def test_audit_sample_draws_a_merge_at_the_policy_rate_into_each_admins_inbox(world):
    rated(world, 1.0)  # above the draw of 0.5: every merge
    first = merged(world, "fix/one", "Fix one")
    entry = by_iid(world)[1]
    assert kinds(entry)[-2:] == ["merged", "sampled"]
    sampled = next(event for event in entry["events"] if event["kind"] == "sampled")
    assert sampled["reason"] == "drawn for an audit sample (audit.sample_rate 1)" and sampled["sha"] == first.sha
    (notice,) = notices(world)
    assert notice["title"] == f"Audit sample: check !1 of {REPO}, merged into dev"
    assert notice["details"]["ask"] == "sample" and notice["details"]["options"] == ["ok", "faulty"]
    assert notice["details"]["sample_id"] == sampled["detail"]["sample"]
    assert notice["details"]["commits"] == [first.sha] and notice["read_at"] is None
    assert "- work: pass" in notice["body"] and "https://" in notice["body"]
    (sample,) = queue_view(world, "other")["samples"]  # a reader sees it
    assert (sample["iid"], sample["result"], sample["repo"]) == (1, None, REPO)

    rated(world, 0.1)  # below the draw: none
    merged(world, "fix/two", "Fix two")
    assert "sampled" not in kinds(by_iid(world)[2]) and len(queue_view(world)["samples"]) == 1
    assert by_iid(world)[2]["state"] == "merged"  # a sample waiting for an answer holds nothing


def test_audit_sample_an_admin_answers_it_once_and_faulty_counts_against_the_judge(world):
    rated(world, 1.0)
    merged(world, "fix/one", "Fix one")
    (sample,) = queue_view(world)["samples"]
    assert answer(world, sample["id"], "other", result="faulty").status_code == 403  # a writer
    assert answer(world, sample["id"], result="unsure").status_code == 422
    assert answer(world, sample["id"] + 1000, result="ok").status_code == 404
    response = answer(world, sample["id"], result="faulty", note="  It drops   the  totals of empty reports. ")
    assert response.status_code == 200, response.text
    found = response.json()
    assert (found["result"], found["note"], found["answered_by"]) == (
        "faulty",
        "It drops the totals of empty reports.",
        "owner",
    )
    assert answer(world, sample["id"], result="ok").status_code == 409  # once
    entry = by_iid(world)[1]
    (line,) = [event for event in entry["events"] if event["kind"] == "sample_answered"]
    assert line["passed"] is False and line["detail"] == {"sample": sample["id"], "result": "faulty"}
    assert line["reason"] == "owner answered the audit sample: faulty"
    (notice,) = notices(world)
    assert notice["read_at"] is not None  # answered: read
    figures = world.client.get(f"/v1/projects/{PROJECT}/merge-queue/figures", headers=world.headers["owner"])
    (figure,) = figures.json()["figures"]
    assert (figure["merged"], figure["reverted"], figure["judge_misses"]) == (1, 0, 1)
    a = tables.audit
    targets = sql(world.db, select(a.c.target).where(a.c.action == "merge.sample"))
    assert targets == [(f"{PROJECT}/{REPO} !1 sample={sample['id']} result=faulty",)]
    assert "drops" not in targets[0][0]  # never the note


def test_audit_sample_never_draws_a_revert_of_the_hub(world):
    rated(world, 1.0, uat_check="uat-smoke")
    first = merged(world, "fix/one", "Fix one")
    pipeline = on_dev(world, first, jobs=[("uat-smoke", "running")])
    gate_pass(world)
    fail_twice(world, pipeline, "uat-smoke")
    gate_pass(world)
    revert = world.project.merge_requests[2]
    green(world, revert, UNDONE)
    gate_pass(world)
    assert by_iid(world)[2]["state"] == "merged" and by_iid(world)[1]["state"] == "reverted"
    s = tables.merge_samples
    assert sql(world.db, select(func.count()).select_from(s))[0][0] == 1  # !1's alone
    assert "sampled" not in kinds(by_iid(world)[2])
    ends(world, pipeline, "uat-smoke", "failed")  # nothing follows a merge request reverted
    gate_pass(world)
    assert by_iid(world)[1]["state"] == "reverted"
