"""The Curator's change the merge queue on GitLab merges (``evo_agents.hub.server.merge_turn.curator_merged``; plan
gitlab-merge-queue, criterion a5): once the queue merges the merge request of a change its Judge passed, the change is
merged too, with the merge's commits and a line in the proposal's ledger, as a merge on GitHub leaves it. The world is
``tests.hub.test_curator_changes``'s; the gates and Judges of the queue are ``tests.hub.test_merge_gate``'s to drive.
"""

from __future__ import annotations

from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select, update

from evo_agents.hub import tables
from evo_agents.hub.server import merge_gate as queue
from tests.hub.fake_gitlab import FakeGitLab
from tests.hub.live import sql
from tests.hub.test_credentials_api import app_key  # noqa: F401 (a fixture)
from tests.hub.test_curator_changes import HEAD, moved_on, one_change, world  # noqa: F401 (world: a fixture)
from tests.hub.test_merge_gate_curator import KB_HOST, PATH, forge_and_policy, judged_on_gitlab

ADDED = {
    "old_path": "tests/test_kb.py",
    "new_path": "tests/test_kb.py",
    "diff": "@@ -0,0 +1,2 @@\n+def test_kb():\n+    assert kb_ready()\n",
    "new_file": True,
    "renamed_file": False,
    "deleted_file": False,
}


def test_merge_queue_merges_the_curators_merge_request_and_its_change_is_merged(world):
    token = forge_and_policy(world)
    judged_on_gitlab(world)
    assert moved_on(world) == {"queued": 1}
    change = one_change(world)
    fake = FakeGitLab(token, host=KB_HOST)
    project = fake.add_project(PATH, protected={"main": {"push": [0], "merge": [40]}})
    commit = {"id": HEAD, "title": "Add the kb test", "message": "Add the kb test", "parent_ids": []}
    opened = fake.add_merge_request(project, change["branch"], sha=HEAD, files=[ADDED], commits=[commit])
    opened.pipeline = fake.add_pipeline(project, HEAD, change["branch"])
    state = world.app.state
    advance = partial(queue.advance, state.engine, state.sealer, transport=fake.transport())
    world.client.portal.call(advance)
    q = tables.merge_queue
    (row,) = sql(world.db, select(q.c.state, q.c.fingerprint))
    assert row[0] == "judging" and row[1]  # gates 0 to 3 passed: its Judges come next
    passed = {"first": {"passed": True}, "fingerprint": row[1]}
    ready = {"state": "ready", "queued_at": func.now(), "judge_run_id": None, "judge_key": None, "verdict": passed}
    sql(world.db, update(q).values(ready))  # its Judges passed it, as tests/hub/test_merge_gate.py drives them

    assert world.client.portal.call(advance) == {"merged": 1}
    assert opened.state == "merged" and project.branches["main"] == HEAD
    c = tables.curator_changes
    (found,) = sql(world.db, select(c.c.state, c.c.merge_sha, c.c.merged_at.is_not(None)))
    assert tuple(found) == ("merged", HEAD, True)
    assert one_change(world)["state"] == "merged"
    ledger = tables.curator_ledger
    lines = sql(
        world.db,
        select(ledger.c.action, ledger.c.actor, ledger.c.after_sha, ledger.c.what).where(ledger.c.action == "merged"),
    )
    assert lines == [("merged", "curator", HEAD, f"The hub's merge queue merged !1 into main as {HEAD}")]
