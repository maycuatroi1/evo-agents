"""The structure guard of evo_agents: size and complexity budgets that only ratchet down.

Three budgets, each with a frozen baseline in tests/structure_baseline.json for the code that already broke it when
the baseline was made (origin/main 9b1f399, 2026-10-10):

- a module of evo_agents has at most MODULE_LINES lines; a module in the baseline keeps at most its frozen count;
- a function has at most FUNCTION_STATEMENTS statements (ruff PLR0915) and a McCabe complexity of at most
  FUNCTION_COMPLEXITY (ruff C901); a function in the baseline keeps at most its frozen numbers;
- a module imports a private name of another module of evo_agents (``from x import _name``) only at the sites the
  baseline lists.

tests/test_structure.py fails when the code breaks a budget or a frozen number, when a baseline entry is looser than
the code (``tighten`` lowers it), and when the baseline, or the ignore_imports of an import-linter contract in
pyproject.toml, adds or raises an entry the main branch does not have. The comparison with main reads ``origin/main``,
or the ref EVO_STRUCTURE_BASE names (CI sets it, so a missing ref fails there instead of skipping).

    python -m tests.structure report [--json]   modules and functions over budget, by overage and recent commits
    python -m tests.structure tighten           lower the baseline to the code; never adds or raises an entry

docs/structure.md says how to split a module or a function along a seam. Needs ruff (the test extra) and git.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "evo_agents"
BASELINE = "tests/structure_baseline.json"
PYPROJECT = "pyproject.toml"
GUIDE = "docs/structure.md"
TIGHTEN = "python -m tests.structure tighten"
BASE_ENV = "EVO_STRUCTURE_BASE"
DEFAULT_BASE = "origin/main"
FETCH_MAIN = "git fetch origin +refs/heads/main:refs/remotes/origin/main"
RECENT = "30 days ago"

MODULE_LINES = 1000
FUNCTION_STATEMENTS = 50
FUNCTION_COMPLEXITY = 15
METRICS = {"statements": FUNCTION_STATEMENTS, "complexity": FUNCTION_COMPLEXITY}
RULES = {"PLR0915": "statements", "C901": "complexity"}  # the ruff rule that measures each metric
RULE_OF = {metric: rule for rule, metric in RULES.items()}
PHRASES = {"statements": "{} statements", "complexity": "a complexity of {}"}
REPORTED = re.compile(r"\((\d+) > \d+\)")  # "Too many statements (63 > 50)", "`f` is too complex (17 > 15)"

SPLIT_MODULE = (
    "move a group of functions that belong together (a seam) into a module of their own, and keep the names other "
    f"modules import (see {GUIDE}, section Splitting a module)"
)
SPLIT_FUNCTION = (
    "extract each stage into a helper named for what it does, or turn a long if/elif chain into a table "
    f"(see {GUIDE}, section Splitting a function)"
)
PUBLIC_NAME = (
    "give the name a public spelling in its module (drop the underscore and update its callers), or move it into a "
    f"module both import (see {GUIDE}, section Private names)"
)
SHRINK_ONLY = f"A baseline only shrinks: bring the code within the budget instead (see {GUIDE})."


def _where(key: str, line: int | None = None) -> str:
    path, qualname = key.split("::", 1)
    return f"{path}:{line} {qualname}" if line else f"{path} {qualname}"


def _qualname(key: str) -> str:
    return key.split("::", 1)[1]


# Measuring


@dataclass
class Measures:
    modules: dict[str, int]  # every module of the package: its lines
    functions: dict[str, dict[str, int]]  # every function over a budget, "path::qualname": line and metrics over
    private_imports: dict[str, list[str]]  # importing module: "module._name" for each site, repeats kept


def package_files(root: Path = ROOT) -> list[Path]:
    return sorted(p for p in (root / PACKAGE).rglob("*.py") if "__pycache__" not in p.parts)


def definitions(source: str) -> dict[int, str]:
    """The qualified name of each function in ``source`` by the line of its ``def``: ``f``, ``Class.method``,
    ``outer.inner``."""
    found: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    found[child.lineno] = name
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(ast.parse(source), "")
    return found


def ruff_findings(root: Path = ROOT) -> list[dict]:
    """PLR0915 and C901 over the package with this file's budgets, whatever pyproject.toml or a ``# noqa`` says."""
    command = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--isolated",
        "--ignore-noqa",
        "--exit-zero",
        "--no-cache",
        "--output-format",
        "json",
        "--select",
        ",".join(RULES),
        "--config",
        f"lint.mccabe.max-complexity={FUNCTION_COMPLEXITY}",
        "--config",
        f"lint.pylint.max-statements={FUNCTION_STATEMENTS}",
        PACKAGE,
    ]
    done = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"ruff exited {done.returncode}: {done.stderr.strip()}")
    return json.loads(done.stdout or "[]")


def function_metrics(root: Path = ROOT) -> dict[str, dict[str, int]]:
    """Every function of the package over a budget, keyed ``path::qualname``: the line of its ``def`` and each metric
    over its budget."""
    names: dict[str, dict[int, str]] = {}
    found: dict[str, dict[str, int]] = {}
    for finding in ruff_findings(root):
        path = Path(finding["filename"]).resolve().relative_to(root.resolve()).as_posix()
        if path not in names:
            names[path] = definitions((root / path).read_text(encoding="utf-8"))
        line = finding["location"]["row"]
        qualname = names[path].get(line)
        reported = REPORTED.search(finding["message"])
        if qualname is None or reported is None:
            raise RuntimeError(f"{path}:{line}: cannot read ruff's {finding['code']}: {finding['message']!r}")
        entry = found.setdefault(f"{path}::{qualname}", {"line": line})
        metric = RULES[finding["code"]]
        entry[metric] = max(entry.get(metric, 0), int(reported.group(1)))
    return found


def module_name(path: str) -> str:
    parts = path.removesuffix(".py").split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def imported_module(module: str, is_package: bool, node: ast.ImportFrom) -> str:
    """The absolute name of the module a ``from ... import`` reads, relative imports resolved."""
    if not node.level:
        return node.module or ""
    base = module.split(".") if is_package else module.split(".")[:-1]
    base = base[: len(base) - node.level + 1]
    return ".".join([*base, node.module] if node.module else base)


def private_imports_in(path: str, source: str) -> list[str]:
    """``module._name`` for each private name the module at ``path`` imports from another module of the package."""
    module = module_name(path)
    is_package = path.endswith("/__init__.py")
    sites = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom):
            continue
        target = imported_module(module, is_package, node)
        if target == module or not (target == PACKAGE or target.startswith(PACKAGE + ".")):
            continue
        sites += [f"{target}.{a.name}" for a in node.names if a.name.startswith("_") and not a.name.startswith("__")]
    return sites


def measure(root: Path = ROOT) -> Measures:
    modules: dict[str, int] = {}
    private: dict[str, list[str]] = {}
    for file in package_files(root):
        path = file.relative_to(root).as_posix()
        source = file.read_text(encoding="utf-8")
        modules[path] = len(source.splitlines())
        sites = private_imports_in(path, source)
        if sites:
            private[path] = sorted(sites)
    return Measures(modules, function_metrics(root), private)


# The baseline


def load_baseline(text: str) -> dict:
    data = json.loads(text)
    return {
        "modules": dict(data.get("modules", {})),
        "functions": {key: dict(value) for key, value in data.get("functions", {}).items()},
        "private_imports": {key: list(value) for key, value in data.get("private_imports", {}).items()},
    }


ABOUT = (
    f"Frozen exceptions to the budgets of tests/structure.py ({MODULE_LINES} lines a module; {FUNCTION_STATEMENTS} "
    f"statements and a complexity of {FUNCTION_COMPLEXITY} a function; no private name imported across modules). "
    f"Entries only shrink: `{TIGHTEN}` lowers them; see {GUIDE}."
)


def dump_baseline(baseline: dict) -> str:
    """The baseline as JSON with one entry a line (one site a line for private imports), so that two changes to
    different entries merge cleanly."""

    def section(name: str, items: list[tuple[str, str]]) -> str:
        if not items:
            return f'  "{name}": {{}}'
        return f'  "{name}": {{\n' + ",\n".join(f"    {json.dumps(key)}: {value}" for key, value in items) + "\n  }"

    def sites(values: list[str]) -> str:
        return "[\n" + ",\n".join(f"      {json.dumps(v)}" for v in sorted(values)) + "\n    ]"

    sections = [
        f'  "about": {json.dumps(ABOUT)}',
        section("modules", [(k, json.dumps(v)) for k, v in sorted(baseline["modules"].items())]),
        section(
            "functions", [(k, json.dumps(dict(sorted(v.items())))) for k, v in sorted(baseline["functions"].items())]
        ),
        section("private_imports", [(k, sites(v)) for k, v in sorted(baseline["private_imports"].items())]),
    ]
    return "{\n" + ",\n".join(sections) + "\n}\n"


def snapshot(measures: Measures) -> dict:
    """A baseline that freezes every module and function over budget, and every private import, as they are."""
    return {
        "modules": {path: n for path, n in measures.modules.items() if n > MODULE_LINES},
        "functions": {
            key: {m: v for m, v in entry.items() if m in METRICS} for key, entry in measures.functions.items()
        },
        "private_imports": {path: list(sites) for path, sites in measures.private_imports.items()},
    }


def _moved(key: str, limits: dict[str, int], candidates: dict[str, dict[str, int]]) -> str | None:
    """The one key of ``candidates`` with the qualname of ``key`` and no metric over ``limits``: the same function,
    moved to another module unchanged."""
    same = [
        other
        for other, entry in candidates.items()
        if other != key
        and _qualname(other) == _qualname(key)
        and all(m in limits and v <= limits[m] for m, v in entry.items() if m in METRICS)
    ]
    return same[0] if len(same) == 1 else None


def tighten(baseline: dict, measures: Measures) -> dict:
    """``baseline`` lowered to the code: numbers come down to what the code measures, entries the code no longer
    needs go, a function moved unchanged keeps its entry under its new key. Never adds or raises anything."""
    modules = {
        path: min(limit, measures.modules[path])
        for path, limit in baseline["modules"].items()
        if measures.modules.get(path, 0) > MODULE_LINES
    }
    unlisted = {key: entry for key, entry in measures.functions.items() if key not in baseline["functions"]}
    functions: dict[str, dict[str, int]] = {}
    for key, limits in baseline["functions"].items():
        current = key
        if key not in measures.functions:
            current = _moved(key, limits, unlisted)
            if current is None:
                continue
            del unlisted[current]
        entry = measures.functions[current]
        kept = {m: min(limit, entry[m]) for m, limit in limits.items() if m in entry}
        if kept:
            functions[current] = kept
    private = {}
    for path, sites in baseline["private_imports"].items():
        kept = sorted((Counter(sites) & Counter(measures.private_imports.get(path, []))).elements())
        if kept:
            private[path] = kept
    return {"modules": modules, "functions": functions, "private_imports": private}


# Checks: each returns the problems it finds, one message each, saying what is over, by how much, and how to fix it


def check_modules(lines: dict[str, int], frozen: dict[str, int]) -> list[str]:
    problems = []
    for path, count in sorted(lines.items()):
        limit = frozen.get(path)
        if limit is None:
            if count > MODULE_LINES:
                problems.append(
                    f"{path} has {count} lines, {count - MODULE_LINES} over the budget of {MODULE_LINES} a module.\n"
                    f"  Split it: {SPLIT_MODULE}."
                )
        elif count > limit:
            problems.append(
                f"{path} has {count} lines, {count - limit} over its frozen baseline of {limit} in {BASELINE} "
                f"(the budget is {MODULE_LINES}).\n  A module over the budget only shrinks: {SPLIT_MODULE}."
            )
        elif count <= MODULE_LINES:
            problems.append(
                f"{path} has {count} lines, within the budget of {MODULE_LINES}, but {BASELINE} still freezes it at "
                f"{limit}.\n  Drop its entry, so it stays within the budget: run `{TIGHTEN}`."
            )
        elif count < limit:
            problems.append(
                f"{path} has {count} lines, {limit - count} under its frozen baseline of {limit} in {BASELINE}.\n"
                f"  Lower the baseline to {count}, so the module cannot grow back: run `{TIGHTEN}`."
            )
    for path in sorted(set(frozen) - set(lines)):
        problems.append(
            f"{path} is frozen in {BASELINE} but no longer exists.\n  Drop its entry: run `{TIGHTEN}`. A module "
            f"that moved is a new module and fits the budget of {MODULE_LINES} lines."
        )
    return problems


def check_functions(found: dict[str, dict[str, int]], frozen: dict[str, dict[str, int]]) -> list[str]:
    problems = []
    stale = {key: limits for key, limits in frozen.items() if key not in found}
    for key, entry in sorted(found.items()):
        limits = frozen.get(key, {})
        where = _where(key, entry["line"])
        for metric, budget in METRICS.items():
            value, limit = entry.get(metric), limits.get(metric)
            has = PHRASES[metric].format(value)
            if value is None:
                if limit is not None:
                    problems.append(
                        f"{where} is within the budget of {budget} {metric} now, but {BASELINE} still freezes its "
                        f"{metric} at {limit}.\n  Drop that number, so it stays within the budget: run `{TIGHTEN}`."
                    )
            elif limit is None:
                moved = next((old for old in stale if _qualname(old) == _qualname(key)), None)
                hint = (
                    f" If you moved it unchanged from {moved.split('::')[0]}, `{TIGHTEN}` moves its entry."
                    if moved
                    else ""
                )
                problems.append(
                    f"{where} has {has}, {value - budget} over the budget of {budget} (ruff {RULE_OF[metric]}).\n"
                    f"  Split it: {SPLIT_FUNCTION}.{hint}"
                )
            elif value > limit:
                problems.append(
                    f"{where} has {has}, {value - limit} over its frozen baseline of {limit} in {BASELINE} (the "
                    f"budget is {budget}, ruff {RULE_OF[metric]}).\n  A function over the budget only shrinks: "
                    f"{SPLIT_FUNCTION}."
                )
            elif value < limit:
                problems.append(
                    f"{where} has {has}, under its frozen baseline of {limit} in {BASELINE}.\n  Lower the baseline "
                    f"to {value}, so the function cannot grow back: run `{TIGHTEN}`."
                )
    for key in sorted(stale):
        problems.append(
            f"{_where(key)} is frozen in {BASELINE} but is no longer over a budget, or no longer exists.\n"
            f"  Drop its entry: run `{TIGHTEN}`."
        )
    return problems


def check_private_imports(found: dict[str, list[str]], frozen: dict[str, list[str]]) -> list[str]:
    total_found = sum(len(sites) for sites in found.values())
    total_frozen = sum(len(sites) for sites in frozen.values())
    problems = []
    for path in sorted(set(found) | set(frozen)):
        now, then = Counter(found.get(path, [])), Counter(frozen.get(path, []))
        for site in sorted(now - then):
            module, name = site.rsplit(".", 1)
            problems.append(
                f"{path} imports {name} from {module}, a private name of another module ({total_found} such "
                f"imports, {total_frozen} in {BASELINE}).\n  Do not add one: {PUBLIC_NAME}."
            )
        for site in sorted(then - now):
            problems.append(
                f"{path} no longer imports {site}, but {BASELINE} still lists it.\n  Drop it, so the count stays "
                f"down: run `{TIGHTEN}`."
            )
    return problems


def compare_baselines(branch: dict, main: dict) -> list[str]:
    """What ``branch``, a baseline, adds to or raises over ``main``'s. A function main lists under another module,
    moved with no number raised, is not an addition."""
    problems = []
    for path, limit in sorted(branch["modules"].items()):
        before = main["modules"].get(path)
        if before is None:
            problems.append(f"{BASELINE} adds {path} ({limit} lines), which main does not list.\n  {SHRINK_ONLY}")
        elif limit > before:
            problems.append(f"{BASELINE} raises {path} from {before} lines on main to {limit}.\n  {SHRINK_ONLY}")
    gone = {key: limits for key, limits in main["functions"].items() if key not in branch["functions"]}
    for key, limits in sorted(branch["functions"].items()):
        before = main["functions"].get(key)
        if before is None:
            moved = next((old for old, was in gone.items() if _moved(old, was, {key: limits})), None)
            if moved:
                del gone[moved]
                continue
            numbers = ", ".join(PHRASES[m].format(v) for m, v in sorted(limits.items()))
            problems.append(f"{BASELINE} adds {_where(key)} ({numbers}), which main does not list.\n  {SHRINK_ONLY}")
            continue
        for metric, limit in sorted(limits.items()):
            if metric not in before:
                problems.append(
                    f"{BASELINE} adds {PHRASES[metric].format(limit)} to {_where(key)}, which main does not freeze."
                    f"\n  {SHRINK_ONLY}"
                )
            elif limit > before[metric]:
                problems.append(
                    f"{BASELINE} raises the {metric} of {_where(key)} from {before[metric]} on main to {limit}.\n"
                    f"  {SHRINK_ONLY}"
                )
    for path, sites in sorted(branch["private_imports"].items()):
        for site in sorted(Counter(sites) - Counter(main["private_imports"].get(path, []))):
            problems.append(
                f"{BASELINE} adds the private import of {site} by {path}, which main does not list.\n"
                f"  Do not add one: {PUBLIC_NAME}."
            )
    return problems


def _import_expression(text: str) -> str:
    return " -> ".join(part.strip() for part in text.split("->"))


def contract_exceptions(pyproject: str) -> dict[str, set[str]] | None:
    """The ignore_imports of each import-linter contract in ``pyproject``, by contract id; None without contracts."""
    contracts = tomllib.loads(pyproject).get("tool", {}).get("importlinter", {}).get("contracts")
    if contracts is None:
        return None
    return {
        contract.get("id") or contract["name"]: {_import_expression(e) for e in contract.get("ignore_imports", [])}
        for contract in contracts
    }


def compare_contracts(branch: dict[str, set[str]], main: dict[str, set[str]]) -> list[str]:
    problems = []
    for contract, entries in sorted(branch.items()):
        for entry in sorted(entries - main.get(contract, set())):
            problems.append(
                f"{PYPROJECT} adds the ignored import `{entry}` to the import-linter contract {contract}, which main "
                f"does not list.\n  The exceptions only shrink: change the import as the contract's guidance says "
                f"(`lint-imports` prints it; see {GUIDE}, section Package boundaries)."
            )
    return problems


def problems_text(title: str, problems: list[str]) -> str:
    return f"{title} ({len(problems)}):\n" + "\n".join(f"- {problem}" for problem in problems)


# git


def git(*args: str, root: Path = ROOT) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None


def base_ref() -> tuple[str, bool]:
    """The ref the baselines are compared with, and whether EVO_STRUCTURE_BASE named it."""
    named = os.environ.get(BASE_ENV, "").strip()
    return (named or DEFAULT_BASE), bool(named)


def ref_exists(ref: str, root: Path = ROOT) -> bool:
    done = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", root=root)
    return done is not None and done.returncode == 0


def file_at(ref: str, path: str, root: Path = ROOT) -> str | None:
    done = git("show", f"{ref}:{path}", root=root)
    return done.stdout if done is not None and done.returncode == 0 else None


def recent_commits(since: str = RECENT, root: Path = ROOT) -> Counter:
    """How many commits since ``since`` touched each file of the package."""
    done = git("log", f"--since={since}", "--format=", "--name-only", "--", PACKAGE, root=root)
    if done is None or done.returncode != 0:
        return Counter()
    return Counter(line.strip() for line in done.stdout.splitlines() if line.strip())


# The report


def report_rows(measures: Measures, commits: Counter) -> dict[str, list[dict]]:
    """Modules and functions over budget, most over first (by the ratio to the budget), then most commits."""
    modules = [
        {"path": path, "lines": n, "over": round(n / MODULE_LINES - 1, 3), "commits": commits[path]}
        for path, n in measures.modules.items()
        if n > MODULE_LINES
    ]
    functions = []
    for key, entry in measures.functions.items():
        path = key.split("::", 1)[0]
        over = max(entry[m] / budget - 1 for m, budget in METRICS.items() if m in entry)
        row = {"function": _qualname(key), "path": path, "line": entry["line"], "over": round(over, 3)}
        row |= {m: entry.get(m) for m in METRICS} | {"commits": commits[path]}
        functions.append(row)
    modules.sort(key=lambda r: (-r["over"], -r["commits"], r["path"]))
    functions.sort(key=lambda r: (-r["over"], -r["commits"], r["path"], r["line"]))
    by_module = Counter(site.rsplit(".", 1)[0] for sites in measures.private_imports.values() for site in sites)
    private = [{"module": module, "imports": n} for module, n in sorted(by_module.items(), key=lambda i: (-i[1], i[0]))]
    return {"modules": modules, "functions": functions, "private_imports": private}


def render_report(rows: dict[str, list[dict]], since: str = RECENT) -> str:
    order = f"most over first, then most commits since {since}"
    out = [
        f"Modules over {MODULE_LINES} lines ({len(rows['modules'])}), {order}:",
        f"  {'over':>6}  {'lines':>5}  {'commits':>7}  module",
    ]
    out += [f"  {r['over']:>+6.0%}  {r['lines']:>5}  {r['commits']:>7}  {r['path']}" for r in rows["modules"]]
    out += [
        "",
        f"Functions over {FUNCTION_STATEMENTS} statements or a complexity of {FUNCTION_COMPLEXITY} "
        f"({len(rows['functions'])}), {order}:",
        f"  {'over':>6}  {'statements':>10}  {'complexity':>10}  {'commits':>7}  function",
    ]
    for r in rows["functions"]:
        statements = r["statements"] or "-"
        complexity = r["complexity"] or "-"
        out.append(
            f"  {r['over']:>+6.0%}  {statements:>10}  {complexity:>10}  {r['commits']:>7}  "
            f"{r['path']}:{r['line']} {r['function']}"
        )
    total = sum(r["imports"] for r in rows["private_imports"])
    out += ["", f"Private names imported from another module ({total}), by the module that defines them:"]
    out += [f"  {r['imports']:>6}  {r['module']}" for r in rows["private_imports"]]
    out += ["", f"How to split a module or a function along a seam: {GUIDE}."]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.structure", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="modules and functions over budget, by overage and recent commits")
    report.add_argument("--json", action="store_true", help="print the rows as JSON")
    report.add_argument("--since", default=RECENT, help=f"count commits since this date (default: {RECENT!r})")
    sub.add_parser("tighten", help=f"lower {BASELINE} to the code; never adds or raises an entry")
    args = parser.parse_args(argv)

    measures = measure()
    if args.command == "report":
        rows = report_rows(measures, recent_commits(args.since))
        print(json.dumps(rows, indent=2) if args.json else render_report(rows, args.since))
        return 0
    path = ROOT / BASELINE
    before = load_baseline(path.read_text(encoding="utf-8"))
    after = tighten(before, measures)
    path.write_text(dump_baseline(after), encoding="utf-8")
    sizes = [
        ("modules", len(before["modules"]), len(after["modules"])),
        ("functions", len(before["functions"]), len(after["functions"])),
        ("private imports", *(sum(map(len, b["private_imports"].values())) for b in (before, after))),
    ]
    print(f"{BASELINE}: " + ", ".join(f"{name} {a} -> {b}" for name, a, b in sizes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
