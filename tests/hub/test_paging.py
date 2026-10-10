"""Optional paging of the five lists that answer a bare JSON array (step 8 of the structure-guardrails plan).

GET /v1/projects/{project}/plans, /v1/skills, /v1/workers, /v1/projects and /v1/admin/users take ``limit`` (at most
1,000) and ``offset``. The checks: without either the answer is the whole list, as before; with them it is that slice
of the whole list in its order, and pages laid end to end give the whole list back; X-Total-Count is the length of
the whole list either way, also past its end; the label rule filters plans before the page is cut, so a member sees
pages and a count of what it may read only; out-of-range values are 422; and the OpenAPI document declares both
parameters and the header. ``Page`` and the document run without Postgres; the routes skip without EVO_HUB_TEST_DSN.
"""

import pytest

from tests.hub import live
from tests.hub.test_plans import PROJECT, draft, needs_pg, plan_url, put, setup_project

pytest.importorskip("fastapi")

from evo_agents.hub.server.paging import MAX_LIMIT, TOTAL_COUNT, WHOLE, Page  # noqa: E402

PAGED_ROUTES = ["/v1/projects/{project}/plans", "/v1/skills", "/v1/workers", "/v1/projects", "/v1/admin/users"]
HOST = {"hostname": "box.local", "os": "linux", "arch": "x86_64", "agent_version": "0.3.0"}


@pytest.fixture
def client(hub_db, tmp_path, github):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app

    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github))) as client:
        yield client


def fetch(client, path: str, headers: dict, **params) -> tuple[list, int]:
    """The list a GET answers and its X-Total-Count."""
    response = client.get(path, params=params, headers=headers)
    assert response.status_code == 200, response.text
    assert isinstance(response.json(), list)
    return response.json(), int(response.headers[TOTAL_COUNT])


def check_paging(client, path: str, headers: dict, **params) -> list:
    """Every page of the list at ``path`` is its slice of the whole list, with the whole list's length in
    X-Total-Count; the whole list, which it returns, is what the route answers without paging."""
    whole, total = fetch(client, path, headers, **params)
    assert total == len(whole) >= 3, path
    for limit, offset in ((1, 0), (2, 1), (MAX_LIMIT, 0), (len(whole), 1), (1, len(whole) - 1)):
        page, count = fetch(client, path, headers, **params, limit=limit, offset=offset)
        assert (page, count) == (whole[offset : offset + limit], total), (path, limit, offset)
    assert fetch(client, path, headers, **params, offset=2) == (whole[2:], total)  # an offset alone: the rest
    assert fetch(client, path, headers, **params, offset=len(whole)) == ([], total)  # past the end: empty, counted
    laid_end_to_end = []
    for offset in range(0, total, 2):
        laid_end_to_end += fetch(client, path, headers, **params, limit=2, offset=offset)[0]
    assert laid_end_to_end == whole, path
    for refused in ({"limit": 0}, {"limit": MAX_LIMIT + 1}, {"offset": -1}, {"limit": "many"}):
        response = client.get(path, params={**params, **refused}, headers=headers)
        assert response.status_code == 422, (path, refused, response.text)
    return whole


# Without Postgres


def test_a_page_of_a_list_is_its_slice_and_the_whole_list_is_the_default():
    items = list(range(7))
    assert WHOLE.whole and WHOLE.of(items) == items  # no response to carry the count: a handler called as a function
    assert Page(limit=3).of(items) == [0, 1, 2]
    assert Page(limit=3, offset=5).of(items) == [5, 6]
    assert Page(offset=6).of(items) == [6]
    assert Page(limit=2, offset=7).of(items) == []
    assert not Page(offset=1).whole and not Page(limit=MAX_LIMIT).whole


def test_a_page_sets_x_total_count_to_the_length_of_the_whole_list():
    from fastapi import Response

    response = Response()
    assert Page(limit=2, offset=1, response=response).of(list("abcde")) == ["b", "c"]
    assert response.headers[TOTAL_COUNT] == "5"


def test_the_openapi_document_declares_limit_offset_and_x_total_count_on_the_five_lists():
    from evo_agents.hub.openapi import document

    paths = document()["paths"]
    for path in PAGED_ROUTES:
        get = paths[path]["get"]
        params = {p["name"]: p for p in get["parameters"] if p["in"] == "query"}
        limit = next(s for s in params["limit"]["schema"]["anyOf"] if s["type"] == "integer")
        assert (limit["minimum"], limit["maximum"], params["limit"]["required"]) == (1, MAX_LIMIT, False), path
        assert (params["offset"]["schema"]["minimum"], params["offset"]["required"]) == (0, False), path
        ok = get["responses"]["200"]
        assert ok["headers"][TOTAL_COUNT]["schema"]["type"] == "integer", path
        assert ok["content"]["application/json"]["schema"]["type"] == "array", path  # still a bare list


# The five lists


@needs_pg
def test_plans_paging_cuts_the_page_after_the_label_rule(client, github):
    hubs = setup_project(client, github)
    plans = {"a": "internal", "b": "public", "c": "internal", "d": "public", "e": "public", "f": "internal"}
    for plan_id, level in plans.items():
        assert put(hubs["alice"], draft(plan_id), label={"level": level}).status_code == 200

    whole = check_paging(client, plan_url(), hubs["reader"].headers)
    assert [p["plan_id"] for p in whole] == list(plans)

    public = hubs["public-reader"].headers
    assert fetch(client, plan_url(), public, limit=2) == (fetch(client, plan_url(), public)[0][:2], 3)
    assert [p["plan_id"] for p in fetch(client, plan_url(), public, limit=2)[0]] == ["b", "d"]
    assert [p["plan_id"] for p in fetch(client, plan_url(), public, limit=2, offset=2)[0]] == ["e"]
    assert fetch(client, plan_url(), public, offset=3) == ([], 3)
    nothing_through = {**hubs["reader"].headers, "X-Evo-Sink": "no-such-sink"}
    assert fetch(client, plan_url(), nothing_through, limit=5) == ([], 0)


@needs_pg
def test_plans_paging_follows_the_area(client, github):
    hubs = setup_project(client, github)
    for plan_id, area in (("p1", "active"), ("p2", "completed"), ("p3", "active"), ("p4", "active")):
        assert put(hubs["alice"], draft(plan_id), area=area).status_code == 200
    active = check_paging(client, plan_url(), hubs["reader"].headers, area="active")
    assert [p["plan_id"] for p in active] == ["p1", "p3", "p4"]
    assert fetch(client, plan_url(), hubs["reader"].headers, area="completed", limit=1)[1] == 1


@needs_pg
def test_skills_paging_counts_the_skills_the_caller_sees(client, github, hub_db):
    from sqlalchemy import insert, select

    from evo_agents.hub import tables

    hubs = setup_project(client, github)
    skills, versions = tables.skills, tables.skill_versions
    with live.engine(hub_db).begin() as conn:
        admin = conn.execute(select(tables.users.c.id).where(tables.users.c.login == live.ADMIN)).scalar_one()
        project = conn.execute(select(tables.projects.c.id).where(tables.projects.c.name == PROJECT)).scalar_one()
        for scope, name in (("global", "alpha"), ("project", "beta"), ("global", "gamma"), ("project", "delta")):
            values = {"scope": scope, "project_id": project if scope == "project" else None, "name": name}
            skill_id = conn.execute(insert(skills).values(**values, created_by=admin).returning(skills.c.id)).scalar()
            for number in (1, 2):
                digest = f"{number:064x}"
                version = {"skill_id": skill_id, "version": number, "name": name, "sha256": digest, "size": number}
                conn.execute(insert(versions).values(**version, r2_key=f"blobs/sha256/{digest}", published_by=admin))

    whole = check_paging(client, "/v1/skills", hubs["reader"].headers)
    assert [(s["scope"], s["name"], s["version"]) for s in whole] == [
        ("global", "alpha", 2),
        ("global", "gamma", 2),
        ("project", "beta", 2),
        ("project", "delta", 2),
    ]
    stranger = hubs["stranger"].headers  # no grant: the global skills alone, and a count of those
    assert fetch(client, "/v1/skills", stranger, limit=1) == ([whole[0]], 2)
    assert fetch(client, "/v1/skills", hubs["reader"].headers, scope="project", limit=1, offset=1) == ([whole[3]], 2)


@needs_pg
def test_workers_paging_counts_live_workers_unless_revoked_ones_are_asked_for(client, github):
    hubs = setup_project(client, github)
    alice = hubs["alice"].headers
    ids = []
    for number in range(5):
        body = {"name": f"box-{number}", "projects": [PROJECT], **HOST}
        response = client.post("/v1/workers", json=body, headers=alice)
        assert response.status_code == 201, response.text
        ids.append(response.json()["worker"]["id"])
    assert client.post(f"/v1/workers/{ids[1]}/revoke", headers=alice).status_code == 200

    live_ones = check_paging(client, "/v1/workers", alice)
    assert [w["id"] for w in live_ones] == [ids[4], ids[3], ids[2], ids[0]]  # newest first
    every = check_paging(client, "/v1/workers", alice, revoked="true")
    assert [w["id"] for w in every] == ids[::-1]
    assert fetch(client, "/v1/workers", hubs["bob"].headers, limit=10) == ([], 0)  # bob owns none


@needs_pg
def test_projects_paging_counts_the_projects_the_caller_holds_a_grant_on(client, github, hub_db):
    hubs = setup_project(client, github)
    admin = hubs["admin"].headers
    for name in ("alpha", "omega", "kappa"):
        live.add_project(hub_db, name)
    grant = {"role": "reader", "max_level": "internal"}
    response = client.put("/v1/admin/projects/omega/grants/reader", json=grant, headers=admin)
    assert response.status_code == 200, response.text

    every = check_paging(client, "/v1/projects", admin)  # a hub admin sees every project
    assert [p["name"] for p in every] == ["alpha", PROJECT, "kappa", "omega"]
    reader = hubs["reader"].headers
    assert [p["name"] for p in fetch(client, "/v1/projects", reader)[0]] == [PROJECT, "omega"]
    page, total = fetch(client, "/v1/projects", reader, limit=1, offset=1)
    assert ([p["name"] for p in page], total) == (["omega"], 2)
    assert page[0]["role"] == "reader" and page[0]["max_level"] == "internal"


@needs_pg
def test_admin_users_paging_keeps_each_users_grants(client, github):
    hubs = setup_project(client, github)
    admin = hubs["admin"].headers
    whole = check_paging(client, "/v1/admin/users", admin)
    assert [u["login"] for u in whole] == sorted((u["login"] for u in whole), key=str.lower)
    by_login = {u["login"]: u for u in whole}
    assert [g["project"] for g in by_login["alice"]["grants"]] == [PROJECT]
    for offset in range(len(whole)):
        (one,), _ = fetch(client, "/v1/admin/users", admin, limit=1, offset=offset)
        assert one == by_login[one["login"]]  # the grants of a user on a page are all of that user's grants
    assert client.get("/v1/admin/users", params={"limit": 1}, headers=hubs["alice"].headers).status_code == 403
