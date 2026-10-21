"""The Curator's merge requests on GitLab and the merge queue (``merge_gate.curator_judged``; plan gitlab-merge-queue,
criterion a4 and the member's answer 5 of 2026-10-21): a change of any tier its Judge passed goes to the queue when the
project's autonomy policy has its repo in mode auto and a forge secret covers it, through the same gates as a plan's;
without a policy it stays open, as before (criterion a9). The world is ``tests.hub.test_curator_changes``'s."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from evo_agents.hub.server import merge_gate as queue
from tests.hub.fake_gitlab import FakeGitLab
from tests.hub.live import sql
from tests.hub.test_credentials_api import GITLAB_KB, app_key, git_secret, sample  # noqa: F401 (app_key: a fixture)
from tests.hub.test_curator import NIGHT, fire, written
from tests.hub.test_curator_changes import (  # noqa: F401 (world: a fixture)
    DRAFT,
    HEAD,
    VERIFY,
    accepted,
    curator_charter,
    judge_run,
    moved_on,
    one_change,
    verdict,
    world,
)
from tests.hub.test_plan_runs import reported
from tests.hub.test_runs import PROJECT, claim, moved

KB_HOST = "gitlab.lumenops.example"
PATH = "fir-gb-lumen/lumen-kb-docs"
REPO = "lumen-kb-docs"


def judged_on_gitlab(world) -> dict:
    """A tier 0 change of lumen-kb-docs built by the night shift (its push opens the merge request) and passed by its
    Judge; the change."""
    owner = world.headers["owner"]
    git_secret(world.client, owner, "gitlab-dev", GITLAB_KB, sample("glpat-dev-"))
    written(world.client, owner, curator_charter(git_secret="gitlab-dev"))
    plan = {**DRAFT, "steps": [{**DRAFT["steps"][0], "repo": REPO}]}
    accepted(world, paths=[{"repo": REPO, "path": "tests/test_kb.py"}], plan=plan)
    assert fire(world.client, NIGHT)["builder"] == 1
    spec = claim(world.client, world.worker)
    moved(world.client, world.worker, spec["id"], "running")
    verify = [{"command": VERIFY, "exit_code": 0}]
    reported(world.client, world.worker, spec["id"], 1, "done", repo=REPO, verify=verify, commit_sha=HEAD)
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    spec = judge_run(world)
    assert verdict(world, spec["id"]).json()["passed"] is True
    return one_change(world)


def forge_and_policy(world) -> str:
    """owner's forge secret over the GitLab group, and the policy with lumen-kb-docs in mode auto into main."""
    owner = world.headers["owner"]
    token = "glpat-" + secrets.token_hex(16)
    expires = (datetime.now(UTC) + timedelta(days=90)).isoformat()
    body = {
        "kind": "forge",
        "url_prefix": f"https://{KB_HOST}/fir-gb-lumen",
        "projects": [PROJECT],
        "expires_at": expires,
        "value": token,
    }
    assert world.client.put("/v1/secrets/gitlab-merge", json=body, headers=owner).status_code == 200
    policy = {"policy": {"repos": {REPO: {"mode": "auto", "target_branch": "main"}}}}
    assert world.client.put(f"/v1/projects/{PROJECT}/policy", json=policy, headers=owner).status_code == 200
    return token


def test_merge_gate_takes_the_curators_merge_request_its_judge_passed_when_the_policy_says_so(world):
    token = forge_and_policy(world)
    change = judged_on_gitlab(world)
    assert moved_on(world) == {"queued": 1}
    change = one_change(world)
    assert change["state"] == "open" and change["reason"].startswith("handed to the merge queue of lumen-kb-docs")
    q = tables.merge_queue
    (row,) = sql(world.db, select(q.c.source, q.c.change_id, q.c.source_branch, q.c.target_branch, q.c.iid, q.c.state))
    assert tuple(row) == ("builder", change["id"], change["branch"], "main", None, "gating")
    assert moved_on(world) == {}  # the change is open now: the job does not hand it twice

    fake = FakeGitLab(token, host=KB_HOST)
    project = fake.add_project(PATH, protected={"main": {"push": [0], "merge": [40]}})
    opened = fake.add_merge_request(project, change["branch"], sha=HEAD)
    state = world.app.state
    assert (
        world.client.portal.call(partial(queue.advance, state.engine, state.sealer, transport=fake.transport())) == {}
    )
    (row,) = sql(world.db, select(q.c.iid, q.c.state, q.c.reason, q.c.head_sha))
    assert tuple(row) == (opened.iid, "gating", "gate 1 (pipeline): no pipeline ran on its head yet", HEAD)
    assert project.merge_requests[1].title == opened.title  # the hub opened none of its own


def test_merge_gate_a_curators_merge_request_without_a_policy_stays_open_as_before(world):
    judged_on_gitlab(world)
    assert moved_on(world) == {"open": 1}
    change = one_change(world)
    assert change["state"] == "open" and change["reason"] == (
        "a merge request on GitLab stays open: the hub never merges one"
    )
    assert sql(world.db, select(tables.merge_queue.c.id)) == []
