"""The autonomy levels (``evo_agents.hub.autonomy_levels``, ``evo_agents.hub.org``, ``evo_agents.hub.server.autonomy_store``
and ``.org_writes``), as criteria a1, a2 and a3 of plan autonomy-levels name them: the catalog of classes, modes and
levels with every cell of the preset table; schema 0100, a person's preference in revisions and the class overrides,
environments and workers of a ceiling, without escalation by class, each write audited as an ``autonomy.`` action; the
effective mode of each class from the seven sources, with the source that sets it, and who sees whose.

The catalog and the effective mode run without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN.
"""

from __future__ import annotations

import pytest

from evo_agents.hub import autonomy_levels as al
from evo_agents.hub import org
from evo_agents.hub.autonomy_levels import Cell
from evo_agents.hub.org import Unit
from tests.hub import pg
from tests.hub.test_orgchart import TODAY, chart

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

# The table "Mức dựng sẵn" of the design report, a row a class, a column a level A0 to A5.
TABLE = {
    "K1": ("auto", "auto", "auto", "auto", "auto", "auto"),
    "K2": ("deny", "auto", "auto", "auto", "auto", "auto"),
    "K3": ("deny", "auto", "auto", "auto", "auto", "auto"),
    "K4": ("deny", "auto", "auto", "auto", "auto", "auto"),
    "K5": ("deny", "ask", "notify:ask_on_risk_paths", "auto", "auto", "auto"),
    "K6": ("deny", "deny", "ask", "notify", "auto", "auto"),
    "K7": ("deny", "auto:run_branch", "auto:test_secrets", "auto", "auto", "auto"),
    "K8": ("deny", "deny", "deny", "ask", "notify", "auto"),
    "K9": ("deny", "deny", "deny", "ask", "notify", "auto"),
    "K10": ("deny", "deny", "ask", "ask", "notify", "auto"),
    "K11": ("deny", "deny", "deny", "ask", "notify:within_budget", "auto:within_budget"),
    "K12": ("deny", "deny", "deny", "deny", "ask", "auto:not_above_own"),
}


# The catalog (criterion a1)


def test_autonomy_level_catalog_holds_every_cell_of_the_preset_table():
    assert al.LEVELS == ("A0", "A1", "A2", "A3", "A4", "A5")
    assert al.MODES == ("deny", "ask", "notify", "auto"), "strictest first"
    assert al.CLASSES == tuple(TABLE) and set(al.CLASS_NAMES) == set(TABLE)
    for klass, row in TABLE.items():
        for level, expected in zip(al.LEVELS, row, strict=True):
            assert al.PRESETS[level][klass].text() == expected, f"{klass} at {level}"


def test_autonomy_level_catalog_k1_to_k4_and_k7_never_take_ask():
    assert al.NO_ASK == {"K1", "K2", "K3", "K4", "K7"}
    for level in al.LEVELS:
        assert all(al.PRESETS[level][klass].mode != al.ASK for klass in al.NO_ASK), level
    for klass in sorted(al.NO_ASK):
        with pytest.raises(ValueError, match="never takes ask"):
            al.parse_override(f"{klass}=ask")
    assert al.overrides_problem({"K3": "ask"}) == "K3 never takes ask; K1 to K4 and K7 take deny, notify or auto"


def test_autonomy_level_catalog_reads_an_override_with_its_condition_and_refuses_a_wrong_one():
    assert al.parse_override("k6=DENY") == ("K6", "deny")
    assert al.parse_override("K5=notify:ask_on_risk_paths") == ("K5", "notify:ask_on_risk_paths")
    assert al.parse_override("K7=auto:run_branch") == ("K7", "auto:run_branch")
    for wrong, problem in (
        ("K13=auto", "no class"),
        ("K2=maybe", "the modes are deny, ask, notify, auto"),
        ("K6=auto:run_branch", "K6 takes no condition run_branch"),
        ("K7=auto:within_budget", "it takes run_branch, test_secrets"),
        ("K5=deny:ask_on_risk_paths", "a condition goes with notify or auto"),
        ("K11=auto:within_budget", "K11 takes no condition"),
        ("K6", "is not Kn=mode"),
    ):
        with pytest.raises(ValueError, match=problem):
            al.parse_override(wrong)
    # K11 and K12 carry their condition in every mode that acts, whoever set it
    assert al.cells("A3", {"K11": "auto", "K12": "notify"})["K11"] == Cell("auto", "within_budget")
    assert al.cells("A3", {"K12": "notify"})["K12"] == Cell("notify", "not_above_own")
    assert al.cells("A3", {"K12": "deny"})["K12"] == Cell("deny")
    assert al.cells(None) == al.PRESETS["A5"], "no level is A5"


def test_autonomy_level_catalog_orders_conditions_strictest_first_within_one_mode():
    run_branch, test_secrets, free = Cell("auto", "run_branch"), Cell("auto", "test_secrets"), Cell("auto")
    assert al.looser("K7", test_secrets, run_branch) and al.looser("K7", free, test_secrets)
    assert not al.looser("K7", run_branch, free)
    assert al.looser("K5", Cell("auto"), Cell("notify")) and al.looser("K5", Cell("ask"), Cell("deny"))
    assert not al.looser("K5", Cell("deny", "ask_on_risk_paths"), Cell("deny")), "a condition counts while it acts"


# The effective mode from the seven sources (criterion a3)


def lab_chart():
    """chart() of the org tests with the lab's ceiling A4 overriding K6 to ask and allowing two environments and two
    workers."""
    c = chart()
    c.units[2] = Unit(
        2,
        1,
        "lab",
        "Lab",
        "team",
        ceiling="A4",
        overrides={"K6": "ask"},
        environments=("host", "docker/py"),
        workers=("w1", "w2"),
    )
    return c


def test_autonomy_level_effective_lists_the_seven_sources_in_order():
    c = lab_chart()
    sources = org.autonomy_sources(
        c,
        100,
        TODAY,
        al.pref_source("lena", None),
        policy=al.policy_source("demo", "shop", "off", "manual"),
        profile=("bot", 500),
    )
    assert [source.name for source in sources] == [
        "pref:lena",
        "seat:10",
        "unit:lab",
        "unit:root",
        "policy:demo/shop",
        "seat:13",
        "unit:lab",
        "unit:root",
        "credential_share",
        "instance",
    ]
    assert sources[0] == al.Source("pref:lena", "A5", detail="no preference set"), "no preference is A5"
    alone = org.autonomy_sources(c, 100, TODAY, al.pref_source("lena", None), profile=("ghost", 999))
    assert [source.name for source in alone][-3:] == ["profile:ghost", "credential_share", "instance"]


def test_autonomy_level_effective_takes_the_strictest_mode_of_each_class_with_its_source():
    c = lab_chart()
    policy = al.policy_source("demo", "shop", "off", "manual")
    reached = al.effective(
        org.autonomy_sources(c, 100, TODAY, al.pref_source("lena", None), policy=policy, profile=("bot", 500))
    )
    assert (reached.level, reached.level_source) == ("A4", "unit:lab")
    k5 = reached.classes["K5"]
    assert (k5.mode, k5.source) == ("ask", "policy:demo/shop"), "repo mode off caps K5 at ask"
    assert k5.detail == "K5=ask of policy:demo/shop (mode off, prod_release manual)"
    k6 = reached.classes["K6"]
    assert (k6.mode, k6.source) == ("ask", "unit:lab"), "the first of two sources at ask: the lab's override"
    assert reached.classes["K11"] == al.Choice(
        "notify", "unit:lab", "level A4 of unit:lab (team Lab); within the budget", "within_budget"
    )
    assert reached.classes["K9"].detail.endswith("floor S2: a backup made within 24 hours comes first")
    assert (reached.classes["K1"].mode, reached.classes["K1"].source) == ("auto", "pref:lena")
    assert reached.environments == ("docker/py", "host") and reached.workers == ("w1", "w2")
    # prod_release manual alone caps K6; mode auto leaves K5 to the levels
    manual = al.effective([al.pref_source("lena", None), al.policy_source("demo", "shop", "auto", "manual")])
    assert (manual.classes["K6"].mode, manual.classes["K5"].mode) == ("ask", "auto")
    assert al.policy_source("demo", "shop", "auto", "immediate").overrides == {}


def test_autonomy_level_effective_of_a_preference_below_and_above_the_ceilings():
    c = lab_chart()
    below = al.effective(org.autonomy_sources(c, 100, TODAY, al.pref_source("lena", "A2", {"K6": "deny"})))
    assert (below.level, below.level_source) == ("A2", "pref:lena")
    assert (below.classes["K6"].mode, below.classes["K6"].source) == ("deny", "pref:lena")
    assert below.cell("K7") == Cell("auto", "test_secrets")
    assert below.cell("K5") == Cell("notify", "ask_on_risk_paths")
    # a preference above the ceilings changes nothing: the ceilings decide
    above = al.effective(org.autonomy_sources(c, 101, TODAY, al.pref_source("ivan", "A5")))
    assert (above.level, above.level_source) == ("A1", "unit:intern-2026")
    assert above.cell("K7") == Cell("auto", "run_branch")
    # the strictest condition of any source holds while the mode acts, naming where it came from
    held = al.effective([al.Source("a", "A3"), al.Source("b", "A2"), al.Source("c", None, {"K7": "auto:run_branch"})])
    assert held.cell("K7") == Cell("auto", "run_branch") and held.classes["K7"].source == "a"
    assert held.classes["K7"].detail.endswith("only the credentials that push the run's branch from c")


def test_autonomy_level_effective_lists_are_the_intersection_and_none_without_any():
    nothing = al.effective([al.Source("a", "A3"), al.instance_source()])
    assert (nothing.environments, nothing.workers) == (None, None), "no source limits them"
    both = al.effective([al.Source("a", environments=("host", "x/y")), al.Source("b", environments=("x/y", "z/w"))])
    assert both.environments == ("x/y",) and both.workers is None
    apart = al.effective([al.Source("a", environments=("host",)), al.Source("b", environments=("x/y",))])
    assert apart.environments == (), "two lists with nothing in common allow nothing"
    empty = al.effective([])
    assert (empty.level, empty.level_source, empty.classes["K12"].mode) == ("A5", "instance", "auto")


def test_autonomy_level_effective_the_org_ceiling_is_its_level():
    c = lab_chart()
    for user_id in (100, 101, 102, 999):
        reached = al.effective(org.person_sources(c, user_id, TODAY))
        assert org.effective(c, user_id, TODAY) == org.Effective(reached.level, reached.level_source)
    assert org.lowest([("seat:1", "A2"), ("unit:root", None)]) == org.Effective("A2", "seat:1")


def test_autonomy_level_ceiling_no_escalation_by_class():
    c, lead = lab_chart(), org.Actor(100, "lena", admin=False)
    cohort = c.units[3]
    assert org.ceiling_refusal(c, lead, org.UNIT, cohort, "A3", TODAY) == (
        "lena may not set a ceiling where K6=notify lies above their own effective mode ask (from unit:lab)"
    )
    assert org.ceiling_refusal(c, lead, org.UNIT, cohort, "A3", TODAY, {"K6": "ask"}) is None
    assert "K8=auto lies above" in org.ceiling_refusal(c, lead, org.UNIT, cohort, "A1", TODAY, {"K8": "auto"})
    # a condition counts: under an A2 lab, K7 without one lies above auto:test_secrets
    c.units[2] = Unit(2, 1, "lab", "Lab", "team", ceiling="A2")
    refused = org.ceiling_refusal(c, lead, org.UNIT, cohort, "A2", TODAY, {"K7": "auto"})
    assert "K7=auto lies above their own effective mode auto:test_secrets" in refused
    assert org.ceiling_refusal(c, lead, org.UNIT, cohort, "A1", TODAY, {"K7": "auto:test_secrets"}) is None
    admin = org.Actor(1, "octo-admin", admin=True)
    assert org.ceiling_refusal(c, admin, org.UNIT, cohort, "A5", TODAY, {"K8": "auto"}) is None


def test_autonomy_level_show_is_for_the_person_a_lead_above_them_and_an_admin():
    c = chart()
    lena, ivan, otto = org.Actor(100, "lena", False), org.Actor(101, "ivan", False), org.Actor(102, "otto", False)
    assert org.view_refusal(c, ivan, 101, "ivan", TODAY) is None, "their own"
    assert org.view_refusal(c, lena, 101, "ivan", TODAY) is None, "a lead of a unit above them"
    assert "lena is none" in org.view_refusal(c, lena, 102, "otto", TODAY), "ops is not below lab"
    assert "ivan is none" in org.view_refusal(c, ivan, 100, "lena", TODAY)
    assert org.view_refusal(c, otto, 999, "nobody", TODAY) is not None, "a person without a seat lies at the root"
    assert org.view_refusal(c, org.Actor(1, "octo-admin", True), 100, "lena", TODAY) is None


# On the hub

if pg.DSN:
    from psycopg import errors
    from sqlalchemy import insert, select

    from evo_agents.hub import tables
    from tests.hub import live
    from tests.hub.live import sql
    from tests.hub.test_chats import OPEN_SINKS
    from tests.hub.test_migrate import ALL, columns, move_to
    from tests.hub.test_orgchart import call, hub, lab_with_lead, user_id  # noqa: F401 (hub is a fixture)
    from tests.hub.test_plans import PROJECT, registration


def api(hub, who: str, method: str, path: str, body: dict | None = None, status: int = 200) -> dict:
    response = hub.client.request(method, f"/v1/autonomy{path}", json=body, headers=hub.headers[who])
    assert response.status_code == status, response.text
    return response.json()


def autonomy_audit(db) -> list[tuple]:
    a, u = tables.audit, tables.users
    query = (
        select(a.c.action, a.c.target, u.c.login, a.c.project_id)
        .join_from(a, u, u.c.id == a.c.actor_id)
        .where(a.c.action.like("autonomy.%"))
        .order_by(a.c.id)
    )
    return sql(db, query)


def modes(view: dict) -> dict:
    return {klass: (held["mode"], held["source"]) for klass, held in view["classes"].items()}


@needs_pg
def test_autonomy_level_pref_migration_0100_adds_the_ceiling_columns_and_autonomy_prefs_and_going_back_drops_them(
    hub_db,
):
    move_to(hub_db, "0099", down=True)
    assert not columns(hub_db, "autonomy_prefs")
    move_to(hub_db, "0100")
    assert {"overrides", "environments", "workers"} <= columns(hub_db, "autonomy_ceilings")
    assert columns(hub_db, "autonomy_prefs") == {"user_id", "revision", "level", "overrides", "created_at"}
    c, p = tables.autonomy_ceilings, tables.autonomy_prefs
    assert sql(hub_db, select(c.c.level, c.c.overrides, c.c.environments, c.c.workers)) == [("A5", {}, [], [])]
    person = sql(hub_db, insert(tables.users).values(login="northwind-dev").returning(tables.users.c.id))[0][0]
    sql(hub_db, insert(p).values(user_id=person, revision=1, level="A2", overrides={"K6": "deny"}))
    root = sql(hub_db, select(c.c.scope_id))[0][0]
    ceiling = {"scope": "unit", "scope_id": root, "level": "A4"}
    for bad in (
        insert(p).values(user_id=person, revision=2, level="A9"),
        insert(p).values(user_id=person, revision=0, level="A2"),
        insert(p).values(user_id=person, revision=3, level="A2", overrides=["K6"]),
        insert(p).values(user_id=person, revision=1, level="A3"),  # one row a revision
        insert(c).values(**ceiling, revision=2, overrides=["K6"]),
        insert(c).values(**ceiling, revision=2, environments=[f"e{n}" for n in range(101)]),
        insert(c).values(**ceiling, revision=2, workers=[f"w{n}" for n in range(101)]),
    ):
        with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
            sql(hub_db, bad)
    move_to(hub_db, "0099", down=True)
    assert not columns(hub_db, "autonomy_prefs")
    assert not {"overrides", "environments", "workers"} & columns(hub_db, "autonomy_ceilings")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0100") :]


@needs_pg
def test_autonomy_level_pref_is_kept_in_revisions_each_audited_and_a5_without_one(hub, hub_db):
    shown = api(hub, "mia", "GET", "")
    assert (shown["principal"], shown["project"], shown["agent"], shown["level"]) == ("mia", None, None, "A5")
    assert set(modes(shown).values()) == {("auto", "pref:mia")} and shown["classes"]["K1"]["detail"] == (
        "level A5 of pref:mia (no preference set)"
    )
    assert (shown["environments"], shown["workers"]) == (None, None)
    first = api(hub, "mia", "PUT", "/prefs", {"level": "A2"})
    assert first == {"login": "mia", "revision": 1, "level": "A2", "previous": None, "classes": {}}
    assert modes(api(hub, "mia", "GET", ""))["K6"] == ("ask", "pref:mia")
    second = api(hub, "mia", "PUT", "/prefs", {"classes": {"K6": "deny"}})
    assert (second["revision"], second["level"], second["classes"]) == (2, "A2", {"K6": "deny"}), "the level stays"
    third = api(hub, "mia", "PUT", "/prefs", {"classes": {"K10": "deny"}})
    assert third["classes"] == {"K6": "deny", "K10": "deny"}, "merged onto those before"
    assert modes(api(hub, "mia", "GET", ""))["K6"] == ("deny", "pref:mia")
    reset = api(hub, "mia", "PUT", "/prefs", {"reset": True})
    assert (reset["revision"], reset["level"], reset["previous"], reset["classes"]) == (4, "A5", "A2", {})
    assert "never takes ask" in api(hub, "mia", "PUT", "/prefs", {"classes": {"K1": "ask"}}, status=422)["message"]
    assert "nothing changed" in api(hub, "mia", "PUT", "/prefs", {}, status=422)["message"]
    p = tables.autonomy_prefs
    kept = sql(hub_db, select(p.c.revision, p.c.level, p.c.overrides).order_by(p.c.revision))
    assert kept == [(1, "A2", {}), (2, "A2", {"K6": "deny"}), (3, "A2", {"K6": "deny", "K10": "deny"}), (4, "A5", {})]
    assert [(action, target, login) for action, target, login, _ in autonomy_audit(hub_db)] == [
        ("autonomy.pref_set", "pref mia level=none->A2 revision=1", "mia"),
        ("autonomy.pref_set", "pref mia level=A2->A2 K6=deny revision=2", "mia"),
        ("autonomy.pref_set", "pref mia level=A2->A2 K10=deny revision=3", "mia"),
        ("autonomy.pref_set", "pref mia level=A2->A5 reset revision=4", "mia"),
    ]


@needs_pg
def test_autonomy_level_pref_above_the_ceiling_is_stored_and_the_ceiling_decides(hub, hub_db):
    lab_with_lead(hub)
    call(hub, "admin", "PUT", "/ceilings", {"unit": "lab", "level": "A3", "note": "a new team"})
    stored = api(hub, "lena", "PUT", "/prefs", {"level": "A5", "classes": {"K12": "auto"}})
    assert (stored["level"], stored["classes"]) == ("A5", {"K12": "auto"})
    shown = api(hub, "lena", "GET", "")
    assert shown["level"] == "A3" and modes(shown)["K12"] == ("deny", "unit:lab")
    p = tables.autonomy_prefs
    assert sql(hub_db, select(p.c.level).where(p.c.user_id == user_id(hub_db, "lena"))) == [("A5",)]


@needs_pg
def test_autonomy_level_ceiling_takes_classes_environments_and_workers_without_escalation(hub, hub_db):
    lab_with_lead(hub)
    limits = {"classes": {"K6": "ask"}, "environments": ["host", "docker/py", "host"], "workers": ["w1", "w2"]}
    lab = call(hub, "admin", "PUT", "/ceilings", {"unit": "lab", "level": "A4", "note": "n", **limits})
    assert (lab["classes"], lab["environments"], lab["workers"]) == ({"K6": "ask"}, ["docker/py", "host"], ["w1", "w2"])
    cohort = {"unit": "intern-2026", "note": "interns"}
    refused = call(hub, "lena", "PUT", "/ceilings", {**cohort, "level": "A3"}, 403)["message"]
    assert (
        refused == "lena may not set a ceiling where K6=notify lies above their own effective mode ask (from unit:lab)"
    )
    assert (
        "never takes ask"
        in call(hub, "lena", "PUT", "/ceilings", {**cohort, "level": "A1", "classes": {"K1": "ask"}}, 422)["message"]
    )
    assert call(hub, "lena", "PUT", "/ceilings", {**cohort, "level": "A3", "environments": ["bad name"]}, 422)
    written = call(
        hub, "lena", "PUT", "/ceilings", {**cohort, "level": "A3", "classes": {"K6": "ask"}, "environments": ["host"]}
    )
    assert (written["level"], written["previous"], written["environments"], written["workers"]) == (
        "A3",
        "A1",
        ["host"],
        [],
    )
    intern = {"login": "ivan", "unit": "intern-2026", "role": "intern", "reports_to": "lena"}
    call(hub, "lena", "POST", "/seats", intern, 201)
    ivan = api(hub, "lena", "GET", "?login=ivan")
    assert (ivan["principal"], ivan["level"]) == ("ivan", "A3")
    assert modes(ivan)["K6"] == ("ask", "unit:intern-2026") and modes(ivan)["K12"] == ("deny", "unit:intern-2026")
    assert (ivan["environments"], ivan["workers"]) == (["host"], ["w1", "w2"]), "the intersection of the ceilings"
    # each revision is whole: what it leaves out it drops
    call(hub, "lena", "PUT", "/ceilings", {**cohort, "level": "A2"})
    assert api(hub, "lena", "GET", "?login=ivan")["environments"] == ["docker/py", "host"]
    rows = [(action, target, login) for action, target, login, _ in autonomy_audit(hub_db)]
    assert rows[0] == ("autonomy.ceiling_set", "ceiling unit:intern-2026 level=none->A1 revision=1", "lena")
    assert rows[1] == (
        "autonomy.ceiling_set",
        "ceiling unit:lab level=none->A4 K6=ask environments=docker/py,host workers=w1,w2 revision=1",
        live.ADMIN,
    )
    assert rows[2] == (
        "autonomy.ceiling_set",
        "ceiling unit:intern-2026 level=A1->A3 K6=ask environments=host revision=2",
        "lena",
    )
    assert rows[3] == ("autonomy.ceiling_set", "ceiling unit:intern-2026 level=A3->A2 revision=3", "lena")
    assert len(rows) == 4


@needs_pg
def test_autonomy_level_show_to_the_person_a_lead_above_them_and_an_admin_and_403_to_anyone_else(hub):
    lab_with_lead(hub)
    call(
        hub,
        "lena",
        "POST",
        "/seats",
        {"login": "ivan", "unit": "intern-2026", "role": "intern", "reports_to": "lena"},
        201,
    )
    assert api(hub, "ivan", "GET", "")["principal"] == "ivan"
    assert api(hub, "lena", "GET", "?login=IVAN")["principal"] == "ivan", "a lead of a unit above him"
    assert api(hub, "admin", "GET", "?login=lena")["principal"] == "lena"
    assert "lena is none" in api(hub, "lena", "GET", "?login=mia", status=403)["message"], "mia is at the root"
    assert api(hub, "ivan", "GET", "?login=lena", status=403)
    assert api(hub, "mia", "GET", "?login=ivan", status=403)
    assert api(hub, "admin", "GET", "?login=ghost", status=404)
    assert "name the project" in api(hub, "mia", "GET", "?agent=bot", status=422)["message"]


@needs_pg
def test_autonomy_level_effective_of_an_agent_on_a_repo_under_the_project_policy(hub):
    lab_with_lead(hub)
    call(hub, "admin", "PUT", "/ceilings", {"unit": "lab", "level": "A4", "note": "n", "workers": ["w1"]})
    hub.client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=hub.headers["admin"])
    agent_seat = {"agent": PROJECT, "project": PROJECT, "unit": "lab", "role": "agent", "sponsor": "lena"}
    seat = call(hub, "admin", "POST", "/seats", agent_seat, 201)
    call(hub, "admin", "PUT", "/ceilings", {"seat": seat["id"], "level": "A3", "note": "new agent"})
    query = f"?login=mia&project={PROJECT}&agent={PROJECT}&repo={PROJECT}"
    shown = api(hub, "admin", "GET", query)
    assert (shown["principal"], shown["project"], shown["agent"]) == ("mia", PROJECT, PROJECT)
    assert shown["level"] == "A3", "the agent's seat bounds the person running it"
    assert modes(shown)["K5"] == ("ask", f"policy:{PROJECT}/{PROJECT}"), "the repo's mode is off by default"
    assert modes(shown)["K6"] == ("notify", f"seat:{seat['id']}")
    assert (shown["environments"], shown["workers"]) == (None, ["w1"])
    assert api(hub, "admin", "GET", f"?project={PROJECT}&repo=ghost", status=404)
    assert api(hub, "admin", "GET", f"?project={PROJECT}&agent=ghost", status=404)
    assert api(hub, "mia", "GET", f"?project={PROJECT}", status=404), "no grant, no project"
