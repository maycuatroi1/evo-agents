"""The principal and the autonomy snapshot of a run (``evo_agents.hub.server.run_autonomy``), as criteria a4, a5 and a7
of plan autonomy-levels name them: schema 0106 gives runs principal_id and autonomy; a dispatched run acts for its
dispatcher, a run the hub makes on its own for its agent's sponsor, and its snapshot is the effective autonomy when it was
queued, over its repos; each gate takes the stricter of the snapshot and the value now, so a lower level reaches the run
at its next gate and a higher one only later runs; a claim gives a run only to a worker its snapshot allows, and a run
with a class below auto only to a daemon that says ``autonomy``, on claude-code. Chat runs: test_autonomy_level_chat.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from evo_agents.hub import autonomy_levels, runs, tables
from evo_agents.hub.server import run_autonomy
from tests.hub.live import sql
from tests.hub.test_migrate import columns, move_to
from tests.hub.test_runs import (  # noqa: F401 (client and hub are fixtures)
    OTHER,
    OWNER,
    PROJECT,
    add_worker,
    claim,
    client,
    dispatched,
    hub,
    of_run,
    run_of,
    set_run,
)

FEATURES = [runs.AUTONOMY, runs.AGENT_PROFILES]


def on_db(db, work):
    """``work(conn)`` on an async connection of ``db``, in one transaction that commits."""

    async def go():
        engine = create_async_engine(db.dsn.replace("postgresql://", "postgresql+psycopg://", 1))
        try:
            async with engine.begin() as conn:
                return await work(conn)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def org(client, headers, method: str, path: str, body: dict | None = None, status: int = 200) -> dict:
    response = client.request(method, f"/v1{path}", json=body, headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def pref(client, headers, level: str | None = None, **body) -> dict:
    return org(client, headers, "PUT", "/autonomy/prefs", {"level": level, **body} if level else body)


def root_ceiling(client, hub, **limits) -> dict:
    return org(client, hub["admin"], "PUT", "/org/ceilings", {"unit": "root", "level": "A5", "note": "n", **limits})


def daemon(client, worker: dict, features=FEATURES) -> dict:
    """The heartbeat of ``worker`` from a daemon that says ``features``."""
    body = {"runtimes": worker["runtimes"], "checkouts": worker["checkouts"], "free_slots": 1, "features": features}
    response = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return worker


def snapshot_of(db, run_id: int) -> dict:
    return of_run(db, run_id, "autonomy")[0][0]


def user_id(db, login: str) -> int:
    u = tables.users
    return sql(db, select(u.c.id).where(u.c.login == login))[0][0]


# Schema 0106


def test_autonomy_level_snapshot_migration_0106_adds_principal_and_autonomy_to_runs_and_going_back_drops_them(hub_db):
    move_to(hub_db, "0105", down=True)
    assert not {"principal_id", "autonomy"} & columns(hub_db, "runs")
    move_to(hub_db, "0106")
    assert {"principal_id", "autonomy"} <= columns(hub_db, "runs")
    move_to(hub_db, "0105", down=True)
    assert not {"principal_id", "autonomy"} & columns(hub_db, "runs")
    move_to(hub_db, "0106")


# The principal and the snapshot (criterion a4)


def test_autonomy_level_principal_and_snapshot_of_a_dispatched_run_are_its_dispatcher_and_their_autonomy_then(
    client, hub, hub_db
):
    pref(client, hub["owner"], "A4")
    run = dispatched(client, hub["owner"], [2])[0]
    assert of_run(hub_db, run["id"], "principal_id") == [(user_id(hub_db, OWNER),)]
    shown = run_of(client, hub["owner"], run["id"])
    assert shown["principal"] == OWNER
    held = shown["autonomy"]
    assert (held["level"], held["level_source"], held["restricted"], held["also"]) == ("A4", f"pref:{OWNER}", True, [])
    assert held["classes"]["K8"]["mode"] == "notify" and held["classes"]["K8"]["source"] == f"pref:{OWNER}"
    assert held["classes"]["K5"]["source"] == f"policy:{PROJECT}/evo-agents", "the run's repo, under its policy off"
    assert (held["environments"], held["workers"], held["tightened_at"]) == (None, None, None)
    assert held["taken_at"]
    # a run of a person without a preference or a ceiling is A5 but for its repo's policy, and claims as today
    other = dispatched(client, hub["other"], [4])[0]
    theirs = run_of(client, hub["other"], other["id"])
    assert (theirs["principal"], theirs["autonomy"]["level"], theirs["autonomy"]["restricted"]) == (OTHER, "A5", False)


def test_autonomy_level_principal_of_a_run_the_hub_makes_is_the_sponsor_of_its_agent_and_the_owner_without_a_seat(
    client, hub, hub_db
):
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    profile = tables.agent_profiles
    profile_id = sql(hub_db, select(profile.c.id).where(profile.c.project_id == project_id))[0][0]
    owner, other = user_id(hub_db, OWNER), user_id(hub_db, OTHER)
    values = {"kind": "review", "project_id": project_id, "dispatched_by": owner, "profile_id": profile_id}
    alone = on_db(hub_db, lambda conn: run_autonomy.stamp(conn, values))
    assert alone["principal_id"] == owner, "an agent without a seat acts for the run's owner, as today"
    seat = {"agent": PROJECT, "project": PROJECT, "unit": "root", "role": "agent", "sponsor": OTHER}
    org(client, hub["admin"], "POST", "/org/seats", seat, 201)
    pref(client, hub["other"], "A2")
    sponsored = on_db(hub_db, lambda conn: run_autonomy.stamp(conn, values))
    assert (sponsored["principal_id"], sponsored["autonomy"]["level"]) == (other, "A2")
    # a run the hub makes for a person (the Loop, for the owner of the work item's goal) acts for them
    loop = on_db(hub_db, lambda conn: run_autonomy.stamp(conn, values, run_autonomy.For(user_id=owner, hub=True)))
    assert (loop["principal_id"], loop["autonomy"]["level"]) == (owner, "A5")
    # a step run acts for its dispatcher, the seat notwithstanding
    step = on_db(hub_db, lambda conn: run_autonomy.stamp(conn, {**values, "kind": "step"}))
    assert step["principal_id"] == owner


def test_autonomy_level_snapshot_of_run_as_is_the_strictest_of_both_and_of_an_agent_within_its_parent(
    hub_db, client, hub
):
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    owner, other = user_id(hub_db, OWNER), user_id(hub_db, OTHER)
    pref(client, hub["owner"], "A4")
    pref(client, hub["other"], "A2", classes={"K10": "deny"})
    values = {"kind": "chat", "project_id": project_id, "dispatched_by": other}
    both = on_db(hub_db, lambda conn: run_autonomy.stamp(conn, values, run_autonomy.For(user_id=owner, also=(other,))))
    held = both["autonomy"]
    assert (both["principal_id"], held["level"], held["also"]) == (owner, "A2", [OTHER])
    assert held["classes"]["K10"]["mode"] == "deny" and held["classes"]["K8"]["mode"] == "deny"
    run = dispatched(client, hub["owner"], [2])[0]
    within = on_db(
        hub_db,
        lambda conn: run_autonomy.stamp(conn, {**values, "dispatched_by": other}, run_autonomy.For(within=run["id"])),
    )
    assert within["principal_id"] == owner, "an agent's run asked by another agent acts for the principal of that run"
    assert within["autonomy"]["level"] == "A4"


# Each gate takes the stricter (criterion a5)


def test_autonomy_level_recheck_lowers_a_queued_run_at_its_next_gate_and_a_raise_reaches_only_later_runs(
    client, hub, hub_db
):
    first = dispatched(client, hub["owner"], [2])[0]
    assert snapshot_of(hub_db, first["id"])["level"] == "A5"
    pref(client, hub["owner"], "A3")
    now = on_db(hub_db, lambda conn: run_autonomy.recheck(conn, first["id"]))
    assert now.level == "A3"
    held = snapshot_of(hub_db, first["id"])
    assert (held["level"], held["restricted"]) == ("A3", True) and held["tightened_at"], "written back"
    # a raise reaches no run queued before it: the snapshot stays the stricter
    pref(client, hub["owner"], reset=True)
    again = on_db(hub_db, lambda conn: run_autonomy.recheck(conn, first["id"]))
    assert again.level == "A3" and snapshot_of(hub_db, first["id"])["level"] == "A3"
    later = dispatched(client, hub["owner"], [4])[0]
    assert snapshot_of(hub_db, later["id"])["level"] == "A5"
    # a gate on one repo takes its policy; a run without a snapshot (local, or older) has none to take
    on_repo = on_db(hub_db, lambda conn: run_autonomy.recheck(conn, later["id"], repo="evo-agents"))
    assert on_repo.classes["K5"].source == f"policy:{PROJECT}/evo-agents"
    set_run(hub_db, later["id"], autonomy=None)
    assert on_db(hub_db, lambda conn: run_autonomy.recheck(conn, later["id"])) is None


def test_autonomy_level_stricter_takes_the_lower_level_each_stricter_class_and_the_common_lists():
    loose = autonomy_levels.effective([autonomy_levels.Source("pref:a", "A5", workers=("w1", "w2"))])
    tight = autonomy_levels.effective([autonomy_levels.Source("pref:b", "A2", environments=("host",), workers=("w2",))])
    both = autonomy_levels.stricter(loose, tight)
    assert (both.level, both.environments, both.workers) == ("A2", ("host",), ("w2",))
    cells = {klass: (held.mode, held.condition) for klass, held in both.classes.items()}
    assert cells == {klass: (held.mode, held.condition) for klass, held in tight.classes.items()}
    assert both.classes["K6"].source == "pref:b" and both.classes["K1"].source == "pref:a", "the first on a tie"
    assert autonomy_levels.from_snapshot(autonomy_levels.snapshot(both, "t")) == both
    assert autonomy_levels.restricted(tight) and not autonomy_levels.restricted(loose)


# The claim (criterion a7)


def test_autonomy_level_claim_of_a_run_below_auto_needs_a_daemon_of_autonomy_on_claude_code(client, hub, hub_db):
    plain = add_worker(client, hub["owner"], "mac-mini")
    pref(client, hub["owner"], "A3")
    run = dispatched(client, hub["owner"], [2], runtime="any")[0]
    assert claim(client, plain) is None, "a daemon that does not apply the modes takes none"
    applying = daemon(client, add_worker(client, hub["owner"], "studio"))
    spec = claim(client, applying)
    assert (spec["id"], spec["runtime"]) == (run["id"], runs.AUTONOMY_RUNTIME)


def test_autonomy_level_claim_of_an_a5_run_and_one_without_a_snapshot_is_as_today(client, hub, hub_db):
    plain = add_worker(client, hub["owner"], "mac-mini")
    first = dispatched(client, hub["owner"], [2])[0]
    assert claim(client, plain)["id"] == first["id"], "policy off of its repo is no class below auto here"
    plain2 = add_worker(client, hub["owner"], "mini-2")
    second = dispatched(client, hub["owner"], [4])[0]
    set_run(hub_db, second["id"], autonomy=None, principal_id=None)
    assert claim(client, plain2)["id"] == second["id"]


def test_autonomy_level_claim_only_on_the_workers_and_environments_the_snapshot_allows(client, hub, hub_db):
    root_ceiling(client, hub, workers=["mac-mini"])
    run = dispatched(client, hub["owner"], [2])[0]
    assert snapshot_of(hub_db, run["id"])["workers"] == ["mac-mini"]
    other = add_worker(client, hub["owner"], "studio")
    assert claim(client, other) is None
    allowed = add_worker(client, hub["owner"], "mac-mini")
    assert claim(client, allowed)["id"] == run["id"]
    root_ceiling(client, hub, environments=["docker/py"])
    boxed = dispatched(client, hub["owner"], [4])[0]
    assert claim(client, other) is None, "a worker that takes neither host nor docker/py"
    assert snapshot_of(hub_db, boxed["id"])["environments"] == ["docker/py"]


def test_autonomy_level_claim_rechecks_a_run_a_lower_level_reached_after_its_dispatch(client, hub, hub_db):
    plain = add_worker(client, hub["owner"], "mac-mini")
    run = dispatched(client, hub["owner"], [2])[0]
    assert snapshot_of(hub_db, run["id"])["restricted"] is False
    pref(client, hub["owner"], "A2")
    assert claim(client, plain) is None, "the claim found the lower level and passed it over"
    held = snapshot_of(hub_db, run["id"])
    assert (held["level"], held["restricted"]) == ("A2", True) and held["tightened_at"]
    assert of_run(hub_db, run["id"], "state") == [("queued",)]
    applying = daemon(client, add_worker(client, hub["owner"], "studio"))
    assert claim(client, applying)["id"] == run["id"]


def test_autonomy_level_claim_fits_says_why_a_worker_may_not_take_a_run():
    tight = autonomy_levels.effective(
        [autonomy_levels.Source("pref:b", "A2", environments=("docker/py",), workers=("w2",))]
    )
    assert "workers w2" in run_autonomy.claim_fits(tight, "w1", (), FEATURES, True)
    assert "environments docker/py" in run_autonomy.claim_fits(tight, "W2", (), FEATURES, True)
    assert "below auto" in run_autonomy.claim_fits(tight, "w2", ("docker/py",), (), True)
    assert "below auto" in run_autonomy.claim_fits(tight, "w2", ("docker/py",), FEATURES, False)
    assert run_autonomy.claim_fits(tight, "w2", ("docker/py",), FEATURES, True) is None
    assert run_autonomy.runtime_fits("any", ["codex", "claude-code"], None)
    assert not run_autonomy.runtime_fits("any", ["codex"], None) and not run_autonomy.runtime_fits("codex", [], None)
    assert not run_autonomy.runtime_fits("any", ["claude-code"], {"mode": "allowlist", "allow": ["codex"]})
    assert run_autonomy.environment_rule(None, tight) == {"mode": "allowlist", "allow": ["docker/py"]}
