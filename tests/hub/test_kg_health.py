"""The health of each charter's knowledge graph on the hub (``evo_agents.hub.server.kg_health``), what each build keeps of
it (``evo_agents.hub.kg_build``), the night's figures that carry it, and the lens kg_health.

The checks step 7 of the history-driven-curator plan names: a build on the hub keeps its coverage (mentions resolved,
ambiguous and dangling, the commit each git source read, the Python methods no call reaches) and the files of each git
source in Postgres, not only in its artifact; the hub measures a charter's graph without any model against the default
branches on GitHub (the commit read, the commits behind and since when, the files missing, stale and deleted as the
source reads its tree) and says why for what it cannot read; the night's figures carry kg_health with the builds that
changed nothing and what the facets say agents got from the graph; a finding and a proposal of lens kg_health are kept,
with metrics in the ledger, and a ``code:`` evidence the graph lacks is kept unmatched; schema 0055 goes down and up.

The api runs in this process (TestClient) and the hub's worker too, as in ``tests.hub.test_kg``; GitHub is the fake of
``tests.hub.fake_github``, playing the workers' App and the repo maycuatroi1/evo-agents."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import delete, func, insert, select

from evo_agents.hub import tables
from evo_agents.hub.kg_push import Pusher
from evo_agents.hub.migrate import migrate
from evo_agents.hub.server import kg_health
from evo_agents.hub.server.collect import collect
from evo_agents.kg.project import load_project_at
from evo_agents.kg.review import RULES
from evo_agents.kg.sync import sync_project
from tests.hub.live import sql
from tests.hub.test_credentials_api import MINE, app_key  # noqa: F401 (a fixture)
from tests.hub.test_curator import PROJECT
from tests.hub.test_curator_changes import review_run, world  # noqa: F401 (world is a fixture)
from tests.hub.test_kg import InProcessHub, hub, run_jobs  # noqa: F401 (hub is a fixture)
from tests.hub.test_migrate import ALL, move_to
from tests.hub.test_review_runs import finding, proposed
from tests.kg.projects import commit, git, write

C1, C2, C3 = "c1" * 20, "c2" * 20, "c3" * 20
T1, T2, T3 = "2026-10-08T01:00:00Z", "2026-10-08T06:00:00Z", "2026-10-08T09:00:00Z"
MEASURED = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)  # 6 hours after T2, the oldest commit the graph has not read
LABEL = {"level": "internal", "location": "any", "integrity": "U"}
CONTENT = "sha256:" + "1" * 64
BRANCH = {"app/a.py": "a1", "app/b.py": "b2", "app/new.py": "n1", "vendor/lib.js": "v1", "docs/x.md": "d1"}
HELD = {"app/a.py": "a1", "app/b.py": "b1", "app/gone.py": "g1", "docs/x.md": "d1"}
KNOWLEDGE = {
    "version": 1,
    "project": PROJECT,
    "sources": [
        {"id": "code", "connector": "git", "repo": "evo-agents", "ref": "origin/main", "fetch": True},
        {"id": "kb", "connector": "git", "repo": "lumen-kb-docs", "ref": "origin/main"},
        {"id": "unbuilt", "connector": "git", "repo": "lumen-identity"},
        {"id": "local", "connector": "git", "path": "../somewhere"},
        {"id": "notes", "connector": "harness"},
    ],
}
COVERAGE = {
    "sources": [{"id": "code", "items": 4, "mentions": 10, "resolved": 8, "ambiguous": 1, "dangling": 1}],
    "mentions": 10,
    "resolved": 8,
    "ambiguous": 1,
    "dangling": 1,
    "resolved_ratio": 0.8,
    "ambiguous_ratio": 0.1,
    "dangling_ratio": 0.1,
    "dangling_edges": 0,
    "python": {
        "functions": 3,
        "uncalled_functions": 1,
        "methods": 4,
        "uncalled_methods": 2,
        "uncalled_method_ratio": 0.5,
    },
    "skipped": False,
}


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def project_id(db) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


def graph_of(world, *, files: dict | None = None, commit_read: str = C1) -> int:
    """The knowledge config of ``world``'s project, two successful builds (the second changing nothing) and the files
    its source code holds in the second; the id of that build."""
    db, pid = world.db, project_id(world.db)
    owner = user_id(db, "owner")
    sql(
        db,
        insert(tables.kg_configs).values(
            project_id=pid, digest="sha256:" + "0" * 64, knowledge=KNOWLEDGE, updated_by=owner
        ),
    )
    b = tables.kg_builds
    built = {"status": "succeeded", "content_hash": CONTENT, "nodes": 9, "edges": 9, "started_at": func.now()}
    built["finished_at"] = func.now()
    artifact = {"artifact_sha256": "e" * 64, "artifact_size": 10}
    (first,) = sql(db, insert(b).values(project_id=pid, coverage=COVERAGE, **built, **artifact).returning(b.c.id))[0]
    reused = {"artifact_reused_from": first, "coverage": {**COVERAGE, "skipped": True}}
    (second,) = sql(db, insert(b).values(project_id=pid, **built, **artifact, **reused).returning(b.c.id))[0]
    sql(
        db, insert(b).values(project_id=pid, status="failed", error="no", started_at=func.now(), finished_at=func.now())
    )
    held = files if files is not None else HELD
    row = {"build_id": second, "read_commit": commit_read, "files": held, "file_count": len(held)}
    sql(db, insert(tables.kg_source_files).values(project_id=pid, source="code", **row))
    for source in ("kb", "local"):
        values = {"project_id": pid, "source": source, "build_id": second, "files": {}, "file_count": 0}
        sql(db, insert(tables.kg_source_files).values(**values))
    world.github.commit(MINE, "evo-agents", C1, T1, HELD)
    world.github.commit(MINE, "evo-agents", C2, T2, {**HELD, "app/new.py": "n1"})
    world.github.commit(MINE, "evo-agents", C3, T3, BRANCH)
    return second


def measured(world, now: datetime = MEASURED, *, app=True, tmp_path=None) -> dict:
    state = world.app.state
    found = partial(
        kg_health.measure,
        state.engine,
        github_app=state.github_app if app else None,
        data_dir=tmp_path,
        now=now,
    )
    return world.client.portal.call(found)


def health_rows(db) -> list:
    h = tables.kg_health
    return sql(db, select(h.c.measured_at, h.c.build_id, h.c.body, h.c.error).order_by(h.c.id))


# What a build keeps


def test_a_build_on_the_hub_keeps_its_coverage_and_the_files_of_each_git_source(hub, tmp_path):  # noqa: F811
    app = tmp_path / "alpha-app"
    write(
        app / "app/search.py",
        "def rank(items):\n    return sorted(items)\n\n\nclass Search:\n    def run(self, q):\n        return rank([q])\n",
    )
    write(app / "README.md", "# Alpha\n\nSee app/search.py.\n")
    git(app, "init", "-q", "-b", "main")
    commit(app, "init")
    harness = tmp_path / "alpha-harness"
    write(
        harness / "harness.yaml",
        "name: alpha\nworkspace: ..\nknowledge_file: knowledge.yaml\nrepos:\n  - name: app\n    path: alpha-app\n",
    )
    write(
        harness / "knowledge.yaml",
        """version: 1
project: alpha
policy:
  levels: [public, internal, customer, secret]
  sinks:
    - {id: claude-code@anthropic, kind: agent-session, clearance: {level: customer}}
    - {id: hub, kind: hub, clearance: {level: internal}}
sources:
  - id: app
    connector: git
    repo: app
    label: {level: internal, integrity: U}
""",
    )
    project = load_project_at(harness, tmp_path / "kg-home")
    assert all(result.ok for result in sync_project(project))
    report = Pusher(InProcessHub(hub.client, hub.alice), project).push()
    assert report.ok, report.errors
    run_jobs(hub.db, hub.s3, hub.worker_dir)

    b, f = tables.kg_builds, tables.kg_source_files
    ((build_id, status, coverage),) = sql(hub.db, select(b.c.id, b.c.status, b.c.coverage))
    assert status == "succeeded"
    head = git(app, "rev-parse", "HEAD").strip()
    (source,) = coverage["sources"]
    assert source["id"] == "app" and source["read"]["commit"] == head and source["items"] >= 2
    assert coverage["python"] == {
        "functions": 1,
        "uncalled_functions": 0,
        "uncalled_function_ratio": 0.0,
        "methods": 1,
        "uncalled_methods": 1,
        "uncalled_method_ratio": 1.0,
    }
    assert coverage["skipped"] is False and 0 <= coverage["resolved_ratio"] <= 1
    tree = dict((line.split("\t")[1], line.split()[2]) for line in git(app, "ls-tree", "-r", "HEAD").splitlines())
    held = sql(hub.db, select(f.c.source, f.c.build_id, f.c.read_commit, f.c.files, f.c.file_count, f.c.truncated))
    assert held == [("app", build_id, head, tree, len(tree), False)]

    write(app / "app/more.py", "X = 1\n")
    commit(app, "more")
    assert all(result.ok for result in sync_project(project))
    assert Pusher(InProcessHub(hub.client, hub.alice), project).push().ok
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    (latest, files) = sql(hub.db, select(f.c.build_id, f.c.files))[0]
    assert latest > build_id and "app/more.py" in files  # replaced by the next build, one row a source


# The measure


def test_the_hub_measures_each_git_source_of_a_charter_against_its_default_branch_on_github(world, tmp_path):  # noqa: F811
    build_id = graph_of(world)
    assert measured(world, tmp_path=tmp_path) == {"measured": 1}
    ((when, read_build, body, error),) = health_rows(world.db)
    assert when == MEASURED and read_build == build_id and error is None
    assert body["build"]["id"] == build_id and body["build"]["content_hash"] == CONTENT
    code, kb, unbuilt, local = body["sources"]
    assert code == {
        "source": "code",
        "repo": "evo-agents",
        "ref": "origin/main",
        "commit": C1,
        "build_id": build_id,
        "default_branch": "main",
        "head": C3,
        "behind": 2,
        "lag_hours": 6.0,
        "status": "ahead",
        "tree_files": 4,  # vendor/ is DEFAULT_EXCLUDE: the source never reads it
        "graph_files": 4,
        "missing": 1,
        "stale": 1,
        "deleted": 1,
        "samples": {"missing": ["app/new.py"], "stale": ["app/b.py"], "deleted": ["app/gone.py"]},
        "evidence": ["code:evo-agents:app/new.py", "code:evo-agents:app/b.py", "code:evo-agents:app/gone.py"],
        "off": {"missing": ["app/new.py"], "stale": ["app/b.py"], "deleted": ["app/gone.py"], "cut": False},
        "why": None,
    }
    assert kb["why"] == kg_health.OFF_GITHUB and kb["behind"] is None and kb["missing"] is None
    assert unbuilt["why"] == "no successful build of the graph holds this source yet"
    assert local["why"] == kg_health.NO_REPO and local["repo"] is None and local["graph_files"] == 0
    assert body["eval"] == {"why": "this process has no blob store and cache to read the graph from"}
    assert body["review"] == {"why": "this process has no blob store and cache to read the graph from"}
    asked = [json.loads(call.body) for call in world.github.requests if call.path.endswith("/access_tokens")]
    assert asked[-1]["permissions"] == {"contents": "read", "metadata": "read"}  # it reads; it never writes

    assert measured(world, MEASURED + timedelta(hours=5), tmp_path=tmp_path) == {}  # measured lately: not due
    assert measured(world, MEASURED + timedelta(hours=7), tmp_path=tmp_path) == {"measured": 1}
    world.github.repos[(MINE.lower(), "evo-agents")].cut_trees.add(C3)
    sql(world.db, delete(tables.kg_health))
    assert measured(world, MEASURED, tmp_path=tmp_path) == {"measured": 1}
    code = health_rows(world.db)[-1][2]["sources"][0]
    assert code["behind"] == 2 and code["missing"] is None and "GitHub cut the tree" in code["why"]


def test_a_commit_github_does_not_know_and_a_hub_without_its_app_say_why(world):  # noqa: F811
    graph_of(world, commit_read="f" * 40)
    assert measured(world) == {"measured": 1}
    code = health_rows(world.db)[-1][2]["sources"][0]
    assert code["behind"] is None and code["lag_hours"] is None and code["head"] == C3
    assert "GitHub answered 404 while comparing" in code["why"] and code["missing"] == 1  # the files still compare
    sql(world.db, delete(tables.kg_health))
    assert measured(world, app=False) == {"measured": 1}
    code = health_rows(world.db)[-1][2]["sources"][0]
    assert code["why"] == kg_health.NO_APP and code["commit"] == "f" * 40 and code["graph_files"] == 4


def test_a_measure_that_fails_is_kept_with_its_error_and_tried_again_within_the_hour(world, monkeypatch):  # noqa: F811
    graph_of(world)

    async def broken(*args, **kwargs):
        raise RuntimeError("the forge fell over")

    monkeypatch.setattr(kg_health, "measure_project", broken)
    assert measured(world) == {"failed": 1}
    ((_, build_id, body, error),) = health_rows(world.db)
    assert build_id is None and body == {} and error == "RuntimeError: the forge fell over"
    assert measured(world, MEASURED + timedelta(minutes=30)) == {}
    assert measured(world, MEASURED + timedelta(minutes=61)) == {"failed": 1}


def test_a_project_without_a_night_shift_or_a_knowledge_config_is_not_measured(world):  # noqa: F811
    assert measured(world) == {}  # a charter, no knowledge config
    graph_of(world)
    sql(world.db, delete(tables.schedules))
    assert measured(world) == {}  # a knowledge config, no night shift


# The night's figures and the lens


def facet(world, session: str, kg: dict) -> None:
    f = tables.session_facets
    body = {"messages": 3, "model": "m", "tools": [], "errors": [], "user_turns": [], "commands": [], "kg": kg}
    values = {"source": "transcript", "version": 1, "label": LABEL, "at": func.now(), "body": body, "summary": {}}
    sql(world.db, insert(f).values(project_id=project_id(world.db), kind="session", session_id=session, **values))


def test_the_nights_figures_hold_the_health_of_the_graph_and_the_lens_kg_health_writes_with_it(world, tmp_path):  # noqa: F811
    build_id = graph_of(world)
    facet(
        world,
        "s-empty",
        {
            "calls": 3,
            "empty": 2,
            "fallbacks": 1,
            "searches": 2,
            "edited": 2,
            "in_results": 1,
            "in_top": 1,
            "tools": [{"tool": "kg_search", "calls": 3}],
        },
    )
    state = world.app.state
    passed = world.client.portal.call(
        partial(collect, state.engine, now=MEASURED, github_app=state.github_app, data_dir=tmp_path)
    )
    assert passed["graphs"] == {"measured": 1}
    run_id = review_run(world)
    cf = tables.curator_figures
    ((figures,),) = sql(world.db, select(cf.c.figures))
    health = figures["kg_health"]
    assert health["measured_at"] == MEASURED.isoformat() and health["build"]["id"] == build_id
    assert [item["source"] for item in health["sources"]] == ["code", "kb", "unbuilt", "local"]
    assert health["sources"][0]["behind"] == 2 and health["sources"][0]["lag_hours"] == 6.0
    assert all("off" not in item for item in health["sources"])  # the tier rules read those paths, not the Reviewer
    assert health["coverage"]["build_id"] == build_id and health["coverage"]["python"]["uncalled_methods"] == 2
    assert health["review"] == {"why": "this process has no blob store and cache to read the graph from"}
    builds = health["builds"]
    assert {key: builds[key] for key in ("builds", "succeeded", "failed", "unchanged", "unchanged_rate")} == {
        "builds": 3,
        "succeeded": 2,
        "failed": 1,
        "unchanged": 1,
        "unchanged_rate": 0.5,
    }
    assert [item["changed"] for item in builds["series"]] == [False, True]
    retrieval = health["retrieval"]
    assert (retrieval["used"], retrieval["empty"], retrieval["fallbacks"], retrieval["misses"]) == (1, 2, 1, 1)
    assert retrieval["evidence"] == ["session:s-empty"]

    made = finding(
        world.client,
        world.worker,
        run_id,
        lens="kg_health",
        title="The graph lags main by two commits",
        evidence=[
            {"kind": "code", "repo": "evo-agents", "path": "app/new.py"},
            {"kind": "session", "session_id": "s-empty"},
        ],
    )
    assert made.status_code == 201, made.text
    assert [item["resolved"] for item in made.json()["evidence"]] == ["repo", "facet"]
    proposal = proposed(
        world.client,
        world.worker,
        run_id,
        lens="kg_health",
        kind="fix",
        title="Sync the graph after each merge",
        evidence=[{"kind": "code", "repo": "evo-agents", "path": "app/new.py"}],
    )
    response = world.client.get(
        f"/v1/projects/{PROJECT}/curator/proposals/{proposal['id']}/ledger", headers=world.headers["owner"]
    )
    assert response.status_code == 200, response.text
    trigger = response.json()["lines"][0]["figures"]
    metrics = {item["key"]: item["value"] for item in trigger["metrics"]}
    assert metrics == {
        "kg_behind": 2.0,
        "kg_files_off": 3.0,
        "kg_empty": 2.0,
        "kg_fallbacks": 1.0,
        "kg_misses": 1.0,
        **{f"kg_review_{rule}": 0.0 for rule in RULES},  # no blob store here: the queue is not counted
    }


def test_a_project_without_a_graph_has_no_health_in_its_figures(world):  # noqa: F811
    review_run(world)
    ((figures,),) = sql(world.db, select(tables.curator_figures.c.figures))
    assert "kg_health" in figures and figures["kg_health"] is None


# Schema 0055


def test_schema_0055_takes_the_lens_kg_health_and_going_back_deletes_what_it_wrote(world):  # noqa: F811
    graph_of(world)
    assert measured(world) == {"measured": 1}
    run_id = review_run(world)
    made = finding(
        world.client,
        world.worker,
        run_id,
        lens="kg_health",
        title="lag",
        evidence=[{"kind": "run", "run_id": run_id, "seq": 1}],
    )
    assert made.status_code == 201, made.text
    kept = finding(
        world.client,
        world.worker,
        run_id,
        lens="cost",
        title="cost",
        evidence=[{"kind": "run", "run_id": run_id, "seq": 1}],
    )
    assert kept.status_code == 201, kept.text

    move_to(world.db, "0054", down=True)
    f = tables.findings
    assert sql(world.db, select(f.c.lens)) == [("cost",)]
    assert sql(world.db, select(func.to_regclass("public.kg_health"), func.to_regclass("public.kg_source_files"))) == [
        (None, None)
    ]
    assert migrate(world.db.dsn).applied == ALL[ALL.index("0055") :]
    b = tables.kg_builds
    assert sql(world.db, select(func.count(), func.count(b.c.coverage)))[0] == (3, 0)  # the column came back empty
    again = finding(
        world.client,
        world.worker,
        run_id,
        lens="kg_health",
        title="lag",
        evidence=[{"kind": "run", "run_id": run_id, "seq": 1}],
    )
    assert again.status_code == 201, again.text


# The route


def test_the_route_lists_the_measures_and_the_builds_of_the_graph_for_its_readers(world):  # noqa: F811
    build_id = graph_of(world)
    assert measured(world) == {"measured": 1}
    assert measured(world, MEASURED + timedelta(hours=7)) == {"measured": 1}
    path = f"/v1/projects/{PROJECT}/curator/kg-health"
    response = world.client.get(path, params={"limit": 5}, headers=world.headers["other"])
    assert response.status_code == 200, response.text
    found = response.json()
    assert found["project"] == PROJECT
    assert [item["measured_at"][:16] for item in found["measures"]] == ["2026-10-08T19:00", "2026-10-08T12:00"]
    latest = found["measures"][0]
    assert latest["build_id"] == build_id and latest["error"] is None and latest["sources"][0]["behind"] == 2
    assert [(item["id"], item["changed"]) for item in found["builds"]] == [(build_id, False), (build_id - 1, True)]
    assert found["builds"][1]["coverage"]["python"]["uncalled_methods"] == 2
    one = world.client.get(path, params={"limit": 1}, headers=world.headers["other"]).json()
    assert len(one["measures"]) == len(one["builds"]) == 1
    assert world.client.get(path, params={"limit": 0}, headers=world.headers["other"]).status_code == 422
    narrow = world.client.get(path, headers={**world.headers["other"], "X-Evo-Sink": "nowhere"})
    assert narrow.status_code == 404
    admin = world.client.get(path, headers=world.headers["admin"])  # a hub admin with no grant reads nothing
    assert admin.status_code in (403, 404)


def test_an_eval_that_fails_leaves_the_measure_of_the_sources_standing(world, tmp_path, monkeypatch):  # noqa: F811
    graph_of(world)
    asked = []

    def broken(cache, blobs, project, access, sink, graphs, plans, previous, found_set=None):
        asked.append((project, sink, [graph.build_id for graph in graphs], [plan_id for plan_id, _ in plans]))
        raise RuntimeError("sqlite went away")

    monkeypatch.setattr(kg_health, "_evaluate", broken)
    state = world.app.state
    found = partial(
        kg_health.measure, state.engine, github_app=state.github_app, blobs=object(), data_dir=tmp_path, now=MEASURED
    )
    assert world.client.portal.call(found) == {"measured": 1}
    ((_, _, body, error),) = health_rows(world.db)
    assert error is None and body["sources"][0]["behind"] == 2
    assert body["eval"] == {"why": "the evaluation failed: RuntimeError"}
    assert body["review"]["why"].startswith("counting the review queue failed")  # the measure stands without it
    ((project, sink, builds, plans),) = asked
    assert project == PROJECT and len(builds) == 2 and "credentials-smoke" in plans  # as the owner
    assert sink == "claude-code@anthropic"  # the agent sink the policy declares, not the hub sink


# The question set and the signals of the eval

EVAL_SET = [
    {"id": f"q{n}", "question": f"where is thing {n}", "type": "where", "files": [f"evo-agents:app/f{n}.py"]}
    for n in range(30)
]


def put_set(world, who: str = "owner", questions=None, name: str = "evo-agents"):
    body = {"set": name, "questions": EVAL_SET if questions is None else questions}
    return world.client.put(f"/v1/kg/{PROJECT}/eval-set", json=body, headers=world.headers[who])


def test_the_eval_set_is_versioned_on_the_hub_and_its_holdout_answers_stay_there(world):  # noqa: F811
    from evo_agents.kg.evaluate import HOLDOUT, TUNE, split_of

    path = f"/v1/kg/{PROJECT}/eval-set"
    assert world.client.get(path, headers=world.headers["owner"]).status_code == 404
    first = put_set(world)
    assert first.status_code == 200, first.text
    holdout = [q["id"] for q in EVAL_SET if split_of(q["id"]) == HOLDOUT]
    assert first.json() | {"digest": None} == {
        "set": "evo-agents",
        "version": 1,
        "digest": None,
        "questions": 30,
        "holdout": len(holdout),
        "created": True,
    }
    again = put_set(world, "other").json()
    assert (again["version"], again["created"], again["digest"]) == (1, False, first.json()["digest"])
    assert put_set(world, questions=EVAL_SET[:20]).json()["version"] == 2
    assert put_set(world, "admin").status_code == 403  # a hub admin with no grant on the project
    bad = put_set(world, questions=[{"id": "x", "question": "q", "type": "t"}])
    assert bad.status_code == 422 and "no answer" in bad.text
    assert put_set(world, questions=[{"question": "q", "type": "t", "files": ["a.py"]}]).status_code == 422

    whole = world.client.get(path, params={"version": 1}, headers=world.headers["other"]).json()
    assert (whole["set"], whole["version"], whole["split"], len(whole["questions"])) == ("evo-agents", 1, "all", 30)
    for question in whole["questions"]:
        assert question["split"] == split_of(question["id"])
        assert ("files" in question) == (question["split"] == TUNE), "a holdout question never carries its answers"
    part = world.client.get(path, params={"split": "holdout"}, headers=world.headers["owner"]).json()
    assert part["version"] == 2 and all(q["split"] == HOLDOUT and "files" not in q for q in part["questions"])
    bare = world.client.get(path, params={"split": "tune", "answers": "false"}, headers=world.headers["owner"]).json()
    assert bare["questions"] and all(set(q) == {"id", "question", "type", "split"} for q in bare["questions"])
    assert world.client.get(path, params={"version": 9}, headers=world.headers["owner"]).status_code == 404
    narrow = world.client.get(path, headers={**world.headers["owner"], "X-Evo-Sink": "nowhere"})
    assert narrow.status_code == 404

    async def held():
        from evo_agents.hub.server import kg_eval_sets

        async with world.app.state.engine.begin() as conn:
            found = await kg_eval_sets.latest_set(conn, project_id(world.db))
            listed = await kg_eval_sets.holdout_questions(conn, project_id(world.db))
        return found, listed

    (question_set, digest), listed = world.client.portal.call(held)
    assert (question_set.name, question_set.version, len(question_set.questions)) == ("evo-agents", 2, 20)
    assert digest == part["digest"] and [q["id"] for q in listed] == [q["id"] for q in part["questions"]]
    assert all(q.files for q in question_set.part(HOLDOUT)), "the hub's own eval reads the holdout answers"
    a = tables.audit
    targets = [t for (t,) in sql(world.db, select(a.c.target).where(a.c.action == "kg.eval_set"))]
    assert targets == [
        f"{PROJECT} set=evo-agents version=1 questions=30",
        f"{PROJECT} set=evo-agents version=2 questions=20",
    ]


def test_the_eval_asks_the_holdout_set_through_the_agent_sink_and_pushes_its_signals(
    world,
    tmp_path,
    monkeypatch,  # noqa: F811
):
    graph_of(world)
    assert put_set(world).status_code == 200
    asked = []
    score = {"kg_search": {"n": 9, "hit@5": 0.625, "recall@5": 0.5, "tokens_per_file": 812.4}}

    def scored(cache, blobs, project, access, sink, graphs, plans, previous, found_set=None):
        asked.append((sink, found_set[0].version, len(found_set[0].questions), found_set[1]))
        return {"questions": 9, "methods": score, "build_id": graphs[0].build_id, "sink": sink}

    monkeypatch.setattr(kg_health, "_evaluate", scored)
    state = world.app.state
    found = partial(
        kg_health.measure, state.engine, github_app=state.github_app, blobs=object(), data_dir=tmp_path, now=MEASURED
    )
    assert world.client.portal.call(found) == {"measured": 1}
    ((sink, version, count, digest),) = asked
    assert (version, count, len(digest)) == (1, 30, 64)
    assert sink == "claude-code@anthropic", "the sink agents search the graph through"
    h, s, k = tables.kg_health, tables.signals, tables.signal_keys
    (measure_id,) = sql(world.db, select(h.c.id))[0]
    rows = sql(
        world.db,
        select(k.c.name, k.c.unit, s.c.value, s.c.user_id, s.c.external_id)
        .select_from(s.join(k, k.c.id == s.c.key_id))
        .order_by(k.c.name),
    )
    owner = user_id(world.db, "owner")
    assert rows == [
        ("kg.retrieval.hit_at_5", "percent", 62.5, owner, f"kg_health:kg.retrieval.hit_at_5:{measure_id}"),
        (
            "kg.retrieval.tokens_per_file",
            "tokens",
            812.4,
            owner,
            f"kg_health:kg.retrieval.tokens_per_file:{measure_id}",
        ),
    ]

    async def again(found):
        async with world.app.state.engine.begin() as conn:
            return await kg_health.push_eval_signals(conn, project_id(world.db), owner, measure_id, found)

    assert world.client.portal.call(again, {"methods": score}) == [], "the same measure pushes once"
    assert world.client.portal.call(again, {"why": "no graph"}) == []
    assert world.client.portal.call(again, {"methods": {"kg_search": {"hit@5": 0.0, "tokens_per_file": None}}}) == []
