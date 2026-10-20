"""A member's emails on the hub (schema 0065, ``evo_agents.hub.server.user_emails``) and ``evo-agents hub email
list|add|remove``.

An address belongs to one member, whatever its case: another member adding it gets 409, the same member adding it
again gets the row back. A member manages their own addresses; a hub admin manages anyone's with ``user`` (``--user
LOGIN``); anyone else naming another member gets 403. ``login_of_email`` names the login of an address from the rows,
or from GitHub's noreply address without a row, and None for any other. Sign-in still asks GitHub for ``read:user``
alone. No audit row holds an address.

The function, the contract and the checks of an address run without Postgres; everything that needs the hub skips
without EVO_HUB_TEST_DSN."""

import asyncio
from types import SimpleNamespace

import pytest

from evo_agents.hub import client as hub_client
from evo_agents.hub.contract import commands
from evo_agents.hub.user_emails import MAX_EMAILS, email_problem, login_of_email, noreply_login
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import HUB_URL, as_json, ok, write_credentials
from tests.hub.test_secrets_cli import needs_pg, run_cli

KNOWN = {"octo@northwind.example": "octo", "ops@lumen.example": "lumen-dev"}
EMAIL_KEYS = ["id", "email", "login", "created_at"]


def email_cli(monkeypatch, capsys, home, *args: str) -> SimpleNamespace:
    return run_cli(monkeypatch, capsys, home, "hub", "email", *args)


# Without a hub


def test_a_listed_address_names_its_member_whatever_its_case():
    assert login_of_email("octo@northwind.example", KNOWN) == "octo"
    assert login_of_email("  Octo@NorthWind.Example ", KNOWN) == "octo"
    assert login_of_email("OPS@lumen.example", KNOWN) == "lumen-dev"


@pytest.mark.parametrize(
    ("email", "login"),
    [
        ("12345+octo@users.noreply.github.com", "octo"),
        ("octo@users.noreply.github.com", "octo"),
        ("987+Lumen-Dev@Users.NoReply.GitHub.com", "Lumen-Dev"),
        ("  4242+northwind-bot@users.noreply.github.com\n", "northwind-bot"),
    ],
)
def test_githubs_noreply_address_names_its_login_without_a_row(email, login):
    assert noreply_login(email) == login
    assert login_of_email(email, {}) == login


def test_a_listed_noreply_address_names_the_member_that_listed_it():
    renamed = {"12345+octo-old@users.noreply.github.com": "octo"}  # the noreply address of the login before a rename
    assert login_of_email("12345+Octo-Old@users.noreply.github.com", renamed) == "octo"


@pytest.mark.parametrize(
    "email",
    [
        "someone@northwind.example",
        "octo@noreply.github.com",
        "octo@users.noreply.github.com.lumen.example",
        "octo+12345@users.noreply.github.com",
        "49699333+dependabot[bot]@users.noreply.github.com",
        "@users.noreply.github.com",
        "not an address",
        "",
    ],
)
def test_any_other_address_names_nobody(email):
    assert noreply_login(email) is None
    assert login_of_email(email, KNOWN) is None


@pytest.mark.parametrize(
    ("email", "problem"),
    [
        ("octo", "is not an email address"),
        ("octo@northwind", "is not an email address"),
        ("oc to@northwind.example", "is not an email address"),
        ("a@b@northwind.example", "is not an email address"),
        ("x" * 250 + "@a.bc", "at most 254 characters"),
    ],
)
def test_an_address_the_hub_refuses(email, problem):
    assert problem in email_problem(email)


def test_an_address_the_hub_takes():
    for email in ("octo@northwind.example", "12345+octo@users.noreply.github.com", "o.c-t_o+tag@sub.lumen.example"):
        assert email_problem(email) is None


def test_the_email_commands_are_in_the_contract_with_the_keys_of_the_hubs_row():
    printed = commands()
    for command, kind in (("hub email list", "array"), ("hub email add", "object"), ("hub email remove", "object")):
        assert printed[command]["json"] == {"kind": kind, "keys": EMAIL_KEYS, "schema": "UserEmail"}
        assert [o["flags"] for o in printed[command]["options"]] == [["--user"], ["--json"]]
    assert [p["name"] for p in printed["hub email add"]["positionals"]] == ["email"]
    assert [p["name"] for p in printed["hub email remove"]["positionals"]] == ["email"]


@pytest.mark.parametrize("command", ["add", "remove"])
def test_add_and_remove_check_the_address_before_asking_the_hub(monkeypatch, capsys, tmp_path, command):
    home = write_credentials(tmp_path / "home", HUB_URL, "octo", "evh_unused")  # never reached: refused before
    result = email_cli(monkeypatch, capsys, home, command, "octo at northwind")
    assert result.code == 2 and result.out == ""
    assert (
        result.err
        == "error: 'octo at northwind' is not an email address: it takes one @, no space and a dot in its domain\n"
    )


def test_sign_in_still_asks_github_for_read_user_alone():
    pytest.importorskip("fastapi")
    from evo_agents.hub.server import web_auth

    assert web_auth.SCOPE == hub_client.DEVICE_SCOPE == "read:user"


# Against a hub

if pg.DSN:
    import httpx
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from evo_agents.hub import tables
    from evo_agents.hub.server.app import create_app
    from evo_agents.hub.server.user_emails import logins_of_emails
    from tests.hub.test_run_stream import serving

MEMBERS = {"octo": ("octo", 101), "other": ("lumen-dev", 102), "admin": (live.ADMIN, 7)}


@pytest.fixture
def hub(hub_db, tmp_path, github):
    """An in-process hub with the members octo and lumen-dev and the hub admin signed in, their headers by role."""
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github))) as client:
        headers = {
            who: live.bearer(live.sign_in(client, github, *account)["token"]) for who, account in MEMBERS.items()
        }
        yield SimpleNamespace(client=client, headers=headers, db=hub_db)


def call(hub, who: str, method: str, path: str, **kwargs):
    return hub.client.request(method, path, headers=hub.headers[who], **kwargs)


def add(hub, who: str, email: str, user: str | None = None):
    return call(hub, who, "POST", "/v1/emails", json={"email": email}, params={"user": user} if user else None)


def listed(hub, who: str, user: str | None = None) -> list[tuple[str, str]]:
    found = call(hub, who, "GET", "/v1/emails", params={"user": user} if user else None)
    assert found.status_code == 200, found.text
    return [(e["email"], e["login"]) for e in found.json()]


@needs_pg
def test_a_member_lists_adds_and_removes_their_own_emails(hub):
    assert listed(hub, "octo") == []
    first = add(hub, "octo", " Octo@Northwind.example ")
    assert first.status_code == 201
    row = first.json()
    assert list(row) == EMAIL_KEYS and (row["email"], row["login"]) == ("Octo@Northwind.example", "octo")
    again = add(hub, "octo", "octo@NORTHWIND.example")  # the same address in another case: the row it has
    assert again.status_code == 200 and again.json() == row
    assert add(hub, "octo", "octo@lumen.example").status_code == 201
    assert listed(hub, "octo") == [("octo@lumen.example", "octo"), ("Octo@Northwind.example", "octo")]

    removed = call(hub, "octo", "DELETE", f"/v1/emails/{row['id']}")
    assert removed.status_code == 200 and removed.json() == row
    assert listed(hub, "octo") == [("octo@lumen.example", "octo")]
    gone = call(hub, "octo", "DELETE", f"/v1/emails/{row['id']}")
    assert gone.status_code == 404 and gone.json()["error"] == "not_found"


@needs_pg
def test_an_address_another_member_holds_is_a_409_whatever_its_case(hub):
    assert add(hub, "octo", "octo@northwind.example").status_code == 201
    taken = add(hub, "other", "OCTO@northwind.example")
    assert taken.status_code == 409 and taken.json()["error"] == "conflict"
    assert "octo" not in taken.json()["message"].replace("OCTO@northwind.example", "")  # a member learns no login
    told = add(hub, "admin", "Octo@Northwind.Example", user="lumen-dev")
    assert told.status_code == 409 and "(octo's)" in told.json()["message"]  # an admin does
    assert listed(hub, "other") == []


@needs_pg
def test_another_members_emails_need_a_hub_admin(hub):
    mine = add(hub, "octo", "octo@northwind.example").json()
    for response in (
        call(hub, "other", "GET", "/v1/emails", params={"user": "octo"}),
        add(hub, "other", "lumen@lumen.example", user="octo"),
        call(hub, "other", "DELETE", f"/v1/emails/{mine['id']}"),
    ):
        assert response.status_code == 403 and response.json()["error"] == "forbidden"
    assert listed(hub, "octo", user="OCTO") == [("octo@northwind.example", "octo")]  # one's own login, any case

    assert listed(hub, "admin", user="Octo") == [("octo@northwind.example", "octo")]
    given = add(hub, "admin", "octo@lumen.example", user="octo")
    assert given.status_code == 201 and given.json()["login"] == "octo"
    assert call(hub, "admin", "DELETE", f"/v1/emails/{mine['id']}").status_code == 200
    assert listed(hub, "octo") == [("octo@lumen.example", "octo")]
    nobody = call(hub, "admin", "GET", "/v1/emails", params={"user": "northwind-nobody"})
    assert nobody.status_code == 404 and "no member northwind-nobody" in nobody.json()["message"]


@needs_pg
def test_the_hub_refuses_a_bad_address_another_logins_noreply_address_and_one_too_many(hub):
    bad = add(hub, "octo", "octo at northwind")
    assert bad.status_code == 422 and "is not an email address" in bad.json()["message"]
    theirs = add(hub, "octo", "102+lumen-dev@users.noreply.github.com")
    assert theirs.status_code == 422 and "noreply address of lumen-dev" in theirs.json()["message"]
    assert add(hub, "octo", "101+Octo@users.noreply.github.com").status_code == 201  # one's own adds nothing, harmless
    for n in range(1, MAX_EMAILS):
        assert add(hub, "octo", f"octo{n}@northwind.example").status_code == 201
    over = add(hub, "octo", "one-more@northwind.example")
    assert over.status_code == 422 and f"holds {MAX_EMAILS} emails" in over.json()["message"]


@needs_pg
def test_no_audit_row_holds_an_address(hub):
    added = add(hub, "admin", "octo@northwind.example", user="octo").json()
    assert call(hub, "octo", "DELETE", f"/v1/emails/{added['id']}").status_code == 200
    audit, users = tables.audit, tables.users
    trail = live.sql(
        hub.db,
        select(users.c.login, audit.c.action, audit.c.target)
        .join_from(audit, users, users.c.id == audit.c.actor_id)
        .where(audit.c.action.startswith("user_email."))
        .order_by(audit.c.id),
    )
    target = f"email:{added['id']} user=octo"
    assert trail == [(live.ADMIN, "user_email.add", target), ("octo", "user_email.remove", target)]


@needs_pg
def test_logins_of_emails_reads_the_rows_then_githubs_noreply_addresses(hub):
    assert add(hub, "octo", "Octo@Northwind.example").status_code == 201

    async def logins():
        engine = create_async_engine(hub.db.dsn.replace("postgresql://", "postgresql+psycopg://", 1))
        try:
            async with engine.connect() as conn:
                return await logins_of_emails(
                    conn,
                    ["octo@NORTHWIND.example", "77+lumen-dev@users.noreply.github.com", "ops@lumen.example"],
                )
        finally:
            await engine.dispose()

    assert asyncio.run(logins()) == {
        "octo@NORTHWIND.example": "octo",
        "77+lumen-dev@users.noreply.github.com": "lumen-dev",
        "ops@lumen.example": None,
    }


@pytest.fixture
def served(hub_db, tmp_path, github):
    """The hub under uvicorn, and a home signed in to it for octo and for the hub admin."""
    with serving(create_app(live.hub_config(hub_db, tmp_path, github))) as url, httpx.Client(base_url=url) as client:
        homes = {}
        for who in ("octo", "admin"):
            login, github_id = MEMBERS[who]
            token = live.sign_in(client, github, login, github_id)["token"]
            homes[who] = write_credentials(tmp_path / who, url, login, token)
        yield homes


@needs_pg
def test_the_email_commands_against_a_hub(served, monkeypatch, capsys):
    octo, admin = served["octo"], served["admin"]
    empty = ok(email_cli(monkeypatch, capsys, octo, "list"))
    assert empty.out == (
        "No email listed for you.\nGitHub's noreply addresses (@users.noreply.github.com) name their login and need "
        "no row.\n"
    )
    added = as_json(email_cli(monkeypatch, capsys, octo, "add", "Octo@Northwind.example", "--json"))
    assert_json_keys("hub email add", added)
    told = ok(email_cli(monkeypatch, capsys, octo, "add", "octo@lumen.example"))
    assert told.out.startswith("octo@lumen.example is an email of octo (id ")
    found = as_json(email_cli(monkeypatch, capsys, octo, "list", "--json"))
    assert_json_keys("hub email list", found)
    assert [e["email"] for e in found] == ["octo@lumen.example", "Octo@Northwind.example"]
    table = ok(email_cli(monkeypatch, capsys, octo, "list"))
    assert table.out.splitlines()[0].split() == ["ID", "EMAIL", "ADDED", "(UTC)"]

    refused = email_cli(monkeypatch, capsys, octo, "list", "--user", live.ADMIN)
    assert refused.code == 1 and refused.err.startswith("error: only a hub admin")
    removed = as_json(
        email_cli(monkeypatch, capsys, admin, "remove", "OCTO@northwind.example", "--user", "octo", "--json")
    )
    assert_json_keys("hub email remove", removed)
    assert removed == added
    missing = email_cli(monkeypatch, capsys, octo, "remove", "octo@northwind.example")
    assert missing.code == 1
    assert missing.err == "error: octo@northwind.example is not an email of you: see `evo-agents hub email list`\n"
    again = ok(email_cli(monkeypatch, capsys, octo, "remove", "octo@lumen.example"))
    assert again.out == "Removed octo@lumen.example from the emails of octo.\n"
