"""The hub's side of a run dispatched with browser (plan agent-browser, criterion a3): only a worker whose heartbeat
lists the feature browser claims one, never more at once than the max its heartbeat reports; a run without browser
claims as before on any worker, an old one included, and its claim leaves the field out (criterion a10); a dispatch
pinned to a worker without the feature is refused; the heartbeat keeps the worker's report; a rerun keeps the browser
of the run it reruns; and the CLI's --browser sends it."""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import runs, tables
from evo_agents.hub.server.runs.service.browsers import NO_BROWSER
from tests.hub import test_run_cli
from tests.hub.live import sql
from tests.hub.test_run_cli import as_json, runs_of
from tests.hub.test_runs import (
    PLAN,
    PROJECT,
    add_worker,
    beat,
    claim,
    client,  # noqa: F401 (a fixture)
    control,
    dispatch,
    dispatched,
    hub,  # noqa: F401 (a fixture)
    moved,
    of_run,
)

cli_hub = test_run_cli.hub  # the hub under uvicorn, with homes signed in to it
REPORT = {"chromium": "153.0.8010.12", "display": "headless", "max": 2, "sandbox": True}


def browser_beat(client, worker: dict, report=REPORT, features=runs.WORKER_FEATURES, **extra) -> dict:
    """A heartbeat of ``worker`` that lists ``features`` and reports its Chromium (``report``; None for none)."""
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": extra.get("free_slots", 1),
        "runs": list(extra.get("runs_held", ())),
        "features": list(features),
        **({} if report is None else {"browser": report}),
    }
    return client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])


def add_browser_worker(client, headers, name: str, *, report=REPORT, **extra) -> dict:
    """A worker of the member ``headers`` names whose daemon ran `evo-agents worker browser install`: its heartbeat
    lists the feature browser and reports ``report``."""
    worker = add_worker(client, headers, name, **extra)
    answered = browser_beat(client, worker, report)
    assert answered.status_code == 200, answered.text
    return worker


def browser_of(db, worker: dict):
    return sql(db, select(tables.workers.c.browser).where(tables.workers.c.id == worker["id"]))[0][0]


def test_browser_claim_goes_only_to_a_worker_of_the_feature_and_never_past_its_max(client, hub, hub_db):
    plain = add_worker(client, hub["owner"], "plain", slots=3)
    lab = add_browser_worker(client, hub["owner"], "lab", slots=3)
    queued = dispatched(client, hub["owner"], [2, 4, 5], browser=True)
    assert all(run["browser"] is True for run in queued)
    assert claim(client, plain) is None  # no feature browser: none of them, slots or not

    first, second = claim(client, lab), claim(client, lab)
    assert first["browser"] is second["browser"] is True
    assert claim(client, lab) is None  # two browsers held, its max: the third waits
    assert claim(client, plain) is None
    assert control(client, hub["owner"], first["id"], "cancel").status_code == 200
    moved(client, lab, first["id"], "cancelled")  # the browser of the first went with it
    third = claim(client, lab)
    assert third["browser"] is True and {first["id"], second["id"], third["id"]} == {run["id"] for run in queued}


def test_browser_claim_of_a_run_without_browser_goes_to_any_worker_as_before(client, hub, hub_db):
    old = add_worker(client, hub["owner"], "old")  # a daemon before the feature: its heartbeat names none
    (run,) = dispatched(client, hub["owner"], [4])
    assert run["browser"] is False
    claimed = claim(client, old)
    assert claimed["id"] == run["id"] and "browser" not in claimed  # the claim of an old daemon is as it was
    assert of_run(hub_db, run["id"], "browser") == [(False,)]

    lab = add_browser_worker(client, hub["owner"], "lab", report={**REPORT, "max": 1})
    (other,) = dispatched(client, hub["owner"], [5])
    held = claim(client, lab)
    assert held["id"] == other["id"] and "browser" not in held
    (browser_run,) = dispatched(client, hub["owner"], [2], browser=True)
    assert claim(client, lab) is None  # its one slot is the run without browser's
    assert control(client, hub["owner"], other["id"], "cancel").status_code == 200
    moved(client, lab, other["id"], "cancelled")
    assert claim(client, lab)["id"] == browser_run["id"]


def test_browser_claim_a_dispatch_pinned_to_a_worker_without_the_feature_is_refused(client, hub, hub_db):
    plain = add_worker(client, hub["owner"], "plain")
    refused = dispatch(client, hub["owner"], [4], worker_id=plain["id"], browser=True)
    assert refused.status_code == 409 and NO_BROWSER in refused.json()["message"]
    plan_run = client.post(
        f"/v1/projects/{PROJECT}/plan-runs",
        json={"plan_id": PLAN, "worker_id": plain["id"], "browser": True},
        headers=hub["owner"],
    )
    assert plan_run.status_code == 409 and NO_BROWSER in plan_run.json()["message"]
    assert sql(hub_db, select(tables.runs.c.id)) == []
    (without,) = dispatched(client, hub["owner"], [4], worker_id=plain["id"])  # without browser, as before
    assert without["browser"] is False

    lab = add_browser_worker(client, hub["owner"], "lab")
    (pinned,) = dispatched(client, hub["owner"], [5], worker_id=lab["id"], browser=True)
    assert claim(client, lab)["id"] == pinned["id"]
    assert claim(client, plain)["id"] == without["id"]


def test_browser_claim_of_a_plan_run_with_browser(client, hub, hub_db):
    plain = add_worker(client, hub["owner"], "plain")
    lab = add_browser_worker(client, hub["owner"], "lab")
    response = client.post(
        f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN, "browser": True}, headers=hub["owner"]
    )
    assert response.status_code == 201, response.text
    plan_run = response.json()
    assert (plan_run["kind"], plan_run["browser"]) == ("plan", True)
    assert claim(client, plain) is None
    claimed = claim(client, lab)
    assert (claimed["id"], claimed["browser"]) == (plan_run["id"], True)


def test_browser_claim_heartbeat_keeps_the_report_with_the_feature_and_clears_it_without(client, hub, hub_db):
    lab = add_browser_worker(client, hub["owner"], "lab")
    assert browser_of(hub_db, lab) == REPORT
    listed = {w["name"]: w for w in client.get("/v1/workers", headers=hub["owner"]).json()}
    assert listed["lab"]["browser"] == REPORT and "browser" in listed["lab"]["features"]

    without = [name for name in runs.WORKER_FEATURES if name != runs.BROWSER]
    assert browser_beat(client, lab, REPORT, features=without).status_code == 200  # a report without the feature
    assert browser_of(hub_db, lab) is None
    assert browser_beat(client, lab, {**REPORT, "display": "screen", "sandbox": False}).status_code == 200
    assert browser_of(hub_db, lab)["display"] == "screen"
    assert browser_beat(client, lab, None).status_code == 200  # the daemon lost its Chromium
    assert browser_of(hub_db, lab) is None
    beat(client, lab)  # and a daemon before the feature says nothing of it
    assert browser_of(hub_db, lab) is None

    for wrong in ({**REPORT, "max": 0}, {**REPORT, "max": runs.MAX_BROWSERS + 1}, {**REPORT, "chromium": "x; y"}):
        assert browser_beat(client, lab, wrong).status_code == 422
    assert browser_beat(client, lab, {**REPORT, "display": "tv"}).status_code == 422


def test_browser_claim_a_rerun_keeps_the_browser_of_the_run_unless_it_asks(client, hub, hub_db):
    lab = add_browser_worker(client, hub["owner"], "lab")
    (run,) = dispatched(client, hub["owner"], [4], browser=True)
    (plain,) = dispatched(client, hub["owner"], [5])
    for queued in (run, plain):
        assert control(client, hub["owner"], queued["id"], "cancel").status_code == 200
    kept = control(client, hub["owner"], run["id"], "rerun")
    assert kept.status_code == 201 and kept.json()["browser"] is True
    assert control(client, hub["owner"], kept.json()["id"], "cancel").status_code == 200
    path = f"/v1/projects/{PROJECT}/runs/{kept.json()['id']}/rerun"
    dropped = client.post(path, json={"browser": False}, headers=hub["owner"])
    assert dropped.status_code == 201 and dropped.json()["browser"] is False
    assert control(client, hub["owner"], dropped.json()["id"], "cancel").status_code == 200
    asked = client.post(
        f"/v1/projects/{PROJECT}/runs/{plain['id']}/rerun", json={"browser": True}, headers=hub["owner"]
    )
    assert asked.status_code == 201 and asked.json()["browser"] is True
    assert claim(client, lab)["id"] == asked.json()["id"]


def test_browser_claim_cli_sends_browser_with_the_flag_alone(cli_hub, monkeypatch, capsys):
    add_browser_worker(cli_hub.client, cli_hub.headers["owner"], "mac-mini")
    asked = as_json(runs_of(cli_hub, monkeypatch, capsys, "owner", "dispatch", PLAN, "4", "--browser", "--json"))
    plain = as_json(runs_of(cli_hub, monkeypatch, capsys, "owner", "dispatch", PLAN, "5", "--json"))
    assert [run["browser"] for run in asked + plain] == [True, False]
    for run in asked + plain:
        as_json(runs_of(cli_hub, monkeypatch, capsys, "owner", "cancel", str(run["id"]), "--json"))
    rerun = as_json(runs_of(cli_hub, monkeypatch, capsys, "owner", "rerun", str(plain[0]["id"]), "--browser", "--json"))
    kept = as_json(runs_of(cli_hub, monkeypatch, capsys, "owner", "rerun", str(asked[0]["id"]), "--json"))
    assert (rerun["browser"], kept["browser"]) == (True, True)
    for run in (rerun, kept):
        as_json(runs_of(cli_hub, monkeypatch, capsys, "owner", "cancel", str(run["id"]), "--json"))
    planned = as_json(runs_of(cli_hub, monkeypatch, capsys, "owner", "plan", PLAN, "--browser", "--json"))
    assert (planned["kind"], planned["browser"]) == ("plan", True)
