"""Old copies of secrets on the hub: built graphs and source blobs that hold one, found and deleted by a hub admin
(``evo_agents.hub.server.kg_scrub``, ``evo-agents hub kg scrub``).

A machine pushed a run whose document held a password (its connector did not redact it) and the worker built a graph
from it; then a run with the value replaced, and a second build. Checked here: a scan, page by page, finds the first
run's blobs and the first graph old, a document every run still holds live, and the graph the tools read newest, and
never answers a value; it changes nothing and is refused to anyone but a hub admin. A purge deletes the old objects
from the bucket and the ``blobs`` rows, marks the builds of a deleted graph pruned, drops it from the api's cache,
writes one audit row counting what went, keeps what is in use, and leaves a project the worker still builds and the
tools still read. A run log missing from the bucket stops a scan and a purge rather than let a blob in use count as
old. A source blob is read as the file the items of the run logs name, as on a machine: code is not taken for a
secret, and a config value only a config file makes a password is found. Runs whose logs a local purge rewrote are
not pushed again, and a rewritten log of a run the hub lacks pushes and builds. The CLI's ``hub kg scrub`` runs
against ``hub serve``, and ``--purge`` needs ``--yes`` there.

The api and the worker run in this process, as in ``tests.hub.test_kg_retention``, whose helpers these tests use;
the blob store is moto's server (``tests.hub.s3``)."""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from evo_agents.hub.blobs import blob_key
from evo_agents.hub.client import Hub
from evo_agents.hub.kg_ingest import read_run_log
from evo_agents.hub.kg_push import Pusher
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.scrub import KEPT, blob_refs, blob_secrets, holding_sources, purge, scan_project, secrets_in
from evo_agents.kg.sync import sync_project
from tests.hub.test_kg import (
    PROJECT,
    REGISTRATION,
    Machine,
    build_rows,
    doc,
    ok,
    open_hub,
    push,
    query,
    sign_in,
    tool_ok,
)
from tests.hub.test_kg_retention import kg_graphs, objects, rebuild, run_worker
from tests.kg.test_redact import GITHUB, LITERAL

PASSWORD_LINE = f"Connect with DB_PASSWORD: {LITERAL}"
CODE_LINE = "client_secret = os_value_q8"  # in code, an expression; a document would take it for a literal
COMPOSE_LINE = "POSTGRES_PASSWORD: lmsadmin"  # a password in a config file only: a plain word under an upper-case name
REDACTED_LINE = "Connect with DB_PASSWORD: [redacted:password]"
DEPLOY_LINE = f"The deploy key is {GITHUB}"
VALUES = (LITERAL, GITHUB)


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    client, found = open_hub(hub_db, tmp_path, s3)
    try:
        yield found
    finally:
        client.__exit__(None, None, None)


def leaked(text: str) -> list[str]:
    return [value for value in VALUES if value in text]


def built(hub, laptop: Machine, guide_line: str, rev: str) -> dict:
    """Sync the docs (a guide holding ``guide_line``, notes holding a deploy key), push, build; the build."""
    laptop.sync("docs", doc("guide", guide_line, rev=rev), doc("notes", DEPLOY_LINE), doc("setup", "Install it."))
    assert push(hub, laptop).ok
    run_worker(hub)
    build = build_rows(hub)[0]
    assert build["status"] == "succeeded", build
    tool_ok(hub, "kg_search", {"query": "guide"})  # the api caches the graph it reads
    return build


@pytest.fixture
def leaky(hub, tmp_path):
    """Two builds: the first from a guide holding a password, the second from the guide with it redacted. The
    builds, and the hashes of the guide's first blobs (its body and the fragment holding the line)."""
    laptop = Machine(tmp_path, "laptop")
    first = built(hub, laptop, PASSWORD_LINE, "1")
    second = built(hub, laptop, REDACTED_LINE, "2")
    assert second["artifact_sha256"] != first["artifact_sha256"]
    return first, second


def scan(hub, who: str = "admin", **body):
    return hub.client.post("/v1/admin/kg/scrub", json={"project": PROJECT, **body}, headers=getattr(hub, who))


def scan_all(hub, limit: int) -> tuple[list[dict], int]:
    """Every page of a scan; the objects found and the pages read."""
    found, after, pages = [], None, 0
    while True:
        response = scan(hub, limit=limit, **({"after": after} if after else {}))
        assert response.status_code == 200, response.text
        page = response.json()
        assert not leaked(response.text)
        found += page["found"]
        pages += 1
        after = page["next"]
        if after is None:
            return found, pages


def by_state(found: list[dict]) -> dict[str, set[tuple[str, str]]]:
    states: dict[str, set[tuple[str, str]]] = {}
    for item in found:
        states.setdefault(item["state"], set()).add((item["kind"], item["sha256"]))
    return states


def held(hub, kinds=("kg-blob", "kg-graph")) -> set[str]:
    blobs = tables.blobs
    return {row[0] for row in query(hub.db, select(blobs.c.sha256).where(blobs.c.kind.in_(kinds)))}


def test_scrub_scan_finds_old_live_and_newest_copies_and_changes_nothing(hub, leaky):
    first, second = leaky
    before = (objects(hub), held(hub), build_rows(hub))

    found, pages = scan_all(hub, limit=2)
    assert pages > 2
    states = by_state(found)
    assert states["old"] >= {("kg-graph", first["artifact_sha256"])}
    assert states["newest"] == {("kg-graph", second["artifact_sha256"])}  # the notes' key is in it
    old_blobs = {sha for kind, sha in states["old"] if kind == "kg-blob"}
    live_blobs = {sha for kind, sha in states["live"] if kind == "kg-blob"}
    assert len(old_blobs) == 2 and len(live_blobs) == 2  # body and fragment of the guide, then of the notes
    secrets = {item["sha256"]: item["secrets"] for item in found}
    assert all(secrets[sha] == {"password": 1} for sha in old_blobs)
    assert all(secrets[sha] == {"github_token": 1} for sha in live_blobs)
    graph = next(item for item in found if item["sha256"] == first["artifact_sha256"])
    assert graph["builds"] == [first["id"]] and graph["secrets"]["password"] >= 1
    assert (objects(hub), held(hub), build_rows(hub)) == before

    one, _ = scan_all(hub, limit=1000)
    assert sorted(one, key=lambda item: item["sha256"]) == sorted(found, key=lambda item: item["sha256"])


def test_scrub_is_for_hub_admins_on_registered_projects(hub, leaky):
    for who in ("alice", "dave"):
        assert scan(hub, who).status_code == 403
        refused = hub.client.post(
            "/v1/admin/kg/scrub/purge", json={"project": PROJECT, "sha256": ["0" * 64]}, headers=getattr(hub, who)
        )
        assert refused.status_code == 403
    missing = hub.client.post("/v1/admin/kg/scrub", json={"project": "nope"}, headers=hub.admin)
    assert missing.status_code == 404
    bad = hub.client.post("/v1/admin/kg/scrub/purge", json={"project": PROJECT, "sha256": ["x"]}, headers=hub.admin)
    assert bad.status_code == 422


def test_scrub_calls_no_blob_old_without_the_run_logs_that_say_which_are_live(hub, leaky):
    ingests = tables.kg_ingests
    newest = query(hub.db, select(ingests.c.log_sha256).order_by(ingests.c.run_id.desc()).limit(1))[0][0]
    hub.s3.client().delete_object(Bucket=hub.s3.bucket, Key=blob_key(newest))
    before = (objects(hub), held(hub))

    refused = scan(hub, limit=1000)
    assert refused.status_code == 503 and "is not in the blob store" in refused.json()["message"]
    blobs = sorted(held(hub, ("kg-blob",)))
    purge = hub.client.post("/v1/admin/kg/scrub/purge", json={"project": PROJECT, "sha256": blobs}, headers=hub.admin)
    assert purge.status_code == 503
    assert (objects(hub), held(hub)) == before


def test_scrub_purge_deletes_old_copies_keeps_what_is_in_use_and_audits(hub, leaky):
    first, second = leaky
    found, _ = scan_all(hub, limit=1000)
    states = by_state(found)
    old = {sha for _, sha in states["old"]}
    in_use = {sha for state in ("live", "newest") for _, sha in states[state]}
    stranger = "f" * 64
    graphs = hub.api_dir / "kg" / "graphs" / PROJECT
    assert (graphs / f"{first['artifact_sha256']}.sqlite").exists()
    present = objects(hub)

    response = hub.client.post(
        "/v1/admin/kg/scrub/purge",
        json={"project": PROJECT, "sha256": sorted(old | in_use | {stranger})},
        headers=hub.admin,
    )
    assert response.status_code == 200, response.text
    assert not leaked(response.text)
    answer = response.json()
    assert {item["sha256"] for item in answer["purged"]} == old
    reasons = {item["sha256"]: item["reason"] for item in answer["kept"]}
    assert reasons == {**{sha: state for state in ("live", "newest") for _, sha in states[state]}, stranger: "not held"}
    assert answer["builds"] == 1 and answer["deleted"] == len(old) and answer["pending"] == 0
    assert answer["deleted_bytes"] == sum(present[sha] for sha in old)

    after = objects(hub)
    assert not old & set(after) and in_use <= set(after)
    assert not old & held(hub) and in_use <= held(hub)
    assert kg_graphs(hub) == {second["artifact_sha256"]}
    assert not (graphs / f"{first['artifact_sha256']}.sqlite").exists()
    rows = {build["id"]: build for build in build_rows(hub)}
    assert rows[first["id"]]["artifact_sha256"] is None and rows[first["id"]]["artifact_pruned_at"]
    assert rows[second["id"]]["artifact_sha256"] == second["artifact_sha256"]
    audit = tables.audit
    trail = query(hub.db, select(audit.c.action, audit.c.target).where(audit.c.action == "kg.scrub"))
    assert trail == [("kg.scrub", f"{PROJECT} artifacts=1 blobs=2")]

    again = rebuild(hub)  # the worker finds every blob the merged runs use
    assert again["content_hash"] == second["content_hash"]
    assert tool_ok(hub, "kg_search", {"query": "guide"})["structuredContent"]["results"]
    found_again, _ = scan_all(hub, limit=1000)
    assert "old" not in by_state(found_again)

    nothing = hub.client.post(
        "/v1/admin/kg/scrub/purge", json={"project": PROJECT, "sha256": sorted(old)}, headers=hub.admin
    )
    assert nothing.status_code == 200 and nothing.json()["purged"] == []
    assert {item["reason"] for item in nothing.json()["kept"]} == {"not held"}
    assert len(query(hub.db, select(audit.c.id).where(audit.c.action == "kg.scrub"))) == 1


def test_the_cli_scrubs_as_a_hub_admin_and_purges_with_yes_only(hub_db, tmp_path, s3):
    laptop = Machine(tmp_path, "laptop")
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN, **s3.env()) as served:
        admin = Hub(served.url, live.insert_token(hub_db, live.ADMIN))
        admin.call("PUT", f"/v1/projects/{PROJECT}", REGISTRATION)
        admin.call("PUT", f"/v1/admin/projects/{PROJECT}/grants/alice", {"role": "writer", "max_level": "internal"})
        alice = Hub(served.url, live.insert_token(hub_db, "alice"))
        for line, rev in ((PASSWORD_LINE, "1"), (REDACTED_LINE, "2")):
            laptop.sync("docs", doc("guide", line, rev=rev), doc("setup", "Install it."))
            assert Pusher(alice, laptop.project()).push().ok
        home = sign_in(tmp_path / "admin-home", served.url, live.ADMIN, live.insert_token(hub_db, live.ADMIN))
        env = pg.clean_env(HOME=str(home))

        counted = ok(pg.cli(["hub", "kg", "scrub", "--json"], env=env))
        body = json.loads(counted.stdout)
        assert_json_keys("hub kg scrub", body)
        assert not leaked(counted.stdout + counted.stderr)
        (report,) = body["projects"]
        assert report["project"] == PROJECT and report["purged"] is None
        assert [(item["kind"], item["state"]) for item in report["found"]] == [("kg-blob", "old")] * 2

        text = ok(pg.cli(["hub", "kg", "scrub", "--project", PROJECT], env=env))
        assert not leaked(text.stdout + text.stderr)
        assert f"{PROJECT}: 2 of " in text.stdout and "2 old, 0 live source blob(s), 0 newest graph(s)" in text.stdout
        assert "Count only, nothing was deleted. --purge deletes the 2 old object(s)." in text.stdout
        assert f"{PROJECT}: " in text.stderr  # progress

        refused = pg.cli(["hub", "kg", "scrub", "--purge"], env=env)
        assert refused.returncode == 2 and "--yes" in refused.stderr
        purged = ok(pg.cli(["hub", "kg", "scrub", "--project", PROJECT, "--purge", "--yes"], env=env))
        assert "About to delete 2 old object(s) of the hub, 0 graph(s) and 2 source blob(s)" in purged.stdout
        assert f"{PROJECT}: deleted 2 object(s)" in purged.stdout and not leaked(purged.stdout + purged.stderr)
        after = json.loads(ok(pg.cli(["hub", "kg", "scrub", "--purge", "--yes", "--json"], env=env)).stdout)
        assert after["purge"] is True and after["projects"][0]["found"] == []

        member = sign_in(tmp_path / "alice-home", served.url, "alice", live.insert_token(hub_db, "alice"))
        denied = pg.cli(["hub", "kg", "scrub", "--project", PROJECT], env=pg.clean_env(HOME=str(member)))
        assert denied.returncode == 1 and "admin" in denied.stderr


# Reading a source blob as its file, and pushing logs a purge rewrote


def item_blobs(machine: Machine, item_id: str) -> set[str]:
    """The blobs the record of ``item_id`` in the machine's corpus refers to."""
    corpus = Corpus(PROJECT, machine.home)
    try:
        return set(blob_refs(corpus.get(item_id).record))
    finally:
        corpus.close()


def leaky_blobs(machine: Machine, item_id: str) -> set[str]:
    """The blobs of ``item_id`` in the machine's corpus that hold a value."""
    corpus = Corpus(PROJECT, machine.home)
    try:
        return {sha for sha in item_blobs(machine, item_id) if leaked(corpus.get_blob(f"sha256:{sha}"))}
    finally:
        corpus.close()


def sync_unlisted(machine: Machine, *items: dict) -> None:
    """Sync the docs with ``items`` and no listing, so no other item is removed."""
    machine.files["docs"].write_text(json.dumps({"items": list(items), "listing": False}), encoding="utf-8")
    assert all(result.ok for result in sync_project(machine.project(), ["docs"]))


def purge_here(machine: Machine):
    """``kg scrub --purge`` on the machine; the scan it purged."""
    project = machine.project()
    with holding_sources(project.root):
        found = scan_project(PROJECT, project.root)
        purge(found)
    return found


def test_scrub_reads_a_source_blob_as_the_file_its_run_logs_name(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    settings = doc("settings", CODE_LINE, path="tests/hub/test_config.py")
    compose = doc("compose", COMPOSE_LINE, path="docker-compose.yml")
    laptop.sync("docs", settings, compose, doc("setup", "Install it."))
    assert push(hub, laptop).ok
    assert secrets_in(settings["text"]) and not secrets_in(compose["text"])  # read as documents: both wrong
    code = item_blobs(laptop, "docs:doc:settings")
    config = item_blobs(laptop, "docs:doc:compose")
    corpus = Corpus(PROJECT, laptop.home)
    holding = {sha for sha in config if blob_secrets(corpus.get_blob(f"sha256:{sha}"), ["docker-compose.yml"])}
    corpus.close()
    assert holding

    found, _ = scan_all(hub, limit=1)
    assert {(item["sha256"], item["state"]) for item in found} == {(sha, "live") for sha in holding}
    assert all(item["secrets"] == {"password": 1} for item in found)
    response = hub.client.post(
        "/v1/admin/kg/scrub/purge", json={"project": PROJECT, "sha256": sorted(code | holding)}, headers=hub.admin
    )
    assert response.status_code == 200, response.text
    reasons = {item["sha256"]: item["reason"] for item in response.json()["kept"]}
    assert reasons == {**dict.fromkeys(code, "clean"), **dict.fromkeys(holding, "live")}

    laptop.sync("docs", settings, doc("compose", "POSTGRES_PASSWORD: [redacted:password]", rev="2"), doc("setup", "x"))
    assert push(hub, laptop).ok  # a new run: the hub reads its log too, and the old blobs keep their path
    found, _ = scan_all(hub, limit=1000)
    assert {(item["sha256"], item["state"]) for item in found} == {(sha, "old") for sha in holding}


def test_push_sends_no_run_again_after_a_purge_rewrote_its_log(hub, tmp_path):
    laptop = Machine(tmp_path, "laptop")
    (first,) = laptop.sync("docs", doc("guide", PASSWORD_LINE), doc("setup", "Install it."))
    assert push(hub, laptop).ok
    log = laptop.home / PROJECT / "log" / f"{first}.jsonl.gz"
    pushed_log = hashlib.sha256(log.read_bytes()).hexdigest()
    leaky = leaky_blobs(laptop, "docs:doc:guide")
    sync_unlisted(laptop, doc("guide", REDACTED_LINE, rev="2"))  # the first run still holds setup's record

    found = purge_here(laptop)
    assert found.rewrite == [log.name] and {f.state for f in found.blobs} == {KEPT}
    assert {f.sha256 for f in found.blobs} == leaky
    assert found.deleted["rewritten"] == 1 and found.deleted["blobs"] == len(found.blobs)
    assert hashlib.sha256(log.read_bytes()).hexdigest() != pushed_log
    logs = len(held(hub, ("kg-log",)))

    report = push(hub, laptop)
    assert report.ok, report.errors
    assert report.present == 1 and len(report.pushed) == 1 and first not in report.pushed
    assert len(held(hub, ("kg-log",))) == logs + 1  # the rewritten log was not sent
    ingests = tables.kg_ingests
    assert query(hub.db, select(ingests.c.log_sha256).where(ingests.c.run_id == first)) == [(pushed_log,)]
    run_worker(hub)
    assert build_rows(hub)[0]["status"] == "succeeded"
    assert tool_ok(hub, "kg_search", {"query": "guide"})["structuredContent"]["results"]
    assert leaky <= held(hub, ("kg-blob",))  # pushed before the purge: the hub's scrub deletes them


def test_a_log_a_purge_rewrote_pushes_and_builds_without_its_old_blobs(hub, tmp_path):
    desk = Machine(tmp_path, "desk")
    (first,) = desk.sync("docs", doc("guide", PASSWORD_LINE), doc("setup", "Install it."))
    leaky = leaky_blobs(desk, "docs:doc:guide")
    sync_unlisted(desk, doc("guide", REDACTED_LINE, rev="2"))
    found = purge_here(desk)
    assert found.rewrite == [f"{first}.jsonl.gz"] and {f.sha256 for f in found.blobs} == leaky
    assert found.deleted["blobs"] == len(leaky) > 0

    log = desk.home / PROJECT / "log" / f"{first}.jsonl.gz"
    project = desk.project()
    checked = read_run_log(log, PROJECT, first, project.policy, lambda label: True)  # the hub's strict reader
    assert checked.items == 1 and not checked.blobs & leaky
    report = push(hub, desk)
    assert report.ok and len(report.pushed) == 2, report.errors
    assert not leaky & held(hub, ("kg-blob",)) and not leaky & set(objects(hub))
    run_worker(hub)
    build = build_rows(hub)[0]
    assert build["status"] == "succeeded", build
    results = tool_ok(hub, "kg_search", {"query": "setup"})["structuredContent"]["results"]
    assert results and not leaked(json.dumps(results))
    found, _ = scan_all(hub, limit=1000)
    assert found == []  # nothing on the hub holds the password
