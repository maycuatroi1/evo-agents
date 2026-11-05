"""The model login of a KG run on the shared KG pool (``share_leases.kg_pool_logins``).

A project made from an upload has no repo and no charter, so its KG runs (reviews with no repo) would be leased no
model login. On a node of the shared KG pool, such a run of a project the pool's owner created is leased the owner's own
env secrets for the model variables bound to that pool, whatever project each binding names, one per variable as
``account_choice`` chooses. A charter that gives a login keeps it; a project of another member, a project with a repo,
a secret of another member and a secret not bound to the pool get nothing of it.
"""

from __future__ import annotations

import secrets

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from evo_agents.hub import tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.sealing import KEY_BYTES
from tests.hub.live import sql
from tests.hub.test_credentials_api import leased, sample
from tests.hub.test_curator import OWNER, PROJECT, charter_body, night, written  # noqa: F401 (night is a fixture)
from tests.hub.test_kg_optimize import project_id
from tests.hub.test_kg_shared_pool import SHARED, asked, shared_node, uploads_only, user_id
from tests.hub.test_review_runs import review  # noqa: F401 (a fixture)
from tests.hub.test_runs import OTHER, claim, registration

VAR = "CLAUDE_CODE_OAUTH_TOKEN"
ELSEWHERE = "lumen-notes"  # another project of the pool's owner, which the pool's binding names


@pytest.fixture
def web_client(hub_db, tmp_path, github):
    """test_runs' web_client on a hub that keeps secrets, so its runs are leased them."""
    changes = {**live.web_changes(github), "secrets_key": secrets.token_bytes(KEY_BYTES)}
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github, **changes)), base_url="https://hub.test") as c:
        yield c


def set_secret(world, name: str, value: str, *, projects: list[str], pools=(), who: str = "owner") -> int:
    body = {"kind": "env", "env_var": VAR, "value": value, "projects": projects, "pools": list(pools)}
    response = world.client.put(f"/v1/secrets/{name}", json=body, headers=world.headers[who])
    assert response.status_code == 200, response.text
    s = tables.secrets
    return sql(world.db, select(s.c.id).where(s.c.name == name, s.c.deleted_at.is_(None)))[0][0]


def elsewhere(world) -> None:
    """ELSEWHERE, a project with a repo on which the owner is a writer."""
    client, admin = world.client, world.headers["admin"]
    assert client.put(f"/v1/projects/{ELSEWHERE}", json=registration(), headers=admin).status_code == 200
    grant = {"role": "writer", "max_level": "internal"}
    assert client.put(f"/v1/admin/projects/{ELSEWHERE}/grants/{OWNER}", json=grant, headers=admin).status_code == 200


def kg_run_on(world, node: dict) -> int:
    """A KG run of the project fed by uploads alone, claimed by ``node``; its id."""
    run_id = asked(world)["agent_run_id"]
    assert run_id and claim(world.client, node)["id"] == run_id
    return run_id


def logins(world, node: dict, run_id: int) -> dict[str, str]:
    """The model logins run ``run_id`` is leased: lease name -> value."""
    answer = leased(world.client, node, run_id)
    return {item["name"]: item["value"] for item in answer["leases"] if item.get("env_var") == VAR}


def test_a_kg_run_on_the_shared_pool_takes_its_owners_login_bound_to_the_pool(review):
    node = shared_node(review)
    uploads_only(review)
    elsewhere(review)
    token = sample("sk-ant-oat01-")
    kg = set_secret(review, "claude-kg", token, projects=[ELSEWHERE], pools=[SHARED])
    set_secret(review, "claude-home", sample(), projects=[PROJECT])  # on any worker, not bound to the pool
    set_secret(review, "claude-elsewhere", sample(), projects=[ELSEWHERE])
    theirs = set_secret(review, "claude-theirs", sample(), projects=[PROJECT], who="other")
    b, pid = tables.secret_bindings, project_id(review.db)
    sql(review.db, b.insert().values(secret_id=theirs, project_id=pid, pool_id=node["pool_id"]))  # not the owner's
    run_id = kg_run_on(review, node)
    sql(review.db, tables.charters.delete().where(tables.charters.c.project_id == pid))  # an upload's: no charter

    assert logins(review, node, run_id) == {"claude-kg": token}
    first = [row for row in leased(review.client, node, run_id)["leases"] if row["name"] == "claude-kg"]
    again = [row for row in leased(review.client, node, run_id)["leases"] if row["name"] == "claude-kg"]
    assert [row["id"] for row in first] == [row["id"] for row in again], "the same lease on every ask"
    ra = tables.run_accounts
    assert sql(review.db, select(ra.c.secret_id, ra.c.how).where(ra.c.run_id == run_id)) == [(kg, "charter")]
    a = tables.audit
    targets = [row[0] for row in sql(review.db, select(a.c.target).where(a.c.action == "credential.lease"))]
    assert targets and all(f"secrets={kg} " in target for target in targets), targets
    assert not any(token in target for target in targets)


def test_a_charter_that_gives_a_login_keeps_it(review):
    node = shared_node(review)
    uploads_only(review)
    set_secret(review, "claude-kg", sample(), projects=[PROJECT], pools=[SHARED])
    charter = sample()
    set_secret(review, "claude-charter", charter, projects=[PROJECT])
    written(review.client, review.headers["owner"], charter_body(night_plans=[], env_secrets=["claude-charter"]))
    run_id = kg_run_on(review, node)
    assert logins(review, node, run_id) == {"claude-charter": charter}


def test_a_kg_run_of_another_members_project_or_of_a_project_with_a_repo_takes_no_pool_login(review):
    node = shared_node(review)
    uploads_only(review)
    token = sample()
    set_secret(review, "claude-kg", token, projects=[PROJECT], pools=[SHARED])
    run_id = kg_run_on(review, node)
    db, pid, projects = review.db, project_id(review.db), tables.projects

    sql(db, update(projects).values(created_by=user_id(db, OTHER)).where(projects.c.id == pid))
    assert logins(review, node, run_id) == {}, "the pool's owner did not create the project"
    sql(db, update(projects).values(created_by=user_id(db, OWNER)).where(projects.c.id == pid))
    sql(db, tables.project_repos.insert().values(project_id=pid, name="evo-agents"))
    assert logins(review, node, run_id) == {}, "a project with a repo is no KG project of the shared pool"
    sql(db, tables.project_repos.delete().where(tables.project_repos.c.project_id == pid))
    assert logins(review, node, run_id) == {"claude-kg": token}
