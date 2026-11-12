"""The draft pull requests of run branches (``evo_agents.hub.server.draft_pulls``), from the push to the end.

Opening: a branch with nothing its base lacks, or that changes only what a run makes beside the work (a ``.venv``
link), gets no pull request, and its open draft is closed; the draft goes into the base the worker names, an open one
moved there; with none named, the project's default branch goes first on a tie. Settling: a draft whose head is in its
base is closed naming the pull request that merged it; one that changes only a ``.venv`` is closed; once its trace
ended, one a merged pull request linked in the trace replaced is closed naming it, and one the hub cannot settle opens
the thread of rule draft to the Orchestrator, once; a draft merged or closed by hand is kept so, and a draft is read at
most every CHECK_MINUTES.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

pytestmark = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

if pg.DSN:
    from sqlalchemy import select, update

    from evo_agents.hub import tables
    from evo_agents.hub.server import draft_pulls, orchestrator
    from tests.hub.fake_github import FakePull
    from tests.hub.live import sql
    from tests.hub.test_chat_runs import (  # noqa: F401
        MINE,
        app_key,
        client,
        hub,
        in_chats,
        message,
        pushed_branch,
    )
    from tests.hub.test_runs import PROJECT, of_run, set_run
    from tests.hub.test_system_orchestrator import led_desk, threads_of

REPO = "evo-agents"
VENV = [{"filename": ".venv", "status": "added"}]
CODE = [{"filename": "user/serializers.py", "status": "modified"}]


def kept_of(db, run_id: int) -> dict:
    (pulls,) = of_run(db, run_id, "pulls")[0]
    (entry,) = pulls
    return entry


def settle(client, ask=None) -> dict:
    found = client.portal.call(draft_pulls.settle_pass, client.app.state.engine, client.app.state.github_app, ask)
    return dict(found)


def comments_on(github, number: int) -> list[str]:
    return [item["body"] for item in github.comments if item["number"] == number]


def pull_number(github, number: int) -> FakePull:
    return github.pulls[(MINE.lower(), REPO, number)]


def merged_elsewhere(github, number: int, branch: str, sha: str, files: list) -> FakePull:
    """Pull request ``number`` of ``branch`` at ``sha`` changing ``files``, merged into main."""
    github.push(MINE, REPO, branch, sha, files=files)
    pull = FakePull(
        number, MINE, REPO, branch, sha, "main", state="closed", merged=True, merged_at="2026-11-12T00:00:00Z"
    )
    github.pulls[(MINE.lower(), REPO, number)] = pull
    github.hold(MINE, REPO, "main", sha)
    return pull


def ended_long_ago(db, run_id: int) -> None:
    set_run(db, run_id, state="done", finished_at=datetime.now(UTC) - timedelta(hours=2))


def unchecked(db, run_id: int) -> None:
    """Forget when the hub read the draft kept on run ``run_id``, so the next pass reads it again."""
    r = tables.runs
    (pulls,) = sql(db, select(r.c.pulls).where(r.c.id == run_id))[0]
    sql(db, update(r).values(pulls=[{**item, "checked_at": None} for item in pulls]).where(r.c.id == run_id))


# Opening


def test_a_branch_that_changes_only_a_venv_link_gets_no_pull_request(client, hub, hub_db, github):
    worker, chat, _, spec, body = pushed_branch(client, hub, github)
    github.push(MINE, REPO, body["branch"], body["head"], files=VENV)
    said = in_chats(client, worker, spec["id"], "chat/pulls", body).json()
    assert (said["url"], said["number"], said["opened"]) == (None, None, False)
    assert not [r for r in github.requests if r.method == "POST" and r.path.endswith("/pulls")]
    text = message(client, hub["owner"], chat["id"], said["message_id"])["body"]
    assert text == (
        f"Pushed {body['branch']} of {REPO}, but {body['branch']} changes only .venv, which a run makes beside the "
        "work and never commits: no pull request."
    )
    assert of_run(hub_db, spec["id"], "pulls") == [(None,)]


def test_a_push_with_no_commit_its_base_lacks_closes_the_open_draft(client, hub, hub_db, github):
    worker, chat, _, spec, body = pushed_branch(client, hub, github)
    opened = in_chats(client, worker, spec["id"], "chat/pulls", body).json()
    assert (opened["number"], opened["opened"]) == (1, True)
    assert kept_of(hub_db, spec["id"])["state"] == "open"
    github.hold(MINE, REPO, "main", "2" * 40)  # the agent's commits went into main another way
    github.push(MINE, REPO, body["branch"], "2" * 40)
    again = in_chats(client, worker, spec["id"], "chat/pulls", {**body, "head": "2" * 40}).json()
    assert again["number"] is None
    assert pull_number(github, 1).state == "closed"
    (comment,) = comments_on(github, 1)
    assert (
        comment
        == f"Closed by the hub: {body['branch']} has no commit that main lacks, so this draft has nothing to merge."
    )
    text = message(client, hub["owner"], chat["id"], again["message_id"])["body"]
    assert text.endswith(f"no pull request. The hub closed its draft pull request {opened['url']}.")
    assert kept_of(hub_db, spec["id"])["state"] == "closed"


def test_the_draft_goes_into_the_base_the_worker_names_and_an_open_one_moves_there(client, hub, hub_db, github):
    worker, _, _, spec, body = pushed_branch(client, hub, github)
    github.push(MINE, REPO, "deploy/test", "3" * 40)
    opened = in_chats(client, worker, spec["id"], "chat/pulls", body).json()
    assert pull_number(github, opened["number"]).base == "main", "a worker that names none: GitHub's default"
    named = in_chats(client, worker, spec["id"], "chat/pulls", {**body, "head": "4" * 40, "base": "deploy/test"}).json()
    assert (named["number"], named["opened"]) == (opened["number"], False)
    assert pull_number(github, opened["number"]).base == "deploy/test"
    assert kept_of(hub_db, spec["id"])["base"] == "deploy/test"
    refused = in_chats(client, worker, spec["id"], "chat/pulls", {**body, "base": "bad base"})
    assert refused.status_code == 422


def test_a_worker_that_names_no_base_gets_the_projects_default_branch_on_a_tie(client, hub, hub_db, github):
    pr = tables.project_repos
    sql(hub_db, update(pr).values(default_branch="develop").where(pr.c.name == REPO))
    worker, _, _, spec, body = pushed_branch(client, hub, github)
    github.push(MINE, REPO, "develop", "5" * 40)
    opened = in_chats(client, worker, spec["id"], "chat/pulls", body).json()
    assert pull_number(github, opened["number"]).base == "develop"


# Settling


def test_a_draft_whose_head_merged_through_another_pull_request_is_closed_naming_it(client, hub, hub_db, github):
    worker, _, _, spec, body = pushed_branch(client, hub, github)
    assert in_chats(client, worker, spec["id"], "chat/pulls", body).json()["number"] == 1
    assert settle(client) == {"waiting": 1}, "still open and the run going on: nothing to do"
    merged_elsewhere(github, 2, "qa/fix-18-19", body["head"], CODE)
    assert settle(client) == {}, "read at most every CHECK_MINUTES"
    unchecked(hub_db, spec["id"])
    assert settle(client) == {"closed": 1}
    assert pull_number(github, 1).state == "closed"
    assert comments_on(github, 1) == [
        f"Closed by the hub: the head of this draft ({body['head'][:12]}) is in main already, merged through #2."
    ]
    entry = kept_of(hub_db, spec["id"])
    assert (entry["state"], entry["replaced_by"]) == ("closed", 2)
    unchecked(hub_db, spec["id"])
    assert settle(client) == {}, "a closed draft is read no more"


def test_a_draft_that_changes_only_a_venv_is_closed_by_the_pass(client, hub, hub_db, github):
    worker, _, _, spec, body = pushed_branch(client, hub, github)
    github.push(MINE, REPO, body["branch"], body["head"], files=CODE)
    assert in_chats(client, worker, spec["id"], "chat/pulls", body).json()["number"] == 1
    github.push(MINE, REPO, body["branch"], body["head"], files=VENV)  # its code went in elsewhere, the link stayed
    unchecked(hub_db, spec["id"])
    assert settle(client) == {"closed": 1}
    (comment,) = comments_on(github, 1)
    assert "changes only .venv, which a run makes beside the work" in comment
    assert pull_number(github, 1).state == "closed"


def test_a_draft_left_when_its_trace_ended_closes_for_the_merged_pull_request_the_trace_links(
    client, hub, hub_db, github
):
    worker, chat, _, spec, body = pushed_branch(client, hub, github)
    github.push(MINE, REPO, body["branch"], body["head"], files=CODE)
    assert in_chats(client, worker, spec["id"], "chat/pulls", body).json()["number"] == 1
    merged_elsewhere(github, 2, "chat/9999", "6" * 40, CODE)  # the same fix, made again and merged
    link = f"Merged the fix through https://github.com/{MINE}/{REPO}/pull/2."
    posted = in_chats(client, worker, spec["id"], "chat/messages", {"body": link})
    assert posted.status_code == 201, posted.text
    asked = []

    async def ask(conn, run_id, lines, key):
        asked.append((run_id, key))

    unchecked(hub_db, spec["id"])
    assert settle(client, ask) == {"waiting": 1}, "the trace has not ended"
    ended_long_ago(hub_db, spec["id"])
    unchecked(hub_db, spec["id"])
    assert settle(client, ask) == {"replaced": 1}
    (comment,) = comments_on(github, 1)
    assert comment.startswith("Closed by the hub: the work of this draft went into main through #2")
    assert pull_number(github, 1).state == "closed" and asked == []
    assert kept_of(hub_db, spec["id"])["replaced_by"] == 2


def test_a_draft_the_hub_cannot_settle_is_asked_of_the_orchestrator_once(client, hub, hub_db, github):
    worker, _, _, spec, body = pushed_branch(client, hub, github)
    github.push(MINE, REPO, body["branch"], body["head"], files=CODE)
    assert in_chats(client, worker, spec["id"], "chat/pulls", body).json()["number"] == 1
    # a merged pull request the trace links, but of other files: no replacement
    merged_elsewhere(github, 2, "chat/9999", "6" * 40, [{"filename": "docs/other.md", "status": "added"}])
    in_chats(client, worker, spec["id"], "chat/messages", {"body": f"See {REPO}#2."})
    ended_long_ago(hub_db, spec["id"])
    asked = []

    async def ask(conn, run_id, lines, key):
        asked.append((run_id, key, lines))

    unchecked(hub_db, spec["id"])
    assert settle(client, ask) == {"asked": 1}
    ((run_id, key, lines),) = asked
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    assert (run_id, key) == (spec["id"], f"draft:{project_id}:{REPO}#1")
    assert "the trace links: #2; none merged into main with a file of this draft" in lines[1]
    assert pull_number(github, 1).state == "open" and kept_of(hub_db, spec["id"])["asked_at"]
    unchecked(hub_db, spec["id"])
    assert settle(client, ask) == {"waiting": 1} and len(asked) == 1, "asked once"
    github.merge_by_hand(MINE, REPO, body["branch"], "maycuatroi1", "7" * 40)
    unchecked(hub_db, spec["id"])
    assert settle(client, ask) == {"merged": 1}
    assert kept_of(hub_db, spec["id"])["state"] == "merged"


def test_the_thread_of_rule_draft_goes_to_the_orchestrator_of_the_agent(client, hub, hub_db, github):
    from tests.hub import test_credentials_api as credentials_api
    from tests.hub.test_chat_runs import WORKERS_APP

    credentials_api.install(github, REPO, permissions=WORKERS_APP)
    github.add_repo(MINE, REPO)
    studio, _, lead, run_id = led_desk(client, hub)
    branch = f"chat/{run_id}"
    github.push(MINE, REPO, branch, "1" * 40, files=CODE)
    pushed = in_chats(client, studio, run_id, "chat/pulls", {"repo": REPO, "branch": branch, "head": "1" * 40})
    assert pushed.json()["number"] == 1, pushed.text
    ended_long_ago(hub_db, run_id)
    assert settle(client, orchestrator.on_draft) == {"asked": 1}
    (thread,) = [item for item in threads_of(hub_db) if item["rule"] == "draft"]
    assert (thread["subject"], thread["subject_run_id"]) == ("reviewer", run_id)
    m = tables.chat_messages
    root = sql(hub_db, select(m.c.body).where(m.c.id == thread["root_message_id"]))[0][0]
    assert root.startswith("Rule: draft:") and f"https://github.com/{MINE}/{REPO}/pull/1" in root and "@lead" in root
    assert thread["orchestrator_run_id"] is not None


def test_what_needs_no_pull_request():
    nothing = draft_pulls.nothing_to_open
    assert nothing(None, "chat/1", "main") is None, "GitHub could not say"
    assert (
        nothing({"status": "behind", "ahead_by": 0, "paths": []}, "chat/1", "main")
        == "chat/1 has no commit that main lacks"
    )
    assert nothing({"status": "ahead", "ahead_by": 1, "paths": None}, "chat/1", "main") is None, "no list of files"
    assert (
        nothing({"status": "ahead", "ahead_by": 1, "paths": []}, "chat/1", "main")
        == "chat/1 changes no file against main"
    )
    venvs = {"status": "ahead", "ahead_by": 1, "paths": [".venv", "web/.venv/bin/python"]}
    assert "changes only .venv, web/.venv/bin/python" in nothing(venvs, "chat/1", "main")
    mixed = {"status": "ahead", "ahead_by": 1, "paths": [".venv", "app.py"]}
    assert nothing(mixed, "chat/1", "main") is None
    assert nothing({**venvs, "truncated": True}, "chat/1", "main") is None, "a list GitHub cut says nothing"
