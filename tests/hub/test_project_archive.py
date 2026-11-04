"""An archived project (``evo_agents.hub.server.project_archive``): an admin of the project or a hub admin archives it
once its active runs are cancelled, never the agent of a run; it then takes no new run, local run, kg push or build,
the night shift, the Loop and the plugins' jobs leave it out, and the project list hides it unless asked; unarchiving
makes it live again. Each change is audited once."""

from __future__ import annotations

import uuid

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select, update

from evo_agents.hub import cli_client, kg_build, loop_jobs, plugin_jobs, tables
from evo_agents.hub.jobs import JobQueue
from evo_agents.hub.server import autonomy_hold, project_archive
from evo_agents.hub.server.curator_shift import due_schedules
from evo_agents.hub.server.runs.service.dispatch import insert_run
from tests.hub.live import ADMIN, sql
from tests.hub.test_autonomy_level_runs import on_db, user_id
from tests.hub.test_curator import charter_body, put_charter
from tests.hub.test_local_runs import register
from tests.hub.test_runs import (  # noqa: F401 (client and hub are fixtures)
    OWNER,
    PLAN,
    PROJECT,
    audit_rows,
    client,
    dispatch,
    dispatched,
    hub,
)


def archive(client, headers, action: str = "archive", project: str = PROJECT):
    return client.post(f"/v1/projects/{project}/{action}", headers=headers)


def archived(client, headers, action: str = "archive") -> dict:
    response = archive(client, headers, action)
    assert response.status_code == 200, response.text
    return response.json()


def listed(client, headers, **query) -> list[str]:
    response = client.get("/v1/projects", params=query, headers=headers)
    assert response.status_code == 200, response.text
    return [project["name"] for project in response.json()]


def project_id(db) -> int:
    p = tables.projects
    return sql(db, select(p.c.id).where(p.c.name == PROJECT))[0][0]


def make_admin(client, hub, login: str) -> None:
    body = {"role": "admin", "max_level": "internal"}
    response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=body, headers=hub["admin"])
    assert response.status_code == 200, response.text


def test_project_archive_by_an_admin_of_it_or_a_hub_admin_hides_it_from_the_list_and_is_audited_once(
    client, hub, hub_db
):
    assert archive(client, hub["owner"]).status_code == 403, "a writer does not archive"
    assert archive(client, hub["reader"]).status_code == 403
    assert archive(client, hub["stranger"]).status_code == 404, "a project one cannot see is not there"
    view = archived(client, hub["admin"])
    assert view["name"] == PROJECT and view["archived_at"] and view["archived_by"] == ADMIN
    assert archived(client, hub["admin"])["archived_at"] == view["archived_at"], "archiving again changes nothing"
    assert PROJECT not in listed(client, hub["owner"])
    assert PROJECT in listed(client, hub["owner"], archived="true")
    shown = client.get(f"/v1/projects/{PROJECT}", headers=hub["owner"])
    assert shown.status_code == 200 and shown.json()["archived_by"] == ADMIN, "one project shows either way"
    make_admin(client, hub, OWNER)
    live = archived(client, hub["owner"], "unarchive")
    assert (live["archived_at"], live["archived_by"]) == (None, None)
    assert archived(client, hub["owner"], "unarchive")["archived_at"] is None
    assert PROJECT in listed(client, hub["owner"])
    assert archived(client, hub["owner"])["archived_by"] == OWNER
    states = ("project.archive", "project.unarchive")
    assert [row[:3] for row in audit_rows(hub_db, "project") if row[0] in states] == [
        ("project.archive", PROJECT, ADMIN),
        ("project.unarchive", PROJECT, OWNER),
        ("project.archive", PROJECT, OWNER),
    ]


def test_project_archive_waits_for_the_active_runs_then_refuses_every_new_run_and_unarchive_lets_them_in(
    client, hub, hub_db
):
    make_admin(client, hub, OWNER)
    (run,) = dispatched(client, hub["owner"], [2])
    busy = archive(client, hub["owner"])
    assert busy.status_code == 409 and f"#{run['id']}" in busy.json()["message"], busy.text
    assert "nothing was archived" in busy.json()["message"]
    cancelled = client.post(f"/v1/projects/{PROJECT}/runs/{run['id']}/cancel", headers=hub["owner"])
    assert cancelled.status_code == 200, cancelled.text
    assert register(client, hub["owner"]).status_code == 201, "a local run does not hold the archive back"
    archived(client, hub["owner"])
    refused = dispatch(client, hub["owner"], [2])
    assert refused.status_code == 409, refused.text
    assert f"project {PROJECT} is archived" in refused.json()["message"]
    assert f"evo-agents hub project unarchive {PROJECT}" in refused.json()["message"]
    local = register(client, hub["owner"], "0199a3c1-0000-7000-8000-0000000000bb")
    assert local.status_code == 409 and "no local run" in local.json()["message"], local.text
    owner = user_id(hub_db, OWNER)
    values = {"kind": "review", "project_id": project_id(hub_db), "dispatched_by": owner}

    async def made_by_the_hub(conn):
        with pytest.raises(project_archive.Archived) as caught:
            await insert_run(conn, values)
        return caught.value

    caught = on_db(hub_db, made_by_the_hub)
    assert isinstance(caught, autonomy_hold.Stopped) and caught.status_code == 409, "chat and Loop answer it in words"
    archived(client, hub["owner"], "unarchive")
    assert dispatch(client, hub["owner"], [2]).status_code == 201


def test_project_archive_refuses_a_kg_push_and_a_build_and_queues_none_itself(client, hub, hub_db):
    archived(client, hub["admin"])
    push = {"run_id": str(uuid.uuid4()), "log_sha256": "a" * 64}
    pushed = client.post(f"/v1/kg/{PROJECT}/runs", json=push, headers=hub["owner"])
    assert pushed.status_code == 409 and "no kg push" in pushed.json()["message"], pushed.text
    built = client.post(f"/v1/kg/{PROJECT}/builds", headers=hub["owner"])
    assert built.status_code == 409 and "not built again" in built.json()["message"], built.text
    queued = on_db(
        hub_db, lambda conn: kg_build.queue_build(JobQueue.in_transaction(), conn, project_id(hub_db), PROJECT, None)
    )
    assert queued == (None, False)
    b = tables.kg_builds
    assert sql(hub_db, select(b.c.id).where(b.c.project_id == project_id(hub_db))) == []


def test_project_archive_leaves_it_out_of_the_night_shift_the_loop_and_the_plugins_jobs(client, hub, hub_db):
    make_admin(client, hub, OWNER)
    written = put_charter(client, hub["owner"], charter_body(night_plans=[PLAN]))
    assert written.status_code == 200, written.text
    pid = project_id(hub_db)
    sql(hub_db, insert(tables.loop_states).values(project_id=pid, enabled=True, enabled_at=func.now()))

    def schedulers():
        async def read(conn):
            due = [row.project for row in await conn.execute(due_schedules(None))]
            loop = await loop_jobs.is_on(conn, pid)
            plugins = [row.project for row in await conn.execute(plugin_jobs._live())]
            return due, loop, plugins

        return on_db(hub_db, read)

    due, loop, _ = schedulers()
    assert (due, loop) == ([PROJECT], True)
    archived(client, hub["owner"])
    assert schedulers() == ([], False, [])
    archived(client, hub["owner"], "unarchive")
    assert schedulers()[:2] == ([PROJECT], True)


def test_project_archive_check_keeps_archived_at_and_archived_by_together(client, hub, hub_db):
    p = tables.projects
    with pytest.raises(Exception, match="projects_archived_check"):
        sql(hub_db, update(p).values(archived_by=None, archived_at=select(p.c.created_at).scalar_subquery()))


def test_project_archive_cli_lines():
    live = {"name": "demo", "archived_at": None, "archived_by": None}
    assert cli_client.archive_lines(live) == [
        "Project demo is live: runs, kg pushes and builds, its night shift, Loop and plugins' jobs go on."
    ]
    lines = cli_client.archive_lines({**live, "archived_at": "2026-11-04T08:00:00Z", "archived_by": "ivan"})
    assert lines[0].startswith("Project demo is archived since 2026-11-04 08:00 UTC, by ivan: it takes no new run")
    assert lines[1] == "Make it live again with `evo-agents hub project unarchive demo`."
