"""Shares of secrets and accounts (``evo_agents.hub.server.credential_shares``), as criteria a1 and a2 of plan
credential-sharing name them: schema 0100 and its constraints; only the owner shares, the owner or a hub admin
revokes; a forge secret, a project the secret is not bound to, a missing member and a unit that is not live are
refused; a personal subscription token is shared only with the acknowledgement of TERMS_WARNING, which the 409 and the
audit row share.terms_ack hold word for word; GET /v1/secrets and GET /v1/accounts carry the live shares; no answer,
audit row or table holds a value.

Everything here needs the hub and skips without EVO_HUB_TEST_DSN."""

import secrets
from datetime import UTC, datetime, timedelta

import pytest

from evo_agents.hub.credential_shares import TERMS_WARNING, credential_class, needs_terms_ack, terms_refusal
from tests.hub import pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

if pg.DSN:
    from sqlalchemy import insert, select, update

    from evo_agents.hub import tables
    from evo_agents.hub.migrate import migrate
    from tests.hub.live import sql
    from tests.hub.test_migrate import ALL, INFO_TABLES, move_to
    from tests.hub.test_secrets_api import (  # noqa: F401  (fixtures)
        ADMIN,
        OTHER,
        OWNER,
        client,
        config,
        env_secret,
        git_secret,
        hub,
        put,
        user_id,
        written,
    )

SHARE_FIELDS = {
    "id",
    "secret",
    "credential",
    "grantee_kind",
    "grantee",
    "projects",
    "level",
    "budget_usd",
    "usage_ceiling_pct",
    "max_running",
    "expires_at",
    "terms_ack_at",
    "created_by",
    "created_at",
    "revoked_at",
    "revoked_by",
}


def sample(prefix: str = "sk-share-") -> str:
    return prefix + secrets.token_hex(16)


def share(client, headers, name: str, body: dict, status: int = 201) -> dict:
    response = client.post(f"/v1/secrets/{name}/shares", json=body, headers=headers)
    assert response.status_code == status, response.text
    if status == 201:
        assert set(response.json()) == SHARE_FIELDS
        assert response.headers["cache-control"] == "no-store"
    return response.json()


def revoke(client, headers, share_id: int, status: int = 200) -> dict:
    response = client.delete(f"/v1/shares/{share_id}", headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def share_audit(db) -> list[tuple[str, str]]:
    a = tables.audit
    return sql(db, select(a.c.action, a.c.target).where(a.c.action.like("share.%")).order_by(a.c.id))


def to_user(login: str, *projects: str, **extra) -> dict:
    return {"grantee_kind": "user", "login": login, "projects": list(projects or ("demo",)), **extra}


# Without the hub


def test_credential_share_terms_classify_a_claude_token_as_a_subscription_and_need_the_acknowledgement():
    assert credential_class("env", "CLAUDE_CODE_OAUTH_TOKEN") == "subscription"
    assert credential_class("env", "ANTHROPIC_API_KEY") == "api_key"
    assert credential_class("env", "OPENAI_API_KEY") == "api_key"
    assert credential_class("git", None) == "git"
    assert credential_class("env", "DATABASE_URL") == "env"
    assert [needs_terms_ack(c) for c in ("subscription", "api_key", "git", "env")] == [True, False, False, False]
    assert terms_refusal().startswith(TERMS_WARNING + " ")


# Schema 0100 (criterion a1)


@needs_pg
def test_credential_share_model_migration_adds_the_tables_and_going_back_drops_them(hub_db):
    public = select(INFO_TABLES.c.table_name).where(INFO_TABLES.c.table_schema == "public")
    assert pg.SHARE_TABLES <= {row[0] for row in sql(hub_db, public)}
    move_to(hub_db, "0099", down=True)
    assert not {row[0] for row in sql(hub_db, public)} & pg.SHARE_TABLES
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0100") :]
    assert pg.SHARE_TABLES <= {row[0] for row in sql(hub_db, public)}


@needs_pg
def test_credential_share_model_constraints_hold_in_the_database(client, hub, hub_db):
    written(client, hub["owner"], "notes-key", env_secret(sample(), env_var="NOTES_KEY"))
    secret = sql(hub_db, select(tables.secrets.c.id).where(tables.secrets.c.name == "notes-key"))[0][0]
    owner = user_id(hub_db, OWNER)
    base = {"secret_id": secret, "created_by": owner, "grantee_kind": "all"}
    cs = tables.credential_shares
    for bad in (
        {"grantee_kind": "team"},
        {"grantee_kind": "user"},  # a user share names its user
        {"grantee_user_id": owner},  # all names none
        {"level": "A6"},
        {"budget_usd": 0},
        {"usage_ceiling_pct": 101},
        {"max_running": 0},
        {"revoked_at": datetime.now(UTC)},  # revoked_at goes with revoked_by
    ):
        with pytest.raises(Exception, match="credential_shares_check"):
            sql(hub_db, insert(cs).values({**base, **bad}))
    sql(hub_db, insert(cs).values(base))


# Who shares and revokes (criterion a1)


@needs_pg
def test_credential_share_access_owner_shares_and_revokes_and_the_secret_lists_its_live_shares(client, hub, hub_db):
    value = sample()
    written(client, hub["owner"], "notes-key", env_secret(value, env_var="NOTES_KEY", projects=("demo", "docs")))
    ends = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)
    limits = {"level": "A2", "budget_usd": 12.5, "usage_ceiling_pct": 80, "max_running": 2}
    made = share(
        client, hub["owner"], "notes-key", to_user(OTHER, "demo", "docs", expires_at=ends.isoformat(), **limits)
    )
    assert (made["secret"], made["credential"], made["grantee_kind"], made["grantee"]) == (
        "notes-key",
        "env",
        "user",
        OTHER,
    )
    assert made["projects"] == ["demo", "docs"] and made["created_by"] == OWNER and made["terms_ack_at"] is None
    assert {key: made[key] for key in limits} == limits
    assert datetime.fromisoformat(made["expires_at"]) == ends
    everyone = share(client, hub["owner"], "notes-key", {"grantee_kind": "all", "projects": ["demo"]})
    [secret] = client.get("/v1/secrets", headers=hub["owner"]).json()
    assert [s["id"] for s in secret["shares"]] == [everyone["id"], made["id"]]
    [account] = client.get("/v1/accounts", headers=hub["owner"]).json()
    assert [s["id"] for s in account["shares"]] == [everyone["id"], made["id"]]
    # nobody else sees, shares or revokes it; a hub admin revokes any share
    for who in ("other", "admin"):
        assert client.get("/v1/secrets/notes-key/shares", headers=hub[who]).status_code == 404
        share(client, hub[who], "notes-key", {"grantee_kind": "all", "projects": ["demo"]}, status=404)
    revoke(client, hub["other"], made["id"], status=404)
    revoked = revoke(client, hub["owner"], made["id"])
    assert revoked["revoked_by"] == OWNER and revoked["revoked_at"] is not None
    revoke(client, hub["owner"], made["id"], status=409)
    assert revoke(client, hub["admin"], everyone["id"])["revoked_by"] == ADMIN
    assert client.get("/v1/secrets", headers=hub["owner"]).json()[0]["shares"] == []
    listed = client.get("/v1/secrets/notes-key/shares", headers=hub["owner"]).json()
    assert [s["id"] for s in listed] == [everyone["id"], made["id"]], "the list keeps revoked shares"
    audit = share_audit(hub_db)
    assert [action for action, _ in audit] == ["share.create", "share.create", "share.revoke", "share.revoke"]
    assert audit[0][1].startswith(f"share:{made['id']} secret:") and f"grantee=user:{OTHER}" in audit[0][1]
    assert audit[2][1].endswith("by=owner") and audit[3][1].endswith("by=admin")
    assert all(value not in target and "notes-key" not in target for _, target in audit)


@needs_pg
def test_credential_share_access_refuses_what_cannot_be_shared(client, hub, hub_db):
    written(client, hub["owner"], "notes-key", env_secret(sample(), env_var="NOTES_KEY"))
    owner = hub["owner"]
    share(client, owner, "notes-key", to_user(OWNER), status=422)  # one's own login
    share(client, owner, "notes-key", to_user("nobody-here"), status=422)
    share(client, owner, "notes-key", to_user(OTHER, "docs"), status=422)  # not bound to docs
    share(client, owner, "notes-key", {"grantee_kind": "user", "unit": "root", "projects": ["demo"]}, status=422)
    share(client, owner, "notes-key", {"grantee_kind": "all", "login": OTHER, "projects": ["demo"]}, status=422)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    share(client, owner, "notes-key", to_user(OTHER, expires_at=past), status=422)
    share(client, owner, "no-such-secret", to_user(OTHER), status=404)
    share(client, owner, "notes-key", to_user(OTHER))
    share(client, owner, "notes-key", to_user(OTHER), status=409)  # a live share to them already
    # a forge secret is never shared
    grant = {"role": "admin", "max_level": "internal"}
    response = client.put(f"/v1/admin/projects/docs/grants/{OWNER}", json=grant, headers=hub["admin"])
    assert response.status_code == 200, response.text
    forge = {
        "kind": "forge",
        "url_prefix": "https://gitlab.example.org/ops",
        "projects": ["docs"],
        "expires_at": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
        "value": sample("glpat-"),
    }
    written(client, owner, "forge-ops", forge)
    refused = share(client, owner, "forge-ops", {"grantee_kind": "all", "projects": ["docs"]}, status=422)
    assert "forge" in refused["message"]
    assert [action for action, _ in share_audit(hub_db)] == ["share.create"]


@needs_pg
def test_credential_share_access_a_unit_grantee_is_a_live_unit(client, hub, hub_db):
    written(client, hub["owner"], "notes-key", env_secret(sample(), env_var="NOTES_KEY"))
    for slug, kind in (("lab", "team"), ("old-lab", "team"), ("intern-2025", "cohort")):
        body = {"slug": slug, "parent": "root", "kind": kind, "name": slug}
        if kind == "cohort":
            body["ends_on"] = "2099-12-31"
        response = client.post("/v1/org/units", json=body, headers=hub["admin"])
        assert response.status_code == 201, response.text
    u = tables.org_units
    sql(hub_db, update(u).where(u.c.slug == "old-lab").values(archived_at=datetime.now(UTC)))
    sql(hub_db, update(u).where(u.c.slug == "intern-2025").values(ends_on=datetime(2025, 12, 31).date()))
    to_unit = {"grantee_kind": "unit", "projects": ["demo"]}
    for slug in ("old-lab", "intern-2025", "no-such-unit"):
        share(client, hub["owner"], "notes-key", {**to_unit, "unit": slug}, status=422)
    made = share(client, hub["owner"], "notes-key", {**to_unit, "unit": "lab"})
    assert (made["grantee_kind"], made["grantee"]) == ("unit", "lab")


@needs_pg
def test_credential_share_access_deleting_the_secret_revokes_its_shares(client, hub, hub_db):
    written(client, hub["owner"], "notes-key", env_secret(sample(), env_var="NOTES_KEY"))
    made = share(client, hub["owner"], "notes-key", to_user(OTHER))
    assert client.delete("/v1/secrets/notes-key", headers=hub["owner"]).status_code in (200, 204)
    cs = tables.credential_shares
    assert sql(hub_db, select(cs.c.revoked_at.is_not(None)).where(cs.c.id == made["id"])) == [(True,)]


# The terms of a subscription token (criterion a2)


@needs_pg
def test_credential_share_terms_a_subscription_token_needs_the_acknowledgement(client, hub, hub_db):
    token = sample("oauth-")
    written(client, hub["owner"], "claude-a", env_secret(token))
    refused = share(client, hub["owner"], "claude-a", to_user(OTHER), status=409)
    assert TERMS_WARNING in refused["message"] and refused["message"] == terms_refusal()
    assert share_audit(hub_db) == []
    made = share(client, hub["owner"], "claude-a", to_user(OTHER, terms_ack=True))
    assert made["credential"] == "subscription" and made["terms_ack_at"] is not None
    audit = share_audit(hub_db)
    assert [action for action, _ in audit] == ["share.create", "share.terms_ack"]
    assert audit[1][1].endswith(f"warning={TERMS_WARNING}")
    assert "credential=subscription" in audit[0][1]
    assert all(token not in target for _, target in audit)
    # an API key needs none
    written(client, hub["owner"], "api-a", env_secret(sample("sk-ant-"), env_var="ANTHROPIC_API_KEY"))
    plain = share(client, hub["owner"], "api-a", to_user(OTHER))
    assert (plain["credential"], plain["terms_ack_at"]) == ("api_key", None)


@needs_pg
def test_credential_share_terms_no_table_or_answer_holds_the_value(client, hub, hub_db):
    token = sample("oauth-")
    written(client, hub["owner"], "claude-a", env_secret(token))
    made = share(client, hub["owner"], "claude-a", {"grantee_kind": "all", "projects": ["demo"], "terms_ack": True})
    assert token not in str(made)
    dumped = [str(row) for table in (tables.credential_shares, tables.audit) for row in sql(hub_db, select(table))]
    assert dumped and all(token not in row for row in dumped)
