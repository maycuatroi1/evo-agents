"""What an isolated plugin writes and reads through the plugin API with its token, in a project that granted it
(``evo_agents.hub.server.plugin_tasks_api``, ``plugin_docs_api``, ``plugin_runs_api`` and ``plugin_srs_api``), and
the repos and base a task names (``task_dispatch``).

Granted tasks:write, a plugin creates tasks under keys of its own, finds one by its key, changes and comments on the
tasks it made, never another, in the name of the admin who granted it its permissions. Granted docs:write, it uploads
images and saves documents whose slug starts with its id, each save the next version. Granted runs:dispatch, it reads
the summary and the outputs of the task runs of its own agent types, and of no other run. Granted srs:read, it lists
the codes and keys of the use cases of the version of a document approved last. A task names the repos its run gets
a worktree of and a base each starts from, one for the run or one of a repo's own, which the draft pull request of
that repo's push goes into.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from evo_agents.hub import autonomy_card, tables, task_runs
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub import test_credentials_api as credentials_api
from tests.hub.account_pool_setup import give_account_pool
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_chat_runs import in_chats
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_docs import PNG, sha
from tests.hub.test_plugin_runtimes import install
from tests.hub.test_run_outputs import item, sent
from tests.hub.test_runs import PLAN, PROJECT, claim, dispatched, moved, of_run, plan_body
from tests.hub.test_task_runs import (
    ADMIN_GRANT,
    CONTRIBUTES,
    DISPATCH,
    MINE,
    OTHER,
    OWNER,
    PLUGIN,
    READ_ONLY,
    READER,
    REPOS,
    WORKERS_APP,
    enable_plugin,
    enabled,
    hand,
    handed,
    node,
    plugin_key,
)

app_key = credentials_api.app_key  # a fixture
WRITES = [DISPATCH, "tasks:write", "docs:write", "srs:read"]
BOARD = f"/v1/plugin-api/projects/{PROJECT}/board/tasks"
DOCS = f"/v1/plugin-api/projects/{PROJECT}/docs"
SAMPLE_SRS = Path(__file__).parent / "fixtures" / "srs" / "demo-shop-srs.md"
SRS = f"/v1/x/srs/{PROJECT}/documents/demo-shop"
SLUG = f"{PLUGIN}-round-1"
BODY = "# Round 1\n\n![Login](shots/login.png)\n\nThe login case passed.\n"


@pytest.fixture
def client(hub_db, tmp_path, github, app_key, s3):  # noqa: F811
    github.app_public_key = app_key[1]
    changes = live.web_changes(github) | {"github_app_id": github.app_id, "github_app_private_key": app_key[0]}
    config = live.hub_config(hub_db, tmp_path, github, **changes, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github, hub_db) -> dict:
    """The project of ``test_credentials_api``, owner its admin, someone-else a writer and reader a reader; the
    features tasks, docs and srs on, the sample SRS imported by someone-else; plugin triage, with its agent type
    triager, enabled by owner with WRITES granted. The headers of each, the plugin's id and its key."""
    headers = {
        "admin": bearer(live.sign_in(client, github, ADMIN, credentials_api.ADMIN_ID)["token"]),
        "owner": bearer(live.sign_in(client, github, OWNER, credentials_api.OWNER_ID)["token"]),
        "other": bearer(live.sign_in(client, github, OTHER, credentials_api.OTHER_ID)["token"]),
        "reader": bearer(live.sign_in(client, github, READER, 504)["token"]),
    }
    body = {**credentials_api.registration(credentials_api.DISPATCHED), "sinks": OPEN_SINKS}
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=headers["admin"]).status_code == 200
    for login, grant in ((OWNER, ADMIN_GRANT), (OTHER, credentials_api.WRITER), (READER, READ_ONLY)):
        response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
        assert response.status_code == 200, response.text
    for feature, asked in (("tasks", ["plans:read"]), ("docs", []), ("srs", [])):
        enable_plugin(client, headers["owner"], feature, asked)
    imported = client.post(
        f"/v1/x/srs/{PROJECT}/import",
        json={"markdown": SAMPLE_SRS.read_text(encoding="utf-8"), "slug": "demo-shop"},
        headers=headers["other"],
    )
    assert imported.status_code == 200, imported.text
    plugin_id = install(hub_db, name=PLUGIN, contributes=CONTRIBUTES, permissions=WRITES)
    enable_plugin(client, headers["owner"], PLUGIN, WRITES)
    return {**headers, "plugin_id": plugin_id, "key": plugin_key(client, plugin_id)}


def other_plugin(client, hub, hub_db, permissions: list[str]) -> dict:
    """The headers of the token of plugin lumen-other, enabled by owner with ``permissions`` granted."""
    plugin_id = install(hub_db, name="lumen-other", contributes={}, permissions=permissions)
    enable_plugin(client, hub["owner"], "lumen-other", permissions)
    return plugin_key(client, plugin_id)


def made(client, key: dict, expect: int = 201, **body) -> dict:
    answer = client.post(BOARD, json={"key": "fp-login", "title": "[qa:fp-login] Login fails", **body}, headers=key)
    assert answer.status_code == expect, answer.text
    return answer.json()


# tasks:write


def test_plugin_tasks_write_makes_finds_changes_and_comments_on_its_own_tasks_in_the_granters_name(client, hub):
    key = hub["key"]
    enabled(client, hub["owner"])
    run_id = handed(client, key, task="Reproduce the failing login.")["run_id"]
    task = made(
        client,
        key,
        type="bug",
        description_md="## Steps\n\n1. Open the login page",
        links={"use_cases": ["UC ORD-1"], "runs": [run_id]},
    )
    assert (task["key"], task["type"], task["status"], task["created_by"]) == ("fp-login", "bug", "backlog", OWNER)
    assert task["external_ref"] == {"plugin": PLUGIN, "key": "fp-login"}
    assert task["codes"] == ["UC ORD-1"] and {link["kind"] for link in task["links"]} == {"srs_item", "run"}
    again = made(client, key, expect=409)
    assert f"made task {task['ref']} under key fp-login already" in again["message"]
    found = client.get(BOARD, params={"key": "fp-login"}, headers=key)
    assert found.status_code == 200 and [item["number"] for item in found.json()] == [task["number"]]
    assert client.get(BOARD, params={"key": "fp-other"}, headers=key).json() == []

    change = {"status": "done", "title": "Login fails on Safari"}
    changed = client.patch(f"{BOARD}/{task['ref']}", json=change, headers=key)
    assert changed.status_code == 200, changed.text
    assert (changed.json()["status"], changed.json()["title"]) == ("done", "Login fails on Safari")
    assert {(event["actor"], event["field"]) for event in changed.json()["events"]} >= {(OWNER, "status")}
    said = client.post(f"{BOARD}/{task['ref']}/comments", json={"body": "Seen again in run 12."}, headers=key)
    assert said.status_code == 201 and (said.json()["author"], said.json()["body"]) == (OWNER, "Seen again in run 12.")
    member = client.get(f"/v1/x/tasks/{PROJECT}/tasks/{task['ref']}", headers=hub["reader"]).json()
    assert member["status"] == "done" and [c["body"] for c in member["comments"]] == ["Seen again in run 12."]

    theirs = client.post(f"/v1/x/tasks/{PROJECT}/tasks", json={"title": "A member's task"}, headers=hub["other"])
    assert theirs.status_code == 201, theirs.text
    ref = theirs.json()["ref"]
    assert client.patch(f"{BOARD}/{ref}", json={"status": "done"}, headers=key).status_code == 404
    assert client.post(f"{BOARD}/{ref}/comments", json={"body": "x"}, headers=key).status_code == 404
    assert client.get(f"/v1/x/tasks/{PROJECT}/tasks/{ref}", headers=hub["reader"]).json()["status"] == "backlog"
    assert client.patch(f"{BOARD}/T-999", json={"status": "done"}, headers=key).status_code == 404


def test_plugin_tasks_write_needs_its_token_its_grant_the_feature_and_a_writer_who_granted_it(client, hub, hub_db):
    assert client.post(BOARD, json={"key": "k", "title": "t"}).status_code == 401
    lacking = other_plugin(client, hub, hub_db, [DISPATCH])
    refused = client.post(BOARD, json={"key": "k", "title": "t"}, headers=lacking)
    assert refused.status_code == 403 and "with tasks:write granted" in refused.json()["message"]
    saved = client.put(f"{DOCS}/documents/lumen-other-x", json={"title": "t", "body": ""}, headers=lacking)
    assert saved.status_code == 403 and "with docs:write granted" in saved.json()["message"]
    bad = client.post(BOARD, json={"key": "a\nb", "title": "t"}, headers=hub["key"])
    assert bad.status_code == 422
    g = tables.grants
    with live.engine(hub_db).begin() as conn:
        owner_id = conn.execute(select(tables.users.c.id).where(tables.users.c.login == OWNER)).scalar_one()
        conn.execute(update(g).where(g.c.user_id == owner_id).values(role="reader"))
    downgraded = client.post(BOARD, json={"key": "k", "title": "t"}, headers=hub["key"])
    assert downgraded.status_code == 403
    assert f"in the name of {OWNER}, who granted it its permissions and holds reader" in downgraded.json()["message"]
    with live.engine(hub_db).begin() as conn:
        conn.execute(update(g).where(g.c.user_id == owner_id).values(role="admin"))
    off = client.post(f"/v1/projects/{PROJECT}/plugins/tasks/disable", headers=hub["owner"])
    assert off.status_code == 200, off.text
    gone = client.post(BOARD, json={"key": "k", "title": "t"}, headers=hub["key"])
    assert gone.status_code == 404 and "does not have the feature tasks on" in gone.json()["message"]
    assert sql(hub_db, select(tables.tasks.items.c.id)) == []


# docs:write


def test_plugin_docs_write_saves_versions_of_its_own_documents_with_their_images(client, hub):
    key = hub["key"]
    uploaded = client.post(f"{DOCS}/images", content=PNG, headers=key)
    assert uploaded.status_code == 201, uploaded.text
    assert (uploaded.json()["sha256"], uploaded.json()["held"]) == (sha(PNG), False)
    images = [{"name": "shots/login.png", "sha256": sha(PNG)}]
    path = f"{DOCS}/documents/{SLUG}"
    first = client.put(path, json={"title": "Round 1", "body": BODY, "images": images}, headers=key)
    assert first.status_code == 200, first.text
    assert (first.json()["version"], first.json()["created"], first.json()["document"]["updated_by"]) == (
        1,
        True,
        OWNER,
    )
    same = client.put(path, json={"title": "Round 1", "body": BODY, "images": images}, headers=key).json()
    assert (same["version"], same["changed"]) == (1, False)
    second = client.put(path, json={"title": "Round 1", "body": BODY + "\nOne more.\n", "images": images}, headers=key)
    assert second.json()["version"] == 2
    shown = client.get(f"/v1/x/docs/{PROJECT}/documents/{SLUG}", headers=hub["reader"]).json()
    assert [image["sha256"] for image in shown["shown"]["images"]] == [sha(PNG)]
    assert client.get(shown["shown"]["images"][0]["url"], headers=hub["reader"]).content == PNG

    for slug, body, said in (
        ("guide", {}, f"saves documents whose slug starts with {PLUGIN}-"),
        (f"{PLUGIN}-", {}, f"saves documents whose slug starts with {PLUGIN}-"),
        (f"{PLUGIN}-labelled", {"label": {"level": "internal"}}, "carries the project's label"),
    ):
        refused = client.put(f"{DOCS}/documents/{slug}", json={"title": "t", "body": "b", **body}, headers=key)
        assert refused.status_code == 422 and said in refused.json()["message"], (slug, refused.text)
    unknown = [{"name": "x.png", "sha256": "0" * 64}]
    assert client.put(path, json={"title": "t", "body": "b", "images": unknown}, headers=key).status_code == 422


# runs:dispatch: the summary and outputs of the plugin's task runs


def test_plugin_reads_the_summary_and_outputs_of_its_own_task_runs_and_of_no_other_run(client, hub, hub_db):
    enabled(client, hub["owner"])
    laptop = node(client, hub_db, hub["other"], "laptop")
    key = hub["key"]
    run_id = handed(client, key)["run_id"]
    assert claim(client, laptop)["id"] == run_id
    moved(client, laptop, run_id, "running", session_id="sess-1")
    report = b'{"cases": 3, "failed": 1}\n'
    files = [
        (PNG, item(PNG, "shots/login.png", "image/png", source="outputs")),
        (report, item(report, "report.json", "application/json", source="outputs")),
    ]
    sent(client, f"/v1/worker/runs/{run_id}", laptop["headers"], files)
    moved(client, laptop, run_id, "verifying", "done", summary="Ran 3 cases; the login case failed.")

    base = f"/v1/plugin-api/projects/{PROJECT}/runs/{run_id}"
    summary = client.get(f"{base}/summary", headers=key)
    assert summary.status_code == 200, summary.text
    shown = summary.json()
    assert (shown["run_id"], shown["task_id"], shown["state"], shown["agent_type"]) == (
        run_id,
        run_id,
        "done",
        "triager",
    )
    assert "Ran 3 cases; the login case failed." in shown["summary"] and shown["error"] is None
    listed = client.get(f"{base}/outputs", headers=key)
    assert listed.status_code == 200, listed.text
    outputs = listed.json()
    assert (outputs["count"], [o["name"] for o in outputs["outputs"]]) == (2, ["shots/login.png", "report.json"])
    shot = outputs["outputs"][0]
    assert shot["url"] == f"{base}/outputs/{shot['id']}/content"
    content = client.get(shot["url"], headers=key)
    assert content.status_code == 200 and content.content == PNG and content.headers["content-type"] == "image/png"
    only = client.get(f"{base}/outputs", params={"after": shot["id"]}, headers=key).json()
    assert [o["name"] for o in only["outputs"]] == ["report.json"]
    assert client.get(f"{base}/outputs/999999/content", headers=key).status_code == 404

    pushed = client.put(f"/v1/projects/{PROJECT}/plans/{PLAN}", json={"body": plan_body()}, headers=hub["owner"])
    assert pushed.status_code == 200, pushed.text
    give_account_pool(hub_db, PROJECT, PROJECT, OWNER)  # the runs of the project's default agent need one
    plan_run = dispatched(client, hub["owner"], [2])[0]["id"]
    for path in (f"/runs/{plan_run}/summary", f"/runs/{plan_run}/outputs", f"/runs/{run_id + 1000}/summary"):
        assert client.get(f"/v1/plugin-api/projects/{PROJECT}{path}", headers=key).status_code == 404, path
    other = other_plugin(client, hub, hub_db, [DISPATCH])
    assert client.get(f"{base}/summary", headers=other).status_code == 404
    assert client.get(f"{base}/outputs/{shot['id']}/content", headers=other).status_code == 404
    assert client.get(f"{base}/summary").status_code == 401


# srs:read


def test_plugin_srs_read_lists_the_codes_and_keys_of_the_use_cases_of_the_version_approved_last(client, hub):
    key = hub["key"]
    path = f"/v1/plugin-api/projects/{PROJECT}/srs/demo-shop/use-cases"
    before = client.get(path, headers=key)
    assert before.status_code == 404 and "has no approved version yet" in before.json()["message"]
    assert client.post(f"{SRS}/versions/1/submit", headers=hub["other"]).status_code == 200
    approved = client.post(f"{SRS}/versions/1/reviews", json={"decision": "approve"}, headers=hub["owner"])
    assert approved.status_code == 200 and approved.json()["state"] == "approved", approved.text
    listed = client.get(path, headers=key)
    assert listed.status_code == 200, listed.text
    found = listed.json()
    assert (found["document"], found["version"]) == ("demo-shop", 1)
    assert [case["code"] for case in found["use_cases"]] == ["UC ORD-1", "UC ORD-2", "UC RET-1"]
    items = client.get(f"{SRS}/versions/1", headers=hub["reader"]).json()["items"]
    keys = {entry["code"]: entry["key"] for entry in items if entry["kind"] == "use_case"}
    assert {case["code"]: case["key"] for case in found["use_cases"]} == keys
    assert set(found["use_cases"][0]) == {"code", "key"}, "identifiers only"
    assert client.get(f"/v1/plugin-api/projects/{PROJECT}/srs/nowhere/use-cases", headers=key).status_code == 404


# A task's repos and base


def test_task_dispatch_names_its_repos_and_a_base_the_draft_pull_request_goes_into(client, hub, hub_db, github):
    credentials_api.install(github, "evo-agents", permissions=WORKERS_APP)
    github.add_repo(MINE, "evo-agents")
    enabled(client, hub["owner"])
    laptop = node(client, hub_db, hub["other"], "laptop")
    key = hub["key"]
    task = handed(client, key, repos=["lumen-kb-docs", "evo-agents"], base="deploy/test")
    run_id, own = task["run_id"], task_runs.branch_of(task["run_id"])
    entries = [{"repo": name, "branch": own, "base": "deploy/test"} for name in ("evo-agents", "lumen-kb-docs")]
    assert of_run(hub_db, run_id, "repos")[0][0] == entries, "in the project's order, each from the base"
    spec = claim(client, laptop)
    assert spec["id"] == run_id and spec["repos"] == entries
    prompt = autonomy_card.without_card(spec["prompt"])
    assert "- evo-agents: evo-agents/ (from deploy/test, which its draft pull request goes into)" in prompt
    assert "notes" not in [
        line.split(":")[0].removeprefix("- ") for line in prompt.splitlines() if line.startswith("- ")
    ]
    moved(client, laptop, run_id, "running", session_id="sess-1")
    github.push(MINE, "evo-agents", own, "1" * 40)
    opened = in_chats(client, laptop, run_id, "task/pulls", {"repo": "evo-agents", "branch": own, "head": "1" * 40})
    assert opened.status_code == 201, opened.text
    made_pull = github.pull_of(MINE, "evo-agents", own)
    assert (made_pull.base, made_pull.draft) == ("deploy/test", True)

    plain = handed(client, key, task="Another task.")
    assert {"repo", "branch"} == set(of_run(hub_db, plain["run_id"], "repos")[0][0][0]), "no base: the shape of old"
    assert [entry["repo"] for entry in of_run(hub_db, plain["run_id"], "repos")[0][0]] == REPOS
    for body, said in (
        ({"repos": ["nowhere"]}, "the project has no repo nowhere"),
        ({"repos": ["evo-agents", "evo-agents"]}, "names each of its repos once"),
        ({"repos": []}, "at least 1 item"),
        ({"base": "task/3"}, "no branch of the repo's own to start from"),
        ({"base": "HEAD"}, "no branch of the repo's own to start from"),
        ({"base": "a..b"}, "not a branch name git takes"),
        ({"repo": "notes", "branch": "post/x", "repos": ["evo-agents"]}, "is one of its repos"),
        ({"repo": "notes", "branch": "post/x", "base": "post/x"}, "never the branch it pushes to"),
    ):
        response = hand(client, key, **body)
        assert response.status_code == 422 and said in response.text, (body, response.text)


def test_each_repo_of_a_task_may_name_a_base_of_its_own_which_its_draft_pull_request_goes_into(
    client, hub, hub_db, github
):
    credentials_api.install(github, "evo-agents", permissions=WORKERS_APP)
    github.add_repo(MINE, "evo-agents")
    enabled(client, hub["owner"])
    laptop = node(client, hub_db, hub["other"], "laptop")
    key = hub["key"]
    repos = [{"repo": "evo-agents", "base": "main"}, "lumen-kb-docs", {"repo": "notes", "base": "release/2"}]
    task = handed(client, key, repo="evo-agents", branch="qa/checkout", repos=repos, base="deploy/test")
    run_id, own = task["run_id"], task_runs.branch_of(task["run_id"])
    entries = [
        {"repo": "evo-agents", "branch": "qa/checkout", "base": "main"},
        {"repo": "lumen-kb-docs", "branch": own, "base": "deploy/test"},
        {"repo": "notes", "branch": own, "base": "release/2"},
    ]
    assert of_run(hub_db, run_id, "repos")[0][0] == entries, "a repo's own base in place of the task's"
    spec = claim(client, laptop)
    assert spec["id"] == run_id and spec["repos"] == entries
    lines = {
        line.split(":")[0].removeprefix("- "): line
        for line in autonomy_card.without_card(spec["prompt"]).splitlines()
        if line.startswith("- ")
    }
    assert lines["evo-agents"].endswith("(from main, which its draft pull request goes into)")
    assert lines["notes"].endswith("(from release/2, which its draft pull request goes into)")
    moved(client, laptop, run_id, "running", session_id="sess-1")
    github.push(MINE, "evo-agents", "qa/checkout", "2" * 40)
    body = {"repo": "evo-agents", "branch": "qa/checkout", "head": "2" * 40}
    opened = in_chats(client, laptop, run_id, "task/pulls", body)
    assert opened.status_code == 201, opened.text
    made_pull = github.pull_of(MINE, "evo-agents", "qa/checkout")
    assert (made_pull.base, made_pull.draft) == ("main", True), "into the repo's own base, not the task's"

    mixed = handed(client, key, task="A mixed task.", repos=["lumen-kb-docs", {"repo": "notes", "base": "deploy/test"}])
    plain_entry, based_entry = of_run(hub_db, mixed["run_id"], "repos")[0][0]
    assert set(plain_entry) == {"repo", "branch"}, "a name without the task's base: the repo's default branch"
    assert based_entry == {"repo": "notes", "branch": task_runs.branch_of(mixed["run_id"]), "base": "deploy/test"}
    for body, said in (
        ({"repos": [{"repo": "nowhere", "base": "deploy/test"}]}, "the project has no repo nowhere"),
        ({"repos": ["notes", {"repo": "notes", "base": "deploy/test"}]}, "names each of its repos once"),
        ({"repos": [{"repo": "notes", "base": "task/3"}]}, "the base of notes: task/3 is no branch of the repo's own"),
        ({"repos": [{"repo": "notes", "base": "HEAD"}]}, "the base of notes: HEAD is no branch of the repo's own"),
        ({"repos": [{"repo": "notes", "base": "a..b"}]}, "the base of notes: 'a..b' is not a branch name git takes"),
        ({"repos": [{"repo": "notes"}]}, "Field required"),
        ({"repos": [{"repo": "notes", "base": "deploy/test", "branch": "x"}]}, "Extra inputs are not permitted"),
        ({"repo": "notes", "branch": "post/x", "repos": [{"repo": "evo-agents", "base": "main"}]}, "is one of its"),
        ({"repo": "notes", "branch": "post/x", "repos": [{"repo": "notes", "base": "post/x"}]}, "never the branch"),
    ):
        response = hand(client, key, **body)
        assert response.status_code == 422 and said in response.text, (body, response.text)
