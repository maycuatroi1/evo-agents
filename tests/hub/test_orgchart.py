"""The org chart of the hub (``evo_agents.hub.org``, ``evo_agents.hub.server.org_store``, ``.org_writes`` and
``.org``), as criteria a1, a2 and a3 of plan org-chart name them: three tables in schema 0099 with their constraints,
the root and a primary member seat for every user; the effective ceiling as a pure function; the rules of who changes
what, without escalation, each change audited as an ``org.`` action; writes at once that keep the chart whole.

The model and the effective ceiling run without Postgres; everything that needs the hub skips without
EVO_HUB_TEST_DSN."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from evo_agents.hub import org
from evo_agents.hub.org import Chart, Seat, Unit
from tests.hub import live, pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
TODAY = date(2026, 10, 22)
ENDED = datetime(2026, 10, 1, tzinfo=UTC)


def chart() -> Chart:
    """root (A5) > lab (team, A4) > intern-2026 (cohort, A1, ends 2026-12-31); root > ops (team, no ceiling)."""
    units = {
        1: Unit(1, None, "root", "Organization", "org", ceiling="A5"),
        2: Unit(2, 1, "lab", "Lab", "team", ceiling="A4"),
        3: Unit(3, 2, "intern-2026", "Interns 2026", "cohort", ends_on=date(2026, 12, 31), ceiling="A1"),
        4: Unit(4, 1, "ops", "Ops", "team"),
    }
    seats = {
        10: Seat(10, 2, "lead", user_id=100, primary=True),
        11: Seat(11, 3, "intern", user_id=101, primary=True, reports_to=10),
        12: Seat(12, 4, "member", user_id=102, primary=True, ceiling="A2"),
        13: Seat(13, 2, "agent", profile_id=500, primary=True, sponsor_id=100),
        14: Seat(14, 1, "member", user_id=101, primary=False),  # a secondary seat at the root decides nothing
    }
    return Chart(units, seats)


# The effective ceiling (criterion a2)


def test_orgchart_effective_is_the_lowest_ceiling_from_the_primary_seat_to_the_root_with_its_source():
    c = chart()
    assert org.effective(c, 100, TODAY) == org.Effective("A4", "unit:lab")
    assert org.effective(c, 101, TODAY) == org.Effective("A1", "unit:intern-2026")
    assert org.effective(c, 102, TODAY) == org.Effective("A2", "seat:12"), "a seat's own ceiling counts"
    assert org.unit_effective(c, 4) == org.Effective("A5", "unit:root"), "a unit without one leaves it to the root"
    assert org.profile_effective(c, 500, TODAY) == org.Effective("A4", "unit:lab")
    assert org.profile_effective(c, 501, TODAY) is None, "a profile without a seat acts for whoever runs it"


def test_orgchart_effective_of_a_primary_seat_that_ended_is_a0_from_the_seat_itself():
    c = chart()
    c.seats[12] = Seat(12, 4, "member", user_id=102, primary=True, ended_at=ENDED)
    assert org.effective(c, 102, TODAY) == org.Effective("A0", "seat:12")
    c.seats[12] = Seat(12, 4, "member", user_id=102, primary=True, ends_on=date(2026, 10, 21))
    assert org.effective(c, 102, TODAY) == org.Effective("A0", "seat:12"), "its end date passed"
    after = date(2027, 1, 1)
    assert org.effective(c, 101, after) == org.Effective("A0", "seat:11"), "the end of the cohort holding it passed"
    # a live primary seat decides over one that ended
    c.seats[15] = Seat(15, 2, "member", user_id=102, primary=True)
    assert org.effective(c, 102, TODAY) == org.Effective("A4", "unit:lab")


def test_orgchart_effective_of_a_person_without_a_seat_is_that_of_a_member_of_the_root():
    c = chart()
    assert org.effective(c, 999, TODAY) == org.Effective("A5", "unit:root")
    c.units[1] = Unit(1, None, "root", "Organization", "org", ceiling="A3")
    assert org.effective(c, 999, TODAY) == org.Effective("A3", "unit:root")


def test_orgchart_effective_names_the_nearest_of_two_equal_ceilings_and_a5_without_any():
    assert org.lowest([("seat:1", "A2"), ("unit:a", "A2"), ("unit:root", "A5")]) == org.Effective("A2", "seat:1")
    assert org.lowest([("seat:1", None), ("unit:a", None), ("unit:root", None)]) == org.Effective("A5", "unit:root")
    assert [org.rank(level) for level in org.LEVELS] == [0, 1, 2, 3, 4, 5]


def test_orgchart_model_ancestors_stop_at_a_cycle_and_the_root_is_one():
    c = chart()
    assert [unit.slug for unit in c.ancestors(3)] == ["intern-2026", "lab", "root"]
    assert c.is_below(3, 1) and c.within(2, 2) and not c.is_below(2, 2) and not c.is_below(1, 2)
    looped = Chart({1: Unit(1, 2, "a", "A", "team"), 2: Unit(2, 1, "b", "B", "team")})
    assert [unit.slug for unit in looped.ancestors(1)] == ["a", "b"]
    with pytest.raises(ValueError, match="0 roots"):
        _ = looped.root


# The rules (criterion a3), on the model


def test_orgchart_access_rules_of_a_lead_without_escalation():
    c, admin = chart(), org.Actor(1, "octo-admin", admin=True)
    lead, intern = org.Actor(100, "lena", admin=False), org.Actor(101, "ivan", admin=False)
    lab, cohort, ops = c.units[2], c.units[3], c.units[4]
    assert org.create_refusal(c, lead, lab, TODAY) is None and org.create_refusal(c, lead, cohort, TODAY) is None
    assert "needs a hub admin or a lead of ops" in org.create_refusal(c, lead, ops, TODAY)
    assert org.seat_refusal(c, lead, lab, "member", TODAY, what="adding a seat") is None
    assert org.seat_refusal(c, lead, lab, "intern", TODAY, what="adding a seat") is None
    assert "manages its member and intern seats" in org.seat_refusal(c, lead, lab, "lead", TODAY, what="adding a seat")
    assert org.seat_refusal(c, lead, cohort, "lead", TODAY, what="adding a seat") is None, "below: every role"
    assert org.ceiling_refusal(c, lead, org.UNIT, cohort, "A3", TODAY) is None
    assert "above their own effective level A4" in org.ceiling_refusal(c, lead, org.UNIT, cohort, "A5", TODAY)
    assert "leads lab itself" in org.ceiling_refusal(c, lead, org.UNIT, lab, "A1", TODAY)
    assert "a seat of their own" in org.ceiling_refusal(c, lead, org.SEAT, c.seats[10], "A1", TODAY)
    assert org.ceiling_refusal(c, lead, org.SEAT, c.seats[11], "A0", TODAY) is None
    assert org.ceiling_refusal(c, intern, org.UNIT, cohort, "A0", TODAY) is not None
    assert org.ceiling_refusal(c, admin, org.UNIT, c.units[1], "A5", TODAY) is None, "an admin sets the root's"
    # a lead whose seat ended leads nothing
    c.seats[10] = Seat(10, 2, "lead", user_id=100, primary=True, ended_at=ENDED)
    assert org.create_refusal(c, lead, lab, TODAY) is not None


# On the hub

if pg.DSN:
    import psycopg
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from sqlalchemy import func, insert, select, update

    from evo_agents.hub import tables
    from evo_agents.hub.migrate import migrate
    from evo_agents.hub.server import org_writes
    from evo_agents.hub.server.app import create_app
    from evo_agents.hub.server.org_models import SeatAdd, UnitEdit
    from tests.hub.live import sql
    from tests.hub.test_chats import OPEN_SINKS
    from tests.hub.test_migrate import ALL, INFO_TABLES, move_to
    from tests.hub.test_plans import PROJECT, registration

PEOPLE = (("lena", 801), ("ivan", 802), ("mia", 803), ("otto", 804))


@pytest.fixture
def hub(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        headers = {"admin": live.bearer(live.sign_in(client, github, live.ADMIN, 302)["token"])}
        for login, github_id in PEOPLE:
            headers[login] = live.bearer(live.sign_in(client, github, login, github_id)["token"])
        yield SimpleNamespace(client=client, headers=headers, db=hub_db, github=github)


def call(hub, who: str, method: str, path: str, body: dict | None = None, status: int = 200) -> dict:
    response = hub.client.request(method, f"/v1/org{path}", json=body, headers=hub.headers[who])
    assert response.status_code == status, response.text
    return response.json()


def shown(hub, who: str = "mia") -> dict:
    return call(hub, who, "GET", "")


def seats_of(hub, login: str) -> list[dict]:
    return [seat for seat in shown(hub)["seats"] if seat["login"] == login]


def org_audit(db) -> list[tuple]:
    a, u = tables.audit, tables.users
    query = (
        select(a.c.action, a.c.target, u.c.login, a.c.project_id)
        .join_from(a, u, u.c.id == a.c.actor_id)
        .where(a.c.action.like("org.%") | (a.c.action == "autonomy.ceiling_set"))
        .order_by(a.c.id)
    )
    return sql(db, query)


def lab_with_lead(hub) -> int:
    """Unit lab below the root, lena its lead (moved from the root), and cohort intern-2026 below it; lena's seat."""
    call(hub, "admin", "POST", "/units", {"slug": "lab", "parent": "root", "kind": "team", "name": "Lab"}, 201)
    seat = call(hub, "admin", "POST", "/seats", {"login": "lena", "unit": "lab", "role": "lead"}, 201)
    cohort = {"slug": "intern-2026", "parent": "lab", "kind": "cohort", "ends_on": "2026-12-31"}
    call(hub, "lena", "POST", "/units", cohort, 201)
    return seat["id"]


# Schema 0099 (criterion a1)


@needs_pg
def test_orgchart_migration_places_every_user_at_the_root_and_going_back_drops_the_tables(hub_db):
    move_to(hub_db, "0098", down=True)
    users = tables.users
    sql(hub_db, insert(users), [{"login": "northwind-dev"}, {"login": "lumen-ops"}])
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0099") :]
    u, s, c = tables.org_units, tables.org_seats, tables.autonomy_ceilings
    assert sql(hub_db, select(u.c.slug, u.c.kind, u.c.parent_id, u.c.revision)) == [("root", "org", None, 1)]
    assert sql(hub_db, select(c.c.scope, c.c.revision, c.c.level, c.c.author_id)) == [("unit", 1, "A5", None)]
    placed = select(users.c.login, s.c.role, s.c.is_primary, s.c.ended_at).join_from(
        s, users, users.c.id == s.c.user_id
    )
    assert sorted(sql(hub_db, placed)) == [("lumen-ops", "member", True, None), ("northwind-dev", "member", True, None)]
    move_to(hub_db, "0098", down=True)
    public = select(INFO_TABLES.c.table_name).where(INFO_TABLES.c.table_schema == "public")
    assert not {row[0] for row in sql(hub_db, public)} & pg.ORG_TABLES
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0099") :]


@needs_pg
def test_orgchart_model_constraints_hold_in_the_database(hub, hub_db):
    lab_with_lead(hub)
    u, s, c = tables.org_units, tables.org_seats, tables.autonomy_ceilings
    lena, ivan = user_id(hub_db, "lena"), user_id(hub_db, "ivan")
    lab = sql(hub_db, select(u.c.id).where(u.c.slug == "lab"))[0][0]
    lead_seat = sql(hub_db, select(s.c.id).where(s.c.user_id == lena, s.c.unit_id == lab))[0][0]
    profile = sql(hub_db, select(tables.agent_profiles.c.id).limit(1))
    seat = {"unit_id": lab, "user_id": ivan, "role": "member"}
    for bad, name in (
        (insert(u).values(slug="second-root", name="Second", kind="org"), "org_units_one_root_key"),
        (insert(u).values(slug="loose", name="Loose", kind="team"), "org_units_check"),  # a team has a parent
        (insert(u).values(slug="Bad Slug", name="B", kind="team", parent_id=lab), "org_units_check"),
        (insert(u).values(slug="t", name="T", kind="team", parent_id=lab, ends_on=TODAY), "org_units_check"),
        (insert(u).values(slug="lab", name="Again", kind="team", parent_id=lab), "org_units_slug_key"),
        (insert(s).values({**seat, "is_primary": True}), "org_seats_one_primary_key"),  # ivan's root seat is primary
        (insert(s).values({**seat, "role": "intern"}), "org_seats_check"),  # an intern reports to someone
        (insert(s).values({**seat, "role": "agent"}), "org_seats_check"),  # agent is a profile's role
        (insert(s).values({**seat, "ends_on": date(2020, 1, 1), "starts_on": TODAY}), "org_seats_check"),
        (insert(c).values(scope="unit", scope_id=lab, revision=1, level="A6"), "autonomy_ceilings_check"),
        (insert(c).values(scope="team", scope_id=lab, revision=1, level="A1"), "autonomy_ceilings_check"),
    ):
        with pytest.raises(psycopg.errors.IntegrityError, match=name):
            sql(hub_db, bad)
    if profile:
        agent = {"unit_id": lab, "profile_id": profile[0][0], "role": "agent", "is_primary": True}
        with pytest.raises(psycopg.errors.CheckViolation, match="org_seats_check"):
            sql(hub_db, insert(s).values(agent))  # no sponsor
        with pytest.raises(psycopg.errors.CheckViolation, match="org_seats_check"):
            sql(hub_db, insert(s).values({**agent, "sponsor_id": lena, "user_id": ivan}))  # a person and a profile
    with pytest.raises(psycopg.errors.CheckViolation, match="org_seats_check"):
        sql(hub_db, update(s).values(reports_to=lead_seat).where(s.c.id == lead_seat))  # never to itself
    good = {**seat, "role": "intern", "reports_to": lead_seat}
    sql(hub_db, insert(s).values(good))  # a second, secondary seat of ivan, reporting to lena's


def user_id(db, login: str) -> int:
    users = tables.users
    return sql(db, select(users.c.id).where(func.lower(users.c.login) == login.lower()))[0][0]


@needs_pg
def test_orgchart_model_a_users_row_gets_its_root_seat_when_it_is_made_and_never_again(hub, hub_db):
    root = [seat for seat in shown(hub)["seats"] if seat["unit"] == "root"]
    assert {seat["login"] for seat in root} == {live.ADMIN, *(login for login, _ in PEOPLE)}
    assert all(seat["primary"] and seat["role"] == "member" and seat["effective"] == "A5" for seat in root)
    # a grant to a login that never signed in makes its row and its seat in the same transaction
    hub.client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=hub.headers["admin"])
    grant = {"role": "writer", "max_level": "internal"}
    put = hub.client.put(f"/v1/admin/projects/{PROJECT}/grants/Northwind-Dev", json=grant, headers=hub.headers["admin"])
    assert put.status_code == 200, put.text
    assert [(seat["unit"], seat["primary"]) for seat in seats_of(hub, "Northwind-Dev")] == [("root", True)]
    # its first sign-in claims the row, and the seat with it; a second grant adds nothing
    live.sign_in(hub.client, hub.github, "Northwind-Dev", 901)
    put = hub.client.put(f"/v1/admin/projects/{PROJECT}/grants/northwind-dev", json=grant, headers=hub.headers["admin"])
    assert put.status_code == 200, put.text
    assert len(seats_of(hub, "Northwind-Dev")) == 1
    # an ended primary seat is never given back by a grant
    seat_id = seats_of(hub, "mia")[0]["id"]
    call(hub, "admin", "POST", f"/seats/{seat_id}/end")
    put = hub.client.put(f"/v1/admin/projects/{PROJECT}/grants/mia", json=grant, headers=hub.headers["admin"])
    assert put.status_code == 200, put.text
    assert [(seat["effective"], seat["ended_at"] is not None) for seat in seats_of(hub, "mia")] == [("A0", True)]


@needs_pg
def test_orgchart_model_a_login_granted_before_it_signed_in_merges_its_seats_into_the_account(hub, hub_db):
    hub.client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=hub.headers["admin"])
    grant = {"role": "writer", "max_level": "internal"}
    hub.client.put(f"/v1/admin/projects/{PROJECT}/grants/lumen-new", json=grant, headers=hub.headers["admin"])
    lab_with_lead(hub)
    call(hub, "lena", "POST", "/seats", {"login": "lumen-new", "unit": "lab", "role": "member"}, 201)
    # otto's account is renamed lumen-new on GitHub, and signs in: the two rows become one
    live.sign_in(hub.client, hub.github, "lumen-new", 804)
    held = sorted((seat["unit"], seat["primary"], seat["ended_at"] is None) for seat in seats_of(hub, "lumen-new"))
    # the account keeps its own primary seat; the lab seat stays, secondary; the second root seat ends
    assert held == [("lab", False, True), ("root", False, False), ("root", True, True)]
    assert seats_of(hub, "otto") == []


@needs_pg
def test_orgchart_model_a_cohort_gets_a1_a_team_inherits_and_the_tree_keeps_its_shape(hub):
    lab_with_lead(hub)
    units = {unit["slug"]: unit for unit in shown(hub)["units"]}
    assert units["root"]["parent"] is None and units["root"]["ceiling"] == "A5"
    assert (units["lab"]["ceiling"], units["lab"]["effective"], units["lab"]["source"]) == (None, "A5", "unit:root")
    cohort = units["intern-2026"]
    assert (cohort["ceiling"], cohort["effective"], cohort["ends_on"]) == ("A1", "A1", "2026-12-31")
    assert "cycle" in call(hub, "admin", "PATCH", "/units/lab", {"parent": "intern-2026"}, 409)["message"]
    assert "the root has no parent" in call(hub, "admin", "PATCH", "/units/root", {"parent": "lab"}, 409)["message"]
    assert "cohort's alone" in call(hub, "admin", "PATCH", "/units/lab", {"ends_on": "2027-01-01"}, 422)["message"]
    team = {"slug": "t1", "parent": "lab", "kind": "team", "ends_on": "2027-01-01"}
    assert "cohort's alone" in call(hub, "admin", "POST", "/units", team, 422)["message"]
    twice = {"slug": "lab", "parent": "root", "kind": "team"}
    assert "already" in call(hub, "admin", "POST", "/units", twice, 409)["message"]
    assert "the root is never archived" in call(hub, "admin", "POST", "/units/root/archive", None, 409)["message"]
    assert "units below it" in call(hub, "admin", "POST", "/units/lab/archive", None, 409)["message"]
    call(hub, "admin", "POST", "/units", {"slug": "ops", "parent": "root", "kind": "team"}, 201)
    moved = call(hub, "admin", "PATCH", "/units/intern-2026", {"parent": "ops", "name": "Interns", "if_revision": 1})
    assert (moved["parent"], moved["name"], moved["revision"]) == ("ops", "Interns", 2)
    assert (
        "at revision 2, not 1" in call(hub, "admin", "PATCH", "/units/intern-2026", {"if_revision": 1}, 409)["message"]
    )
    archived = call(hub, "admin", "POST", "/units/intern-2026/archive")
    assert archived["archived_at"] is not None and archived["revision"] == 3
    assert (
        "archived"
        in call(hub, "admin", "POST", "/seats", {"login": "ivan", "unit": "intern-2026", "role": "member"}, 409)[
            "message"
        ]
    )


@needs_pg
def test_orgchart_model_an_intern_reports_to_a_live_seat_of_another_person_and_an_agent_has_a_sponsor(hub, hub_db):
    lab_with_lead(hub)
    intern = {"login": "ivan", "unit": "intern-2026", "role": "intern"}
    assert "reports to a person" in call(hub, "lena", "POST", "/seats", intern, 422)["message"]
    assert "same person" in call(hub, "lena", "POST", "/seats", {**intern, "reports_to": "ivan"}, 422)["message"]
    seat = call(hub, "lena", "POST", "/seats", {**intern, "reports_to": "lena"}, 201)
    assert (seat["reports_to"], seat["primary"], seat["effective"], seat["source"]) == (
        "lena",
        True,
        "A1",
        "unit:intern-2026",
    )
    assert "already" in call(hub, "lena", "POST", "/seats", {**intern, "reports_to": "lena"}, 409)["message"]
    assert (
        "nobody"
        in call(hub, "lena", "POST", "/seats", {**intern, "login": "nobody", "reports_to": "lena"}, 404)["message"]
    )
    # the seat ivan had at the root stays, secondary; his ceiling is the cohort's
    root_seat = next(s for s in seats_of(hub, "ivan") if s["unit"] == "root")
    assert root_seat["primary"] is False and root_seat["ended_at"] is None
    # an agent profile's seat: role agent, a sponsor, always primary, one live seat a profile
    hub.client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=hub.headers["admin"])
    agent = {"agent": PROJECT, "project": PROJECT, "unit": "lab", "role": "agent"}
    assert "sponsor" in call(hub, "admin", "POST", "/seats", agent, 422)["message"]
    assert (
        "role agent"
        in call(hub, "admin", "POST", "/seats", {**agent, "role": "member", "sponsor": "lena"}, 422)["message"]
    )
    assert (
        "no agent profile"
        in call(hub, "admin", "POST", "/seats", {**agent, "agent": "ghost", "sponsor": "lena"}, 404)["message"]
    )
    placed = call(hub, "admin", "POST", "/seats", {**agent, "sponsor": "lena"}, 201)
    assert (placed["agent"], placed["project"], placed["login"], placed["sponsor"], placed["primary"]) == (
        PROJECT,
        PROJECT,
        None,
        "lena",
        True,
    )
    assert "live seat already" in call(hub, "admin", "POST", "/seats", {**agent, "sponsor": "lena"}, 409)["message"]
    assert (
        "always has a sponsor"
        in call(hub, "admin", "PATCH", f"/seats/{placed['id']}", {"sponsor": None}, 422)["message"]
    )


# Who changes what (criterion a3)


@needs_pg
def test_orgchart_access_an_admin_changes_everything_the_root_and_its_ceiling_included(hub):
    written = call(hub, "admin", "PUT", "/ceilings", {"unit": "root", "level": "A4", "note": "a quieter week"})
    assert (written["target"], written["revision"], written["previous"], written["level"]) == (
        "unit:root",
        2,
        "A5",
        "A4",
    )
    assert shown(hub)["viewer"] == {
        "login": "mia",
        "admin": False,
        "leads": [],
        "effective": "A4",
        "source": "unit:root",
    }
    root = call(hub, "admin", "PATCH", "/units/root", {"name": "Northwind"})
    assert (root["name"], root["revision"]) == ("Northwind", 2)
    call(hub, "admin", "PUT", "/ceilings", {"login": "mia", "level": "A2"})
    assert seats_of(hub, "mia")[0]["effective"] == "A2" and seats_of(hub, "mia")[0]["source"].startswith("seat:")
    assert (
        "exactly one"
        in call(hub, "admin", "PUT", "/ceilings", {"unit": "root", "login": "mia", "level": "A1"}, 422)["message"]
    )


@needs_pg
def test_orgchart_access_a_lead_manages_below_without_escalation_and_others_are_told_why(hub):
    lead_seat = lab_with_lead(hub)
    assert shown(hub, "lena")["viewer"]["leads"] == ["lab"]
    # a lead creates units below, adds members and interns to the unit, and every role below it
    call(hub, "lena", "POST", "/seats", {"login": "mia", "unit": "lab", "role": "member"}, 201)
    refused = call(hub, "lena", "POST", "/seats", {"login": "otto", "unit": "lab", "role": "lead"}, 403)
    assert "member and intern seats" in refused["message"]
    call(hub, "lena", "POST", "/seats", {"login": "otto", "unit": "intern-2026", "role": "lead"}, 201)
    # ceilings: below alone, never above the lead's own level, never the unit they lead or a seat of theirs
    call(hub, "admin", "PUT", "/ceilings", {"unit": "lab", "level": "A3"})
    assert (
        "above their own effective level A3"
        in call(hub, "lena", "PUT", "/ceilings", {"unit": "intern-2026", "level": "A4"}, 403)["message"]
    )
    call(hub, "lena", "PUT", "/ceilings", {"unit": "intern-2026", "level": "A2"})
    assert "leads lab itself" in call(hub, "lena", "PUT", "/ceilings", {"unit": "lab", "level": "A1"}, 403)["message"]
    assert (
        "a seat of their own"
        in call(hub, "lena", "PUT", "/ceilings", {"seat": lead_seat, "level": "A1"}, 403)["message"]
    )
    call(hub, "lena", "PUT", "/ceilings", {"login": "mia", "level": "A1"})
    assert "leads lab itself" in call(hub, "lena", "PATCH", "/units/lab", {"name": "Mine"}, 403)["message"]
    assert (
        "a lead of root"
        in call(hub, "lena", "POST", "/units", {"slug": "x", "parent": "root", "kind": "team"}, 403)["message"]
    )
    # a cohort a lead creates without a level gets A1, which must not lie above their own level
    call(hub, "admin", "PUT", "/ceilings", {"seat": lead_seat, "level": "A0"})
    refused = call(hub, "lena", "POST", "/units", {"slug": "c2", "parent": "lab", "kind": "cohort"}, 403)
    assert "above their own effective level A0" in refused["message"]
    # someone with no lead seat
    for method, path, body in (
        ("POST", "/units", {"slug": "y", "parent": "lab", "kind": "team"}),
        ("POST", "/seats", {"login": "ivan", "unit": "lab", "role": "member"}),
        ("PUT", "/ceilings", {"unit": "intern-2026", "level": "A0"}),
        ("POST", f"/seats/{lead_seat}/end", None),
    ):
        refused = call(hub, "ivan", method, path, body, 403)
        assert "needs a hub admin" in refused["message"] and refused["error"] == "forbidden"


@needs_pg
def test_orgchart_access_a_lead_pulls_people_from_the_root_never_out_of_another_leads_unit(hub):
    lab_with_lead(hub)
    call(hub, "admin", "POST", "/units", {"slug": "ops", "parent": "root", "kind": "team"}, 201)
    call(hub, "admin", "POST", "/seats", {"login": "otto", "unit": "ops", "role": "lead"}, 201)
    call(hub, "otto", "POST", "/seats", {"login": "mia", "unit": "ops", "role": "member"}, 201)
    assert [s["unit"] for s in seats_of(hub, "mia") if s["primary"]] == ["ops"]
    refused = call(
        hub, "lena", "POST", "/seats", {"login": "mia", "unit": "lab", "role": "member", "primary": True}, 403
    )
    assert "moving the primary seat" in refused["message"]
    secondary = call(hub, "lena", "POST", "/seats", {"login": "mia", "unit": "lab", "role": "member"}, 201)
    assert secondary["primary"] is False, "a person placed already keeps their primary seat"
    refused = call(hub, "lena", "PATCH", f"/seats/{secondary['id']}", {"primary": True}, 403)
    assert "moving the primary seat" in refused["message"]
    assert call(hub, "admin", "PATCH", f"/seats/{secondary['id']}", {"primary": True})["primary"] is True
    assert [s["unit"] for s in seats_of(hub, "mia") if s["primary"]] == ["lab"]
    call(hub, "admin", "PATCH", f"/seats/{secondary['id']}", {"primary": False}, 422)


# Every change audited (criterion a3)


@needs_pg
def test_orgchart_audit_every_change_names_the_actor_the_target_and_the_values_before_and_after(hub, hub_db):
    lead_seat = lab_with_lead(hub)
    seat = call(
        hub,
        "lena",
        "POST",
        "/seats",
        {"login": "ivan", "unit": "intern-2026", "role": "intern", "reports_to": "lena"},
        201,
    )
    call(hub, "lena", "PATCH", f"/seats/{seat['id']}", {"ends_on": "2026-12-01", "role": "member"})
    call(hub, "lena", "PUT", "/ceilings", {"seat": seat["id"], "level": "A0", "note": "a secret note"})
    call(hub, "lena", "POST", f"/seats/{seat['id']}/end")
    call(hub, "admin", "PATCH", "/units/lab", {"name": "The lab"})
    rows = org_audit(hub_db)
    assert all(project is None for *_, project in rows) and all(
        action.startswith(("org.", "autonomy.")) for action, *_ in rows
    )
    lab_seat = f"seat:{lead_seat} unit=lab login=lena role=lead primary=true revision=2"
    assert [(action, target, login) for action, target, login, _ in rows] == [
        ("org.unit_create", "unit:lab parent=root kind=team revision=1", live.ADMIN),
        ("org.seat_edit", f"seat:{rows[1][1].split()[0][5:]} unit=root primary=true->false revision=2", live.ADMIN),
        ("org.seat_add", lab_seat, live.ADMIN),
        ("org.unit_create", "unit:intern-2026 parent=lab kind=cohort ends_on=2026-12-31 revision=1", "lena"),
        ("autonomy.ceiling_set", "ceiling unit:intern-2026 level=none->A1 revision=1", "lena"),
        ("org.seat_edit", rows[5][1], "lena"),
        ("org.seat_add", f"seat:{seat['id']} unit=intern-2026 login=ivan role=intern primary=true revision=2", "lena"),
        (
            "org.seat_edit",
            f"seat:{seat['id']} unit=intern-2026 role=intern->member ends_on=none->2026-12-01 revision=3",
            "lena",
        ),
        ("autonomy.ceiling_set", f"ceiling seat:{seat['id']} level=none->A0 revision=1", "lena"),
        ("org.seat_end", f"seat:{seat['id']} unit=intern-2026 ended=none->now revision=4", "lena"),
        ("org.unit_edit", "unit:lab name=Lab->The lab revision=3", live.ADMIN),
    ]
    assert rows[5][1].endswith("unit=root primary=true->false revision=3")
    assert "secret" not in str(rows)
    c = tables.autonomy_ceilings
    notes = sql(hub_db, select(c.c.note, c.c.author_id).where(c.c.scope == "seat"))
    assert notes == [("a secret note", user_id(hub_db, "lena"))]


# Writes at once (criterion a3)


@needs_pg
def test_orgchart_concurrent_moves_of_two_units_below_each_other_never_make_a_cycle(hub, hub_db):
    call(hub, "admin", "POST", "/units", {"slug": "north", "parent": "root", "kind": "team"}, 201)
    call(hub, "admin", "POST", "/units", {"slug": "south", "parent": "root", "kind": "team"}, 201)
    admin = org.Actor(user_id(hub_db, live.ADMIN), live.ADMIN, admin=True)
    engine = hub.client.app.state.engine

    async def move(slug: str, parent: str):
        async with engine.begin() as conn:
            write = await org_writes.begin(conn, admin, None)
            await asyncio.sleep(0.05)
            return await org_writes.edit_unit(write, slug, UnitEdit(parent=parent))

    async def both():
        return await asyncio.gather(move("north", "south"), move("south", "north"), return_exceptions=True)

    done = hub.client.portal.call(both)
    refused = [result for result in done if isinstance(result, HTTPException)]
    assert len(refused) == 1 and refused[0].status_code == 409 and "cycle" in refused[0].detail, done
    parents = {unit["slug"]: unit["parent"] for unit in shown(hub)["units"]}
    assert sorted([parents["north"], parents["south"]]) == ["root", "south"] or sorted(
        [parents["north"], parents["south"]]
    ) == ["north", "root"]


@needs_pg
def test_orgchart_concurrent_primary_seats_of_one_person_leave_one_live_primary(hub, hub_db):
    for slug in ("east", "west"):
        call(hub, "admin", "POST", "/units", {"slug": slug, "parent": "root", "kind": "team"}, 201)
    admin = org.Actor(user_id(hub_db, live.ADMIN), live.ADMIN, admin=True)
    engine = hub.client.app.state.engine

    async def place(slug: str):
        async with engine.begin() as conn:
            write = await org_writes.begin(conn, admin, None)
            return await org_writes.add_seat(write, SeatAdd(login="mia", unit=slug, role="member", primary=True))

    async def both():
        return await asyncio.gather(place("east"), place("west"), return_exceptions=True)

    done = hub.client.portal.call(both)
    assert all(isinstance(result, int) for result in done), done
    s = tables.org_seats
    live_primary = select(func.count()).where(
        s.c.user_id == user_id(hub_db, "mia"), s.c.is_primary, s.c.ended_at.is_(None)
    )
    assert sql(hub_db, live_primary) == [(1,)]
    u = tables.org_units
    revisions = sql(hub_db, select(u.c.revision).where(u.c.slug.in_(["east", "west"])))
    assert sorted(revisions) == [(2,), (3,)], "the seat added first was demoted by the second: its unit bumped twice"
