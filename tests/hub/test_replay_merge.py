"""Step 11 of plan history-driven-curator on the hub, criterion a10: a change of the Curator that edits a harness alone
(the files of one skill, an AGENTS.md, a CLAUDE.md) and whose replays on the holdout tasks won merges as tier 0 once
the Judge and CI pass; replays that lost, too few replays, or another file in the diff keep the tier the rules give; a
change left open goes back to judged once its replays win; the merge of a global skill publishes its version from the
merge commit and keeps the one before, which the hub publishes again when the figures get worse; the detector of
score hacking reads the instructions of a harness to test or verify; schema 0058. The world, GitHub and the helpers are
``tests.hub.test_curator_changes``', the suite ``tests.hub.test_replays``'."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

from evo_agents.hub import replay, skills, tables
from evo_agents.hub.blobs import blob_key
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.changes import advance
from tests.hub.live import sql
from tests.hub.test_credentials_api import MINE, app_key  # noqa: F401 (a fixture)
from tests.hub.test_credentials_api import hub_config as credentials_config
from tests.hub.test_curator import NIGHT, PROJECT, fire, grant, written
from tests.hub.test_curator_changes import (  # noqa: F401 (world is a fixture)
    GREEN,
    HEAD,
    RULESET,
    SUCCESS,
    WORKERS_APP,
    accepted,
    builder,
    credential_members,
    credential_worker,
    curator_charter,
    install,
    judged,
    moved_on,
    one_change,
    proposal_of,
    taking,
    world,
)
from tests.hub.test_curator_ledger import acts, blocked, ledger_of, measured, merged_at
from tests.hub.test_migrate import move_to
from tests.hub.test_replays import BASE, arm, built, history_plan, put_history, report
from tests.hub.test_runs import claim, moved

SKILL = "skills/demo/SKILL.md"
SKILL_PATHS = [{"repo": "evo-agents", "path": SKILL}]
SKILL_FILES = [
    {"filename": SKILL, "status": "modified", "patch": "@@ -4,1 +4,1 @@\n-Read the code.\n+Read the docs.\n"}
]
SKILL_TEXT = b"---\nname: demo\ndescription: A demo skill of the project.\n---\nRead the docs.\n"
REPLAYS = {"tasks": 6, "budget_usd": 1.0}


def won(**extra) -> dict:
    """An arm b whose harness is the change's head, cheaper than ``arm(True)`` by a third unless ``extra`` says."""
    return arm(True, **{"harness_sha": HEAD, "harness_paths": [SKILL], "cost_usd": 0.08, **extra})


def owner_id(world) -> int:
    s = tables.schedules
    return sql(world.db, select(s.c.owner_id).where(s.c.kind == "night_shift"))[0][0]


def suite(world, steps: int = 15) -> list[int]:
    """The suite of the history plan built; the ids of its holdout tasks, in order."""
    put_history(world, history_plan(steps))
    assert built(world)["added"] == steps
    t = tables.replay_tasks
    return [row[0] for row in sql(world.db, select(t.c.id).where(t.c.split == "holdout").order_by(t.c.id))]


def replayed(world, tasks: list[int], b: dict, a: dict | None = None) -> None:
    """A replay done on each of ``tasks`` for the one change, arm a as ``a`` (``arm(True)``), arm b as ``b``."""
    change = one_change(world)
    p = tables.projects
    project_id = sql(world.db, select(p.c.id).where(p.c.name == PROJECT))[0][0]
    for task in tasks:
        values = {"project_id": project_id, "change_id": change["id"], "task_id": task, "state": "done"}
        sql(world.db, insert(tables.curator_replays).values(**values, result_a=a or arm(True), result_b=b))


def skill_change(world, files=None, *, kind: str = "skill") -> dict:
    """A proposal of ``kind`` of the skill accepted, built with ``files`` and its pull request opened; the proposal."""
    proposal = accepted(world, kind=kind, paths=SKILL_PATHS)
    assert proposal["tier"] in (1, 2)
    builder(world, SKILL_FILES if files is None else files)
    assert moved_on(world) == {"opened": 1}
    return proposal


def merged_line(world, proposal_id: int) -> dict:
    return next(line for line in ledger_of(world, proposal_id)["lines"] if line["action"] == "merged")


# Tier 0 by replays that won


def test_replay_won_merges_a_skill_change_as_tier_0_and_its_merge_keeps_the_verdict(world):
    tasks = suite(world)
    proposal = skill_change(world)
    replayed(world, tasks[:6], won())
    assert judged(world)["passed"] is True
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "merged": 1}
    change = one_change(world)
    assert (change["state"], change["tier"]) == ("merged", 0)
    assert proposal_of(world, proposal["id"])["tier"] == 1  # the proposal keeps the tier its rules gave
    line = merged_line(world, proposal["id"])
    assert line["details"]["replay"]["won"] is True and line["details"]["replay"]["tasks"] == 6
    assert "its replays won: arm b passed as many of the 6 holdout tasks as arm a (6), with a mean cost" in line["what"]
    assert "release" not in line["details"]  # the hub has no global skill demo


@pytest.mark.parametrize(
    "replays, why",
    [
        (
            6,
            "; its replays did not win: arm b passed as many of the 6 holdout tasks as arm a (6), with neither its mean "
            "cost nor its mean turns 10% lower",
        ),
        (5, "; its replays did not win: 5 holdout task(s) replayed with both arms at the head, of the 6 a win needs"),
        (0, ""),  # no replay: the reason the rules give
    ],
    ids=["lost", "too_few", "missing"],
)
def test_replay_lost_too_few_or_missing_leaves_a_skill_change_open_at_its_tier(world, replays, why):
    tasks = suite(world)
    skill_change(world)
    if replays:
        cheaper = 0.08 if replays == 5 else 0.115  # 0.115 is 4% under arm a's 0.12: too little
        replayed(world, tasks[:replays], won(cost_usd=cheaper))
    judged(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "open": 1}
    change = one_change(world)
    assert (change["state"], change["tier"]) == ("open", 1)
    assert change["reason"] == "tier 1 waits for its owner to merge it" + why
    assert world.github.merges == []


def test_replay_won_on_a_diff_that_also_edits_code_keeps_the_rules_tier(world):
    tasks = suite(world)
    code = {"filename": "evo_agents/hub/notes.py", "status": "modified", "patch": "@@ -1,1 +1,1 @@\n-a = 1\n+a = 2\n"}
    skill_change(world, [*SKILL_FILES, code])
    replayed(world, tasks[:6], won())
    judged(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "open": 1}
    change = one_change(world)
    assert (change["state"], change["tier"], change["reason"]) == ("open", 1, "tier 1 waits for its owner to merge it")
    assert world.github.merges == []


def test_replay_won_after_the_change_was_left_open_judges_it_again_and_merges_it(world):
    tasks = suite(world)
    skill_change(world)
    replayed(world, tasks[:5], won())
    judged(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "open": 1}
    written(world.client, world.headers["owner"], curator_charter(replay=REPLAYS))
    assert fire(world.client, NIGHT).get("replay") == 1
    spec = claim(world.client, world.worker)
    assert spec["kind"] == "replay" and spec["curator"]["replay"]["task"]["id"] == tasks[5]
    moved(world.client, world.worker, spec["id"], "running")
    key = spec["curator"]["replay_key"]
    assert report(world, spec["id"], key, arm="a", base_sha=BASE, result=arm(True)).status_code == 200
    assert one_change(world)["state"] == "open"  # one arm alone decides nothing
    assert report(world, spec["id"], key, arm="b", base_sha=BASE, result=won()).status_code == 200
    change = one_change(world)
    assert change["state"] == "judged" and change["reason"].startswith("its replays won (arm b passed as many")
    shown = world.client.get(
        f"/v1/projects/{PROJECT}/curator/changes/{change['id']}/replay", headers=world.headers["owner"]
    ).json()
    assert shown["verdict"]["won"] is True and shown["verdict"]["tasks"] == 6
    assert moved_on(world) == {"merged": 1}
    assert one_change(world)["state"] == "merged" and world.github.merges == [(MINE, "evo-agents", 1, HEAD, "workers")]


def test_replay_a_judge_that_failed_is_never_judged_again_by_replays(world):
    tasks = suite(world)
    skill_change(world)
    replayed(world, tasks[:5], won())
    judged(world, verdict="fail", reasons="It reads the wrong docs.")
    assert moved_on(world) == {"checked": 1, "open": 1}
    written(world.client, world.headers["owner"], curator_charter(replay=REPLAYS))
    assert fire(world.client, NIGHT).get("replay") is None  # a change the Judge failed is replayed no more
    assert one_change(world)["state"] == "open" and world.github.merges == []


def test_replay_hack_sign_a_skill_change_that_drops_its_test_instruction_fails_before_its_judge(world):
    suite(world)
    proposal = accepted(world, kind="skill", paths=SKILL_PATHS)
    dropped = [
        {"filename": SKILL, "status": "modified", "patch": "@@ -4,2 +4,1 @@\n Read the code.\n-Run the tests.\n"}
    ]
    builder(world, dropped)
    assert moved_on(world) == {"signs": 1}
    change = one_change(world)
    assert change["state"] == "judged" and change["passed"] is False
    assert [sign["kind"] for sign in change["verdict"]["signs"]] == ["instruction_weakened"]
    assert proposal_of(world, proposal["id"])["tier"] == 3


# The global skill the merge releases


@pytest.fixture
def skill_world(request, hub_db, tmp_path, github, app_key, s3):  # noqa: F811
    """``tests.hub.test_curator_changes.world`` with a blob store, the global skill demo at version 1 published from
    evo-agents (or the repo the test's parameter names), and the change's head holding the skill's directory."""
    config = dataclasses.replace(credentials_config(hub_db, tmp_path, github, app_key, curator=True), **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = credential_members(client, github)
        grant(client, headers, "owner", "admin")
        worker = taking(client, credential_worker(client, headers["owner"], "mac-mini"))
        written(client, headers["owner"], curator_charter())
        install(github, "evo-agents", permissions=WORKERS_APP)
        install(github, "evo-agents", app="curator")
        github.add_repo(MINE, "evo-agents", rulesets=[dict(RULESET)])
        found = SimpleNamespace(client=client, headers=headers, worker=worker, github=github, db=hub_db, app=client.app)
        found.s3 = s3
        found.skill_id = global_skill(found, getattr(request, "param", "evo-agents"))
        github.snapshot(
            MINE,
            "evo-agents",
            HEAD,
            {SKILL: SKILL_TEXT, "skills/demo/scripts/run.sh": (b"#!/bin/sh\necho demo\n", "100755"), "README.md": b"x"},
        )
        yield found


def global_skill(world, source_repo: str) -> int:
    """The global skill demo, its version 1 published from ``source_repo`` by the night shift's owner; its id."""
    s, v = tables.skills, tables.skill_versions
    by = owner_id(world)
    (skill_id,) = sql(world.db, insert(s).values(scope="global", name="demo", created_by=by).returning(s.c.id))[0]
    sha = "c" * 64
    values = {"skill_id": skill_id, "version": 1, "name": "demo", "description": "The first one.", "sha256": sha}
    values |= {"size": 10, "r2_key": "blobs/sha256/" + sha, "source_repo": source_repo, "source_commit": "1a2b3c4"}
    sql(world.db, insert(v).values(**values, published_by=by))
    return skill_id


def versions(world) -> list[tuple]:
    v = tables.skill_versions
    query = select(v.c.version, v.c.sha256, v.c.source_repo, v.c.source_commit, v.c.published_by).order_by(v.c.version)
    return sql(world.db, query)


def released(world) -> dict:
    """One pass of the job curator.changes with the hub's blob store."""
    state = world.app.state
    return world.client.portal.call(
        partial(advance, state.engine, state.github_app, state.curator_app, blobs=state.blobs)
    )


def merged_skill(world) -> dict:
    """Blocked commands before, the skill change built, replayed to a win, judged and merged by the hub, which releases
    the skill in the same pass; the change."""
    blocked(world, 1, 3, 1)
    tasks = suite(world)
    skill_change(world)
    replayed(world, tasks[:6], won())
    judged(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert released(world) == {"checked": 1, "merged": 1, "released": 1}
    return one_change(world)


def release_lines(world, proposal_id: int) -> list[dict]:
    return [line for line in ledger_of(world, proposal_id)["lines"] if line["action"] == "skill_release"]


def test_skill_release_publishes_the_global_skill_from_the_merge_commit_and_keeps_the_version_before(skill_world):
    world = skill_world
    change = merged_skill(world)
    merge = change["merge_sha"]
    (first, second) = versions(world)
    assert first[:2] == (1, "c" * 64)  # version 1 stays
    assert second[0] == 2 and second[2:] == ("evo-agents", merge, owner_id(world))
    bundle = skills.read_bundle(world.s3.get(blob_key(second[1])), "demo")
    assert [(entry.path, entry.executable) for entry in bundle.files] == [("SKILL.md", False), ("scripts/run.sh", True)]
    assert bundle.files[0].data == SKILL_TEXT and bundle.description == "A demo skill of the project."
    b = tables.blobs
    assert sql(world.db, select(b.c.project_id, b.c.kind).where(b.c.sha256 == second[1])) == [(None, skills.KIND)]
    line = merged_line(world, change["proposal_id"])
    assert line["details"]["release"] == {"skill": "demo", "directory": "skills/demo", "scope": "global", "version": 1}
    assert "the hub publishes global skill demo from it" in line["what"]
    (made,) = release_lines(world, change["proposal_id"])
    assert (made["actor"], made["commit_sha"]) == ("curator", merge)
    assert made["details"] | {"sha256": None} == {
        "kind": "publish",
        "skill": "demo",
        "scope": "global",
        "sha256": None,
        "previous": 1,
        "version": 2,
        "published": True,
    }
    assert (
        made["what"]
        == f"The hub published version 2 of global skill demo from {merge} of evo-agents; version 1 before it stays"
    )
    a = tables.audit
    assert sql(world.db, select(a.c.target).where(a.c.action == "skill.publish")) == [("skill:global/demo",)]
    assert released(world) == {}  # released once
    listed = world.client.get("/v1/skills/global/demo", headers=world.headers["owner"]).json()
    assert [item["version"] for item in listed["versions"]] == [2, 1] and listed["versions"][0][
        "source_commit"
    ] == merge


def test_skill_release_worse_figures_revert_the_merge_and_publish_the_version_before_again(skill_world):
    world = skill_world
    change = merged_skill(world)
    blocked(world, 10, 3, 4)  # after the merge: four blocked commands a session, from one
    assert measured(world, merged_at(world) + timedelta(days=8)) == {"revert": 1}
    assert [row[:2] for row in versions(world)] == [(1, "c" * 64), (2, versions(world)[1][1]), (3, "c" * 64)]
    assert versions(world)[2][2:4] == ("evo-agents", "1a2b3c4")  # version 1 again, its source as it was
    outcome = ledger_of(world, change["proposal_id"])["lines"]
    assert [action for action, _ in acts({"lines": outcome})][-2:] == ["outcome", "skill_release"]
    assert outcome[-1]["what"] == "The hub published version 1 of global skill demo again as version 3, after 2"
    again = outcome[-1]["details"]
    assert (again["kind"], again["reverted"], again["previous"], again["version"], again["published"]) == (
        "republish",
        2,
        1,
        3,
        True,
    )
    figures = outcome[-2]["figures"]
    assert figures["result"] == "revert" and figures["replay"]["won"] is True and figures["replay"]["tasks"] == 6


def test_skill_release_a_newer_version_since_is_left_as_it_is_after_a_revert(skill_world):
    world = skill_world
    change = merged_skill(world)
    v = tables.skill_versions
    newer = {**dict(zip(("version", "sha256"), (3, "d" * 64), strict=True)), "r2_key": "blobs/sha256/" + "d" * 64}
    sql(
        world.db, insert(v).values(skill_id=world.skill_id, name="demo", size=10, published_by=owner_id(world), **newer)
    )
    blocked(world, 10, 3, 4)
    assert measured(world, merged_at(world) + timedelta(days=8)) == {"revert": 1}
    assert [row[0] for row in versions(world)] == [1, 2, 3]
    last = release_lines(world, change["proposal_id"])[-1]
    assert last["details"]["published"] is False and "no longer the 2 the hub published" in last["details"]["why"]


@pytest.mark.parametrize("skill_world", ["agent-skills"], indirect=True)
def test_skill_release_a_global_skill_from_another_repo_is_not_released(skill_world):
    world = skill_world
    blocked(world, 1, 3, 1)
    tasks = suite(world)
    skill_change(world)
    replayed(world, tasks[:6], won())
    judged(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert released(world) == {"checked": 1, "merged": 1}
    change = one_change(world)
    assert "release" not in merged_line(world, change["proposal_id"])["details"]
    assert release_lines(world, change["proposal_id"]) == [] and [row[0] for row in versions(world)] == [1]


def test_skill_release_a_directory_that_cannot_be_a_bundle_says_why_and_publishes_nothing(skill_world):
    world = skill_world
    world.github.snapshot(MINE, "evo-agents", HEAD, {SKILL: SKILL_TEXT, "skills/demo/link": (b"SKILL.md", "120000")})
    blocked(world, 1, 3, 1)
    tasks = suite(world)
    skill_change(world)
    replayed(world, tasks[:6], won())
    judged(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert released(world) == {"checked": 1, "merged": 1, "release_refused": 1}
    (line,) = release_lines(world, one_change(world)["proposal_id"])
    assert line["details"]["published"] is False and "skills/demo/link is a symbolic link" in line["details"]["why"]
    assert [row[0] for row in versions(world)] == [1] and released(world) == {}


# Schema 0058


def test_replay_schema_0058_takes_the_line_skill_release_and_going_back_deletes_those_lines(skill_world):
    world = skill_world
    change = merged_skill(world)
    lg, p = tables.curator_ledger, tables.projects
    project_id = sql(world.db, select(p.c.id).where(p.c.name == PROJECT))[0][0]
    line = {"project_id": project_id, "proposal_id": change["proposal_id"], "actor": "curator", "what": "x"}
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(world.db, insert(lg).values(**line, action="other"))
    move_to(world.db, "0057", down=True)
    assert sql(world.db, select(func.count()).select_from(lg).where(lg.c.action == "skill_release")) == [(0,)]
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(world.db, insert(lg).values(**line, action="skill_release"))
    move_to(world.db, "0058")
    assert [row[0] for row in versions(world)] == [1, 2]  # the versions published stay
    assert replay.MIN_HOLDOUT == 6
