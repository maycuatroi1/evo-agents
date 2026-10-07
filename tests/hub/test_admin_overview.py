"""GET /v1/admin/overview: what may need a hub admin, in one read.

Hub admins only: a member gets 403 and a request without a credential 401. The counts are checked against rows put
straight into the database around each window's edge: members seen within 30 days and those granted access before
signing in; live tokens, those expiring within 14 days and those unused for 90 days, which GET /v1/admin/tokens lists
under state=expiring and state=unused; every project's grants by role, a project without any included; blobs counted
by object, the same bytes held by two projects once, and the deletions still pending; the graph builds that failed
within 7 days by project, with each project's newest build; workers offline or never heard from, revoked ones left
out; and the audit rows of the last 24 hours."""

from datetime import datetime, timedelta

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from evo_agents.hub.server.app import create_app
from tests.hub.live import ADMIN, bearer, sql

OVERVIEW = "/v1/admin/overview"
DAY = timedelta(days=1)
HEX = "0123456789abcdef" * 4
TOKEN_COLUMNS = "user_id, kind, token_hash, host, created_at, last_used_at, expires_at, revoked_at"


def sha(n: int) -> str:
    return f"{n:064x}"


@pytest.fixture
def client(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def admin(client, hub_db) -> dict:
    """The hub admin's machine token; the admin signed in through GitHub and was seen just now."""
    headers = bearer(live.insert_token(hub_db, ADMIN))
    sql(hub_db, "UPDATE users SET github_id = 1, last_seen_at = now() WHERE login = %s", (ADMIN,))
    return headers


def seed(db: pg.Database) -> dict:
    """Rows on each side of every window, relative to the database's now(); the ids the token lists must return."""
    tokens = {}
    with pg.admin(db.admin_dsn) as conn:

        def one(statement: str, params=()):
            return conn.execute(statement, params).fetchone()[0]

        admin_id = one("SELECT id FROM users WHERE login = %s", (ADMIN,))
        users = {}
        # (login, GitHub id or None for a login granted access before signing in, last seen this long ago)
        for login, github_id, seen in (
            ("ann", 11, 2 * DAY),
            ("bob", 12, 40 * DAY),
            ("cyd", None, None),
            ("eve", 14, 29 * DAY),
        ):
            users[login] = one(
                "INSERT INTO users (login, github_id, last_seen_at) VALUES (%s, %s, now() - %s::interval) RETURNING id",
                (login, github_id, seen),
            )

        # (name, owner, kind, issued, last used, expires (after now; negative: before), revoked) as times ago
        for name, login, kind, issued, used, expires, revoked in (
            ("idle-web", "ann", "web", 80 * DAY, 80 * DAY, 10 * DAY, None),  # expiring
            ("edge-expiring", "bob", "machine", 100 * DAY, 77 * DAY, 13 * DAY, None),  # expiring
            ("not-yet", "bob", "machine", 60 * DAY, 60 * DAY, 30 * DAY, None),  # live only
            ("last-day", "bob", "machine", 100 * DAY, 89 * DAY, 1 * DAY, None),  # expiring, used 89 days ago
            ("lapsed", "bob", "machine", 200 * DAY, 120 * DAY, -30 * DAY, None),  # unused
            ("never-used", "eve", "machine", 100 * DAY, None, -10 * DAY, None),  # unused, issued 100 days ago
            ("revoked", "eve", "web", 95 * DAY, 95 * DAY, -5 * DAY, 6 * DAY),  # nothing: revoked
            ("idle-new", "eve", "machine", 50 * DAY, None, 40 * DAY, None),  # live only, issued 50 days ago
        ):
            tokens[name] = one(
                f"INSERT INTO tokens ({TOKEN_COLUMNS}) "
                "VALUES (%s, %s, %s, %s, now() - %s::interval, now() - %s::interval, now() + %s::interval, "
                "now() - %s::interval) RETURNING id",
                (
                    users[login],
                    kind,
                    sha(len(tokens) + 1),
                    "laptop" if kind == "machine" else None,
                    issued,
                    used,
                    expires,
                    revoked,
                ),
            )

        # Workers of ann: (name, last heartbeat this long ago, drained, revoked); each with its worker token.
        minute = timedelta(minutes=1)
        for index, (name, beat, drained, revoked) in enumerate(
            (
                ("online", timedelta(seconds=30), False, False),
                ("quiet", 10 * minute, False, False),  # offline
                ("never-beat", None, False, False),  # offline
                ("drained-down", 20 * minute, True, False),  # offline wins over draining
                ("gone", None, False, True),  # revoked: not counted
            )
        ):
            token = one(
                f"INSERT INTO tokens ({TOKEN_COLUMNS}) "
                "VALUES (%s, 'worker', %s, 'mini', now() - interval '1 day', now(), now() + interval '90 days', "
                "CASE WHEN %s THEN now() END) RETURNING id",
                (users["ann"], sha(100 + index), revoked),
            )
            conn.execute(
                "INSERT INTO workers (owner_id, token_id, name, hostname, os, arch, agent_version, last_heartbeat_at, "
                "drained_at, revoked_at) VALUES (%s, %s, %s, 'mini', 'darwin', 'arm64', '0.5.0', "
                "now() - %s::interval, CASE WHEN %s THEN now() - interval '1 hour' END, CASE WHEN %s THEN now() END)",
                (users["ann"], token, name, beat, drained, revoked),
            )

        projects = {}
        for name in ("alpha", "beta", "gamma"):
            projects[name] = one(
                "INSERT INTO projects (name, levels, locations, default_label, created_by) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (name, live.LEVELS, ["any"], Jsonb({"level": "public"}), admin_id),
            )
        for project, login, role in (
            ("alpha", "ann", "admin"),
            ("alpha", "bob", "writer"),
            ("alpha", "eve", "writer"),
            ("alpha", "cyd", "reader"),
            ("gamma", "eve", "reader"),
        ):
            conn.execute(
                "INSERT INTO grants (user_id, project_id, role, max_level, granted_by) "
                "VALUES (%s, %s, %s, 'public', %s)",
                (users[login], projects[project], role, admin_id),
            )

        # The same bytes in alpha and beta are one object.
        for project, digest, size in (
            ("alpha", sha(1), 100),
            ("beta", sha(1), 100),
            ("alpha", sha(2), 50),
            ("gamma", sha(3), 0),
        ):
            conn.execute(
                "INSERT INTO blobs (project_id, sha256, size, kind, created_by) VALUES (%s, %s, %s, 'source', %s)",
                (projects[project], digest, size, admin_id),
            )
        for digest, size, deleted in ((sha(4), 30, False), (sha(5), 70, True), (sha(6), 5, False)):
            conn.execute(
                "INSERT INTO blob_deletions (sha256, size, kind, requested_at, deleted_at) "
                "VALUES (%s, %s, 'kg-graph', now() - interval '2 hours', CASE WHEN %s THEN now() END)",
                (digest, size, deleted),
            )

        # Builds in id order, each project's newest last: (project, status, finished this long ago)
        builds = {}
        for key, project, status, finished in (
            ("alpha-old", "alpha", "failed", 10 * DAY),  # outside the window
            ("alpha-failed", "alpha", "failed", 2 * DAY),
            ("alpha-fixed", "alpha", "succeeded", 1 * DAY),
            ("beta-first", "beta", "failed", 3 * DAY),
            ("beta-again", "beta", "failed", timedelta(hours=1)),
            ("gamma-failed", "gamma", "failed", 6 * DAY),
            ("gamma-running", "gamma", "running", None),
        ):
            done = status in ("succeeded", "failed")
            builds[key] = one(
                "INSERT INTO kg_builds (project_id, status, queued_at, started_at, finished_at, error, "
                "artifact_sha256, artifact_size, content_hash, nodes, edges) VALUES (%(project)s, %(status)s, "
                "now() - %(started)s::interval, now() - %(started)s::interval, now() - %(finished)s::interval, "
                "%(error)s, %(artifact)s, %(size)s, %(hash)s, %(nodes)s, %(edges)s) RETURNING id",
                {
                    "project": projects[project],
                    "status": status,
                    "started": (finished or timedelta()) + timedelta(minutes=2),
                    "finished": finished if done else None,
                    "error": "extractor crashed" if status == "failed" else None,
                    "artifact": HEX if status == "succeeded" else None,
                    "size": 10 if status == "succeeded" else None,
                    "hash": f"sha256:{HEX}" if status == "succeeded" else None,
                    "nodes": 3 if status == "succeeded" else None,
                    "edges": 2 if status == "succeeded" else None,
                },
            )

        for ago in (timedelta(hours=1), timedelta(hours=1), timedelta(hours=23), timedelta(hours=25), 3 * DAY):
            conn.execute(
                "INSERT INTO audit (at, actor_id, action, target) VALUES (now() - %s::interval, %s, 'grant.put', 'x')",
                (ago, admin_id),
            )
    return {"tokens": tokens, "builds": builds}


def failed_at(db: pg.Database, build_id: int):
    return sql(db, "SELECT finished_at FROM kg_builds WHERE id = %s", (build_id,))[0][0]


def test_only_a_hub_admin_reads_the_overview(client, hub_db, admin):
    member = bearer(live.insert_token(hub_db, "member"))
    refused = client.get(OVERVIEW, headers=member)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"] == "forbidden" and "EVO_HUB_ADMINS" in refused.json()["message"]
    assert client.get(OVERVIEW).status_code == 401
    assert client.get(OVERVIEW, headers=admin).status_code == 200


def test_the_counts_match_the_rows_on_each_side_of_every_window(client, hub_db, admin):
    seeded = seed(hub_db)
    builds = seeded["builds"]
    response = client.get(OVERVIEW, headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    for item in body["kg_builds"]["projects"]:
        item["last_failed_at"] = datetime.fromisoformat(item["last_failed_at"])
    assert body == {
        # octo-admin and ann, eve (29 days) seen lately; bob 40 days ago; cyd granted, never signed in
        "members": {"total": 5, "active": 3, "not_signed_in": 1, "active_days": 30},
        # live: the admin's, idle-web, edge-expiring, not-yet, last-day, idle-new and four workers' tokens
        "tokens": {"live": 10, "expiring": 3, "unused": 2, "expiring_days": 14, "unused_days": 90},
        "grants": [
            {"project": "alpha", "admins": 1, "writers": 2, "readers": 1},
            {"project": "beta", "admins": 0, "writers": 0, "readers": 0},
            {"project": "gamma", "admins": 0, "writers": 0, "readers": 1},
        ],
        "storage": {"objects": 3, "bytes": 150, "pending_deletions": 2, "pending_bytes": 35},
        "kg_builds": {
            "failed": 4,
            "days": 7,
            "projects": [
                {
                    "project": "beta",
                    "failed": 2,
                    "last_failed_id": builds["beta-again"],
                    "last_failed_at": failed_at(hub_db, builds["beta-again"]),
                    "latest_id": builds["beta-again"],
                    "latest_status": "failed",
                },
                {
                    "project": "alpha",
                    "failed": 1,
                    "last_failed_id": builds["alpha-failed"],
                    "last_failed_at": failed_at(hub_db, builds["alpha-failed"]),
                    "latest_id": builds["alpha-fixed"],
                    "latest_status": "succeeded",
                },
                {
                    "project": "gamma",
                    "failed": 1,
                    "last_failed_id": builds["gamma-failed"],
                    "last_failed_at": failed_at(hub_db, builds["gamma-failed"]),
                    "latest_id": builds["gamma-running"],
                    "latest_status": "running",
                },
            ],
        },
        "workers": {"live": 4, "offline": 3, "offline_after_seconds": 300},
        "audit": {"rows": 3, "hours": 24},
    }


def test_the_token_lists_hold_what_the_overview_counts(client, hub_db, admin):
    tokens = seed(hub_db)["tokens"]
    counts = client.get(OVERVIEW, headers=admin).json()["tokens"]
    expected = {
        "expiring": {tokens["idle-web"], tokens["edge-expiring"], tokens["last-day"]},
        "unused": {tokens["lapsed"], tokens["never-used"]},
    }
    for state, ids in expected.items():
        response = client.get("/v1/admin/tokens", params={"state": state, "limit": 200}, headers=admin)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert {item["id"] for item in items} == ids, state
        assert len(items) == counts[state]
    # An expiring token is still live; an unused one, with tokens expiring after 90 days without use, has expired.
    listed = client.get("/v1/admin/tokens", params={"state": "expiring"}, headers=admin).json()["items"]
    assert {item["state"] for item in listed} == {"active"}
    listed = client.get("/v1/admin/tokens", params={"state": "unused"}, headers=admin).json()["items"]
    assert {item["state"] for item in listed} == {"expired"}
    bad = client.get("/v1/admin/tokens", params={"state": "stale"}, headers=admin)
    assert bad.status_code == 422


def test_a_new_hub_has_nothing_to_report(client, admin):
    body = client.get(OVERVIEW, headers=admin).json()
    assert body == {
        "members": {"total": 1, "active": 1, "not_signed_in": 0, "active_days": 30},
        "tokens": {"live": 1, "expiring": 0, "unused": 0, "expiring_days": 14, "unused_days": 90},
        "grants": [],
        "storage": {"objects": 0, "bytes": 0, "pending_deletions": 0, "pending_bytes": 0},
        "kg_builds": {"failed": 0, "days": 7, "projects": []},
        "workers": {"live": 0, "offline": 0, "offline_after_seconds": 300},
        "audit": {"rows": 0, "hours": 24},
    }
