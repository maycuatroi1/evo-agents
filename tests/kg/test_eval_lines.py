"""``kg eval`` with line answers: parsing ``lines``, the line scores, Acc@k, and that a set without ``lines`` keeps
the columns it had. Synthetic fixtures only."""

import math

import pytest

from evo_agents.kg import evaluate as ev


def question(**more):
    entry = {"id": "q", "question": "ask", "type": "t", **more}
    parsed, problems = ev.parse_question(entry, 1)
    assert not problems, problems
    return parsed


def test_lines_parse_from_an_object_or_a_list_and_add_their_files_as_answers():
    q = question(lines={"app:a.py": [[10, 12], "20-21"], "b.py": [3, 4]}, files=["app:a.py"])
    assert q.lines == (("app", "a.py", 10, 12), ("app", "a.py", 20, 21), (None, "b.py", 3, 4))
    assert q.files == (("app", "a.py"), (None, "b.py"))
    assert question(lines=["app:a.py:5-9"]).lines == (("app", "a.py", 5, 9),)


@pytest.mark.parametrize("bad", [{"a.py": [[0, 3]]}, {"a.py": [[5, 2]]}, ["a.py"], "a.py", {"a.py": []}, {}])
def test_bad_lines_are_reported(bad):
    _, problems = ev.parse_question({"question": "q", "type": "t", "lines": bad}, 1)
    assert problems


def sym(path, first, last, repo="r"):
    return {
        "id": f"symbol:{repo}:{path}::f{first}",
        "kind": "Symbol",
        "props": {"path": path, "key": [repo, path, f"f{first}"], "line": first, "end_line": last},
    }


def test_node_spans_come_from_symbol_section_passage_and_file():
    assert ev.node_span(sym("a.py", 4, 9)) == (4, 9)
    assert ev.node_span({"id": "s", "props": {"span": "lines 7-30"}}) == (7, 30)
    assert ev.node_span({"id": "s", "props": {"span": "lines 7-30"}}, {"s": {"lines": "lines 10-12"}}) == (10, 12)
    assert ev.node_span({"id": "f", "props": {"path": "a.py"}}) == (None, None)
    assert ev.node_span({"id": "s", "props": {"line": 5}}) == (5, 5)


def test_scores_count_distinct_lines_within_the_budget():
    q = question(lines={"r:a.py": [[1, 10]]})
    items = [("r", "a.py", 1, 5), ("r", "a.py", 3, 8), ("r", "b.py", 1, 10)]
    got = ev.score_lines(q, items, budget=100)
    # returned lines: 5 + 6 + 10 = 21 with the repeats; distinct 18, of which 8 are in the answer
    assert got["returned"] == 21
    assert got["precision"] == round(8 / 18, 4) and got["recall"] == 0.8
    assert got["context_efficiency"] == round(11 / 21, 4)
    ideal = sum(1 / math.log2(i + 1) for i in range(1, 11))
    dcg = sum(1 / math.log2(i + 1) for i in [1, 2, 3, 4, 5, 9, 10, 11])  # the repeated lines 3-5 gain nothing
    assert got["ndcg"] == round(dcg / ideal, 4)
    cut = ev.score_lines(q, items, budget=5)
    assert cut["returned"] == 5 and cut["precision"] == 1.0 and cut["recall"] == 0.5 and cut["f1"] == round(2 / 3, 4)


def test_a_result_that_is_only_a_file_counts_the_whole_file():
    q = question(lines={"r:a.py": [[1, 2]]})
    whole = [("r", "a.py", None, None)]
    assert ev.score_lines(q, whole, 500, lambda repo, path: 20)["precision"] == 0.1
    assert ev.score_lines(q, whole, 500)["returned"] == 500  # length unknown: the room that is left
    other_repo = ev.score_lines(q, [("x", "a.py", 1, 2)], 500)  # same path, another repo, is not the answer
    assert other_repo["recall"] == 0.0 and other_repo["returned"] == 2
    assert ev.score_lines(q, [], 500) == {
        "returned": 0,
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
        "context_efficiency": 0.0,
        "ndcg": 0.0,
    }


def run(lines, **more):
    qs = [question(lines=lines, **more)]
    nodes = [sym("a.py", 1, 4), {"id": "r:file:b.py", "props": {"path": "b.py", "repo": "r"}}]

    class Session:
        def call(self, name, args):
            key = "results" if name == "kg_search" else "nodes"
            return {"content": [{"type": "text", "text": "x"}], "structuredContent": {key: nodes}}

    return ev.evaluate(
        qs, session_for=lambda p: Session(), checkouts_for=lambda p: {}, methods=("kg_search",), line_budget=50
    )


def test_a_set_with_lines_reports_line_scores_and_acc_in_table_and_json():
    report = run({"r:a.py": [[1, 4]]}, nodes=["symbol:r:a.py::f1"])
    row = report.rows[-1]
    assert row["line_precision"] > 0
    assert row["line_recall"] == 1.0 and row["acc_file@1"] == 1.0 and row["acc_func@1"] == 1.0
    assert report.settings["line_budget"] == 50
    assert report.to_json()["results"][0]["lines"]["recall"] == 1.0
    text = ev.render(report)
    assert "CE" in text and "ACC5 FILE" in text and "within 50 lines" in text


def test_acc_needs_every_answer_file_in_the_first_k():
    report = run({"r:a.py": [[1, 4]], "r:b.py": [[1, 2]], "r:c.py": [[1, 2]]})
    row = report.rows[-1]
    assert row["acc_file@1"] == row["acc_file@5"] == row["acc_file@10"] == 0.0  # c.py is never returned
    assert row["acc_func@1"] is None  # no nodes in the question
    assert run({"r:a.py": [[1, 4]], "r:b.py": [[1, 2]]}).rows[-1]["acc_file@5"] == 1.0
    assert run({"r:a.py": [[1, 4]], "r:b.py": [[1, 2]]}).rows[-1]["acc_file@1"] == 0.0


def test_a_set_without_lines_keeps_its_columns():
    plain = ev.parse_questions(['{"id": "a", "question": "q", "type": "t", "files": ["r:a.py"]}'])

    class Session:
        def call(self, name, args):
            return {"content": [{"type": "text", "text": "x"}], "structuredContent": {"results": [sym("a.py", 1, 2)]}}

    report = ev.evaluate(plain, session_for=lambda p: Session(), checkouts_for=lambda p: {}, methods=("kg_search",))
    assert "line_budget" not in report.settings and "line_precision" not in report.rows[0]
    assert "lines" not in report.to_json()["results"][0]
    assert [c for c in ev.render(report).splitlines()[0].split()][-1] == "MS"
