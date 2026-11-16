"""A Curator's draft that works in several repos becomes one plan in them, owners of a seam first, and the pull
request of a consumer merges only after its owners'. The tests of the hub (``change_repos``) run on Postgres, with the
world of ``tests.hub.test_curator_changes``: lumen-identity, on GitHub under another account, owns a seam evo-agents
consumes."""

import pytest

from evo_agents.hub import judge
from tests.hub import pg

if pg.DSN:  # the world of the Curator's changes, on Postgres alone
    from sqlalchemy import insert, select, update

    from evo_agents.hub import tables
    from evo_agents.hub.kg_graph_rows import MAIN
    from tests.hub.fake_github import Account
    from tests.hub.live import sql
    from tests.hub.test_credentials_api import OWNER, OWNER_ID, THEIRS
    from tests.hub.test_curator import NIGHT, PROJECT, fire, written
    from tests.hub.test_curator_changes import (  # noqa: F401 (app_key and world are fixtures)
        GREEN,
        HEAD,
        MINE,
        RULESET,
        SUCCESS,
        WORKERS_APP,
        accepted,
        app_key,
        calls,
        changes,
        curator_charter,
        inputs,
        judge_key,
        moved_on,
        path,
        verdict,
        world,
    )
    from tests.hub.test_curator_ledger import acts, ledger_of
    from tests.hub.test_plan_runs import reported
    from tests.hub.test_review_runs import DRAFT as ONE_DRAFT
    from tests.hub.test_runs import claim, moved

on_postgres = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

DRAFT = {
    "id": "shared-flag",
    "steps": [
        {"id": 1, "repo": "lumen-web", "title": "use it", "verify": "cd lumen-web && npm test", "status": "done"},
        {"id": 2, "repo": "lumen-core", "title": "add it", "verify": "pytest -q"},
        {"id": 3, "repo": "lumen-web", "title": "doc it", "depends_on": [1], "acceptance": ["A1"]},
    ],
}
SEAMS = [("lumen-core", "lumen-web")]


def plan(seams=SEAMS, repos=("lumen-web", "lumen-core")):
    return judge.curator_repos_plan(
        DRAFT,
        plan_id=judge.curator_plan_id(61, "shared-flag"),
        repos=list(repos),
        branch=judge.curator_branch(61, "shared-flag"),
        proposal_id=61,
        project="northwind",
        seams=seams,
    )


def test_the_owner_of_a_seam_comes_first_and_its_consumer_depends_on_it():
    body = plan()
    assert body["id"] == "curator-61-shared-flag"
    assert [(item["repo"], item["order"], item["branch"]) for item in body["repos"]] == [
        ("lumen-core", 1, "curator/61-shared-flag"),
        ("lumen-web", 2, "curator/61-shared-flag"),
    ]
    steps = [(s["id"], s["repo"], s["order"], s["depends_on"], s["status"]) for s in body["steps"]]
    assert steps == [
        (2, "lumen-core", 1, [], "pending"),
        (1, "lumen-web", 2, [2], "pending"),
        (3, "lumen-web", 3, [1, 2], "pending"),
    ]
    assert body["steps"][1]["verify"] == "npm test" and body["steps"][2]["acceptance"] == ["a1"]


def test_without_a_seam_the_drafts_order_holds():
    body = plan(seams=[])
    assert [item["repo"] for item in body["repos"]] == ["lumen-web", "lumen-core"]
    assert [(s["id"], s["depends_on"]) for s in body["steps"]] == [(1, []), (3, [1]), (2, [3])]


def test_a_step_in_a_repo_the_plan_does_not_name_is_refused_and_one_repo_is_the_plain_plan():
    with pytest.raises(ValueError, match="not one of"):
        plan(repos=("lumen-core",) * 1 + ("lumen-api",))
    single = judge.curator_repos_plan(
        {"steps": [{"id": 1, "what": "x"}]},
        plan_id="curator-1-x",
        repos=["lumen-core"],
        branch="curator/1-x",
        proposal_id=1,
        project="northwind",
    )
    assert single["repos"] == [{"repo": "lumen-core", "branch": "curator/1-x"}]


def test_a_consumers_pull_request_waits_for_its_owners_merge():
    ordered = judge.repo_order(["lumen-web", "lumen-core"], SEAMS)
    assert ordered == ["lumen-core", "lumen-web"]
    assert judge.merge_waits("lumen-web", {"lumen-core": "judged"}, ordered, SEAMS) == ["lumen-core"]
    assert judge.merge_waits("lumen-web", {"lumen-core": "merged"}, ordered, SEAMS) == []
    assert judge.merge_waits("lumen-core", {}, ordered, SEAMS) == []
    assert judge.repo_order(["a", "b"], [("a", "b"), ("b", "a")]) == ["a", "b"]  # a cycle keeps the draft's order


# On the hub


OWNER_HEAD = "3" * 40  # the head of the change's branch in lumen-identity
TWO_DRAFT = {
    "id": "shared-login",
    "title": "Share the login flag",
    "goal": "Both repos read one login flag.",
    "steps": [
        {
            "id": 1,
            "title": "Read the flag",
            "repo": "evo-agents",
            "what": "evo-agents reads the login flag lumen-identity serves.",
            "verify": "python -m pytest -q tests/test_login.py",
            "acceptance": ["evo-agents reads the flag"],
        },
        {
            "id": 2,
            "title": "Serve the flag",
            "repo": "lumen-identity",
            "what": "lumen-identity serves the login flag.",
            "verify": "python -m pytest -q tests/test_flag.py",
            "acceptance": ["lumen-identity serves the flag"],
        },
    ],
}
TWO_PLAN = "curator-1-shared-login"
TWO_BRANCH = "curator/1-shared-login"
TWO_PATHS = [
    {"repo": "evo-agents", "path": "tests/test_login.py"},
    {"repo": "lumen-identity", "path": "tests/test_flag.py"},
]
OWNER_FILES = [{"filename": "tests/test_flag.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+FLAG = True\n"}]
CONSUMER_FILES = [{"filename": "tests/test_login.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+import flag\n"}]


@pytest.fixture
def seamed(world):  # noqa: F811
    """The world with lumen-identity on GitHub under THEIRS, both Apps installed on it and a ruleset only the workers'
    App bypasses, and the seam login of the project's graph, owned by lumen-identity and consumed by evo-agents."""
    world.github.install(THEIRS, "lumen-identity", app="workers", permissions=WORKERS_APP)
    world.github.install(THEIRS, "lumen-identity", app="curator")
    world.github.collaborate(THEIRS, "lumen-identity", Account(OWNER, OWNER_ID), "write")
    world.github.add_repo(THEIRS, "lumen-identity", rulesets=[dict(RULESET)])
    ((project_id,),) = sql(world.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))
    edge = {"project_id": project_id, "branch_id": MAIN, "src": "seam:login", "props": "{}", "status": "parsed"}
    rows = [
        {**edge, "edge_id": "e1", "rel": "owned_by", "dst": "repo:lumen-identity"},
        {**edge, "edge_id": "e2", "rel": "consumed_by", "dst": "repo:evo-agents"},
    ]
    sql(world.db, insert(tables.kg_edges).values(rows))
    return world


def repo_rows(world) -> list[tuple]:
    cr = tables.curator_change_repos
    query = select(cr.c.repo, cr.c.position, cr.c.depends_on, cr.c.state, cr.c.pr_number).order_by(cr.c.position)
    return [tuple(row) for row in sql(world.db, query)]


def built(world) -> None:
    """The Builder of the change in both repos: queued, claimed, each step done, ended done, each branch pushed."""
    assert fire(world.client, NIGHT)["builder"] == 1
    spec = claim(world.client, world.worker)
    assert spec["plan_id"] == TWO_PLAN and spec["curator"]["role"] == "builder"
    assert [item["repo"] for item in spec["repos"]] == ["lumen-identity", "evo-agents"]
    moved(world.client, world.worker, spec["id"], "running")
    for key, repo, sha in ((2, "lumen-identity", OWNER_HEAD), (1, "evo-agents", HEAD)):
        verify = [{"command": TWO_DRAFT["steps"][key - 1]["verify"], "exit_code": 0}]
        reported(world.client, world.worker, spec["id"], key, "done", repo=repo, verify=verify, commit_sha=sha)
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    world.github.push(THEIRS, "lumen-identity", TWO_BRANCH, OWNER_HEAD, files=OWNER_FILES)
    world.github.push(MINE, "evo-agents", TWO_BRANCH, HEAD, files=CONSUMER_FILES)


def judged_repo(world, repo: str, head: str) -> dict:
    """The judge run of ``repo``'s pull request: queued, claimed, its inputs read, passed, ended done; the change."""
    assert fire(world.client, NIGHT)["judge"] == 1
    spec = judge_key(claim(world.client, world.worker))
    assert spec["kind"] == "judge" and spec["curator"]["head_sha"] == head
    assert [item["repo"] for item in spec["repos"]] == [repo]
    found = inputs(world, spec["id"]).json()
    step = next(item for item in TWO_DRAFT["steps"] if item["repo"] == repo)
    assert (found["repo"], found["head_sha"], found["verify"]) == (repo, head, [step["verify"]])
    response = verdict(world, spec["id"], head_sha=head, verify=[{"command": step["verify"], "exit_code": 0}])
    assert response.status_code == 200, response.text
    moved(world.client, world.worker, spec["id"], "running", "verifying", "done")
    return response.json()


@on_postgres
def test_hub_a_proposal_in_two_repos_is_one_plan_owner_first_with_a_change_row_per_repo(seamed):
    accepted(seamed, paths=TWO_PATHS, plan=TWO_DRAFT)
    (change,) = changes(seamed)
    assert (change["state"], change["plan_id"], change["repo"], change["branch"], change["forge"]) == (
        "planned",
        TWO_PLAN,
        "lumen-identity",
        TWO_BRANCH,
        "github",
    )
    owner = seamed.headers["owner"]
    url = f"/v1/projects/{PROJECT}/plans/{TWO_PLAN}"
    plan = seamed.client.get(url, headers=owner).json()
    assert plan["body"]["repos"] == [
        {"repo": "lumen-identity", "branch": TWO_BRANCH, "order": 1},
        {"repo": "evo-agents", "branch": TWO_BRANCH, "order": 2},
    ]
    assert [(s["id"], s["repo"], s["depends_on"]) for s in plan["body"]["steps"]] == [
        (2, "lumen-identity", []),
        (1, "evo-agents", [2]),
    ]
    assert repo_rows(seamed) == [
        ("lumen-identity", 1, [], "pr_pending", None),
        ("evo-agents", 2, ["lumen-identity"], "pr_pending", None),
    ]
    shown = [(item["repo"], item["position"], item["depends_on"], item["state"]) for item in change["repos"]]
    assert shown == [("lumen-identity", 1, [], "pr_pending"), ("evo-agents", 2, ["lumen-identity"], "pr_pending")]
    blocked = {**plan["body"], "steps": [{**step, "status": "blocked"} for step in plan["body"]["steps"]]}
    put = {"body": blocked, "if_revision": plan["revision"]}
    assert seamed.client.put(url, json=put, headers=owner).status_code == 200  # its progress is written
    one = {**plan["body"], "repos": plan["body"]["repos"][:1]}
    refused = seamed.client.put(url, json={"body": one, "if_revision": plan["revision"] + 1}, headers=owner)
    assert refused.status_code == 409 and f"works on {TWO_BRANCH} of lumen-identity, evo-agents alone" in refused.text


@on_postgres
def test_hub_a_proposal_in_one_repo_keeps_its_plain_plan_and_no_repo_row(seamed):
    accepted(seamed, plan=ONE_DRAFT)
    (change,) = changes(seamed)
    assert change["state"] == "planned" and change["repo"] == "evo-agents" and change["repos"] == []
    assert repo_rows(seamed) == []


@on_postgres
def test_hub_the_rulesets_of_both_repos_are_checked_before_the_builder_runs(seamed):
    accepted(seamed, paths=TWO_PATHS, plan=TWO_DRAFT)
    assert fire(seamed.client, NIGHT).get("builder") is None  # neither ruleset checked
    assert "lumen-identity" in changes(seamed)[0]["reason"]
    assert moved_on(seamed) == {"ruleset_checked": 2}
    asked = {tuple(r.path.split("/")[2:4]) for r in seamed.github.requests if "/rules/branches/" in r.path}
    assert asked == {(THEIRS, "lumen-identity"), (MINE, "evo-agents")}
    built(seamed)


@on_postgres
def test_hub_one_pull_request_and_judge_run_per_repo_and_the_consumer_merges_after_its_owner(seamed):
    accepted(seamed, paths=TWO_PATHS, plan=TWO_DRAFT)
    assert moved_on(seamed) == {"ruleset_checked": 2}
    built(seamed)
    assert moved_on(seamed) == {"opened": 1}, changes(seamed)[0]["reason"]
    opened = {r.path for r in calls(seamed.github, "POST", "/pulls")}
    assert opened == {f"/repos/{THEIRS}/lumen-identity/pulls", f"/repos/{MINE}/evo-agents/pulls"}
    assert repo_rows(seamed) == [
        ("lumen-identity", 1, [], "judge_pending", 1),
        ("evo-agents", 2, ["lumen-identity"], "judge_pending", 1),
    ]
    assert changes(seamed)[0]["state"] == "judge_pending"

    assert judged_repo(seamed, "lumen-identity", OWNER_HEAD)["state"] == "judge_pending"  # evo-agents waits for its own
    change = judged_repo(seamed, "evo-agents", HEAD)
    assert change["state"] == "judged" and change["passed"] is True
    assert [row[3] for row in repo_rows(seamed)] == ["judged", "judged"]

    seamed.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)  # the consumer's CI is green, the owner's still runs
    seamed.github.ci(OWNER_HEAD, runs=[{"name": "test", "status": "in_progress", "conclusion": None}])
    assert moved_on(seamed) == {"wait": 1}, changes(seamed)[0]["reason"]
    assert seamed.github.merges == []
    assert "evo-agents waits for the merge of lumen-identity" in changes(seamed)[0]["reason"]
    checked = {r.path.split("/")[3] for r in calls(seamed.github, "POST", "/check-runs")}
    assert checked == {"evo-agents", "lumen-identity"}

    seamed.github.ci(OWNER_HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(seamed) == {"merged": 1}
    assert seamed.github.merges == [
        (THEIRS, "lumen-identity", 1, OWNER_HEAD, "workers"),
        (MINE, "evo-agents", 1, HEAD, "workers"),
    ]
    (change,) = changes(seamed)
    assert change["state"] == "merged" and change["merge_sha"]
    assert [row[3] for row in repo_rows(seamed)] == ["merged", "merged"]
    assert [(item["repo"], item["pr_number"], item["passed"]) for item in change["repos"]] == [
        ("lumen-identity", 1, True),
        ("evo-agents", 1, True),
    ]
    assert moved_on(seamed) == {}


STUCK_WHY = "it cannot become the Curator's plan: a Curator's plan works in one repo, and the proposal names two"


@on_postgres
@pytest.mark.parametrize("stuck_state", ["open", "closed"])
def test_hub_a_stuck_change_in_two_repos_is_planned_again_in_place_and_a_live_one_refuses(seamed, stuck_state):
    """A proposal in two repos accepted when a plan could not work in them kept a change with no plan, repo or branch:
    POST .../proposals/{id}/plan makes that same change planned in both repos; a live change, or a closed one that
    had a plan, is still 409."""
    owner = seamed.headers["owner"]
    written(seamed.client, owner, curator_charter(planned_tiers=[0, 1]))  # accepting tier 2 records the answer alone
    proposal = accepted(seamed, kind="feature", paths=TWO_PATHS, plan=TWO_DRAFT)
    written(seamed.client, owner, curator_charter())
    assert changes(seamed) == []
    c = tables.curator_changes
    ((project_id,),) = sql(seamed.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))
    stuck = {"project_id": project_id, "proposal_id": proposal["id"], "tier": 2, "state": stuck_state}
    ((change_id,),) = sql(seamed.db, insert(c).values(**stuck, reason=STUCK_WHY).returning(c.c.id))

    made = seamed.client.post(path("proposals", str(proposal["id"]), "plan"), headers=owner)
    assert made.status_code == 201, made.text
    change = made.json()
    assert (change["id"], change["state"], change["plan_id"], change["repo"], change["branch"], change["reason"]) == (
        change_id,
        "planned",
        TWO_PLAN,
        "lumen-identity",
        TWO_BRANCH,
        None,
    )
    assert [item["repo"] for item in change["repos"]] == ["lumen-identity", "evo-agents"]
    assert len(changes(seamed)) == 1
    last = ledger_of(seamed, proposal["id"])["lines"][-1]
    assert (last["action"], last["actor"]) == ("planned", "curator")
    assert last["details"]["replaced"] == {"state": stuck_state, "reason": STUCK_WHY}
    assert acts(ledger_of(seamed, proposal["id"]))[-2:] == [("accepted", "user"), ("planned", "curator")]

    again = seamed.client.post(path("proposals", str(proposal["id"]), "plan"), headers=owner)
    assert again.status_code == 409 and f"has change #{change_id} already, planned" in again.text, again.text
    sql(seamed.db, update(c).where(c.c.id == change_id).values(state="closed"))  # its pull request closed, say
    again = seamed.client.post(path("proposals", str(proposal["id"]), "plan"), headers=owner)
    assert again.status_code == 409 and f"has change #{change_id} already, closed" in again.text, again.text
