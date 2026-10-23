"""The commands of the org chart (``evo_agents.hub.cli_org``, ``.cli_autonomy``), as criterion a4 of plan org-chart
names them: `hub org show [--json]`, `hub org unit create|edit|archive`, `hub org seat add|edit|end` and
`hub autonomy ceiling set`, over HTTP alone; a refusal of the hub prints its reason and exits non-zero.

The tree and the choice of a seat run without Postgres; the commands against a hub skip without EVO_HUB_TEST_DSN."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evo_agents.hub import cli_org
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
TODAY = "2026-10-22"


def unit(slug, parent, kind="team", **extra) -> dict:
    found = {
        "slug": slug,
        "name": slug,
        "kind": kind,
        "parent": parent,
        "ends_on": None,
        "archived_at": None,
        "revision": 1,
        "ceiling": None,
        "effective": "A5",
        "source": "unit:root",
    }
    return {**found, **extra}


def seat(seat_id, unit_slug, login=None, role="member", primary=True, **extra) -> dict:
    found = {
        "id": seat_id,
        "unit": unit_slug,
        "login": login,
        "agent": None,
        "project": None,
        "role": role,
        "primary": primary,
        "reports_to": None,
        "reports_to_seat": None,
        "sponsor": None,
        "starts_on": "2026-10-01",
        "ends_on": None,
        "ended_at": None,
        "ceiling": None,
        "effective": "A5",
        "source": "unit:root",
    }
    return {**found, **extra}


def chart() -> dict:
    return {
        "units": [
            unit("root", None, "org", name="Organization", ceiling="A5"),
            unit("lab", "root", ceiling="A4", effective="A4", source="unit:lab"),
            unit("intern-2026", "lab", "cohort", ends_on="2026-12-31", ceiling="A1", effective="A1"),
            unit("old", "root", archived_at="2026-10-02T10:00:00+00:00"),
        ],
        "seats": [
            seat(1, "root", "lena", primary=False),
            seat(2, "lab", "lena", "lead", effective="A4", source="unit:lab"),
            seat(3, "intern-2026", "ivan", "intern", reports_to="lena", effective="A1", source="unit:intern-2026"),
            seat(4, "lab", None, "agent", agent="bot", project="demo", sponsor="lena", effective="A4"),
            seat(5, "lab", "mia", ended_at="2026-10-10T00:00:00+00:00", effective="A0", source="seat:5"),
            seat(6, "root", "mia", primary=False),
            seat(7, "lab", "mia", primary=False),
        ],
        "viewer": {"login": "lena", "admin": False, "leads": ["lab"], "effective": "A4", "source": "unit:lab"},
    }


def named(**given) -> SimpleNamespace:
    return SimpleNamespace(**{"seat": None, "login": None, "agent": None, "project": None, "unit": None, **given})


def test_orgchart_cli_show_indents_units_below_their_parent_with_their_seats_and_levels():
    lines = cli_org.tree_lines(chart(), today=TODAY)
    assert lines[0] == "root (org, Organization, ceiling A5, effective A5 from unit:root)"
    assert lines[1] == "  - seat 1: lena, member, effective A5 from unit:root"
    assert lines[2] == "  - seat 6: mia, member, effective A5 from unit:root"
    assert lines[3] == "  lab (team, ceiling A4, effective A4 from unit:lab)"
    assert "    - seat 2: lena, lead, primary, effective A4 from unit:lab" in lines
    assert "    - seat 4: agent bot (demo), agent, primary, sponsor lena, effective A4 from unit:root" in lines
    assert any(line.startswith("    intern-2026 (cohort, ends 2026-12-31, ceiling A1") for line in lines)
    assert "      - seat 3: ivan, intern, primary, reports to lena, effective A1 from unit:intern-2026" in lines
    assert not any("old" in line or "seat 5" in line for line in lines), "archived units and ended seats are left out"
    every = cli_org.tree_lines(chart(), every=True, today=TODAY)
    assert any(line.startswith("  old (team, ceiling -") and "archived 2026-10-02" in line for line in every)
    assert any("seat 5: mia, member, primary, effective A0 from seat:5, ended 2026-10-10" in line for line in every)
    ended = chart()
    ended["seats"][2]["ends_on"] = "2026-10-21"
    assert not any("seat 3" in line for line in cli_org.tree_lines(ended, today=TODAY)), "its last day passed"


def test_orgchart_cli_finds_a_seat_by_id_by_login_in_a_unit_or_primary_and_by_agent():
    c = chart()
    assert cli_org.find_seat(c, named(seat=3))["login"] == "ivan"
    assert cli_org.find_seat(c, named(login="lena"))["id"] == 2, "the live primary seat"
    assert cli_org.find_seat(c, named(login="lena", unit="root"))["id"] == 1
    assert cli_org.find_seat(c, named(agent="bot", project="demo"))["id"] == 4
    with pytest.raises(HubError, match="holds live seats 6, 7; pass --unit or --seat"):
        cli_org.find_seat(c, named(login="mia"))
    with pytest.raises(HubError, match="ivan holds no live seat in unit lab"):
        cli_org.find_seat(c, named(login="ivan", unit="lab"))
    with pytest.raises(HubError, match="no seat 99"):
        cli_org.find_seat(c, named(seat=99))
    with pytest.raises(cli_org.Usage, match="--agent needs --project"):
        cli_org.find_seat(c, named(agent="bot"))
    with pytest.raises(cli_org.Usage, match="not both"):
        cli_org.find_seat(c, named(seat=1, login="lena"))


def test_orgchart_cli_commands_are_in_the_contract_with_the_keys_of_the_hub():
    printed = commands()
    for path in (
        "hub org show",
        "hub org unit create",
        "hub org unit edit",
        "hub org unit archive",
        "hub org seat add",
        "hub org seat edit",
        "hub org seat end",
        "hub autonomy ceiling set",
    ):
        assert printed[path]["json"] is not None, path
    assert printed["hub org show"]["json"]["keys"] == ["units", "seats", "viewer"]
    flags = {flag for option in printed["hub autonomy ceiling set"]["options"] for flag in option["flags"]}
    assert {
        "--unit",
        "--login",
        "--seat",
        "--level",
        "--note",
        "--class",
        "--environment",
        "--worker",
        "--json",
    } == flags
    level = next(o for o in printed["hub autonomy ceiling set"]["options"] if o["flags"] == ["--level"])
    assert level["choices"] == ["A0", "A1", "A2", "A3", "A4", "A5"] and level["required"]


# Against a hub

if pg.DSN:
    import httpx

    from evo_agents.hub.server.app import create_app
    from tests.hub.test_run_stream import serving

PEOPLE = (("lena", 811), ("ivan", 812), ("mia", 813))


@pytest.fixture
def org_cli(hub_db, tmp_path, github, monkeypatch, capsys):
    """A hub under uvicorn with the admin and PEOPLE signed in; ``run(who, *args)`` runs `evo-agents hub ARGS`."""
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url:
        homes = {}
        with httpx.Client(base_url=url, timeout=10) as client:
            for login, github_id in ((live.ADMIN, 302), *PEOPLE):
                token = live.sign_in(client, github, login, github_id)["token"]
                homes[login] = write_credentials(tmp_path / login, url, login, token)

        def run(who: str, *args: str) -> SimpleNamespace:
            return run_cli(monkeypatch, capsys, homes[who], "hub", *args)

        yield run


def refused(result: SimpleNamespace) -> str:
    assert result.code == 1 and result.err.startswith("error: "), result.out + result.err
    return result.err


@needs_pg
def test_orgchart_cli_builds_shows_and_takes_apart_a_unit_over_http(org_cli):
    admin = live.ADMIN
    created = as_json(org_cli(admin, "org", "unit", "create", "lab", "--parent", "root", "--kind", "team", "--json"))
    assert_json_keys("hub org unit create", created)
    assert (created["slug"], created["parent"], created["ceiling"]) == ("lab", "root", None)
    lead = as_json(org_cli(admin, "org", "seat", "add", "lena", "--unit", "lab", "--role", "lead", "--json"))
    assert_json_keys("hub org seat add", lead)
    assert (lead["login"], lead["role"], lead["primary"]) == ("lena", "lead", True)
    said = ok(org_cli(admin, "autonomy", "ceiling", "set", "--unit", "lab", "--level", "A3", "--note", "new team"))
    assert said.out.strip() == "Ceiling of unit:lab is A3 (was none), revision 1."
    # the lead works below the unit, through the same commands
    cohort = ("org", "unit", "create", "intern-2026", "--parent", "lab", "--kind", "cohort", "--ends-on", "2026-12-31")
    assert "Created cohort intern-2026 below lab" in ok(org_cli("lena", *cohort)).out
    intern = ("org", "seat", "add", "ivan", "--unit", "intern-2026", "--role", "intern", "--reports-to", "lena")
    ok(org_cli("lena", *intern))
    written = as_json(
        org_cli(
            "lena", "autonomy", "ceiling", "set", "--login", "ivan", "--level", "A0", "--note", "first week", "--json"
        )
    )
    assert_json_keys("hub autonomy ceiling set", written)
    assert (written["scope"], written["level"], written["previous"]) == ("seat", "A0", None)
    # the chart, as JSON and as a tree
    shown = as_json(org_cli("mia", "org", "show", "--json"))
    assert_json_keys("hub org show", shown)
    root = next(u for u in shown["units"] if u["parent"] is None)
    assert root["ceiling"] == "A5"
    ivan = next(s for s in shown["seats"] if s["login"] == "ivan" and s["primary"])
    assert (ivan["unit"], ivan["effective"], ivan["source"]) == ("intern-2026", "A0", f"seat:{ivan['id']}")
    tree = ok(org_cli("mia", "org", "show")).out.splitlines()
    assert tree[0].startswith("root (org,") and "ceiling A5, effective A5 from unit:root" in tree[0]
    assert any(line.startswith("  lab (team, ceiling A3, effective A3 from unit:lab)") for line in tree)
    assert any(line.startswith("      - seat ") and "ivan, intern, primary, reports to lena" in line for line in tree)
    assert tree[-1] == "You are mia: effective A5 from unit:root."
    # refusals print the hub's reason and exit 1
    assert "needs a hub admin" in refused(
        org_cli("mia", "org", "unit", "create", "x", "--parent", "lab", "--kind", "team")
    )
    assert "member and intern seats" in refused(
        org_cli("lena", "org", "seat", "add", "mia", "--unit", "lab", "--role", "lead")
    )
    assert "above their own effective level A3" in refused(
        org_cli("lena", "autonomy", "ceiling", "set", "--unit", "intern-2026", "--level", "A4", "--note", "more")
    )
    assert "already" in refused(org_cli("lena", *intern))
    assert "reports to a person" in refused(
        org_cli("lena", "org", "seat", "add", "mia", "--unit", "intern-2026", "--role", "intern")
    )
    stale = org_cli(admin, "org", "unit", "edit", "lab", "--name", "Lab", "--if-revision", "1")
    assert "revision" in refused(stale)
    # a usage error never reaches the hub
    wrong = org_cli(admin, "org", "unit", "create", "c", "--parent", "lab", "--kind", "cohort", "--ends-on", "soon")
    assert wrong.code == 2 and "YYYY-MM-DD" in wrong.err
    # edit, end and archive
    renamed = as_json(org_cli(admin, "org", "unit", "edit", "intern-2026", "--name", "Interns 2026", "--json"))
    assert renamed["name"] == "Interns 2026"
    moved = as_json(org_cli("lena", "org", "seat", "edit", "ivan", "--ends-on", "2026-11-30", "--json"))
    assert_json_keys("hub org seat edit", moved)
    assert (moved["login"], moved["ends_on"]) == ("ivan", "2026-11-30")
    assert "holds no live seat in unit lab" in refused(org_cli("lena", "org", "seat", "end", "ivan", "--unit", "lab"))
    ended = as_json(org_cli("lena", "org", "seat", "end", "ivan", "--unit", "intern-2026", "--json"))
    assert_json_keys("hub org seat end", ended)
    assert ended["ended_at"] is not None and ended["effective"] == "A0"
    assert "units below it" in refused(org_cli(admin, "org", "unit", "archive", "lab"))
    ok(org_cli(admin, "org", "unit", "archive", "intern-2026"))
    archived = as_json(org_cli("mia", "org", "show", "--json"))
    assert [u["archived_at"] is not None for u in archived["units"] if u["slug"] == "intern-2026"] == [True]
    assert not any("intern-2026" in line for line in ok(org_cli("mia", "org", "show")).out.splitlines())
    assert any("intern-2026" in line for line in ok(org_cli("mia", "org", "show", "--all")).out.splitlines())
