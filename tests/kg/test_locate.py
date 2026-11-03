from dataclasses import replace

from evo_agents.kg import locate
from evo_agents.kg.locate import Cand, Pool, anchors, clean


def _pool(rows, links=None):
    pool = Pool()
    for node_id, kind, path, rel in rows:
        pool.cands[node_id] = Cand(node_id, kind, node_id.rsplit(":", 1)[-1], path, None, None, rel)
        pool.order.append(node_id)
    pool.links = links or {}
    return pool


def test_clean_drops_comments_and_links_the_text_only_quotes():
    text = "Bug <!-- fill this in: pandas/core/frame.py -->\nsee https://example.test/a/b.py now"
    assert "frame.py" not in clean(text) and "example.test" not in clean(text)


def test_anchors_read_traceback_lines_paths_and_names_the_text_gives():
    text = (
        'Traceback:\n  File "src/app/core.py", line 42, in run_job\nCalling `Widget.render()` fails; see docs/guide.md '
        "and version 1.2.3, also load_config(path)."
    )
    found = {(a.kind, a.text, a.line) for a in anchors(text)}
    assert ("line", "src/app/core.py", 42) in found
    assert ("name", "run_job", None) in found
    assert ("name", "Widget.render", None) in found
    assert not any(a.text == "1.2.3" for a in anchors(text))


def test_a_named_node_comes_before_a_text_match_and_neighbours_follow_the_best():
    rows = [
        ("s:a.py::one", "Symbol", "a.py", 1.0),
        ("s:b.py::two", "Symbol", "b.py", 0.9),
        ("s:c.py::named", "Symbol", "c.py", 0.0),
    ]
    pool = _pool(rows)
    pool.cands["s:c.py::named"].anchor = 1.0
    ranked = locate.order(pool)
    assert ranked[0] == "s:c.py::named"
    linked = _pool(rows[:2], {"s:a.py::one": [("s:d.py::near", "calls", 1.0)]})
    linked.cands["s:d.py::near"] = Cand("s:d.py::near", "Symbol", "near", "d.py", None, None, 0.0)
    assert "s:d.py::near" in locate.order(linked, replace(locate.DEFAULT, expand=1.0))
    assert "s:d.py::near" not in locate.order(linked, replace(locate.DEFAULT, expand=0.0))


def test_a_file_lists_its_second_symbol_after_other_files_first_ones():
    rows = [
        ("s:a.py::one", "Symbol", "a.py", 1.0),
        ("s:a.py::two", "Symbol", "a.py", 0.9),
        ("s:b.py::three", "Symbol", "b.py", 0.5),
    ]
    ranked = locate.order(_pool(rows), replace(locate.DEFAULT, second=5.0))
    assert ranked == ["s:a.py::one", "s:b.py::three", "s:a.py::two"]


def test_a_graph_that_is_not_code_keeps_the_text_order():
    pool = _pool([("doc:x", "Document", None, 1.0), ("sec:y", "Section", None, 0.9)])
    assert not locate.code_like(pool)
    assert locate.code_like(_pool([("s:a.py::one", "Symbol", "a.py", 1.0)]))
