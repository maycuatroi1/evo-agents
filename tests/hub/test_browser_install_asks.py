"""The hub's own Install browser (``evo_agents.hub.server.browser_install.ask_for_queued``, on each pass of the reaper): a
queued run dispatched with browser that no worker can take has its owner's nearest worker that lacks only Chromium,
and whose daemon says browser_install, asked to install it; the run waits for that install (``queue_fit``) and the
worker claims it once its heartbeat lists the feature browser. A worker whose last install ended without Chromium,
one of another member and one of a daemon too old are not asked, and the diagnosis says what to do instead."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import runs, tables
from tests.hub.live import sql
from tests.hub.test_agent_pool_placement import ADMIN_GRANT, OWNER, join, make_agent
from tests.hub.test_agent_pool_placement import worker as pool_worker
from tests.hub.test_browser_claim import REPORT, browser_beat
from tests.hub.test_queued_candidates import by_name, candidates
from tests.hub.test_runs import (  # noqa: F401  (fixtures)
    PROJECT,
    add_worker,
    beat,
    claim,
    client,
    dispatched,
    hub,
    recover,
    state_of,
)
from tests.hub.test_workers import browser_install_of

LACKS = "starts no browser, and the run asks for a Chromium of its own"
CAN_INSTALL = [feature for feature in runs.WORKER_FEATURES if feature != runs.BROWSER]  # browser_install, no browser


def hub_asks(db) -> list[tuple]:
    """The audit rows of an Install browser the hub opened itself: (target, project)."""
    a, p = tables.audit, tables.projects
    query = (
        select(a.c.target, p.c.name)
        .outerjoin(p, p.c.id == a.c.project_id)
        .where(a.c.action == "worker.browser_install", a.c.actor_id.is_(None))
        .order_by(a.c.id)
    )
    return sql(db, query)


def lacking_chromium(client, headers, name: str, features=CAN_INSTALL, **extra) -> dict:
    worker = add_worker(client, headers, name, **extra)
    assert browser_beat(client, worker, None, features).status_code == 200
    return worker


def test_a_browser_run_no_worker_can_take_has_the_hub_install_chromium_and_the_worker_takes_it(client, hub, hub_db):
    lab = lacking_chromium(client, hub["owner"], "lab")
    (run,) = dispatched(client, hub["owner"], [2], browser=True)
    assert run["warning"].endswith(f"the nearest, lab, {LACKS}: the hub asks it to install one within a minute")
    assert browser_install_of(hub_db, lab["id"])[0] is None

    recover(client)
    requested_at, ended_at, _, _ = browser_install_of(hub_db, lab["id"])
    assert requested_at is not None and ended_at is None
    assert hub_asks(hub_db) == [(f"worker:{lab['id']} name=lab owner={OWNER} run={run['id']}", PROJECT)]
    answer = candidates(client, hub["owner"], run["id"])
    assert answer["diagnosis"] is None  # it waits for the install, which nobody has to do
    found = by_name(answer)["lab"]
    assert found["fit"] and found["reasons"] == [
        f"{LACKS}: it installs the Chromium asked 0 min ago, and takes the run then"
    ]
    assert claim(client, lab) is None  # no Chromium yet

    recover(client)  # the request is open: no second one
    assert len(hub_asks(hub_db)) == 1
    answered = browser_beat(client, lab, REPORT, runs.WORKER_FEATURES)  # installed: its heartbeat lists browser
    assert answered.status_code == 200 and "browser_install" not in answered.json()
    assert browser_install_of(hub_db, lab["id"])[2] == "installed"
    assert claim(client, lab)["id"] == run["id"]


def test_a_worker_whose_install_failed_is_not_asked_again_and_the_diagnosis_says_why(client, hub, hub_db):
    lab = lacking_chromium(client, hub["owner"], "lab")
    (run,) = dispatched(client, hub["owner"], [2], browser=True)
    recover(client)
    requested_at = browser_install_of(hub_db, lab["id"])[0].isoformat()
    failed = {"requested_at": requested_at, "ended": "failed", "error": "npx is not on PATH: install Node.js"}
    beat(client, lab, features=CAN_INSTALL, browser_install=failed)
    assert browser_install_of(hub_db, lab["id"])[2:] == ("failed", "npx is not on PATH: install Node.js")

    recover(client)
    assert len(hub_asks(hub_db)) == 1 and state_of(hub_db, run["id"]) == "queued"
    answer = candidates(client, hub["owner"], run["id"])
    why = (
        f"{LACKS}, and its last install from the hub ended failed (npx is not on PATH: install Node.js): "
        "on its machine, `evo-agents worker browser install`"
    )
    assert answer["diagnosis"].endswith(f"the nearest, lab, {why}")
    assert by_name(answer)["lab"]["reasons"] == [why]


def test_a_daemon_that_cannot_install_from_the_hub_is_told_to_install_on_its_machine(client, hub, hub_db):
    old = lacking_chromium(client, hub["owner"], "old", features=[])
    (run,) = dispatched(client, hub["owner"], [2], browser=True)
    recover(client)
    assert browser_install_of(hub_db, old["id"])[0] is None and hub_asks(hub_db) == []
    reason = (
        f"{LACKS}: on its machine, `evo-agents worker browser install`, or update its daemon so the hub installs one"
    )
    assert by_name(candidates(client, hub["owner"], run["id"]))["old"]["reasons"] == [reason]


def test_a_node_of_another_member_is_not_asked_and_its_owner_is_named(client, hub, hub_db):
    grant = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=hub["admin"])
    assert grant.status_code == 200, grant.text
    make_agent(client, hub["owner"], "helper", db=hub_db)
    node = join(client, hub_db, pool_worker(client, hub["other"], "node"), "helper")
    assert browser_beat(client, node, None, CAN_INSTALL).status_code == 200
    (run,) = dispatched(client, hub["owner"], [2], profile="helper", browser=True)
    recover(client)
    assert browser_install_of(hub_db, node["id"])[0] is None and hub_asks(hub_db) == []
    (reason,) = by_name(candidates(client, hub["owner"], run["id"]))["node"]["reasons"]
    assert reason.startswith(f"{LACKS}: its owner ") and "installs one with Install browser" in reason
