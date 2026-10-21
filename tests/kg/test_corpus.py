import json
import os
import random
import stat

from evo_agents.cli import main
from evo_agents.kg import retention
from evo_agents.kg.audit import audit_path
from evo_agents.kg.build import build_project, verify_build
from evo_agents.kg.ids import uuid7
from evo_agents.kg.project import load_project_at
from evo_agents.kg.scrub import blob_refs
from evo_agents.kg.sync import accept_removals, sync_project, sync_source
from tests.kg.conftest import FakeSource, doc
from tests.kg.projects import commit, make_project, write
from tests.kg.test_scrub import snapshot

ZERO = {"logs": "0", "history": "0", "memo": "0"}


def alive_ids(corpus):
    return sorted(r.item_id for r in corpus.items())


def test_first_sync_logs_and_merges(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a"), doc("b")], cursor={"page": 2})
    result = sync_source(project, "docs")
    assert result.ok, result.issues
    corpus = project.corpus()
    assert alive_ids(corpus) == ["docs:doc:a", "docs:doc:b"]
    assert corpus.cursor("docs") == {"page": 2}
    record = corpus.get("docs:doc:a").record
    assert "text" not in record["body"] and record["body"]["blob"].startswith("sha256:")
    assert corpus.body_text(record).startswith("# a")


def test_replay_shuffled_and_duplicated_gives_same_state(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a"), doc("b"), doc("c")])
    sync_source(project, "docs")
    fake.set(items=[doc("a", "# a\n\nedited\n", rev="2", rev_time="2026-10-02T00:00:00Z"), doc("b"), doc("d")])
    sync_source(project, "docs")  # edits a, adds d; the listing drops c, which the ratio guard holds
    assert accept_removals(project, "docs") == 1
    fake.set(items=[doc("a", "# a\n\nedited\n", rev="2", rev_time="2026-10-02T00:00:00Z"), doc("d")])
    sync_source(project, "docs")
    assert accept_removals(project, "docs") == 1  # b
    corpus = project.corpus()
    assert alive_ids(corpus) == ["docs:doc:a", "docs:doc:d"]
    expected = corpus.state_digest()

    files = corpus.log_files()
    rng = random.Random(7)
    for _ in range(5):
        order = files * 2
        rng.shuffle(order)
        assert corpus.state_digest(corpus.rebuild_state(order)) == expected


def test_truncated_run_removes_nothing(fake_project):
    project, fake = fake_project
    fake.set(items=[doc(k) for k in "abcde"])
    sync_source(project, "docs")
    fake.set(items=[doc(k) for k in "abcde"], die_after=2)
    result = sync_source(project, "docs")
    assert not result.ok
    assert result.removals == 0
    assert any("without closed" in issue for issue in result.issues)
    assert len(alive_ids(project.corpus())) == 5


def test_killed_connector_removes_nothing(fake_project):
    project, fake = fake_project
    fake.set(items=[doc(k) for k in "abcde"])
    sync_source(project, "docs")
    result = sync_source(project, "docs", kill_after=3)
    assert not result.ok and result.removals == 0
    assert len(alive_ids(project.corpus())) == 5


def test_ratio_guard_holds_then_accept_applies(fake_project):
    project, fake = fake_project
    fake.set(items=[doc(f"d{i:02}") for i in range(20)])
    sync_source(project, "docs")
    fake.set(items=[doc(f"d{i:02}") for i in range(10)])
    result = sync_source(project, "docs")
    assert result.ok and result.removals == 0
    assert result.held and "max_removal_ratio" in result.held[0]["reason"]
    corpus = project.corpus()
    assert len(alive_ids(corpus)) == 20
    assert accept_removals(project, "docs") == 10
    assert len(alive_ids(project.corpus())) == 10
    assert project.corpus().held() == []


def test_empty_listing_is_held(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a")])
    sync_source(project, "docs")
    fake.set(items=[])
    result = sync_source(project, "docs")
    assert result.held and "empty listing" in result.held[0]["reason"]
    assert alive_ids(project.corpus()) == ["docs:doc:a"]


def test_items_reported_as_errors_are_not_deleted(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a"), doc("b")])
    sync_source(project, "docs")
    fake.set(items=[doc("a")], errors=["b"])
    result = sync_source(project, "docs")
    assert result.ok and result.removals == 0
    assert alive_ids(project.corpus()) == ["docs:doc:a", "docs:doc:b"]


def test_explicit_tombstone_then_restore(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a")], listing=False)
    sync_source(project, "docs")
    fake.set(tombstones=["a"], listing=False)
    sync_source(project, "docs")
    corpus = project.corpus()
    assert corpus.get("docs:doc:a").deleted
    assert corpus.get("docs:doc:a").rev == "1"  # filled from the known record
    fake.set(items=[doc("a")], listing=False)  # same revision comes back, e.g. restored from trash
    sync_source(project, "docs")
    assert not project.corpus().get("docs:doc:a").deleted


def test_stale_replay_does_not_regress(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a", "# a\n\nnew\n", rev="5", rev_time="2026-10-05T00:00:00Z")], listing=False)
    sync_source(project, "docs")
    fake.set(items=[doc("a", "# a\n\nold\n", rev="4", rev_time="2026-10-04T00:00:00Z")], listing=False)
    sync_source(project, "docs")
    assert project.corpus().get("docs:doc:a").rev == "5"


def test_corrupt_item_is_rejected_and_fails_the_run(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a"), {**doc("b"), "corrupt": True}])
    result = sync_source(project, "docs")
    assert not result.ok and result.rejected == 1
    assert alive_ids(project.corpus()) == ["docs:doc:a"]


def test_unknown_protocol_major_rejects_everything(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a")], bad_hello=True)
    result = sync_source(project, "docs")
    assert not result.ok
    assert alive_ids(project.corpus()) == []


def test_corpus_lives_outside_repos_with_private_mode(fake_project, kg_env):
    project, fake = fake_project
    fake.set(items=[doc("a")])
    sync_source(project, "docs")
    root = project.corpus().root
    assert root.parent == kg_env
    if os.name != "nt":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert project.harness.root not in root.parents


def test_record_run_writes_in_the_callers_transaction_and_run_ids_lists_it(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a")])
    synced = sync_source(project, "docs")
    corpus = project.corpus()
    assert corpus.run_ids() == {synced.run_id}
    corpus.record_run("run-2", source="docs", connector="fake", status="ok", detail={"hub": True})
    corpus.db.rollback()
    assert corpus.run_ids() == {synced.run_id}  # nothing was committed
    with corpus.db:
        corpus.record_run("run-2", source="docs", connector="fake", status="ok", detail={"hub": True})
    assert corpus.run_ids() == {synced.run_id, "run-2"}
    (row,) = corpus.runs(source="docs", status="ok", limit=1)  # newest first: run-2 sorts after the uuid
    assert row["run_id"] == "run-2" and row["detail"] == {"hub": True} and row["version"] is None


def test_record_cursor_keeps_the_newest_run_s_whatever_the_order(fake_project):
    project, _ = fake_project
    corpus = project.corpus()
    older, newer = "0190a000-0000-7000-8000-000000000001", "0190a000-0000-7000-8000-000000000002"
    with corpus.db:
        corpus.record_cursor("docs", {"commit": "b" * 40}, newer)
        corpus.record_cursor("docs", {"commit": "a" * 40}, older)  # merged later, read earlier
    assert corpus.cursor("docs") == {"commit": "b" * 40}
    with corpus.db:
        corpus.record_cursor("docs", {"commit": "c" * 40}, "0190a000-0000-7000-8000-000000000003")
    assert corpus.cursor("docs") == {"commit": "c" * 40}
    corpus.record_cursor("docs", {"commit": "d" * 40}, "0190a000-0000-7000-8000-000000000004")
    corpus.db.rollback()
    assert corpus.cursor("docs") == {"commit": "c" * 40}  # nothing was committed


# Retention of run logs, blobs and runs rows (evo_agents.kg.retention)


def refs_of(corpus, names) -> set[str]:
    return {
        ref
        for name in names
        for record in corpus.read_log(corpus.log_dir / name)
        if record.get("type") == "item"
        for ref in blob_refs(record)
    }


def prune_lines(root) -> list[dict]:
    lines = [json.loads(line) for line in audit_path(root).read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line["event"] == "prune"]


def test_prune_counts_then_deletes_the_run_logs_the_merged_state_no_longer_uses(fake_project):
    project, fake = fake_project
    edited = doc("a", "# a\n\nedited\n", rev="2", rev_time="2026-10-02T00:00:00Z")
    runs = []
    for items, listing in (
        ([doc("a"), doc("b")], True),
        ([edited, doc("b")], True),
        ([edited, doc("b")], True),
        ([edited], False),  # wins a only: the third run still holds b
    ):
        fake.set(items=items, listing=listing)
        runs.append(sync_source(project, "docs").run_id)
    first, second, third, fourth = (f"{run_id}.jsonl.gz" for run_id in runs)
    corpus = project.corpus()
    state = corpus.state_digest()
    old = refs_of(corpus, [first, second]) - refs_of(corpus, [third, fourth])
    assert old  # the first text of a
    before = snapshot(project.root)

    young = retention.plan(project.name, project.root, {"logs": "1d"}, policy=project.policy)
    assert young.logs == [] and young.rewrite == {} and young.blobs == [] and young.runs == []
    assert young.kept[retention.WINDOW] == 4

    found = retention.plan(project.name, project.root, ZERO, policy=project.policy)
    assert found.logs == [first, second]  # every record of theirs lost to a later run
    assert found.rewrite == {third: 1}  # its a lost to the fourth run, its b holds
    assert found.kept[retention.IN_USE] == 1 and set(found.blobs) == old
    assert sorted(found.runs) == runs[:2]  # no log left, nothing in use, not a source's newest
    assert found.counts()["logs"] == 2 and found.size > 0
    assert snapshot(project.root) == before  # counting changes nothing

    deleted = retention.apply(found)
    assert (deleted["logs"], deleted["rewritten"], deleted["dropped"], deleted["runs"]) == (2, 1, 1, 2)
    assert deleted["blobs"] == len(old) and deleted["blob_bytes"] > 0 and deleted["size_after"] > 0
    corpus = project.corpus()
    assert [p.name for p in corpus.log_files()] == [third, fourth]
    assert corpus.state_digest(corpus.rebuild_state()) == state == corpus.state_digest()
    for ref in refs_of(corpus, [third, fourth]):  # no log names a blob that is gone
        assert corpus.blob_path(f"sha256:{ref}").exists()
    assert not any(corpus.blob_path(f"sha256:{ref}").exists() for ref in old)
    assert corpus.body_text(corpus.get("docs:doc:a").record) == "# a\n\nedited\n"
    assert corpus.run_ids() == set(runs[2:])
    [line] = prune_lines(project.root)
    assert line["logs"] == 2 and line["rewritten"] == 1 and line["runs"] == 2 and line["periods"]["logs"] == "0"

    again = retention.plan(project.name, project.root, ZERO, policy=project.policy)
    assert again.empty and again.kept[retention.IN_USE] == 2


def hub_project(tmp_path, kg_env):
    """A harness with a hub sink that clears internal, a source it clears and one it does not."""
    root = tmp_path / "hub-harness"
    root.mkdir()
    docs, private = FakeSource(tmp_path / "docs.json"), FakeSource(tmp_path / "private.json")
    (root / "harness.yaml").write_text("name: hubbed\nrepos: []\nknowledge_file: knowledge.yaml\n")
    sources = "".join(
        f"""  - id: {sid}
    connector: "python:tests.kg.fakes:run"
    config: {{file: '{fake.path}'}}
    label: {{level: {level}, integrity: U}}
"""
        for sid, fake, level in (("docs", docs, "internal"), ("private", private, "customer"))
    )
    (root / "knowledge.yaml").write_text(
        f"""version: 1
project: hubbed
policy:
  levels: [public, internal, customer]
  sinks:
    - {{id: agent, kind: agent-session, clearance: {{level: customer}}}}
    - {{id: hub, kind: hub, clearance: {{level: internal}}}}
sources:
{sources}"""
    )
    return load_project_at(root, kg_env), docs, private


def two_runs(project, fake, source) -> list[str]:
    runs = []
    for text in ("one", "two"):
        fake.set(items=[doc("a", f"# a\n\n{text}\n")])
        runs.append(sync_source(project, source).run_id)
    return runs


def test_prune_never_touches_a_run_log_the_hub_has_not_received(tmp_path, kg_env):
    project, docs, private = hub_project(tmp_path, kg_env)
    docs_runs, private_runs = two_runs(project, docs, "docs"), two_runs(project, private, "private")
    policy = project.policy
    found = retention.plan(project.name, project.root, ZERO, policy=policy)
    assert found.logs == [f"{private_runs[0]}.jsonl.gz"]  # above the hub sink: it never leaves the machine
    assert found.kept[retention.HUB] == 2 and found.kept[retention.IN_USE] == 1
    unknown = retention.plan(project.name, project.root, ZERO)  # a project whose config does not load
    assert unknown.logs == [] and unknown.kept[retention.HUB] == 4 and unknown.blobs == []

    corpus = project.corpus()
    corpus.record_on_hub([docs_runs[0]], "https://hub.test")
    found = retention.plan(project.name, project.root, ZERO, policy=policy)
    assert found.logs == sorted(f"{run_id}.jsonl.gz" for run_id in (docs_runs[0], private_runs[0]))
    assert found.kept[retention.HUB] == 1  # the second docs run is in use, and not on the hub either
    retention.apply(found)
    assert project.corpus().runs_on_hub() == set()  # the receipt went with its log


def test_a_project_without_a_hub_sink_waits_for_the_hub_once_a_push_recorded_a_run(fake_project):
    project, fake = fake_project
    runs = two_runs(project, fake, "docs")
    assert retention.plan(project.name, project.root, ZERO, policy=project.policy).logs == [f"{runs[0]}.jsonl.gz"]
    project.corpus().record_on_hub([runs[1]], "https://hub.test")  # the hub takes this project's pushes after all
    found = retention.plan(project.name, project.root, ZERO, policy=project.policy)
    assert found.logs == [] and found.kept[retention.HUB] == 1


def test_prune_deletes_old_runs_rows_without_a_log_but_the_newest_of_each_source(fake_project):
    project, fake = fake_project
    fake.set(items=[doc("a")])
    synced = sync_source(project, "docs").run_id
    corpus = project.corpus()
    unchanged = [uuid7() for _ in range(3)]  # a git source at the commit it read: a row, no log
    failed = uuid7()
    with corpus.db:
        for run_id in unchanged:
            corpus.record_run(run_id, source="docs", connector="git", status="ok", detail={"unchanged": True})
        corpus.record_run(failed, source="docs", connector="git", status="failed", detail={})
    assert retention.plan(project.name, project.root, {"logs": "1h"}).runs == []
    found = retention.plan(project.name, project.root, ZERO)
    assert sorted(found.runs) == unchanged[:2] and found.logs == []  # the synced run's log holds a
    assert retention.apply(found)["runs"] == 2
    corpus = project.corpus()
    assert corpus.run_ids() == {synced, unchanged[2], failed}
    assert corpus.runs("docs", limit=1)[0]["run_id"] == failed
    assert corpus.runs("docs", limit=1, status="ok")[0]["run_id"] == unchanged[2]


def test_kg_prune_counts_by_default_and_a_cold_build_gives_the_same_hash_after_it_deletes(tmp_path, kg_env, capsys):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    for version in range(3):
        write(app / "docs/notes.md", f"# Notes\n\nVersion {version} of the notes, about app/search.py.\n")
        commit(app, f"notes {version}")
        assert all(r.ok for r in sync_project(project))
        report = build_project(project, verify=True, cold=True)
        assert report.ok and report.verify["match"], report.verify
    content_hash = report.content_hash
    before = snapshot(project.root)
    args = ["kg", "prune", "--project", project.name, "--logs", "0", "--history", "0", "--memo", "0"]

    assert main(args) == 0
    out = capsys.readouterr().out
    assert "Count only, nothing was deleted." in out and "Scheduled pruning is off" in out
    assert f"{project.name}: " in out and "graph history: delete" in out
    assert snapshot(project.root) == before
    assert main([*args, "--delete"]) == 2  # no terminal to ask, no --yes
    assert snapshot(project.root) == before

    assert main([*args, "--delete", "--yes", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    (deleted,) = [p["deleted"] for p in body["projects"]]
    assert deleted["logs"] > 0 and deleted["history"] > 0 and deleted["builds"] == 1 and deleted["memo"] > 0
    assert verify_build(project, content_hash, cold=True)["match"]
    again = build_project(project, verify=True, cold=True)  # nothing changed: the build is skipped, then verified
    assert again.skipped and again.verify["match"] and again.content_hash == content_hash
    assert build_project(project, force=True).content_hash == content_hash
