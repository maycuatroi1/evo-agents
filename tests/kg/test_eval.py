"""``evo-agents kg eval`` and ``kg eval-questions``: the question set, the matching of answers, the numbers per
question type, and questions built from a hub's plans. Synthetic fixtures only."""

import json
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.hub.kg_eval import changed_files, plan_questions
from evo_agents.kg import evaluate as ev
from evo_agents.kg.build import build_project
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, git, make_project, write

FIXTURE = Path(__file__).parent / "fixtures" / "eval" / "demo.jsonl"


# -- the question set ----------------------------------------------------------------------------------------------


def test_eval_reads_files_nodes_and_defaults():
    [first, second] = ev.parse_questions(
        [
            '{"question": "rank_results", "type": "identifier", "files": ["app:./app/search.py", "README.md"]}',
            "",
            '{"id": 7, "question": "KB-01", "type": "code", "nodes": "usecase:KB-01", "project": "demo", '
            '"repo": "app", "note": "kept for the author"}',
        ]
    )
    assert first.id == "line 1" and first.files == (("app", "app/search.py"), (None, "README.md"))
    assert first.nodes == () and first.answers == 2 and first.project is None
    assert second.id == "7" and second.nodes == ("usecase:KB-01",) and (second.project, second.repo) == ("demo", "app")


def test_eval_names_every_bad_line_of_a_question_set():
    lines = [
        "not json",
        '["a list"]',
        '{"type": "x", "files": ["a.py"]}',
        '{"question": "q", "type": "x"}',
        '{"question": "q", "type": "x", "files": [""]}',
        '{"id": "a", "question": "q", "type": "x", "files": ["a.py"]}',
        '{"id": "a", "question": "q2", "type": "x", "files": ["b.py"]}',
    ]
    with pytest.raises(ev.QuestionError) as caught:
        ev.parse_questions(lines)
    message = str(caught.value)
    assert "line 1: not JSON" in message
    assert "line 2: not a JSON object" in message
    assert "line 3: question is required" in message
    assert "line 4: no answer: give files, nodes or both" in message
    assert "line 5: files is a list of non-empty strings" in message
    assert "line 7: id 'a' is used twice" in message and "line 6" not in message
    with pytest.raises(ev.QuestionError, match="no question"):
        ev.parse_questions(["", "  "])


# -- matching answers ----------------------------------------------------------------------------------------------

SYMBOL = {
    "id": "symbol:app:app/search.py::search",
    "kind": "Symbol",
    "props": {"path": "app/search.py", "key": ["app", "app/search.py", "search"]},
    "aliases": [],
}
FILE = {"id": "app:file:app/search.py", "kind": "File", "props": {"path": "app/search.py", "repo": "app"}}
SECTION = {"id": "app:file:docs/guide.md#setup", "kind": "Section", "props": {"item": "app:file:docs/guide.md"}}
OTHER_REPO = {"id": "lib:file:app/search.py", "kind": "File", "props": {"path": "app/search.py", "repo": "lib"}}
ALIASED = {"id": "usecase:KB-01", "kind": "UseCase", "props": {}, "aliases": ["kb-01"]}


def test_eval_matches_a_node_to_a_file_by_path_and_repo_and_to_a_node_by_id_or_alias():
    assert ev.node_file(SYMBOL) == ("app", "app/search.py")
    assert ev.node_file(FILE) == ("app", "app/search.py")
    assert ev.node_file(SECTION) == (None, "docs/guide.md")
    question = ev.Question(
        id="q",
        text="search",
        type="t",
        files=(("app", "app/search.py"), (None, "docs/guide.md"), ("lib", "lib/none.py")),
        nodes=("symbol:app:app/search.py::search", "kb-01"),
    )
    assert ev.node_ranks(question, [OTHER_REPO, SECTION, SYMBOL, ALIASED]) == [3, 2, None, 3, 4]
    assert ev.node_ranks(question, []) == [None] * 5


def test_eval_grep_answers_a_node_by_the_file_its_id_names():
    assert ev.id_file("symbol:app:app/search.py::Server.handle") == ("app", "app/search.py")
    assert ev.id_file("app:file:docs/guide.md#setup") == ("app", "docs/guide.md")
    assert ev.id_file("usecase:KB-01") is None
    question = ev.Question(
        id="q",
        text="x",
        type="t",
        files=((None, "b.py"),),
        nodes=("symbol:app:a.py::f", "usecase:KB-01"),
    )
    assert ev.file_ranks(question, [("lib", "a.py"), ("app", "a.py"), ("app", "b.py")]) == [3, 2, None]


def test_eval_grep_terms_keep_identifiers_and_drop_stop_words():
    assert ev.grep_terms("Server.handle") == ["Server.handle", "Server", "handle"]
    assert ev.grep_terms("where is the ranking of results") == ["ranking", "results"]
    assert ev.grep_terms("Lệnh đánh giá của graph") == ["Lệnh", "đánh", "giá", "graph"]
    assert ev.grep_terms("a an of") == []
    assert len(ev.grep_terms(" ".join(f"word{i}" for i in range(20)))) == ev.GREP_MAX_TERMS


# -- the numbers ---------------------------------------------------------------------------------------------------


def node(path, repo="r", name=None):
    nid = f"symbol:{repo}:{path}::{name}" if name else f"{repo}:file:{path}"
    props = {"path": path, "key": [repo, path, name]} if name else {"path": path, "repo": repo}
    return {"id": nid, "kind": "Symbol" if name else "File", "props": props, "aliases": []}


ERROR = "error: nothing visible matches; try kg_search"
ANSWERS = {
    ("kg_search", "alpha_fn"): ("x" * 30, [node("src/beta.py"), node("src/alpha.py", name="alpha_fn")]),
    ("kg_context", "alpha_fn"): ("y" * 300, [node("src/alpha.py", name="alpha_fn")]),
    ("kg_search", "gamma"): ("z" * 15, []),
    ("kg_context", "gamma"): (ERROR, None),
    ("kg_search", "change beta and alpha"): (
        "w" * 60,
        [node("src/beta.py"), {"id": "r:file:docs/a.md#top", "props": {"item": "r:file:docs/a.md"}}],
    ),
    ("kg_context", "change beta and alpha"): ("v" * 90, [node("docs/a.md"), node("src/alpha.py", repo="other")]),
}


class FakeSession:
    """Answers kg_search and kg_context as Session.call does, from ANSWERS."""

    def __init__(self):
        self.calls = []

    def call(self, name, args):
        self.calls.append((name, args))
        text, nodes = ANSWERS[(name, args["query"])]
        if nodes is None:
            return {"content": [{"type": "text", "text": text}], "isError": True}
        key = "results" if name == "kg_search" else "nodes"
        return {"content": [{"type": "text", "text": text}], "structuredContent": {key: nodes}}


QUESTIONS = [
    '{"id": "q1", "question": "alpha_fn", "type": "identifier", "files": ["r:src/alpha.py"], '
    '"nodes": ["symbol:r:src/alpha.py::alpha_fn"]}',
    '{"id": "q2", "question": "gamma", "type": "identifier", "nodes": ["usecase:G-1"]}',
    '{"id": "q3", "question": "change beta and alpha", "type": "plan-step", "repo": "r", '
    '"files": ["r:src/beta.py", "r:src/alpha.py"]}',
]


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "r"
    write(repo / "src/alpha.py", "def alpha_fn():\n    return beta()\n")
    write(repo / "src/beta.py", "def beta():\n    return 1\n")
    write(repo / "docs/a.md", "alpha_fn is documented here\n")
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "init")
    return repo


def row(report, kind, method):
    return next(r for r in report.rows if (r["type"], r["method"]) == (kind, method))


def test_eval_prints_the_numbers_of_each_method_per_question_type(checkout):
    session = FakeSession()
    report = ev.evaluate(
        ev.parse_questions(QUESTIONS),
        session_for=lambda project: session,
        checkouts_for=lambda project: {"r": checkout},
        limit=7,
        budget_tokens=1000,
    )
    assert session.calls[0] == ("kg_search", {"query": "alpha_fn", "limit": 7})
    assert session.calls[1] == ("kg_context", {"query": "alpha_fn", "budget_tokens": 1000})
    ranks = {(o.question.id, o.method): o.ranks for o in report.outcomes}
    assert ranks == {
        ("q1", "kg_search"): [2, 2],
        ("q1", "kg_context"): [1, 1],
        ("q1", "grep"): [2, 2],  # docs/a.md and src/alpha.py hold the one term once each: the path breaks the tie
        ("q2", "kg_search"): [None],
        ("q2", "kg_context"): [None],
        ("q2", "grep"): [None],
        ("q3", "kg_search"): [1, None],  # the section is of docs/a.md
        ("q3", "kg_context"): [None, None],  # src/alpha.py of repo other is not the answer's file
        ("q3", "grep"): [3, 1],  # src/alpha.py holds beta and alpha; then docs/a.md and src/beta.py by path
    }
    grep_q1 = "docs/a.md:1:alpha_fn is documented here\nsrc/alpha.py:1:def alpha_fn():"
    grep_q3 = (
        "src/alpha.py:1:def alpha_fn():\nsrc/alpha.py:2:return beta()\n"
        "docs/a.md:1:alpha_fn is documented here\nsrc/beta.py:1:def beta():"
    )
    error_tokens = ev.estimate_tokens(ERROR)
    expected = {
        ("identifier", "kg_search"): (2, 0.0, 0.5, 0.25, 0.5, 0, 2, 10 + 5),
        ("identifier", "kg_context"): (2, 0.5, 0.5, 0.5, 0.5, 1, 2, 100 + error_tokens),
        ("identifier", "grep"): (2, 0.0, 0.5, 0.25, 0.5, 0, 2, ev.estimate_tokens(grep_q1)),
        ("plan-step", "kg_search"): (1, 0.5, 0.5, 1.0, 0.0, 0, 1, 20),
        ("plan-step", "kg_context"): (1, 0.0, 0.0, 0.0, 0.0, 0, 0, 30),
        ("plan-step", "grep"): (1, 0.5, 1.0, 1.0, 0.0, 0, 2, ev.estimate_tokens(grep_q3)),
        ("all", "kg_search"): (3, 0.1667, 0.5, 0.5, 0.3333, 0, 3, 35),
        ("all", "kg_context"): (3, 0.3333, 0.3333, 0.3333, 0.3333, 1, 2, 130 + error_tokens),
        ("all", "grep"): (
            3,
            0.1667,
            0.6667,
            0.5,
            0.3333,
            0,
            4,
            ev.estimate_tokens(grep_q1) + ev.estimate_tokens(grep_q3),
        ),
    }
    assert [(r["type"], r["method"]) for r in report.rows] == list(expected)
    for (kind, method), (n, r1, r5, mrr, empty, errors, found, tokens) in expected.items():
        got = row(report, kind, method)
        assert (got["n"], got["recall@1"], got["recall@5"], got["mrr"], got["empty"], got["errors"]) == (
            n,
            r1,
            r5,
            mrr,
            empty,
            errors,
        ), (kind, method)
        assert got["correct_per_1k_tokens"] == round(1000 * found / tokens, 3), (kind, method)
        assert got["mean_tokens"] == round(tokens / n, 1) and got["skipped"] == 0

    table = ev.render(report).splitlines()
    assert table[0].split() == [
        "TYPE",
        "METHOD",
        "N",
        "R@1",
        "R@5",
        "MRR",
        "EMPTY",
        "OK/1K",
        "TOK",
        "MEAN",
        "TOK",
        "P95",
        "MS",
    ]
    assert table[1].split()[:8] == ["identifier", "kg_search", "2", "0.000", "0.500", "0.250", "50%", "133.33"]
    assert table[9].split()[:7] == ["all", "grep", "3", "0.167", "0.667", "0.500", "33%"]
    assert "kg_context: 1 call(s) answered with an error; first, on q2: nothing visible matches" in table[-1]
    assert "3 question(s); kg_search limit 7, kg_context and grep within 1000 tokens" in "\n".join(table)

    data = report.to_json()
    assert data["questions"] == 3 and data["methods"] == list(ev.METHODS) and len(data["results"]) == 9


def test_eval_grep_cuts_its_output_at_the_budget_and_reads_a_ref(checkout):
    write(checkout / "src/alpha.py", "def alpha_fn():\n    return 2\n")  # the working tree moves on, main does not
    files = ev.grep_checkout("r", checkout, ["beta"], "main")
    assert sorted(files) == ["src/alpha.py", "src/beta.py"]
    assert sorted(ev.grep_checkout("r", checkout, ["beta"], None)) == ["src/beta.py"]
    long = [ev.GrepFile("r", f"f{i}.py", {"t"}, 1, [("1", "x" * 100)]) for i in range(20)]
    text, shown = ev.grep_output(long, ev.MIN_BUDGET_TOKENS)
    assert len(text.encode()) <= ev.MIN_BUDGET_TOKENS * ev.BYTES_PER_TOKEN
    assert len(shown) == text.count("\n") + 1 < 20
    with pytest.raises(RuntimeError, match="git grep"):
        ev.grep_checkout("r", checkout, ["beta"], "no-such-ref")


def test_eval_skips_grep_without_a_checkout_and_leaves_it_out_of_the_numbers(checkout):
    questions = ev.parse_questions(QUESTIONS[2:] + ['{"question": "beta", "type": "plan-step", "files": ["x.py"]}'])
    report = ev.evaluate(
        questions,
        session_for=lambda project: FakeSession(),
        checkouts_for=lambda project: {"other": checkout},
        methods=["grep"],
    )
    [only] = report.rows[:1]
    assert (only["method"], only["n"], only["skipped"]) == ("grep", 1, 1)
    assert "grep: skipped 1 question(s): no checkout of repo r" in ev.render(report)


def test_eval_p95_is_the_nearest_rank():
    assert ev.p95([]) == 0.0
    assert ev.p95([5.0]) == 5.0
    assert ev.p95([float(v) for v in range(20, 0, -1)]) == 19.0
    assert ev.p95([float(v) for v in range(1, 101)]) == 95.0


# -- the command on a built graph ----------------------------------------------------------------------------------


@pytest.fixture
def demo(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    sync_project(project)
    assert build_project(project).ok
    return project


def test_eval_command_runs_the_fixture_set_on_a_built_graph(demo, capsys):
    root = str(demo.harness.root)
    assert main(["kg", "eval", str(FIXTURE), "--project", root, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["backend"], data["sink"], data["limit"], data["budget_tokens"]) == ("local", "cli", 10, 3000)
    results = {(r["id"], r["method"]): r for r in data["results"]}
    # stable facts of the demo graph, whatever the ranking does elsewhere
    assert results[("rank", "kg_search")]["ranks"] == [1, 1]
    assert results[("code", "kg_search")]["ranks"] == [1]
    assert results[("ranking-step", "grep")]["ranks"][0] <= 5
    assert all(results[("nothing", m)]["count"] == 0 for m in ev.METHODS)
    assert all(r["skipped"] is None for r in data["results"])  # the app repo is a checkout, the harness is not one

    # every row is what its questions' results say
    types = ["identifier", "plan-step", "words"]
    assert [(r["type"], r["method"]) for r in data["rows"]] == [(t, m) for t in [*types, "all"] for m in ev.METHODS]
    for got in data["rows"]:
        mine = [r for r in data["results"] if r["method"] == got["method"] and got["type"] in ("all", r["type"])]
        recall5 = sum(sum(1 for k in r["ranks"] if k and k <= 5) / len(r["ranks"]) for r in mine) / len(mine)
        mrr = sum(1 / min((k for k in r["ranks"] if k), default=float("inf")) for r in mine) / len(mine)
        tokens = sum(r["tokens"] for r in mine)
        assert got["n"] == len(mine)
        assert got["recall@5"] == round(recall5, 4) and got["mrr"] == round(mrr, 4)
        assert got["empty"] == round(sum(1 for r in mine if r["count"] == 0) / len(mine), 4)
        assert got["correct_per_1k_tokens"] == round(1000 * sum(r["found"] for r in mine) / tokens, 3)

    assert main(["kg", "eval", str(FIXTURE), "--project", root, "--type", "identifier", "--method", "kg_search"]) == 0
    table = capsys.readouterr().out.splitlines()
    assert table[0].startswith("TYPE") and [line.split()[:3] for line in table[1:3]] == [
        ["identifier", "kg_search", "3"],
        ["all", "kg_search", "3"],
    ]


def test_eval_command_refuses_a_bad_set_and_fails_when_a_method_errs_on_every_question(demo, tmp_path, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"question": "x", "type": "t"}\n')
    assert main(["kg", "eval", str(bad), "--project", str(demo.harness.root)]) == 2
    assert "line 1: no answer" in capsys.readouterr().err
    assert main(["kg", "eval", str(FIXTURE), "--project", str(demo.harness.root), "--type", "nope"]) == 2
    assert main(["kg", "eval", str(FIXTURE), "--project", str(demo.harness.root), "--repo", "app"]) == 2
    assert "--repo takes NAME=PATH" in capsys.readouterr().err
    unknown = tmp_path / "unknown.jsonl"
    unknown.write_text('{"question": "x", "type": "t", "project": "no-such-project", "files": ["a.py"]}\n')
    assert main(["kg", "eval", str(unknown), "--method", "kg_search"]) == 1
    assert "kg_search: 1 call(s) answered with an error" in capsys.readouterr().out


# -- questions from the plans of a hub -----------------------------------------------------------------------------


class StubHub:
    """The two reads plan_questions makes: the plan list of a project and one plan."""

    def __init__(self, plans: dict):
        self.plans = plans
        self.paths = []

    def call(self, method, path):
        assert method == "GET"
        self.paths.append(path)
        if path.endswith("/plans"):
            return [{"plan_id": plan_id} for plan_id in self.plans]
        return {"body": self.plans[path.rsplit("/", 1)[1]]}


@pytest.fixture
def history(tmp_path):
    """A repo with a root commit, a commit of two files and a merge of a branch that adds a third."""
    repo = tmp_path / "app"
    write(repo / "README.md", "# app\n")
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "root")
    root = git(repo, "rev-parse", "HEAD").strip()
    write(repo / "a.py", "a = 1\n")
    write(repo / "b.py", "b = 1\n")
    commit(repo, "two files")
    two = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo / "c.py", "c = 1\n")
    commit(repo, "third")
    git(repo, "checkout", "-q", "main")
    write(repo / "a.py", "a = 2\n")
    commit(repo, "main moves")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    merge = git(repo, "rev-parse", "HEAD").strip()
    return repo, {"root": root, "two": two, "merge": merge}


def plan_body(shas):
    return {
        "id": "demo",
        "repos": [{"repo": "app"}],
        "steps": [
            {
                "id": 1,
                "title": "Two files",
                "what": "Add  a.py\nand b.py.",
                "status": "done",
                "evidence": f"app@{shas['two'][:10]}; Decision: none",
            },
            {"id": 2, "title": "Merge the feature", "status": "done", "evidence": f"PR merged as {shas['merge']}"},
            {"id": 3, "title": "Root", "repo": "app", "status": "done", "evidence": f"app@{shas['root'][:12]}"},
            {"id": 4, "title": "Pending", "status": "pending", "evidence": f"app@{shas['two'][:10]}"},
            {"id": 5, "title": "Lost", "status": "done", "evidence": "app@deadbeef12"},
            {"id": 6, "title": "Elsewhere", "repo": "web", "status": "done", "evidence": f"web@{shas['two'][:10]}"},
            {"id": 7, "title": "No commit", "status": "done", "evidence": "verified by hand"},
        ],
    }


def test_eval_questions_from_plans_name_the_files_each_steps_commits_changed(history):
    repo, shas = history
    assert changed_files(repo, shas["two"]) == ["a.py", "b.py"]
    assert changed_files(repo, shas["merge"]) == ["c.py"]  # against the first parent: what the branch brought
    assert changed_files(repo, shas["root"]) == ["README.md"]
    assert changed_files(repo, "deadbeef12") is None

    hub = StubHub({"demo": plan_body(shas), "other": {"id": "other", "steps": []}})
    questions, skipped = plan_questions(hub, "proj", {"app": repo}, plans=["demo"])
    assert hub.paths == ["/v1/projects/proj/plans", "/v1/projects/proj/plans/demo"]
    assert [(q["id"], q["question"], q["files"]) for q in questions] == [
        ("demo#1", "Two files", ["app:a.py", "app:b.py"]),
        ("demo#2", "Merge the feature", ["app:c.py"]),
        ("demo#3", "Root", ["app:README.md"]),
    ]
    assert questions[0]["type"] == "plan-step" and questions[0]["project"] == "proj" and questions[0]["repo"] == "app"
    assert questions[1]["commits"] == [shas["merge"]]
    assert skipped == [
        "demo#5: none of its 1 commit(s) is in the checkout of app",
        "demo#6: no checkout of repo web",
    ]

    questions, skipped = plan_questions(hub, "proj", {"app": repo}, plans=["demo"], text="what", max_files=1)
    assert [(q["id"], q["question"]) for q in questions] == [("demo#2", "Merge the feature"), ("demo#3", "Root")]
    assert "demo#1: its commits change 2 file(s), not 1 to 1" in skipped
    assert ev.parse_questions(json.dumps(q) for q in questions)[0].files == (("app", "c.py"),)

    write(repo / "docs/Báo cáo.md", "x\n")  # git quotes such a path unless asked for it verbatim
    commit(repo, "report")
    assert changed_files(repo, git(repo, "rev-parse", "HEAD").strip()) == ["docs/Báo cáo.md"]


def test_eval_questions_command_prints_a_set_kg_eval_reads(history, kg_env, monkeypatch, capsys):
    from evo_agents.hub import client

    repo, shas = history
    hub = StubHub({"demo": plan_body(shas)})
    monkeypatch.setattr(
        client, "load_credentials", lambda: client.Credentials(url="https://hub.test", login="me", token="t")
    )
    monkeypatch.setattr(client, "Hub", lambda url, token: hub)
    assert main(["kg", "eval-questions", "--project", "proj", "--repo", f"app={repo}"]) == 0
    out = capsys.readouterr()
    assert "3 question(s) from the plans of proj; 2 step(s) left out" in out.err
    assert "left out demo#6: no checkout of repo web" in out.err
    questions = ev.parse_questions(out.out.splitlines())
    assert [q.id for q in questions] == ["demo#1", "demo#2", "demo#3"]
    assert all(q.type == "plan-step" and q.project == "proj" for q in questions)
