"""An accepted proposal of tier 2 becomes the Curator's plan as one of tier 1 does, and POST
/v1/projects/{p}/curator/proposals/{id}/plan makes the Curator's plan of an accepted proposal that has no change yet
(``evo_agents.hub.server.proposal_plans``): criteria a4 and a5 of the plan accepted-proposals-move-on.

The night shift builds a tier 2 change, the Judge judges it and its pull request stays open for its owner: the hub
merges tier 0 alone, and the charter's auto_merge takes 0 alone. Accepting a tier 3 proposal records the answer alone. A
tier 2 change that signs of score hacking, or the files its diff really touches, put at tier 3 stops as tier 1 does.
The world, GitHub and its helpers are ``tests.hub.test_curator_changes``'s."""

from __future__ import annotations

import json

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select

from evo_agents.hub import judge, tables
from evo_agents.hub.curator_cli import CHANGE_KEYS
from evo_agents.hub.server.app import create_app
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_credentials_api import MINE, app_key  # noqa: F401 (app_key is a fixture)
from tests.hub.test_curator import NIGHT, PROJECT, fire, put_charter
from tests.hub.test_curator_changes import (  # noqa: F401 (world is a fixture)
    BRANCH,
    CLEAN,
    GREEN,
    HEAD,
    PLAN_ID,
    SUCCESS,
    accepted,
    builder,
    calls,
    changes,
    curator_charter,
    judged,
    moved_on,
    one_change,
    path,
    proposal_of,
    protect,
    review_run,
    world,
)
from tests.hub.test_curator_ledger import acts, ledger_of
from tests.hub.test_review_runs import DRAFT, answer, answered, proposed
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials
from tests.hub.test_run_stream import serving
from tests.hub.test_runs import moved

WAIT_TEST = [{"repo": "evo-agents", "path": "tests/test_wait.py"}]
FEATURE = {"kind": "feature", "paths": WAIT_TEST}  # tier 2, from its kind alone
CI_CHANGE = {"kind": "ci_change", "paths": [{"repo": "evo-agents", "path": ".github/workflows/ci.yml"}]}  # tier 3
TWO_REPOS = {"kind": "feature", "paths": [*WAIT_TEST, {"repo": "lumen-identity", "path": "tests/test_x.py"}]}


def audited_plans(world) -> list[str]:  # noqa: F811
    a = tables.audit
    return [row[0] for row in sql(world.db, select(a.c.target).where(a.c.action == "curator.plan").order_by(a.c.id))]


def answered_all(world, *proposals: tuple[dict, str]) -> list[dict]:  # noqa: F811
    """One review run's proposals, each ``(body, action)``, recorded, the run ended, and each answered by owner; the
    proposals as answered."""
    run_id = review_run(world)
    evidence = [{"kind": "run", "run_id": run_id, "seq": 1}]
    made = [
        proposed(world.client, world.worker, run_id, title=f"change {n}", evidence=evidence, plan=DRAFT, **body)
        for n, (body, _) in enumerate(proposals)
    ]
    moved(world.client, world.worker, run_id, "verifying", "done")
    return [
        answered(world.client, world.headers["owner"], item["id"], action)
        for item, (_, action) in zip(made, proposals, strict=True)
    ]


def plan_of(world, proposal_id: int, headers=None):  # noqa: F811
    return world.client.post(path("proposals", str(proposal_id), "plan"), headers=headers or world.headers["owner"])


# a4: accepting a tier 2 proposal makes its plan, as tier 1


def test_curator_plan_an_accepted_tier_2_proposal_becomes_a_plan_as_tier_1_does(world):  # noqa: F811
    proposal = accepted(world, **FEATURE)
    assert (proposal["state"], proposal["tier"]) == ("accepted", 2)
    change = one_change(world)
    assert {key: change[key] for key in ("proposal_id", "plan_id", "repo", "branch", "forge", "tier", "state")} == {
        "proposal_id": proposal["id"],
        "plan_id": PLAN_ID,
        "repo": "evo-agents",
        "branch": BRANCH,
        "forge": "github",
        "tier": 2,
        "state": "planned",
    }
    plan = world.client.get(f"/v1/projects/{PROJECT}/plans/{PLAN_ID}", headers=world.headers["owner"]).json()
    assert plan["body"]["repos"] == [{"repo": "evo-agents", "branch": BRANCH}]
    assert acts(ledger_of(world, proposal["id"]))[-2:] == [("accepted", "user"), ("planned", "curator")]
    assert audited_plans(world) == [f"{PROJECT}/{PLAN_ID} proposal:1 change:1 branch={BRANCH}"]


def test_curator_plan_a_tier_3_proposal_accepted_records_the_answer_alone(world):  # noqa: F811
    proposal = accepted(world, **CI_CHANGE)
    assert (proposal["state"], proposal["tier"]) == ("accepted", 3)
    assert changes(world) == [] and audited_plans(world) == []
    assert acts(ledger_of(world, proposal["id"]))[-1] == ("accepted", "user")


def test_curator_plan_a_tier_2_change_is_built_judged_and_left_open_for_its_owner(world):  # noqa: F811
    accepted(world, **FEATURE)
    builder(world)
    assert moved_on(world) == {"opened": 1}
    change = judged(world)
    assert (change["passed"], change["tier"]) == (True, 2)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)  # every condition of a merge but its tier holds
    found = moved_on(world)
    assert found.get("open") == 1 and "merged" not in found, found
    change = one_change(world)
    assert (change["state"], change["reason"]) == ("open", "tier 2 waits for its owner to merge it")
    assert world.github.merges == [] and calls(world.github, "PUT", "/merge") == []
    (check,) = calls(world.github, "POST", "/check-runs")  # the Judge's check run is there for its owner
    assert json.loads(check.body)["conclusion"] == "success"


def test_curator_plan_auto_merge_still_takes_tier_0_alone(world):  # noqa: F811
    refused = put_charter(world.client, world.headers["owner"], curator_charter(auto_merge=[0, 2]))
    assert refused.status_code == 422, refused.text


SIGNED = [*CLEAN, {"filename": "curator.yaml", "status": "modified", "patch": "@@ -1 +1 @@\n-a: 1\n+a: 2\n"}]
STOPS = [
    ("signs", "the Judge did not pass it: the diff shows signs of score hacking: protected_path"),
    ("retier", "tier 3 waits for its owner to merge it"),
]


@pytest.mark.parametrize("raised_by, why", STOPS)
def test_curator_plan_a_tier_2_change_raised_to_tier_3_stops_as_tier_1_does(world, raised_by, why):  # noqa: F811
    proposal = accepted(world, **FEATURE)
    if raised_by == "signs":  # its pull request touches a protected path of the charter: the hub fails it at once
        builder(world, SIGNED)
        assert moved_on(world) == {"signs": 1}
        world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
        assert moved_on(world) == {"checked": 1, "open": 1}
        assert "signs of score hacking" in proposal_of(world, proposal["id"])["tier_reasons"][-1]
    else:  # the diff the Judge read touches the configuration of tests: tier 3, though the Judge passed it
        builder(world)
        assert moved_on(world) == {"opened": 1}
        change = judged(world, paths=["tests/test_wait.py", "pytest.ini"])
        assert (change["passed"], change["tier"]) == (True, 3)
        world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
        found = moved_on(world)
        assert found.get("open") == 1 and "merged" not in found, found
        reasons = proposal_of(world, proposal["id"])["tier_reasons"]
        assert reasons[-1] == "the files the change really touches give it tier 3, not 2"
    change = one_change(world)
    assert (change["state"], change["tier"]) == ("open", 3) and why in change["reason"], change
    assert proposal_of(world, proposal["id"])["tier"] == 3
    assert world.github.merges == [] and calls(world.github, "PUT", "/merge") == []
    assert fire(world.client, NIGHT).get("builder") is None  # nothing of it is built again


# a5: POST .../proposals/{id}/plan for a proposal accepted without a change


def test_curator_plan_route_makes_an_accepted_proposal_without_a_change_the_curators_plan(world, monkeypatch):  # noqa: F811
    with monkeypatch.context() as patched:  # accepted while tier 2 still recorded the answer alone
        patched.setattr(judge, "PLANNED_TIERS", (0, 1))
        (old,) = answered_all(world, (FEATURE, "accept"))
    assert changes(world) == []

    assert plan_of(world, old["id"], world.headers["other"]).status_code == 403  # a writer, not an admin
    worker_token = {"Authorization": world.worker["headers"]["Authorization"]}
    assert plan_of(world, old["id"], worker_token).status_code == 403
    assert plan_of(world, 999).status_code == 404
    assert changes(world) == [] and audited_plans(world) == []

    made = plan_of(world, old["id"])
    assert made.status_code == 201, made.text
    change = made.json()
    assert set(change) == set(CHANGE_KEYS)
    assert {key: change[key] for key in ("proposal_id", "plan_id", "branch", "tier", "state", "reason")} == {
        "proposal_id": old["id"],
        "plan_id": PLAN_ID,
        "branch": BRANCH,
        "tier": 2,
        "state": "planned",
        "reason": None,
    }
    assert one_change(world) == change
    assert acts(ledger_of(world, old["id"]))[-2:] == [("accepted", "user"), ("planned", "curator")]
    assert audited_plans(world) == [f"{PROJECT}/{PLAN_ID} proposal:1 change:1 branch={BRANCH}"]

    again = plan_of(world, old["id"])
    assert again.status_code == 409 and "has change #1 already, planned" in again.text, again.text
    assert len(changes(world)) == 1 and len(audited_plans(world)) == 1

    assert protect(world).json()["protected"] is True
    assert fire(world.client, NIGHT)["builder"] == 1  # the night shift builds it as any planned change


def test_curator_plan_route_refuses_a_proposal_not_accepted_or_of_tier_3(world, monkeypatch):  # noqa: F811
    with monkeypatch.context() as patched:
        patched.setattr(judge, "PLANNED_TIERS", (0, 1))
        deferred, top, wide = answered_all(world, (FEATURE, "defer"), (CI_CHANGE, "accept"), (TWO_REPOS, "accept"))
    assert (deferred["state"], top["tier"], wide["tier"]) == ("deferred", 3, 2)

    refused = plan_of(world, deferred["id"])
    assert refused.status_code == 409 and "is deferred: only an accepted proposal becomes" in refused.text
    refused = plan_of(world, top["id"])
    assert refused.status_code == 409, refused.text
    assert (
        "is tier 3: the night shift builds tiers 0 to 2 alone, and its owner makes a change of tier 3" in refused.text
    )
    assert changes(world) == [] and audited_plans(world) == []

    left = plan_of(world, wide["id"])  # a draft that cannot become a plan: its change is left open, with why
    assert left.status_code == 201, left.text
    change = left.json()
    assert (change["state"], change["plan_id"], change["branch"]) == ("open", None, None)
    assert "works in one repo, and the proposal names evo-agents, lumen-identity" in change["reason"]
    assert acts(ledger_of(world, wide["id"]))[-1] == ("left_open", "curator")
    assert audited_plans(world) == [f"{PROJECT}/no plan proposal:{wide['id']} change:{change['id']} branch=-"]
    assert answer(world.client, world.headers["owner"], deferred["id"], "accept").status_code == 200
    assert {item["proposal_id"] for item in changes(world)} == {wide["id"], deferred["id"]}  # accepting it plans it


def test_curator_plan_the_brief_lists_a_change_with_no_plan_to_build_not_to_merge(world):  # noqa: F811
    from tests.hub.test_brief import BRIEF, body_of, briefed, briefs_of

    (wide,) = answered_all(world, (TWO_REPOS, "accept"))
    (change,) = changes(world)
    assert (change["state"], change["plan_id"], change["branch"]) == ("open", None, None)
    assert briefed(world.client, BRIEF) == {"brief": 1}
    body = body_of(world.db)
    assert body["awaiting_merge"] == {"total": 0, "listed": []}  # no branch: nothing to merge
    (planned,) = body["planned_changes"]["listed"]
    assert (planned["id"], planned["plan_id"], planned["repo"]) == (change["id"], None, None)
    (note,) = briefs_of(world.client, world.headers["owner"])
    listed = f"- change #{change['id']} (proposal #{wide['id']}, tier 2): {planned['reason']}"
    assert f"The Curator's changes waiting to be built: 1.\n\n{listed}" in note["body"]
    assert "merge request" not in note["body"] and "None" not in note["body"]


def test_curator_plan_route_on_the_command_line(world, tmp_path, monkeypatch, capsys):  # noqa: F811
    with monkeypatch.context() as patched:
        patched.setattr(judge, "PLANNED_TIERS", (0, 1))
        first, second = answered_all(world, (FEATURE, "accept"), (TWO_REPOS, "accept"))
    with serving(create_app(world.app.state.config)) as url:
        token = world.headers["owner"]["Authorization"].removeprefix("Bearer ")
        home = write_credentials(tmp_path / "owner", url, "owner", token)

        def curator(*args: str):
            return cli(monkeypatch, capsys, home, "hub", "curator", "proposal", *args, "--project", PROJECT)

        found = as_json(curator("plan", str(first["id"]), "--json"))
        assert_json_keys("hub curator proposal plan", found)
        assert (found["proposal_id"], found["state"]) == (first["id"], "planned")
        printed = ok(curator("plan", str(second["id"]))).out
        assert f"Proposal #{second['id']} of {PROJECT} did not become the Curator's plan" in printed
        assert "works in one repo" in printed
        refused = curator("plan", str(first["id"]))
        assert refused.code == 1 and "has change #1 already, planned" in refused.err


# a3: a proposal carries the change accepting it made, which its page shows


def test_curator_plan_a_proposal_carries_the_change_accepting_it_made(world):  # noqa: F811
    deferred, top, wide, made = answered_all(
        world, (FEATURE, "defer"), (CI_CHANGE, "accept"), (TWO_REPOS, "accept"), (FEATURE, "accept")
    )
    listed = {item["proposal_id"]: item for item in changes(world)}
    assert made["change"] == listed[made["id"]]  # the answer carries it at once
    assert (made["change"]["state"], made["change"]["tier"]) == ("planned", 2)
    assert made["change"]["plan_id"].startswith(f"curator-{made['id']}-")
    for who in ("owner", "other"):  # an admin and a writer read it alike
        shown = world.client.get(path("proposals", str(made["id"])), headers=world.headers[who])
        assert shown.status_code == 200 and shown.json()["change"] == listed[made["id"]], shown.text
    left = proposal_of(world, wide["id"])["change"]
    assert (left["state"], left["plan_id"], left["branch"]) == ("open", None, None)
    assert "works in one repo, and the proposal names evo-agents, lumen-identity" in left["reason"]
    assert proposal_of(world, deferred["id"])["change"] is None  # not accepted
    assert top["change"] is None and proposal_of(world, top["id"])["change"] is None  # tier 3 builds nothing

    fire(world.client, NIGHT)  # the night shift passes over it: the repo's ruleset is not checked yet
    waiting = proposal_of(world, made["id"])["change"]
    assert waiting["state"] == "planned"
    assert waiting["reason"].startswith("the hub has not checked the ruleset of repo evo-agents yet"), waiting


def test_curator_plan_proposal_show_prints_its_change(world, tmp_path, monkeypatch, capsys):  # noqa: F811
    made, wide = answered_all(world, (FEATURE, "accept"), (TWO_REPOS, "accept"))
    with serving(create_app(world.app.state.config)) as url:
        token = world.headers["owner"]["Authorization"].removeprefix("Bearer ")
        home = write_credentials(tmp_path / "owner", url, "owner", token)

        def curator(*args: str):
            return cli(monkeypatch, capsys, home, "hub", "curator", "proposal", *args, "--project", PROJECT)

        found = as_json(curator("show", str(made["id"]), "--json"))
        assert_json_keys("hub curator proposal show", found)
        assert set(found["change"]) == set(CHANGE_KEYS)
        printed = ok(curator("show", str(made["id"]))).out
        change = found["change"]
        assert f"#{change['id']}, planned: plan {change['plan_id']} on {change['branch']} of evo-agents" in printed
        printed = ok(curator("show", str(wide["id"]))).out
        assert "open: no plan" in printed and "works in one repo" in printed


def test_curator_plan_route_is_in_the_openapi_document(world):  # noqa: F811
    with TestClient(create_app(world.app.state.config)) as client:
        spec = client.get("/v1/openapi.json").json()
    route = spec["paths"]["/v1/projects/{project}/curator/proposals/{proposal_id}/plan"]["post"]
    assert route["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("/Change")
    assert {"403", "404", "409"} <= set(route["responses"])
