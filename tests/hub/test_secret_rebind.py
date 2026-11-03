"""PATCH /v1/secrets/{name}: the binding of a member's secret replaced alone, without its value
(``evo_agents.hub.server.secrets.rebind_secret``).

A secret bound to a worker that was revoked reaches no run; bound again to the pool its runs now go to, a node of the
pool is leased the same value, and its sealed column and ``updated_at`` stay as they were. The body names workers or
pools, exactly one of them, and an empty list binds the secret to any worker of its owner again, on each of its
projects. Names are checked as PUT checks them: the caller's own workers that are not revoked, their own pools, and
none for a forge secret. Another member's secret is 404. Each PATCH writes one audit row secret.rebind naming the
secret by id and the new binding, never a value. A run's token is refused the route with the rest of /v1/secrets
(``tests.hub.test_run_access``, ``tests.hub.test_run_reach``); a provider account's name is 409
(``tests.hub.test_provider_accounts``)."""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from tests.hub.live import add_project, sql, table_dump
from tests.hub.test_credentials_api import (  # noqa: F401  (app_key, client, config and hub are fixtures)
    OWNER,
    WRITER,
    app_key,
    audit_rows,
    by_name,
    client,
    config,
    env_secret,
    hub,
    leased,
    sample,
    step_run,
    worker_of,
)
from tests.hub.test_pool_bindings import ci_node, secret_id_of
from tests.hub.test_pool_scheduling import pool
from tests.hub.test_runs import PROJECT
from tests.hub.test_secrets_api import SECRET_FIELDS

OPS = "lumen-ops"  # a second project of the owner's


def rebind(client, headers, name: str, **body) -> dict:
    response = client.patch(f"/v1/secrets/{name}", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == SECRET_FIELDS
    return response.json()


def bindings_of(db, name: str) -> list[tuple]:
    """(project, worker, pool) of each binding of the live secret ``name``, by name, in order."""
    b, p, w, wp = tables.secret_bindings, tables.projects, tables.workers, tables.worker_pools
    joined = b.join(p, p.c.id == b.c.project_id).outerjoin(w, w.c.id == b.c.worker_id)
    query = (
        select(p.c.name, w.c.name, wp.c.name)
        .select_from(joined.outerjoin(wp, wp.c.id == b.c.pool_id))
        .where(b.c.secret_id == secret_id_of(db, name))
        .order_by(p.c.name, w.c.name, wp.c.name)
    )
    return sql(db, query)


def sealed_of(db, name: str) -> tuple:
    """(sealed, nonce, key_id, created_at, updated_at) of the live secret ``name``."""
    s = tables.secrets
    columns = (s.c.sealed, s.c.nonce, s.c.key_id, s.c.created_at, s.c.updated_at)
    return sql(db, select(*columns).where(s.c.name == name, s.c.deleted_at.is_(None)))[0]


def revoke(client, headers, worker: dict) -> None:
    response = client.post(f"/v1/workers/{worker['id']}/revoke", headers=headers)
    assert response.status_code == 200, response.text


def test_a_secret_of_a_revoked_worker_moves_to_a_pool_and_back_its_value_untouched(client, hub, hub_db):
    add_project(hub_db, OPS)
    granted = client.put(f"/v1/admin/projects/{OPS}/grants/{OWNER}", json=WRITER, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    value = sample("api-")
    old = worker_of(client, hub["owner"], "old-box")
    env_secret(client, hub["owner"], "api", "API_TOKEN", value, projects=[PROJECT, OPS], workers=["old-box"])
    revoke(client, hub["owner"], old)
    before = sealed_of(hub_db, "api")
    ci = pool(client, hub["owner"])
    lab = ci_node(client, hub["owner"], hub_db, "lab", ci)

    moved = rebind(client, hub["owner"], "api", pools=["CI"])  # a pool's name in any case, as PUT takes it
    assert (moved["projects"], moved["workers"], moved["pools"]) == ([PROJECT, OPS], [], ["ci"])
    assert (moved["kind"], moved["env_var"], moved["expires_at"]) == ("env", "API_TOKEN", None)
    assert bindings_of(hub_db, "api") == [(PROJECT, None, "ci"), (OPS, None, "ci")]
    assert sealed_of(hub_db, "api") == before  # the value is not written again, nor is updated_at moved
    on_lab = step_run(client, hub["owner"], "notes", lab)
    assert by_name(leased(client, lab, on_lab))["api"]["value"] == value

    desk = worker_of(client, hub["owner"], "desk")
    back = rebind(client, hub["owner"], "api", workers=["desk", "DESK"])
    assert (back["workers"], back["pools"]) == (["desk"], [])
    assert bindings_of(hub_db, "api") == [(PROJECT, "desk", None), (OPS, "desk", None)]
    on_desk = step_run(client, hub["owner"], "lumen-kb-docs", desk)
    assert by_name(leased(client, desk, on_desk))["api"]["value"] == value

    anywhere = rebind(client, hub["owner"], "api", pools=[])
    assert (anywhere["workers"], anywhere["pools"]) == ([], [])
    assert bindings_of(hub_db, "api") == [(PROJECT, None, None), (OPS, None, None)]
    assert sealed_of(hub_db, "api") == before
    assert client.get("/v1/secrets", headers=hub["owner"]).json() == [anywhere]

    target = f"secret:{secret_id_of(hub_db, 'api')}"
    assert audit_rows(hub_db, "secret.rebind") == [
        (f"{target} pools=ci", OWNER, None, True),
        (f"{target} workers=desk", OWNER, None, True),
        (f"{target} anywhere", OWNER, None, True),
    ]
    assert value not in table_dump(hub_db)


def test_a_patch_names_workers_or_pools_exactly_one_and_nothing_else(client, hub, hub_db):
    worker_of(client, hub["owner"], "desk")
    pool(client, hub["owner"])
    env_secret(client, hub["owner"], "api", "API_TOKEN", sample(), workers=["desk"])
    value = sample("never-")
    refused = (
        ({}, "give workers or pools:"),
        ({"workers": None}, "give workers or pools:"),
        ({"workers": ["desk"], "pools": ["ci"]}, "not both"),
        ({"workers": [], "pools": []}, "not both"),
        ({"pools": ["ci"], "value": value}, "does not match the API"),
        ({"pools": ["ci"], "projects": [PROJECT]}, "does not match the API"),
        ({"workers": ["no spaces"]}, "does not match the API"),
    )
    for body, problem in refused:
        response = client.patch("/v1/secrets/api", json=body, headers=hub["owner"])
        assert response.status_code == 422 and problem in response.text, (body, response.text)
        assert value not in response.text
    assert bindings_of(hub_db, "api") == [(PROJECT, "desk", None)]
    assert audit_rows(hub_db, "secret.rebind") == []


def test_only_the_owner_rebinds_and_only_to_their_own_live_workers_and_pools(client, hub, hub_db):
    worker_of(client, hub["owner"], "desk")
    pool(client, hub["owner"])
    revoke(client, hub["owner"], worker_of(client, hub["owner"], "gone-box"))
    worker_of(client, hub["other"], "their-box")
    pool(client, hub["other"], "their-ci")
    env_secret(client, hub["owner"], "api", "API_TOKEN", sample(), workers=["desk"])

    theirs = client.patch("/v1/secrets/api", json={"pools": []}, headers=hub["other"])
    assert theirs.status_code == 404 and "you have no secret named api" in theirs.text
    for body, problem in (
        ({"workers": ["their-box"]}, "their-box is not a worker of yours"),
        ({"workers": ["gone-box"]}, "gone-box is not a worker of yours"),
        ({"workers": ["desk", "nowhere"]}, "nowhere is not a worker of yours"),
        ({"pools": ["their-ci"]}, "their-ci is not a worker pool of yours"),
    ):
        response = client.patch("/v1/secrets/api", json=body, headers=hub["owner"])
        assert response.status_code == 403 and problem in response.text, (body, response.text)
    assert client.patch("/v1/secrets/none-such", json={"pools": []}, headers=hub["owner"]).status_code == 404
    assert bindings_of(hub_db, "api") == [(PROJECT, "desk", None)]

    # a forge secret binds to no worker or pool, by PATCH as by PUT
    admin = {"role": "admin", "max_level": "internal"}
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=admin, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    forge = {"kind": "forge", "url_prefix": "https://gitlab.lumenops.example/fir-gb-lumen", "value": sample("glpat-")}
    written = client.put(
        "/v1/secrets/merge",
        json={**forge, "projects": [PROJECT], "expires_at": "2099-01-31T00:00:00Z"},
        headers=hub["owner"],
    )
    assert written.status_code == 200, written.text
    for body in ({"pools": ["ci"]}, {"workers": ["desk"]}):
        response = client.patch("/v1/secrets/merge", json=body, headers=hub["owner"])
        assert response.status_code == 422 and "binds to no worker or pool" in response.text, response.text
    assert bindings_of(hub_db, "merge") == [(PROJECT, None, None)]
    assert audit_rows(hub_db, "secret.rebind") == []
