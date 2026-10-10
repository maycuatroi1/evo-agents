"""evo_agents keeps within its size and complexity budgets, and its baselines and import exceptions only shrink.

tests/structure.py measures and checks; docs/structure.md says how to split a module or a function along a seam. The
import boundaries themselves are import-linter's (`lint-imports`, contracts in pyproject.toml): this file only keeps
their ignore_imports from growing against main.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter

import pytest

from tests import structure
from tests.structure import (
    BASELINE,
    GUIDE,
    MODULE_LINES,
    PYPROJECT,
    ROOT,
    TIGHTEN,
    Measures,
)

CONTRACTS = {
    "worker-not-hub-server",
    "kg-not-hub-or-worker",
    "hub-server-from-hub-server",
    "packages-acyclic",
    "hub-runs-layers",
}


@pytest.fixture(scope="module")
def measures() -> Measures:
    return structure.measure()


@pytest.fixture(scope="module")
def baseline() -> dict:
    return structure.load_baseline((ROOT / BASELINE).read_text(encoding="utf-8"))


def on_main(path: str) -> str:
    """``path`` as the base ref holds it; skips when the ref or the file is not there, unless CI named the ref."""
    ref, named = structure.base_ref()
    if not structure.ref_exists(ref):
        if named:
            pytest.fail(
                f"{structure.BASE_ENV}={ref} names no commit in this checkout; fetch it: {structure.FETCH_MAIN}"
            )
        pytest.skip(f"{ref} is not in this checkout, so nothing is compared with main ({structure.FETCH_MAIN})")
    text = structure.file_at(ref, path)
    if text is None:
        pytest.skip(f"{ref} has no {path} yet")
    return text


# The code against the baseline


def test_modules_keep_within_the_line_budget(measures, baseline):
    problems = structure.check_modules(measures.modules, baseline["modules"])
    assert not problems, structure.problems_text("Modules over their line budget", problems)


def test_functions_keep_within_the_statement_and_complexity_budgets(measures, baseline):
    problems = structure.check_functions(measures.functions, baseline["functions"])
    assert not problems, structure.problems_text("Functions over their budget", problems)


def test_private_names_are_not_imported_across_modules_beyond_the_baseline(measures, baseline):
    problems = structure.check_private_imports(measures.private_imports, baseline["private_imports"])
    assert not problems, structure.problems_text("Private names imported across modules", problems)


def test_the_baseline_file_is_written_as_tighten_writes_it(baseline):
    assert (ROOT / BASELINE).read_text(encoding="utf-8") == structure.dump_baseline(baseline), (
        f"{BASELINE} is not in the layout `{TIGHTEN}` writes (one entry a line, sorted); run it to rewrite the file"
    )


# The baselines against main


def test_the_baseline_adds_or_raises_nothing_against_main(baseline):
    main = structure.load_baseline(on_main(BASELINE))
    problems = structure.compare_baselines(baseline, main)
    assert not problems, structure.problems_text(f"{BASELINE} against main", problems)


def test_the_import_exceptions_add_nothing_against_main():
    main = structure.contract_exceptions(on_main(PYPROJECT))
    if main is None:
        pytest.skip("main declares no import-linter contracts yet")
    branch = structure.contract_exceptions((ROOT / PYPROJECT).read_text(encoding="utf-8"))
    problems = structure.compare_contracts(branch or {}, main)
    assert not problems, structure.problems_text(f"{PYPROJECT} against main", problems)


def test_the_package_boundaries_are_declared():
    declared = structure.contract_exceptions((ROOT / PYPROJECT).read_text(encoding="utf-8")) or {}
    missing = sorted(CONTRACTS - set(declared))
    assert not missing, (
        f"{PYPROJECT} no longer declares the import-linter contracts {', '.join(missing)}; the package boundaries of "
        f"{GUIDE} (section Package boundaries) need them"
    )


# The checks themselves


def test_a_module_over_the_budget_is_named_with_its_overage_and_the_way_to_split_it():
    [problem] = structure.check_modules({"evo_agents/hub/big.py": 1043}, {})
    assert "evo_agents/hub/big.py has 1043 lines, 43 over the budget of 1000" in problem
    assert "seam" in problem and GUIDE in problem


def test_a_frozen_module_may_not_grow_and_must_be_lowered_when_it_shrinks():
    frozen = {"evo_agents/a.py": 2000}
    [grew] = structure.check_modules({"evo_agents/a.py": 2010}, frozen)
    assert "10 over its frozen baseline of 2000" in grew and GUIDE in grew
    [shrank] = structure.check_modules({"evo_agents/a.py": 1990}, frozen)
    assert "Lower the baseline to 1990" in shrank and TIGHTEN in shrank
    [fits] = structure.check_modules({"evo_agents/a.py": 900}, frozen)
    assert "within the budget" in fits and TIGHTEN in fits
    [gone] = structure.check_modules({}, frozen)
    assert "no longer exists" in gone
    assert structure.check_modules({"evo_agents/a.py": 2000, "evo_agents/b.py": MODULE_LINES}, frozen) == []


def test_a_function_over_a_budget_is_named_with_its_line_its_overage_and_the_rule():
    found = {"evo_agents/x.py::Thing.run": {"line": 12, "statements": 63, "complexity": 17}}
    problems = structure.check_functions(found, {})
    assert len(problems) == 2
    assert "evo_agents/x.py:12 Thing.run has 63 statements, 13 over the budget of 50 (ruff PLR0915)" in problems[0]
    assert "evo_agents/x.py:12 Thing.run has a complexity of 17, 2 over the budget of 15 (ruff C901)" in problems[1]
    assert all(GUIDE in p for p in problems)


def test_a_frozen_function_keeps_its_numbers_and_only_its_numbers():
    frozen = {"evo_agents/x.py::f": {"statements": 60}}
    assert structure.check_functions({"evo_agents/x.py::f": {"line": 3, "statements": 60}}, frozen) == []
    [grew] = structure.check_functions({"evo_agents/x.py::f": {"line": 3, "statements": 61}}, frozen)
    assert "1 over its frozen baseline of 60" in grew
    [shrank] = structure.check_functions({"evo_agents/x.py::f": {"line": 3, "statements": 55}}, frozen)
    assert "Lower the baseline to 55" in shrank
    [complex_too] = structure.check_functions(
        {"evo_agents/x.py::f": {"line": 3, "statements": 60, "complexity": 16}}, frozen
    )
    assert "a complexity of 16, 1 over the budget of 15" in complex_too
    [fixed] = structure.check_functions({}, frozen)
    assert "evo_agents/x.py f is frozen" in fixed and TIGHTEN in fixed


def test_a_function_moved_to_another_module_is_pointed_at_tighten():
    frozen = {"evo_agents/old.py::report_state": {"statements": 52}}
    problems = structure.check_functions({"evo_agents/new.py::report_state": {"line": 9, "statements": 52}}, frozen)
    assert any("If you moved it unchanged from evo_agents/old.py" in p for p in problems)


def test_a_new_private_import_is_named_with_the_count_and_the_fix():
    frozen = {"evo_agents/a.py": ["evo_agents.b._x"]}
    [problem] = structure.check_private_imports({"evo_agents/a.py": ["evo_agents.b._x", "evo_agents.c._y"]}, frozen)
    assert "evo_agents/a.py imports _y from evo_agents.c" in problem
    assert "2 such imports, 1 in" in problem and "public spelling" in problem and GUIDE in problem
    [stale] = structure.check_private_imports({}, frozen)
    assert "no longer imports evo_agents.b._x" in stale and TIGHTEN in stale


def test_the_baseline_may_shrink_but_not_grow_against_main():
    main = {
        "modules": {"evo_agents/a.py": 2000},
        "functions": {"evo_agents/a.py::f": {"statements": 60}, "evo_agents/a.py::g": {"complexity": 20}},
        "private_imports": {"evo_agents/a.py": ["evo_agents.b._x"]},
    }
    shrunk = {
        "modules": {"evo_agents/a.py": 1900},
        "functions": {"evo_agents/a.py::f": {"statements": 55}},
        "private_imports": {},
    }
    assert structure.compare_baselines(shrunk, main) == []
    grown = {
        "modules": {"evo_agents/a.py": 2001, "evo_agents/b.py": 1100},
        "functions": {
            "evo_agents/a.py::f": {"statements": 61, "complexity": 16},
            "evo_agents/a.py::h": {"statements": 51},
        },
        "private_imports": {"evo_agents/a.py": ["evo_agents.b._x", "evo_agents.b._y"]},
    }
    problems = "\n".join(structure.compare_baselines(grown, main))
    assert "raises evo_agents/a.py from 2000 lines on main to 2001" in problems
    assert "adds evo_agents/b.py (1100 lines)" in problems
    assert "raises the statements of evo_agents/a.py f from 60 on main to 61" in problems
    assert "adds a complexity of 16 to evo_agents/a.py f" in problems
    assert "adds evo_agents/a.py h (51 statements)" in problems
    assert "adds the private import of evo_agents.b._y by evo_agents/a.py" in problems


def test_a_function_moved_unchanged_is_not_an_addition_against_main():
    main = {"modules": {}, "functions": {"evo_agents/old.py::f": {"statements": 60}}, "private_imports": {}}
    moved = {"modules": {}, "functions": {"evo_agents/new.py::f": {"statements": 58}}, "private_imports": {}}
    assert structure.compare_baselines(moved, main) == []
    grown = {"modules": {}, "functions": {"evo_agents/new.py::f": {"statements": 61}}, "private_imports": {}}
    assert structure.compare_baselines(grown, main)
    kept_and_copied = {
        "modules": {},
        "functions": {"evo_agents/old.py::f": {"statements": 60}, "evo_agents/new.py::f": {"statements": 60}},
        "private_imports": {},
    }
    assert structure.compare_baselines(kept_and_copied, main)


def test_import_exceptions_may_shrink_but_not_grow_against_main():
    main = {"c": {"a.x -> b.y", "a.z -> b.y"}}
    assert structure.compare_contracts({"c": {"a.x -> b.y"}}, main) == []
    [problem] = structure.compare_contracts({"c": {"a.x -> b.y", "a.w -> b.y"}, "d": set()}, main)
    assert "adds the ignored import `a.w -> b.y` to the import-linter contract c" in problem
    assert "lint-imports" in problem and GUIDE in problem
    [new_contract] = structure.compare_contracts({"e": {"a.q -> b.q"}}, main)
    assert "contract e" in new_contract


def test_contract_exceptions_read_pyproject_by_contract_id():
    pyproject = """
[[tool.importlinter.contracts]]
id = "one"
name = "One"
type = "forbidden"
ignore_imports = ["a.b->c.d", "e.f  ->  g.**"]

[[tool.importlinter.contracts]]
name = "Two"
type = "acyclic_siblings"
"""
    assert structure.contract_exceptions(pyproject) == {"one": {"a.b -> c.d", "e.f -> g.**"}, "Two": set()}
    assert structure.contract_exceptions("[project]\nname = 'x'\n") is None


def test_tighten_lowers_and_drops_but_never_adds_or_raises():
    baseline = {
        "modules": {"evo_agents/a.py": 2000, "evo_agents/b.py": 1500, "evo_agents/gone.py": 1200},
        "functions": {
            "evo_agents/a.py::f": {"statements": 60, "complexity": 20},
            "evo_agents/a.py::fixed": {"statements": 70},
            "evo_agents/old.py::moved": {"statements": 55},
        },
        "private_imports": {"evo_agents/a.py": ["evo_agents.b._x", "evo_agents.b._x", "evo_agents.b._y"]},
    }
    measures = Measures(
        modules={"evo_agents/a.py": 1800, "evo_agents/b.py": 1600, "evo_agents/c.py": 1300},
        functions={
            "evo_agents/a.py::f": {"line": 1, "statements": 58},
            "evo_agents/a.py::new": {"line": 9, "statements": 80},
            "evo_agents/new.py::moved": {"line": 4, "statements": 54},
        },
        private_imports={"evo_agents/a.py": ["evo_agents.b._x", "evo_agents.b._z"]},
    )
    assert structure.tighten(baseline, measures) == {
        "modules": {"evo_agents/a.py": 1800, "evo_agents/b.py": 1500},
        "functions": {"evo_agents/a.py::f": {"statements": 58}, "evo_agents/new.py::moved": {"statements": 54}},
        "private_imports": {"evo_agents/a.py": ["evo_agents.b._x"]},
    }


def test_the_baseline_layout_round_trips():
    baseline = {
        "modules": {"evo_agents/a.py": 2000},
        "functions": {"evo_agents/a.py::A.f": {"statements": 60, "complexity": 20}},
        "private_imports": {"evo_agents/a.py": ["evo_agents.b._y", "evo_agents.b._x"]},
    }
    text = structure.dump_baseline(baseline)
    assert '    "evo_agents/a.py::A.f": {"complexity": 20, "statements": 60}' in text
    assert structure.load_baseline(text) == baseline | {
        "private_imports": {"evo_agents/a.py": ["evo_agents.b._x", "evo_agents.b._y"]}
    }
    empty = {"modules": {}, "functions": {}, "private_imports": {}}
    assert structure.load_baseline(structure.dump_baseline(empty)) == empty


# Measuring


def test_definitions_name_functions_by_class_and_enclosing_function():
    source = (
        "import functools\n"
        "def top():\n"
        "    def inner():\n"
        "        pass\n"
        "class A:\n"
        "    @functools.cache\n"
        "    def method(self):\n"
        "        pass\n"
        "    async def run(self):\n"
        "        pass\n"
        "if True:\n"
        "    def guarded():\n"
        "        pass\n"
    )
    assert structure.definitions(source) == {
        2: "top",
        3: "top.inner",
        7: "A.method",
        9: "A.run",
        12: "guarded",
    }


@pytest.mark.parametrize(
    ("path", "source", "sites"),
    [
        ("evo_agents/hub/a.py", "from evo_agents.hub.b import _x, y, __all__", ["evo_agents.hub.b._x"]),
        ("evo_agents/hub/a.py", "from .b import _x as x", ["evo_agents.hub.b._x"]),
        ("evo_agents/hub/a.py", "from ..kg import _y", ["evo_agents.kg._y"]),
        ("evo_agents/hub/__init__.py", "from .b import _x", ["evo_agents.hub.b._x"]),
        ("evo_agents/hub/a.py", "def f():\n    from evo_agents.kg.c import _z", ["evo_agents.kg.c._z"]),
        ("evo_agents/hub/a.py", "from evo_agents.hub.a import _self", []),
        ("evo_agents/hub/a.py", "from collections import _private", []),
        ("evo_agents/hub/a.py", "import evo_agents.hub.b as b\nb._x", []),
    ],
)
def test_private_imports_count_each_private_name_imported_from_another_module(path, source, sites):
    assert structure.private_imports_in(path, source) == sites


def test_function_metrics_read_ruff_whatever_a_noqa_says(tmp_path):
    package = tmp_path / "evo_agents"
    package.mkdir()
    branchy = ["class K:", "    def branchy(self, x):  # noqa: C901"]
    branchy += [f"        if x == {i}:\n            return {i}" for i in range(17)] + ["        return -1"]
    long = ["def long():  # noqa: PLR0915"] + [f"    y{i} = {i}" for i in range(52)] + ["    return 0"]
    (package / "m.py").write_text("\n".join(branchy + long) + "\n", encoding="utf-8")
    found = structure.function_metrics(tmp_path)
    assert {key: set(entry) for key, entry in found.items()} == {
        "evo_agents/m.py::K.branchy": {"line", "complexity"},
        "evo_agents/m.py::long": {"line", "statements"},
    }
    assert found["evo_agents/m.py::K.branchy"]["line"] == 2 and found["evo_agents/m.py::long"]["line"] == 38
    assert found["evo_agents/m.py::K.branchy"]["complexity"] > 15 and found["evo_agents/m.py::long"]["statements"] > 50


# The report


def test_the_report_puts_the_most_over_first_then_the_most_committed():
    measures = Measures(
        modules={"evo_agents/a.py": 1500, "evo_agents/b.py": 3000, "evo_agents/c.py": 1500, "evo_agents/d.py": 900},
        functions={
            "evo_agents/a.py::f": {"line": 3, "statements": 75},
            "evo_agents/b.py::g": {"line": 8, "complexity": 30},
        },
        private_imports={"evo_agents/a.py": ["evo_agents.b._x", "evo_agents.b._y"], "evo_agents/c.py": ["m._z"]},
    )
    rows = structure.report_rows(measures, Counter({"evo_agents/c.py": 5, "evo_agents/a.py": 1}))
    assert [r["path"] for r in rows["modules"]] == ["evo_agents/b.py", "evo_agents/c.py", "evo_agents/a.py"]
    assert [r["function"] for r in rows["functions"]] == ["g", "f"]
    assert rows["functions"][1] == {
        "function": "f",
        "path": "evo_agents/a.py",
        "line": 3,
        "over": 0.5,
        "statements": 75,
        "complexity": None,
        "commits": 1,
    }
    assert rows["private_imports"] == [{"module": "evo_agents.b", "imports": 2}, {"module": "m", "imports": 1}]
    text = structure.render_report(rows)
    assert "+200%" in text and "evo_agents/b.py:8 g" in text and GUIDE in text


def test_the_report_runs_from_the_command_line():
    done = subprocess.run(
        [structure.sys.executable, "-m", "tests.structure", "report", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    rows = json.loads(done.stdout)
    assert {"modules", "functions", "private_imports"} <= set(rows)
    assert all(r["lines"] > MODULE_LINES for r in rows["modules"])
