"""A Curator's draft that works in several repos becomes one plan in them, owners of a seam first, and the pull
request of a consumer merges only after its owners'."""

import pytest

from evo_agents.hub import judge

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
