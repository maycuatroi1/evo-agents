"""A release run of a project's daily release on the hub, from its claim to its tag (``evo_agents.hub.release_runs``,
``evo_agents.hub.server.release_claims``, ``evo_agents.hub.server.release_land``).

The checks step 2 of the daily-release plan names (criterion a2). The check that opens a cycle keeps the range of the
release (the commits from the newest v* tag to nightly, the pull requests they merge, the migrations nightly adds);
only a daemon that says it runs release runs is handed one; its claim says where its worktree starts and its prompt
names the range, the rule of the version (minor for a new migration or a pull request from feat/..., else patch, never
a major), the bump, the three faults of macOS and Windows and that it neither pushes nor tags. The worker reports the
branch release/<version> it pushed; once the run is done the hub opens the pull request of that branch into the
default branch with its own App, merges it with a merge commit at the run's commit and tags that commit v<version>.
It refuses, and the cycle stops saying why, a version that is not greater than the default branch's, changes the
major, whose tag exists, or that the branch does not name; a run that fails or ends without its branch stops the cycle
too; GitHub failing is tried again on the next pass, from the pull request already opened. GitHub is the fake of
``tests.hub.fake_github``, with the workers' App installed on evo-agents with contents and pull_requests write.
"""

from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from evo_agents.hub import release_runs, runs, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.release_check import release_pass
from evo_agents.hub.server.release_land import land_releases
from tests.hub.live import sql
from tests.hub.test_credentials_api import MINE, OWNER, app_key, install  # noqa: F401 (app_key is a fixture)
from tests.hub.test_credentials_api import hub_config as credentials_config
from tests.hub.test_credentials_api import members as credential_members
from tests.hub.test_credentials_api import worker_of as credential_worker
from tests.hub.test_curator import PROJECT, grant
from tests.hub.test_release_schedule import (
    BASE,
    HEAD,
    NEXT,
    REPO,
    SIX,
    fire,
    repo_at,
    set_schedule,
    status_of,
)
from tests.hub.test_runs import claim, moved, report

WRITE = {"contents": "write", "pull_requests": "write", "metadata": "read"}
MID, PUSHED, ELSEWHERE = "1" * 40, "e" * 40, "f" * 40
VERSION_FILE = release_runs.VERSION_FILE
DECISION = "Decision: 0.25.0, a minor: the range merges #7 from feat/release-runs."
MERGED_FEATURE = "Merge pull request #7 from maycuatroi1/feat/release-runs\n\nRelease runs cut a version"
MERGED_FIX = "Merge pull request #8 from maycuatroi1/fix/claim-wait\n\nThe claim waits for its lease"


def version_line(version: str) -> str:
    return f'"""evo-agents."""\n\n__version__ = "{version}"\n'


def at_head(github, main_version: str = "0.24.0") -> None:
    """evo-agents with two commits after v0.24.0 on main up to nightly (HEAD), the first the merge of a pull request
    from feat/..., and main's tip naming ``main_version``."""
    found = github.repos[(MINE.lower(), REPO)]
    found.history = [(BASE, "2026-10-20T01:00:00Z"), (MID, "2026-10-20T02:00:00Z"), (HEAD, "2026-10-20T03:00:00Z")]
    found.messages.update({MID: MERGED_FEATURE, HEAD: MERGED_FIX})
    github.push(MINE, REPO, "main", HEAD)
    github.put_file(MINE, REPO, HEAD, VERSION_FILE, version_line(main_version))


@pytest.fixture
def world(hub_db, tmp_path, github, app_key):  # noqa: F811
    """test_credentials_api's project on a hub holding the workers' App, installed on evo-agents with contents and
    pull_requests write; owner an admin of the project with a worker that says it runs release runs; evo-agents at
    v0.24.0 with nightly one packaging change and two commits ahead."""
    config = credentials_config(hub_db, tmp_path, github, app_key)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = credential_members(client, github)
        grant(client, headers, OWNER, "admin")
        install(github, REPO, permissions=WRITE)
        repo_at(github, HEAD)
        at_head(github)
        worker = credential_worker(client, headers["owner"], "mac")
        yield SimpleNamespace(
            client=client, headers=headers, github=github, db=hub_db, app=client.app, config=config, worker=worker
        )


def says_release(world, kinds=runs.RUN_KINDS) -> None:
    """A heartbeat of the world's worker whose daemon says it runs ``kinds``."""
    worker = world.worker
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": 1,
        "runs": [],
        "agent_version": "0.25.0",
        "run_kinds": list(kinds),
    }
    response = world.client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text


def opened(world) -> dict:
    """The schedule set, the 06:00 check made, and the cut run claimed by the world's worker and running."""
    set_schedule(world, repo=REPO)
    assert fire(world, SIX) == {"opened": 1}
    says_release(world)
    spec = claim(world.client, world.worker)
    assert spec is not None and spec["kind"] == "release"
    moved(world.client, world.worker, spec["id"], "running")
    return spec


def pushed(world, run_id: int, version: str = "0.25.0", *, head: str = PUSHED, named: str | None = None, **body):
    """The branch release/<version> pushed at ``head`` on the fake (its VERSION_FILE naming ``named``, the version by
    default), and the worker telling the hub."""
    world.github.push(MINE, REPO, release_runs.branch_of(version), head)
    world.github.put_file(MINE, REPO, head, VERSION_FILE, version_line(named or version))
    told = {"repo": REPO, "branch": release_runs.branch_of(version), "head": head, "version": version, **body}
    told.setdefault("decision", DECISION)
    return world.client.post(f"/v1/worker/runs/{run_id}/release/branch", json=told, headers=world.worker["headers"])


def done(world, run_id: int) -> None:
    moved(world.client, world.worker, run_id, "verifying", "done", summary="Bumped to 0.25.0; no fault in the range.")


def land(world) -> dict:
    state = world.app.state
    return world.client.portal.call(partial(land_releases, state.engine, state.github_app))


def cut(world, version: str = "0.25.0", **body) -> int:
    """A cut run that pushed release/<version> and ended done; its id."""
    spec = opened(world)
    answer = pushed(world, spec["id"], version, **body)
    assert answer.status_code == 201, answer.text
    done(world, spec["id"])
    return spec["id"]


def landed_row(db, run_id: int) -> dict:
    rr = tables.release_runs
    names = ("version", "branch", "head_commit", "decision", "pull_number", "merge_commit", "tagged", "land_state")
    row = sql(db, select(*(rr.c[name] for name in (*names, "land_reason", "land_tries"))).where(rr.c.run_id == run_id))
    return dict(zip((*names, "land_reason", "land_tries"), row[0], strict=True))


def cycle_of(db) -> tuple:
    c = tables.release_cycles
    return sql(db, select(c.c.state, c.c.stop_reason, c.c.ended_at))[0]


def tag_of(github, tag: str):
    return github.repos[(MINE.lower(), REPO)].refs.get(f"refs/tags/{tag}")


# The model


def test_the_rule_of_the_version_the_branch_and_the_refusals_of_the_model():
    assert release_runs.bump_of({"migrations": ["0068_x.py"]})[0] == "minor"
    assert release_runs.bump_of({"pulls": [{"number": 7, "branch": "feat/x", "title": ""}]}) == (
        "minor",
        "the range merges #7 from feat/x",
    )
    assert release_runs.bump_of({"pulls": [{"number": 8, "branch": "fix/x", "title": ""}]})[0] == "patch"
    assert release_runs.bumped((0, 24, 2), "minor") == (0, 25, 0)
    assert release_runs.bumped((0, 24, 2), "patch") == (0, 24, 3)
    assert release_runs.branch_of("0.25.0") == "release/0.25.0"
    assert release_runs.is_release_branch("release/1.2.3")
    assert not any(map(release_runs.is_release_branch, ("release/1.2", "release/v1.2.3", "main", "release/")))
    commits = [
        release_runs.commit_of({"sha": MID, "commit": {"message": MERGED_FEATURE}}),
        release_runs.commit_of({"sha": HEAD, "commit": {"message": "web: the page reads (#9)"}}),
        release_runs.commit_of({"sha": BASE, "commit": {"message": "hub: a plain change"}}),
    ]
    assert release_runs.pulls_of(commits) == [
        {"number": 7, "branch": "feat/release-runs", "title": "Release runs cut a version"},
        {"number": 9, "branch": None, "title": "web: the page reads"},
    ]
    base = {"evo_agents/hub/migrations/versions/0066_a.py": "1", "evo_agents/x.py": "2"}
    head = {
        **base,
        "evo_agents/hub/migrations/versions/0067_b.py": "3",
        "evo_agents/hub/migrations/versions/x/y.py": "4",
    }
    assert release_runs.new_migrations(base, head) == ["0067_b.py"]
    assert release_runs.file_version(version_line("0.24.2")) == "0.24.2"
    problem = partial(release_runs.land_problem, branch="release/x", tag="v0.25.0", tag_exists=False)
    assert problem("0.25.0", "0.24.2", "0.25.0") is None
    assert "not greater than 0.25.0" in problem("0.25.0", "0.25.0", "0.25.0")
    assert "not greater than 0.25.1" in problem("0.25.0", "0.25.1", "0.25.0")
    assert "changes the major version" in problem("1.0.0", "0.24.2", "1.0.0")
    assert "names '0.24.2', not 0.25.0" in problem("0.25.0", "0.24.2", "0.24.2")
    assert "exists already" in release_runs.land_problem(
        "0.25.0", "0.24.2", "0.25.0", branch="release/0.25.0", tag="v0.25.0", tag_exists=True
    )


# The claim


def test_the_check_keeps_the_range_and_only_a_daemon_that_runs_release_runs_is_handed_the_cut(world):
    set_schedule(world, repo=REPO)
    assert fire(world, SIX) == {"opened": 1}
    c = tables.release_cycles
    [(span,)] = sql(world.db, select(c.c.span))
    assert span["total"] == 2 and [item["sha"] for item in span["commits"]] == [MID, HEAD]
    assert span["pulls"] == [
        {"number": 7, "branch": "feat/release-runs", "title": "Release runs cut a version"},
        {"number": 8, "branch": "fix/claim-wait", "title": "The claim waits for its lease"},
    ]
    assert span["migrations"] == [] and span["problem"] is None
    says_release(world, [kind for kind in runs.RUN_KINDS if kind != "release"])
    assert claim(world.client, world.worker) is None, "a daemon that does not say it runs release runs gets none"
    says_release(world)
    spec = claim(world.client, world.worker)
    assert spec["kind"] == "release" and spec["repos"] == [{"repo": REPO, "branch": None}]
    cycle_id = sql(world.db, select(c.c.id))[0][0]
    assert spec["release"] == {
        "cycle_id": cycle_id,
        "mode": "cut",
        "repo": REPO,
        "start_commit": HEAD,
        "base_tag": "v0.24.0",
        "tag_prefix": "v",
        "red_tag": None,
    }
    prompt = spec["prompt"]
    for said in (
        f"cut the next release of {REPO} from nightly at {HEAD[:12]} for cycle #{cycle_id}",
        f"From the newest release tag v0.24.0 at {BASE[:12]} to nightly at {HEAD[:12]}: 2 commits.",
        "New migrations under evo_agents/hub/migrations/versions/: none.",
        "  - #7 from feat/release-runs: Release runs cut a version",
        f"  - {MID[:12]} Merge pull request #7 from maycuatroi1/feat/release-runs",
        "By what the hub read, that is 0.25.0, a minor: the range merges #7 from feat/release-runs.",
        "never a new major",
        "`Decision: `",
        "Run `python scripts/bump_version.py <version>` in the worktree.",
        "`pytest.importorskip`",
        'newline="\\n"',
        "`sqlite3` connection that is never closed",
        "Do not push, tag or open a pull request",
        "no commit message names a customer",
        '"version" (X.Y.Z), "decision"',
    ):
        assert said in prompt, said
    assert len(prompt.encode()) <= runs.MAX_PROMPT_BYTES


def test_a_range_github_does_not_list_still_opens_the_cycle_and_the_prompt_says_to_read_git(world):
    found = world.github.repos[(MINE.lower(), REPO)]
    found.history, found.merge_bases = [], {}  # GitHub answers 404 to the comparison
    set_schedule(world, repo=REPO)
    assert fire(world, SIX) == {"opened": 1}
    says_release(world)
    prompt = claim(world.client, world.worker)["prompt"]
    assert "The hub could not list the commits (GitHub answered 404" in prompt
    assert "a patch: no migration is new, and the hub could not list the pull requests" in prompt


# The branch the worker pushed


def test_the_worker_reports_only_the_release_branch_of_the_run_it_holds(world):
    spec = opened(world)
    run_id = spec["id"]
    wrong_branch = pushed(world, run_id, branch="release/0.26.0")
    assert wrong_branch.status_code == 422 and "pushes release/0.25.0 for 0.25.0" in wrong_branch.text
    other_repo = pushed(world, run_id, repo="lumen-identity")
    assert other_repo.status_code == 422 and f"releases {REPO}, not lumen-identity" in other_repo.text
    bad = pushed(world, run_id, version="0.25")
    assert bad.status_code == 422
    stranger = credential_worker(world.client, world.headers["other"], "pc")
    response = world.client.post(
        f"/v1/worker/runs/{run_id}/release/branch",
        json={"repo": REPO, "branch": "release/0.25.0", "head": PUSHED, "version": "0.25.0"},
        headers=stranger["headers"],
    )
    assert response.status_code == 404
    answer = pushed(world, run_id)
    assert answer.status_code == 201, answer.text
    assert answer.json() == {
        "run_id": run_id,
        "cycle_id": spec["release"]["cycle_id"],
        "repo": REPO,
        "branch": "release/0.25.0",
        "head": PUSHED,
        "version": "0.25.0",
    }
    row = landed_row(world.db, run_id)
    assert (row["version"], row["branch"], row["head_commit"], row["decision"]) == (
        "0.25.0",
        "release/0.25.0",
        PUSHED,
        DECISION,
    )
    assert row["land_state"] is None and land(world) == {}, "the run is not done yet: nothing lands"


# Landing


def test_a_cut_that_pushed_its_branch_is_merged_with_a_merge_commit_and_tagged_on_it(world):
    run_id = cut(world)
    assert land(world) == {"tagged": 1}
    github = world.github
    pull = github.pull_of(MINE, REPO, "release/0.25.0")
    assert (pull.base, pull.merged, pull.title) == ("main", True, "Release 0.25.0")
    assert github.merges == [(MINE, REPO, pull.number, PUSHED, "workers")]
    merge = pull.merge_sha
    assert tag_of(github, "v0.25.0") == (merge, None)
    assert github.made_refs == [(MINE, REPO, "refs/tags/v0.25.0", merge, "workers")]
    assert tag_of(github, "v0.24.0") is not None and len(github.made_refs) == 1, "no other tag made, none moved"
    row = landed_row(world.db, run_id)
    assert (row["pull_number"], row["merge_commit"], row["tagged"], row["land_state"]) == (
        pull.number,
        merge,
        "v0.25.0",
        "tagged",
    )
    state, reason, ended = cycle_of(world.db)
    assert (state, reason, ended) == ("open", None, None), "the cycle stays open for the tag's CI"
    status = status_of(world)
    [shown] = status["cycle"]["runs"]
    assert shown["state"] == "done" and shown["version"] == "0.25.0" and shown["tagged"] == "v0.25.0"
    assert shown["pull_url"] == f"https://github.com/{MINE}/{REPO}/pull/{pull.number}"
    assert shown["merge_commit"] == merge and shown["decision"] == DECISION and shown["land_state"] == "tagged"
    a = tables.audit
    targets = sql(world.db, select(a.c.target).where(a.c.action == "release.tag"))
    assert targets == [(f"{PROJECT} cycle:{status['cycle']['id']} run:{run_id} tag=v0.25.0 commit={merge}",)]
    assert land(world) == {}, "a tagged run lands once"
    assert len(github.made_refs) == 1 and len(github.merges) == 1


def test_the_pull_request_names_its_cycle_and_run_and_no_member(world):
    run_id = cut(world)
    land(world)
    requests = [item for item in world.github.calls(f"/repos/{MINE}/{REPO}/pulls") if item.method == "POST"]
    [asked] = [json.loads(item.body) for item in requests]
    assert asked["head"] == "release/0.25.0" and asked["base"] == "main" and asked.get("draft") is not True
    assert f"cut by release run #{run_id} of cycle #" in asked["body"] and DECISION in asked["body"]
    assert OWNER not in asked["body"] and "tags that commit v0.25.0" in asked["body"]


@pytest.mark.parametrize(
    ("main_version", "version", "named", "tagged", "why"),
    [
        ("0.25.0", "0.25.0", None, False, "0.25.0 is not greater than 0.25.0"),
        ("0.24.0", "1.0.0", None, False, "1.0.0 changes the major version of 0.24.0"),
        ("0.24.0", "0.25.0", None, True, "the tag v0.25.0 exists already"),
        ("0.24.0", "0.25.0", "0.24.0", False, "names '0.24.0', not 0.25.0"),
    ],
)
def test_a_release_the_hub_refuses_stops_its_cycle_with_the_reason_and_opens_no_pull_request(
    world, main_version, version, named, tagged, why
):
    at_head(world.github, main_version)
    run_id = cut(world, version, named=named)
    if tagged:  # after the check, whose newest tag it would be
        world.github.tag(MINE, REPO, "v0.25.0", ELSEWHERE)
    assert land(world) == {"refused": 1}
    assert world.github.pull_of(MINE, REPO, release_runs.branch_of(version)) is None
    assert world.github.merges == [] and world.github.made_refs == []
    row = landed_row(world.db, run_id)
    assert row["land_state"] == "refused" and why in row["land_reason"]
    state, reason, ended = cycle_of(world.db)
    assert state == "stopped" and ended is None
    assert (
        f"the hub did not land {release_runs.branch_of(version)} of release run #{run_id}" in reason and why in reason
    )
    assert why in status_of(world)["cycle"]["stop_reason"]
    assert fire(world, NEXT) == {"busy": 1}, "a stopped cycle opens no other"


def test_github_failing_is_tried_again_from_the_pull_request_already_opened(world):
    run_id = cut(world)
    world.github.merge_refusals.append((502, {"message": "Bad Gateway"}))
    assert land(world) == {"waiting": 1}
    row = landed_row(world.db, run_id)
    assert row["pull_number"] is not None and row["merge_commit"] is None and row["land_tries"] == 1
    assert "502" in row["land_reason"] and cycle_of(world.db)[0] == "open"
    assert land(world) == {"tagged": 1}
    assert len(world.github.pulls) == 1, "the pull request opened once"
    assert landed_row(world.db, run_id)["land_state"] == "tagged"


def test_github_failing_pass_after_pass_marks_the_run_failed_and_stops_the_cycle(world):
    run_id = cut(world)
    world.github.merge_refusals.extend([(503, {"message": "Unavailable"})] * release_runs.LAND_TRIES)
    outcomes = [land(world) for _ in range(release_runs.LAND_TRIES)]
    assert outcomes == [{"waiting": 1}] * (release_runs.LAND_TRIES - 1) + [{"failed": 1}]
    row = landed_row(world.db, run_id)
    assert row["land_state"] == "failed" and row["land_tries"] == release_runs.LAND_TRIES
    state, reason, _ = cycle_of(world.db)
    assert state == "stopped" and f"GitHub failed {release_runs.LAND_TRIES} passes in a row" in reason


def test_a_pull_request_merged_by_an_earlier_pass_gives_its_merge_commit_and_a_tag_there_is_kept(world):
    run_id = cut(world)
    world.github.merge_refusals.append((502, {"message": "Bad Gateway"}))
    land(world)
    merged = world.github.merge_by_hand(MINE, REPO, "release/0.25.0", "evo-agents-workers[bot]", "9" * 40)
    world.github.tag(MINE, REPO, "v0.25.0", merged.merge_sha)
    assert land(world) == {"tagged": 1}
    row = landed_row(world.db, run_id)
    assert (row["merge_commit"], row["tagged"]) == ("9" * 40, "v0.25.0")
    assert world.github.made_refs == [], "the tag was there already, on the merge commit"


def test_the_release_job_checks_then_lands(world):
    run_id = cut(world)
    state = world.app.state
    found = world.client.portal.call(partial(release_pass, state.engine, state.github_app, now=SIX))
    assert found == {"done": 1, "landed_tagged": 1}
    assert landed_row(world.db, run_id)["tagged"] == "v0.25.0"


# What a run's end does to its cycle


def test_a_release_run_that_fails_stops_its_cycle(world):
    spec = opened(world)
    response = report(world.client, world.worker, spec["id"], "failed", error="the bump script failed")
    assert response.status_code == 200, response.text
    state, reason, ended = cycle_of(world.db)
    assert state == "stopped" and ended is None
    assert reason == f"release run #{spec['id']} failed: the bump script failed"
    assert land(world) == {}


def test_a_release_run_done_without_its_branch_stops_its_cycle_and_is_refused(world):
    spec = opened(world)
    done(world, spec["id"])
    row = landed_row(world.db, spec["id"])
    assert row["land_state"] == "refused" and "without pushing a branch release/<version>" in row["land_reason"]
    state, reason, _ = cycle_of(world.db)
    assert state == "stopped" and "nothing to land" in reason


def test_a_cancelled_release_run_stops_its_cycle_while_a_pause_closes_it(world):
    spec = opened(world)
    r = tables.runs
    sql(world.db, update(r).where(r.c.id == spec["id"]).values(cancel_requested_at=r.c.queued_at))
    assert report(world.client, world.worker, spec["id"], "cancelled").status_code == 200
    state, reason, _ = cycle_of(world.db)
    assert state == "stopped" and f"release run #{spec['id']} was cancelled" in reason


def test_a_branch_reported_once_the_hub_landed_the_run_is_refused(world):
    spec = opened(world)
    rr = tables.release_runs
    sql(world.db, update(rr).where(rr.c.run_id == spec["id"]).values(land_state="refused", land_reason="no"))
    late = pushed(world, spec["id"])
    assert late.status_code == 409 and "landed run" in late.text
