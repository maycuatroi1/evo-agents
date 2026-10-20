"""What the worker's sync needs of the corpus (``evo_agents.kg.sync.merge_pulled``, ``Corpus.adopt_lineage``,
``Corpus.alive_digest``, ``sync_source(force=True)``), on two corpora of one project as a member's machine and the
worker on duty hold them: a run merged from the other corpus is merged as the hub merges it and gives its lineage and
epoch when they are newer, so the deletions a run here derives cover the other's items and its records compete with
the other's within one epoch; what a source holds alive hashes alike whichever runs got there; a forced sync reads a
git source again at the commit it read last."""

from __future__ import annotations

import dataclasses
import gzip
import json
import shutil
import subprocess

from evo_agents.kg.corpus import BASE_EPOCH, Corpus
from evo_agents.kg.sync import merge_pulled, source_lineage, sync_source
from tests.kg.conftest import doc
from tests.kg.test_connectors import make_repo, sh


def other(project, tmp_path):
    """The same project as another machine holds it: its own kg home, so its own corpus."""
    return dataclasses.replace(project, home=tmp_path / "other-kg-home")


def synced(project, fake, *docs) -> str:
    fake.set(items=list(docs))
    result = sync_source(project, "docs")
    assert result.ok, result.issues
    return result.run_id


def header(corpus: Corpus, run_id: str) -> dict:
    with gzip.open(corpus.log_dir / f"{run_id}.jsonl.gz", "rt", encoding="utf-8") as handle:
        return json.loads(handle.readline())


def pulled(source: Corpus, target: Corpus, run_id: str) -> int:
    """Copy run ``run_id`` of ``source`` into ``target``'s log and merge it there, as the worker's pull does."""
    path = target.log_dir / f"{run_id}.jsonl.gz"
    shutil.copy(source.log_dir / f"{run_id}.jsonl.gz", path)
    return merge_pulled(target, path)


def alive(corpus: Corpus, source: str = "docs") -> list[str]:
    return [record.item_id for record in corpus.items(source)]


KEPT = [doc(key) for key in "acdef"]  # five items stay, so removing one stays under the deletion guard's ratio
KEPT_IDS = [f"docs:doc:{key}" for key in "acdef"]


def test_sync_of_a_pulled_run_derives_the_deletions_of_the_other_machines_items(fake_project, tmp_path):
    laptop, fake = fake_project
    worker = other(laptop, tmp_path)
    first = synced(laptop, fake, *KEPT, doc("b"))
    mine, theirs = laptop.corpus(), worker.corpus()
    assert pulled(mine, theirs, first) == 6
    assert alive(theirs) == sorted([*KEPT_IDS, "docs:doc:b"])
    assert theirs.alive_digest("docs") == mine.alive_digest("docs")
    assert theirs.lineage_of("docs") == mine.lineage_of("docs") == source_lineage(laptop.source("docs"))

    second = synced(worker, fake, *KEPT)  # b is gone: the worker knew it only from the laptop's run
    assert theirs.get("docs:doc:b").deleted and alive(theirs) == KEPT_IDS
    pulled(theirs, mine, second)  # the hub merges both runs: the tombstone of b wins there too
    assert alive(mine) == KEPT_IDS and mine.alive_digest("docs") == theirs.alive_digest("docs")


def test_a_pulled_run_of_a_newer_epoch_gives_its_epoch_to_the_next_run_here(fake_project, tmp_path):
    laptop, fake = fake_project
    worker = other(laptop, tmp_path)
    old = synced(laptop, fake, doc("a", rev="1"))
    laptop.source("docs")["config"]["where"] = "elsewhere"  # a config key of the lineage: a new epoch
    moved = synced(laptop, fake, doc("a", rev="2"))
    mine, theirs = laptop.corpus(), worker.corpus()
    assert header(mine, old)["epoch"] == BASE_EPOCH and header(mine, moved)["epoch"] == moved

    worker.source("docs")["config"]["where"] = "elsewhere"
    pulled(mine, theirs, moved)
    pulled(mine, theirs, old)  # older: it neither takes the lineage back nor lowers the epoch
    assert theirs.lineage_of("docs") == source_lineage(worker.source("docs"))
    assert theirs.current_epoch("docs") == moved

    newer = synced(worker, fake, doc("a", rev="3"))
    assert header(theirs, newer)["epoch"] == moved  # the laptop's epoch: the worker's records compete within it
    pulled(theirs, mine, newer)
    assert mine.get("docs:doc:a").rev == "3"


def test_a_run_here_that_reads_from_elsewhere_than_the_pulled_runs_starts_a_newer_epoch(fake_project, tmp_path):
    laptop, fake = fake_project
    worker = other(laptop, tmp_path)
    first = synced(laptop, fake, *KEPT, doc("b"))
    theirs = worker.corpus()
    pulled(laptop.corpus(), theirs, first)
    worker.source("docs")["config"]["where"] = "elsewhere"
    run_id = synced(worker, fake, *KEPT)
    assert header(theirs, run_id)["epoch"] == run_id
    assert alive(theirs) == KEPT_IDS  # its listing still removes what the laptop's runs left


def test_the_alive_digest_hashes_live_records_alone_whichever_run_wrote_them(fake_project, tmp_path):
    laptop, fake = fake_project
    worker = other(laptop, tmp_path)
    synced(laptop, fake, *KEPT, doc("b"))
    synced(laptop, fake, *KEPT)  # b becomes a tombstone here
    synced(worker, fake, *KEPT)  # never knew b
    mine, theirs = laptop.corpus(), worker.corpus()
    assert mine.get("docs:doc:b").deleted and theirs.get("docs:doc:b") is None
    assert mine.alive_digest("docs") == theirs.alive_digest("docs")
    synced(worker, fake, doc("a", text="# a\n\nanother body\n", rev="2"), *KEPT[1:])
    assert mine.alive_digest("docs") != theirs.alive_digest("docs")


def test_merge_pulled_takes_nothing_from_a_file_that_is_not_a_run_log(fake_project):
    laptop, _ = fake_project
    corpus = laptop.corpus()
    path = corpus.log_dir / "not-a-log.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "item", "id": "docs:doc:x"}) + "\n")
    assert merge_pulled(corpus, path) == 0 and alive(corpus) == []


def git_project(tmp_path, kg_env):
    from evo_agents.kg.project import load_project_at

    upstream = make_repo(tmp_path / "upstream")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True, capture_output=True)
    root = tmp_path / "git-harness"
    root.mkdir()
    (root / "harness.yaml").write_text(f"name: proj\nrepos:\n  - name: app\n    path: '{clone}'\n")
    (root / "knowledge.yaml").write_text(
        "version: 1\nproject: proj\npolicy:\n  levels: [internal]\nsources:\n  - id: app\n    connector: git\n"
        "    repo: app\n    ref: origin/main\n    fetch: true\n    pulls: false\n    label: {level: internal}\n"
    )
    return load_project_at(root, kg_env), upstream


def test_a_forced_sync_reads_a_git_source_again_at_the_commit_it_read_last(tmp_path, kg_env):
    project, upstream = git_project(tmp_path, kg_env)
    first = sync_source(project, "app")
    assert first.ok and not first.unchanged and first.log
    again = sync_source(project, "app")
    assert again.ok and again.unchanged and again.log is None
    forced = sync_source(project, "app", force=True)
    assert forced.ok and not forced.unchanged and forced.log
    commit = sh(upstream, "rev-parse", "HEAD").strip()
    assert project.corpus().cursor("app")["commit"] == commit
