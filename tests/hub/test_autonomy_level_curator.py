"""K5 at the Curator's tier 0 merge (``evo_agents.hub.server.changes``), as criterion a6 of plan autonomy-levels names
it: before the hub merges a change its Judge passed, it reads K5 of the principal of the Builder's run. deny leaves the
pull request open with a reason that names the class, the mode and the source; ask leaves the change judged while the
principal is asked, and merges once they approve; notify merges and tells them. The world is
``tests.hub.test_curator_changes``'s; the projects and people are fictional."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import runs, tables
from tests.hub.live import sql
from tests.hub.test_credentials_api import MINE, OWNER, audit_rows
from tests.hub.test_curator import PROJECT
from tests.hub.test_curator_changes import (  # noqa: F401 (app_key and world are fixtures)
    GREEN,
    HEAD,
    SUCCESS,
    app_key,
    moved_on,
    one_change,
    through_the_judge,
    world,
)

KEY = "merge evo-agents#1"


def at(world, **classes) -> None:
    """The owner's autonomy holds ``classes``, and mac-mini's daemon says it may run a restricted run."""
    body = {"level": "A5", "classes": classes}
    response = world.client.put("/v1/autonomy/prefs", json=body, headers=world.headers["owner"])
    assert response.status_code == 200, response.text
    worker = world.worker
    beat = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": 1,
        "runs": [],
        "agent_version": "0.7.0",
        "run_kinds": list(runs.RUN_KINDS),
        "features": list(runs.WORKER_FEATURES),
    }
    assert world.client.post("/v1/worker/heartbeat", json=beat, headers=worker["headers"]).status_code == 200


def judged_green(world) -> None:
    through_the_judge(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)


def test_autonomy_level_merge_a_curator_change_under_deny_stays_open_with_the_class_mode_and_source(world):
    at(world, K5="deny")
    judged_green(world)
    assert moved_on(world) == {"checked": 1, "open": 1}
    change = one_change(world)
    assert change["state"] == "open" and world.github.merges == []
    assert "K5 (merge into the default or target branch) is deny" in change["reason"]
    assert f"set by pref:{OWNER}" in change["reason"]
    ((target, *_),) = audit_rows(world.db, "autonomy.refused")
    assert target.endswith(f"K5 {KEY} mode=deny")


def test_autonomy_level_merge_a_curator_change_under_ask_merges_once_the_principal_approves(world):
    at(world, K5="ask")
    judged_green(world)
    assert moved_on(world) == {"checked": 1, "wait": 1}
    change = one_change(world)
    assert change["state"] == "judged" and world.github.merges == []
    d = tables.decisions
    ((decision_id, key),) = sql(
        world.db, select(d.c.id, d.c.step_key).where(d.c.category == runs.AUTONOMY_DECISION_CATEGORY)
    )
    assert key == KEY and f"decision #{decision_id}" in change["reason"]
    path = f"/v1/projects/{PROJECT}/decisions/{decision_id}/answer"
    answered = world.client.post(path, json={"option": "approve"}, headers=world.headers["owner"])
    assert answered.status_code == 200, answered.text
    assert moved_on(world) == {"merged": 1}
    assert world.github.merges == [(MINE, "evo-agents", 1, HEAD, "workers")]


def test_autonomy_level_merge_a_curator_change_under_notify_in_a_repo_the_policy_keeps_off_merges_as_before(world):
    """The project has no policy, so evo-agents is in mode off: the policy's ask of K5 is stricter than notify, and
    an ask a project policy sets is left to the flow that holds it, here the charter's auto_merge: the hub merges and
    tells no one."""
    at(world, K5="notify")
    judged_green(world)
    assert moved_on(world) == {"checked": 1, "merged": 1}
    n = tables.notifications
    assert sql(world.db, select(n.c.title).where(n.c.notice_kind == "autonomy")) == []
    assert audit_rows(world.db, "autonomy.refused") == [] and audit_rows(world.db, "autonomy.asked") == []
