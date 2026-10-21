"""The release cycles of a project that ended, as the web's Releases page lists them under the cycle open now
(``GET /v1/projects/{p}/releases/cycles``, ``evo_agents.hub.server.releases``).

Step 4 of the daily-release plan: the page shows the schedule, the cycle open now and the cycles before it. A cycle
that ended, published or closed, is listed newest first with its runs (their mode, state, cost and landing) and the
tags it followed with their CI, a page of ``limit`` from ``offset`` with the total; the open cycle stays in the
status alone. Any member with a grant reads the list.
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select, update

from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_credentials_api import MINE, OWNER, app_key  # noqa: F401 (app_key is a fixture)
from tests.hub.test_credentials_api import worker_of as credential_worker
from tests.hub.test_release_schedule import HEAD, REPO, SIX, fire, path, set_schedule, status_of, user_id, world  # noqa: F401

PUSHED, MERGE = "f" * 40, "e" * 40
WORKFLOW_URL = f"https://github.com/{MINE}/{REPO}/actions/runs/7001"


def published(world) -> tuple[int, int]:
    """The 06:00 check opened a cycle and queued its cut run; the run ended done at $4.25, the hub tagged v0.25.0 and
    its workflow passed, so the cycle is published: (cycle id, run id)."""
    set_schedule(world, repo=REPO)
    assert fire(world, SIX) == {"opened": 1}
    c, rr, r, t = tables.release_cycles, tables.release_runs, tables.runs, tables.release_tags
    [(cycle_id, run_id)] = sql(world.db, select(rr.c.cycle_id, rr.c.run_id))
    worker = credential_worker(world.client, world.headers["owner"], "mac")["id"]
    ran = {"worker_id": worker, "leased_at": func.now(), "started_at": func.now(), "finished_at": func.now()}
    sql(world.db, update(r).values(**ran, state="done", usage={"total_cost_usd": 4.25}).where(r.c.id == run_id))
    landed = {"version": "0.25.0", "branch": "release/0.25.0", "head_commit": PUSHED, "merge_commit": MERGE}
    landed |= {"tagged": "v0.25.0", "pull_url": f"https://github.com/{MINE}/{REPO}/pull/12"}
    sql(world.db, update(rr).values(**landed, land_state="tagged", tagged_at=func.now()).where(rr.c.run_id == run_id))
    tag = {"cycle_id": cycle_id, "tag": "v0.25.0", "commit": MERGE, "run_id": run_id, "ci_state": "passed"}
    sql(world.db, insert(t).values(**tag, workflow_url=WORKFLOW_URL, run_attempt=1, checked_at=func.now()))
    sql(world.db, update(c).values(state="published", ended_at=func.now()).where(c.c.id == cycle_id))
    return cycle_id, run_id


def another_cycle(world, state: str, **values) -> int:
    """A cycle of the project's schedule with no run, in ``state``; its id."""
    c, s = tables.release_cycles, tables.release_schedules
    [(schedule_id, project_id)] = sql(world.db, select(s.c.id, s.c.project_id))
    row = {"project_id": project_id, "schedule_id": schedule_id, "state": state, "start": "cut"}
    row |= {"owner_id": user_id(world.db, OWNER), "local_day": date(2026, 10, 23), "target_ref": "nightly"}
    row |= {"budget_usd": 200.0, "changed_paths": ["web/"], **values}
    [(cycle_id,)] = sql(world.db, insert(c).values(**row).returning(c.c.id))
    return cycle_id


def ended(world, who: str = "other", **query):
    return world.client.get(path("cycles"), params=query, headers=world.headers[who])


def test_the_cycles_that_ended_are_listed_newest_first_with_their_runs_and_tags(world):
    cycle_id, run_id = published(world)
    closed = another_cycle(world, "closed", stop_reason="the release schedule was paused by owner", ended_at=func.now())
    still_open = another_cycle(world, "open")
    response = ended(world)
    assert response.status_code == 200, response.text
    found = response.json()
    assert (found["total"], found["limit"], found["offset"]) == (2, 20, 0)
    assert [cycle["id"] for cycle in found["cycles"]] == [closed, cycle_id], "newest first, the open cycle left out"
    last, first = found["cycles"]
    assert (last["state"], last["stop_reason"], last["runs"], last["tags"]) == (
        "closed",
        "the release schedule was paused by owner",
        [],
        [],
    )
    assert (first["state"], first["base_tag"], first["target_commit"], first["cost_usd"]) == (
        "published",
        "v0.24.0",
        HEAD,
        4.25,
    )
    [run] = first["runs"]
    assert {key: run[key] for key in ("run_id", "mode", "state", "cost_usd", "version", "tagged", "land_state")} == {
        "run_id": run_id,
        "mode": "cut",
        "state": "done",
        "cost_usd": 4.25,
        "version": "0.25.0",
        "tagged": "v0.25.0",
        "land_state": "tagged",
    }
    [tag] = first["tags"]
    assert {key: tag[key] for key in ("tag", "commit", "run_id", "ci_state", "workflow_url", "failed_tests")} == {
        "tag": "v0.25.0",
        "commit": MERGE,
        "run_id": run_id,
        "ci_state": "passed",
        "workflow_url": WORKFLOW_URL,
        "failed_tests": [],
    }
    assert status_of(world, "other")["cycle"]["id"] == still_open, "the open cycle is the status's"


def test_the_list_pages_by_limit_and_offset(world):
    cycle_id, _ = published(world)
    another_cycle(world, "closed", ended_at=func.now())
    page = ended(world, limit=1, offset=1).json()
    assert ([cycle["id"] for cycle in page["cycles"]], page["total"], page["limit"], page["offset"]) == (
        [cycle_id],
        2,
        1,
        1,
    )
    assert ended(world, limit=0).status_code == 422
    assert ended(world, limit=101).status_code == 422
    assert ended(world, offset=-1).status_code == 422


def test_a_project_without_a_schedule_lists_no_cycle_and_a_visitor_without_a_grant_reads_none(world):
    assert ended(world).json() == {
        "project": status_of(world)["project"],
        "cycles": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }
    refused = ended(world, "admin")
    assert refused.status_code == 403 and "needs a grant" in refused.text
