"""The tier rules of step 8 of plan history-driven-curator on the hub (``evo_agents.hub.server.change_tiers``): a
proposal's path the knowledge graph lacks, holds older than the default branch or cannot read is at least tier 1; the
tier the hub computes again from a pull request's real files reads the graph too; a change of kind kg_source that edits
only the ref, fetch, refresh, include, exclude or allow of a source in knowledge.yaml stays tier 0, its Judge running
harness validate and a trial sync of the source, and the hub merges it; any other change of the file is at least tier
1, read key by key from the pull request at its merge base and its head; schema 0057. The world, GitHub and its helpers
are ``tests.hub.test_curator_changes``'s."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
import yaml
from sqlalchemy import column, func, insert, select, table, update

from evo_agents.hub import knowledge_diff, tables
from evo_agents.hub.migrate import migrate
from evo_agents.hub.server import change_tiers, kg
from evo_agents.hub.server import proposals as proposal_routes
from tests.hub.live import sql
from tests.hub.test_credentials_api import MINE, app_key  # noqa: F401 (app_key is a fixture)
from tests.hub.test_curator import PROJECT
from tests.hub.test_curator_changes import (  # noqa: F401 (world is a fixture)
    GREEN,
    HEAD,
    SUCCESS,
    VERIFY,
    accepted,
    builder,
    calls,
    inputs,
    judge_run,
    moved_on,
    one_change,
    proposal_of,
    review_run,
    verdict,
    world,
)
from tests.hub.test_migrate import move_to
from tests.hub.test_review_runs import DRAFT, proposed
from tests.hub.test_runs import moved

MERGE_BASE = "0" * 40  # the default branch's head when the Builder pushed: the merge base of its branch
KNOWLEDGE = [{"repo": "evo-agents", "path": "knowledge.yaml"}]
FILES = [
    {
        "filename": "knowledge.yaml",
        "status": "modified",
        "patch": "@@ -9,1 +9,1 @@\n-    ref: origin/dev\n+    ref: origin/main\n",
    }
]
BEFORE = {
    "version": 1,
    "project": PROJECT,
    "policy": {
        "levels": ["public", "internal"],
        "sinks": [{"id": "evo-hub", "kind": "hub", "clearance": {"level": "internal"}}],
    },
    "sources": [
        {"id": "harness", "connector": "harness", "refresh": "1h"},
        {
            "id": "code",
            "connector": "git",
            "repo": "evo-agents",
            "ref": "origin/dev",
            "fetch": True,
            "label": {"level": "public"},
        },
    ],
}


def text(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False)


def knowledge(**code) -> str:
    """knowledge.yaml as BEFORE with source code's keys ``code`` (None removes one)."""
    data = yaml.safe_load(text(BEFORE))
    found = data["sources"][1]
    for key, value in code.items():
        if value is None:
            found.pop(key, None)
        else:
            found[key] = value
    return text(data)


def kg_source_change(world, after: str | None, *, before: str | None = None) -> dict:
    """An accepted proposal of kind kg_source of knowledge.yaml, built, the file at the merge base ``before`` (BEFORE by
    default) and at the head ``after``, and its pull request opened; the proposal as accepted."""
    proposal = accepted(world, kind="kg_source", paths=KNOWLEDGE)
    world.github.put_file(MINE, "evo-agents", MERGE_BASE, "knowledge.yaml", text(BEFORE) if before is None else before)
    world.github.put_file(MINE, "evo-agents", HEAD, "knowledge.yaml", after)
    builder(world, FILES)
    assert moved_on(world) == {"opened": 1}
    return proposal


def judged_with_checks(world, exit_codes=None, **body) -> dict:
    """The judge run queued and claimed, its inputs read, its verdict posted with the plan's verify and each check of
    knowledge.yaml its inputs name (exit 0 unless ``exit_codes`` says otherwise), and the run ended; the change."""
    spec = judge_run(world)
    found = inputs(world, spec["id"])
    assert found.status_code == 200, found.text
    checks = (found.json()["knowledge"] or {}).get("checks") or []
    ran = [{"command": VERIFY, "exit_code": 0}]
    ran += [{"command": name, "exit_code": (exit_codes or {}).get(name, 0)} for name in checks]
    response = verdict(world, spec["id"], verify=ran, **body)
    assert response.status_code == 200, response.text
    moved(world.client, world.worker, spec["id"], "running", "verifying", "done")
    return response.json()


def stored_knowledge(world):
    c = tables.curator_changes
    ((found,),) = sql(world.db, select(c.c.knowledge))
    return found


def project_id(world) -> int:
    p = tables.projects
    return sql(world.db, select(p.c.id).where(p.c.name == PROJECT))[0][0]


# Criterion a7: a change of the sources of knowledge.yaml


def test_tier_a_kg_source_change_of_a_ref_alone_stays_tier_0_and_its_judge_checks_it_before_the_merge(world):
    proposal = kg_source_change(world, knowledge(ref="origin/main"))
    assert proposal["tier"] == 0 and proposal["tier_reasons"][1].startswith(
        "evo-agents:knowledge.yaml is knowledge.yaml: tier 0 while the pull request's diff edits only the ref"
    )
    assert one_change(world)["tier"] == 0 and proposal_of(world, proposal["id"])["tier"] == 0
    assert stored_knowledge(world) == {
        "evo-agents:knowledge.yaml": {"sources": ["code"], "edits": ["source code: ref"], "others": []}
    }
    read = calls(world.github, "GET", "/contents/knowledge.yaml")
    assert sorted(item.query["ref"] for item in read) == sorted([MERGE_BASE, HEAD])  # its merge base and its head

    spec = judge_run(world)
    found = inputs(world, spec["id"]).json()
    checks = knowledge_diff.judge_checks(["code"])
    assert found["knowledge"] == {"files": ["knowledge.yaml"], "sources": ["code"], "checks": checks}
    assert found["verify"] == [VERIFY]  # the plan's verify stays the plan's
    ran = [{"command": VERIFY, "exit_code": 0}, *({"command": name, "exit_code": 0} for name in checks)]
    response = verdict(world, spec["id"], verify=ran, paths=["knowledge.yaml"])
    assert response.status_code == 200 and response.json()["passed"] is True, response.text
    moved(world.client, world.worker, spec["id"], "running", "verifying", "done")
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "merged": 1}
    assert one_change(world)["state"] == "merged" and world.github.merges == [(MINE, "evo-agents", 1, HEAD, "workers")]


@pytest.mark.parametrize(
    "exit_codes, failure",
    [
        (None, "2 verify command(s) of the plan did not run"),  # an older worker that runs the plan's verify alone
        ({knowledge_diff.trial_name("code"): 1}, "1 verify command(s) of the plan exited other than 0"),
        ({knowledge_diff.JUDGE_VALIDATE: 2}, "1 verify command(s) of the plan exited other than 0"),
    ],
)
def test_tier_a_kg_source_change_whose_judge_skips_or_fails_a_check_of_knowledge_yaml_fails(world, exit_codes, failure):
    kg_source_change(world, knowledge(ref="origin/main", include=["evo_agents/**"]))
    if exit_codes is None:
        spec = judge_run(world)
        assert inputs(world, spec["id"]).status_code == 200
        response = verdict(world, spec["id"])  # the plan's verify alone
        assert response.status_code == 200, response.text
        change = response.json()
    else:
        change = judged_with_checks(world, exit_codes)
    assert change["passed"] is False and change["verdict"]["failures"] == [failure]
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    moved_on(world)
    assert one_change(world)["state"] == "open" and world.github.merges == []


@pytest.mark.parametrize(
    "after, before, why",
    [
        (knowledge(ref="origin/main", label={"level": "internal"}), None, "source code changes its label"),
        (
            text({**BEFORE, "policy": {**BEFORE["policy"], "sinks": []}}),
            None,
            "knowledge.yaml changes policy.sinks",
        ),
        (
            text({**BEFORE, "sources": [*BEFORE["sources"], {"id": "wiki", "connector": "exec", "command": ["x"]}]}),
            None,
            "knowledge.yaml adds source wiki",
        ),
        (
            text({**BEFORE, "identifiers": [{"kind": "Seam", "pattern": "x"}]}),
            None,
            "knowledge.yaml changes identifiers",
        ),
        (knowledge(ref="origin/main"), "", "knowledge.yaml is not a mapping before the change"),
        (None, None, "knowledge.yaml is not there after the change"),
    ],
)
def test_tier_a_kg_source_pull_request_that_changes_more_of_knowledge_yaml_is_tier_1_and_stays_open(
    world, after, before, why
):
    proposal = kg_source_change(world, after, before=before)
    assert one_change(world)["tier"] == 1
    reasons = proposal_of(world, proposal["id"])["tier_reasons"]
    assert any(why in reason and reason.endswith("at least tier 1") for reason in reasons), reasons
    assert reasons[-1] == "the files the change really touches give it tier 1, not 0"
    change = judged_with_checks(world)
    assert change["passed"] is True and change["tier"] == 1
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "open": 1}
    assert one_change(world)["reason"] == "tier 1 waits for its owner to merge it" and world.github.merges == []


def test_tier_a_kg_source_knowledge_yaml_the_hub_cannot_read_is_tier_1(world):
    accepted(world, kind="kg_source", paths=KNOWLEDGE)
    builder(world, FILES)
    world.github.repos[(MINE.lower(), "evo-agents")].merge_bases.clear()  # GitHub cannot compare the branch
    assert moved_on(world) == {"opened": 1}
    assert one_change(world)["tier"] == 1
    assert stored_knowledge(world) == {"evo-agents:knowledge.yaml": None}
    reasons = proposal_of(world, one_change(world)["proposal_id"])["tier_reasons"]
    assert "the hub could not read evo-agents:knowledge.yaml at both ends of the change" in reasons[-2]
    spec = judge_run(world)
    found = inputs(world, spec["id"]).json()["knowledge"]
    assert found == {"files": ["knowledge.yaml"], "sources": [], "checks": [knowledge_diff.JUDGE_VALIDATE]}


def test_tier_another_kind_never_reads_knowledge_yaml_and_keeps_it_code(world):
    accepted(world, kind="docs", paths=KNOWLEDGE)
    builder(world, FILES)
    assert moved_on(world) == {"opened": 1}
    assert one_change(world)["tier"] == 1 and stored_knowledge(world) is None
    assert calls(world.github, "GET", "/contents/knowledge.yaml") == []
    spec = judge_run(world)
    assert inputs(world, spec["id"]).json()["knowledge"] is None


# Criterion a6: what the graph cannot tell


def test_tier_a_proposal_of_a_path_the_graph_lacks_or_holds_old_is_at_least_tier_1(world, monkeypatch):
    async def graph(request, owner, access, targets):
        return change_tiers.GraphView(frozenset({"evo-agents:docs/new.md"}), frozenset())

    monkeypatch.setattr(proposal_routes, "graph_view", graph)
    off = {"missing": ["docs/missing.md"], "stale": ["docs/old.md"], "deleted": ["docs/gone.md"], "cut": False}
    entry = {"source": "code", "repo": "evo-agents", "default_branch": "main", "head": "a" * 40, "off": off}
    h = tables.kg_health
    sql(world.db, insert(h).values(project_id=project_id(world), body={"sources": [entry]}, error=None))
    sql(world.db, insert(h).values(project_id=project_id(world), body={}, error="a failed measure counts for nothing"))
    run_id = review_run(world)

    def tier_of(path: str) -> dict:
        evidence = [{"kind": "run", "run_id": run_id, "seq": 1}]
        paths = [{"repo": "evo-agents", "path": path}]
        return proposed(world.client, world.worker, run_id, kind="docs", paths=paths, evidence=evidence, plan=DRAFT)

    lacks = tier_of("docs/new.md")
    assert lacks["tier"] == 1 and lacks["tier_reasons"][-1] == (
        "the knowledge graph has no file evo-agents:docs/new.md: the raise by what a change reaches in the graph "
        "cannot tell what editing evo-agents:docs/new.md reaches, so at least tier 1"
    )
    old = tier_of("docs/old.md")["tier_reasons"][-1]
    assert old.startswith("the knowledge graph holds evo-agents:docs/old.md older than main at aaaaaaaaaaaa has it")
    assert "lacks evo-agents:docs/missing.md, which main at" in tier_of("docs/missing.md")["tier_reasons"][-1]
    assert "holds evo-agents:docs/gone.md, which main at" in tier_of("docs/gone.md")["tier_reasons"][-1]
    assert tier_of("docs/hub.md")["tier"] == 0  # a file the graph holds as the branch has it

    sql(world.db, update(h).values(body={"sources": [{**entry, "off": {**off, "cut": True}}]}))
    assert "holds more files of evo-agents off main at" in tier_of("docs/x.md")["tier_reasons"][-1]


def test_tier_a_proposal_while_the_projects_graph_cannot_be_read_is_at_least_tier_1(world, monkeypatch):
    async def graph(request, owner, access, targets):
        return None

    async def one_graph(conn, project_id, limit):
        return ["a build"]

    monkeypatch.setattr(proposal_routes, "graph_view", graph)
    run_id = review_run(world)
    evidence = [{"kind": "run", "run_id": run_id, "seq": 1}]
    paths = [{"repo": "evo-agents", "path": "docs/hub.md"}]
    plain = proposed(world.client, world.worker, run_id, kind="docs", paths=paths, evidence=evidence, plan=DRAFT)
    assert plain["tier"] == 0  # no graph on the hub at all: the tier its kind and paths give
    monkeypatch.setattr(kg, "built_graphs", one_graph)
    paths = [{"repo": "evo-agents", "path": "docs/kg.md"}]
    made = proposed(world.client, world.worker, run_id, kind="docs", paths=paths, evidence=evidence, plan=DRAFT)
    assert made["tier"] == 1
    assert made["tier_reasons"][-1].startswith("the knowledge graph of the project could not be read now: the raise")


@pytest.mark.parametrize(
    "unmatched, impacted, tier, why",
    [
        (["evo-agents:tests/test_wait.py"], [], 1, "the knowledge graph has no file evo-agents:tests/test_wait.py"),
        ([], [("evo-agents", "curator.yaml")], 3, "the change reaches evo-agents:curator.yaml in the knowledge graph"),
        ([], [("evo-agents", "docs/x.md")], 0, None),
    ],
)
def test_tier_again_from_a_pull_requests_files_reads_the_graph(world, monkeypatch, unmatched, impacted, tier, why):
    asked = []

    async def impact(engine, project_id, targets, reader):
        asked.append(sorted(targets))
        return change_tiers.GraphView(frozenset(unmatched), frozenset(impacted))

    proposal = accepted(world)  # test_add of tests/test_wait.py: tier 0
    monkeypatch.setattr(change_tiers, "impact_as_owner", impact)
    builder(world)
    assert moved_on(world) == {"opened": 1}
    assert asked == [["evo-agents:tests/test_wait.py"]]  # the files of the pull request, as the night shift's owner
    assert one_change(world)["tier"] == tier
    reasons = proposal_of(world, proposal["id"])["tier_reasons"]
    if why is not None:
        assert any(reason.startswith(why) for reason in reasons), reasons
    else:
        assert len(reasons) == 1


def test_tier_again_from_a_judge_runs_paths_reads_the_graph_and_never_lowers_it(world, monkeypatch):
    seen = {"unmatched": frozenset()}

    async def impact(engine, project_id, targets, reader):
        return change_tiers.GraphView(seen["unmatched"], frozenset())

    proposal = accepted(world)
    monkeypatch.setattr(change_tiers, "impact_as_owner", impact)
    builder(world)
    moved_on(world)
    assert one_change(world)["tier"] == 0
    seen["unmatched"] = frozenset({"evo-agents:tests/test_wait.py"})  # the graph lost the file since
    change = judged_with_checks(world, paths=["tests/test_wait.py"])
    assert change["tier"] == 1 and proposal_of(world, proposal["id"])["tier"] == 1


def test_tier_a_pull_request_of_a_project_whose_graph_the_job_cannot_read_is_at_least_tier_1(world, monkeypatch):
    async def one_graph(conn, project_id, limit):
        return ["a build"]

    accepted(world)
    builder(world)
    monkeypatch.setattr(kg, "built_graphs", one_graph)  # a graph, and the job holds no cache to read it from
    assert moved_on(world) == {"opened": 1}
    assert one_change(world)["tier"] == 1
    reasons = proposal_of(world, one_change(world)["proposal_id"])["tier_reasons"]
    assert any(reason.startswith("the knowledge graph of the project could not be read now") for reason in reasons)


def test_tier_the_job_reads_kg_impact_as_the_night_shifts_owner_from_a_cache_of_its_own(world, monkeypatch, tmp_path):
    from evo_agents.hub.kg_graph import GraphUnavailable

    b = tables.kg_builds
    built = {"status": "succeeded", "content_hash": "sha256:" + "1" * 64, "nodes": 1, "edges": 0}
    artifact = {"artifact_sha256": "e" * 64, "artifact_size": 10, "started_at": func.now(), "finished_at": func.now()}
    ((build_id,),) = sql(
        world.db, insert(b).values(project_id=project_id(world), **built, **artifact).returning(b.c.id)
    )
    asked = []

    def answer(cache, blobs, handles, owner, project, policy, ceiling, sink, graphs, tool, arguments):
        asked.append((cache.root, owner, project, sink, tool, arguments, [graph.build_id for graph in graphs]))
        if arguments["paths"] == ["evo-agents:down.py"]:
            raise GraphUnavailable("the blob store did not answer")
        node = {"id": "file:evo-agents:app/a.py", "props": {"repo": "evo-agents", "path": "app/a.py"}}
        found = {"impacted": [{"hop": 1, "node": node}], "unmatched": ["evo-agents:gone.py"]}
        return {"content": [], "structuredContent": found}

    monkeypatch.setattr(change_tiers, "answer", answer)
    state = world.app.state
    reader = change_tiers.GraphReader.at(state.blobs, tmp_path)

    def impact(targets, using=reader):
        return world.client.portal.call(change_tiers.impact_as_owner, state.engine, project_id(world), targets, using)

    found = impact(["evo-agents:b.py", "evo-agents:gone.py"])
    assert found == change_tiers.GraphView(frozenset({"evo-agents:gone.py"}), frozenset({("evo-agents", "app/a.py")}))
    u = tables.users
    owner_id = sql(world.db, select(u.c.id).where(u.c.login == "owner"))[0][0]
    ((root, owner, project, sink, tool, arguments, builds),) = asked
    assert (root, owner, project, tool, builds) == (
        tmp_path / "kg" / "tiers",
        (owner_id, PROJECT),
        PROJECT,
        "kg_impact",
        [build_id],
    )
    assert arguments == {"paths": ["evo-agents:b.py", "evo-agents:gone.py"], "depth": 2}
    assert sink == "hub"  # through the project's hub sink
    assert impact(["evo-agents:down.py"]) is None  # the graph cannot be read now
    assert impact(["evo-agents:b.py"], change_tiers.GraphReader()) is None and len(asked) == 2  # no cache: not asked
    assert impact([]) is None and len(asked) == 2


# Schema 0057


def test_tier_schema_0057_takes_kind_kg_source_and_going_back_deletes_its_proposals(world):
    kg_source_change(world, knowledge(ref="origin/main"))
    db = world.db
    c, p = tables.curator_changes, tables.proposals
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(db, update(c).values(knowledge=["a list"]))
    assert "kg_source" in kind_check(db)
    move_to(db, "0056", down=True)
    assert sql(db, select(func.count()).select_from(p).where(p.c.kind == "kg_source")) == [(0,)]
    assert sql(db, select(func.count()).select_from(c)) == [(0,)]  # its change went with it
    info = table("columns", column("table_name"), column("column_name"), schema="information_schema")
    where = (info.c.table_name == "curator_changes") & (info.c.column_name == "knowledge")
    assert sql(db, select(func.count()).select_from(info).where(where)) == [(0,)]
    assert "kg_source" not in kind_check(db) and "'revert'" in kind_check(db)
    assert migrate(db.dsn).applied == (
        "0057",
        "0058",
        "0059",
        "0060",
        "0061",
        "0062",
        "0063",
        "0064",
        "0065",
        "0066",
        "0067",
    )
    assert "kg_source" in kind_check(db)


def kind_check(db) -> str:
    constraints = table("pg_constraint", column("oid"), column("conname"))
    query = select(func.pg_get_constraintdef(constraints.c.oid)).where(constraints.c.conname == "proposals_kind_check")
    return sql(db, query)[0][0]
