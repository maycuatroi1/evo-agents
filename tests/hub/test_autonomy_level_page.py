"""What the web's autonomy pages read (``evo_agents.hub.server.autonomy_page`` and the preview of
``autonomy_stops``), plan autonomy-levels step 7: the caller's own settings with the ceiling over their preference,
the classes it lowers, the stop that holds them and the 30-day figures; every seat's mode of each class with the
stops that hold; and the count of runs a stop would cancel, read with the same rights as the stop."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert

from evo_agents.hub import tables
from evo_agents.hub.server import autonomy_ledger
from tests.hub.live import sql
from tests.hub.test_autonomy_level_routing import COHORT, interns
from tests.hub.test_autonomy_level_runs import org, pref, user_id
from tests.hub.test_autonomy_level_stop import stop
from tests.hub.test_runs import OTHER, OWNER, client, dispatched, hub  # noqa: F401 (client and hub are fixtures)


def settings(client, headers) -> dict:
    return org(client, headers, "GET", "/autonomy/settings")


def preview(client, headers, status: int = 200, **params) -> dict:
    response = client.get("/v1/autonomy/stops/preview", params=params, headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def test_autonomy_level_page_settings_without_a_preference_or_a_ceiling_are_a5_from_the_instance(client, hub):
    view = settings(client, hub["owner"])
    assert (view["principal"], view["level"], view["revision"], view["pref_level"]) == (OWNER, "A5", None, None)
    assert view["ceiling"] == "A5" and view["stop"] is None and view["days"] == 30
    k5 = view["classes"]["K5"]
    assert (k5["mode"], k5["ceiling"], k5["pref"], k5["lowered"]) == ("auto", "auto", None, False)
    assert k5["figures"] == {
        "refused": 0,
        "asked": 0,
        "decisions": 0,
        "merged": 0,
        "reverted": 0,
        "rolled_back": None,
    }
    assert view["classes"]["K1"]["figures"] is None
    assert view["classes"]["K1"]["modes"] == ["deny", "notify", "auto"]
    assert view["classes"]["K6"]["modes"] == ["deny", "ask", "notify", "auto"]


def test_autonomy_level_page_settings_name_the_ceiling_over_the_preference_and_the_classes_it_lowers(
    client, hub, hub_db
):
    interns(client, hub)  # owner leads lab
    org(client, hub["admin"], "PUT", "/org/ceilings", {"unit": "lab", "level": "A2"})
    pref(client, hub["owner"], "A4", classes={"K10": "deny"})
    view = settings(client, hub["owner"])
    assert (view["level"], view["level_source"], view["pref_level"], view["revision"]) == ("A2", "unit:lab", "A4", 1)
    assert view["pref_classes"] == {"K10": "deny"}
    assert (view["ceiling"], view["ceiling_source"]) == ("A2", "unit:lab") and "lab" in view["ceiling_detail"]
    k6 = view["classes"]["K6"]  # A4 sets auto, the ceiling A2 ask
    assert (k6["mode"], k6["ceiling"], k6["ceiling_source"], k6["pref"], k6["lowered"]) == (
        "ask",
        "ask",
        "unit:lab",
        "auto",
        True,
    )
    k10 = view["classes"]["K10"]  # the preference is stricter than the ceiling: not lowered
    assert (k10["mode"], k10["source"], k10["lowered"]) == ("deny", f"pref:{OWNER}", False)


def test_autonomy_level_page_figures_count_the_ledger_and_decisions_of_the_persons_runs(client, hub, hub_db):
    run = dispatched(client, hub["owner"], [2])[0]
    owner = user_id(hub_db, OWNER)
    rows = [
        autonomy_ledger.target(run["id"], OWNER, None, "K9", "deny", autonomy_ledger.REFUSED, "force push main"),
        autonomy_ledger.target(run["id"], OWNER, None, "K9", "deny", autonomy_ledger.REFUSED, "rm -rf data"),
        autonomy_ledger.target(run["id"], OWNER, None, "K10", "ask", autonomy_ledger.ASKED, "send mail"),
    ]
    a = tables.audit
    sql(hub_db, insert(a), [{"actor_id": owner, "action": "autonomy.decided", "target": text} for text in rows])
    k9, k10 = (settings(client, hub["owner"])["classes"][klass]["figures"] for klass in ("K9", "K10"))
    assert (k9["refused"], k9["asked"], k10["refused"], k10["asked"]) == (2, 0, 0, 1)
    assert settings(client, hub["other"])["classes"]["K9"]["figures"]["refused"] == 0


def test_autonomy_level_page_settings_show_the_stop_that_holds_and_org_lists_it(client, hub):
    stop(client, hub["owner"], scope="login", login=OWNER, reason="runaway")
    held = settings(client, hub["owner"])["stop"]
    assert (held["scope"], held["target"], held["login"], held["reason"]) == (
        "login",
        f"login:{OWNER}",
        OWNER,
        "runaway",
    )
    assert settings(client, hub["other"])["stop"] is None
    stops = org(client, hub["other"], "GET", "/autonomy/org")["stops"]
    assert [item["target"] for item in stops] == [f"login:{OWNER}"]


def test_autonomy_level_page_org_gives_each_seat_its_mode_of_each_class(client, hub):
    interns(client, hub)
    org(client, hub["admin"], "PUT", "/org/ceilings", {"unit": COHORT, "level": "A1"})
    chart = org(client, hub["other"], "GET", "/org")
    seats = {seat["id"]: seat for seat in org(client, hub["other"], "GET", "/autonomy/org")["seats"]}
    assert set(seats) == {seat["id"] for seat in chart["seats"]}
    (intern,) = [seat for seat in chart["seats"] if seat["unit"] == COHORT]
    k5 = seats[intern["id"]]["classes"]["K5"]
    assert (k5["mode"], k5["source"]) == ("ask", f"unit:{COHORT}")
    (lead,) = [seat for seat in chart["seats"] if seat["unit"] == "lab" and seat["login"] == OWNER]
    assert seats[lead["id"]]["classes"]["K5"]["mode"] == "auto"


def test_autonomy_level_page_preview_counts_the_runs_with_the_rights_of_the_stop(client, hub):
    interns(client, hub)  # owner and the admin lead lab; someone-else is an intern of its cohort
    dispatched(client, hub["other"], [2])
    dispatched(client, hub["other"], [5])
    dispatched(client, hub["owner"], [4])
    assert preview(client, hub["other"], scope="login", login=OTHER) == {
        "target": f"login:{OTHER}",
        "runs": 2,
        "people": 1,
        "holds": False,
    }
    unit = preview(client, hub["owner"], scope="unit", unit="lab")
    assert (unit["runs"], unit["people"]) == (3, 3)
    assert preview(client, hub["admin"], scope="all")["people"] is None
    preview(client, hub["other"], 403, scope="unit", unit="lab")
    preview(client, hub["other"], 403, scope="all")
    preview(client, hub["owner"], 404, scope="login", login="nobody-here")
    preview(client, hub["owner"], 422, scope="unit")
    stop(client, hub["owner"], scope="login", login=OTHER)
    assert preview(client, hub["owner"], scope="login", login=OTHER) == {
        "target": f"login:{OTHER}",
        "runs": 0,
        "people": 1,
        "holds": True,
    }
