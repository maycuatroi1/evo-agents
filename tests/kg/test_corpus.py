import random
import stat

from evo_agents.kg.sync import accept_removals, sync_source
from tests.kg.conftest import doc


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
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert project.harness.root not in root.parents
