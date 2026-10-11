"""Inheritance across files: ``inherits`` from a class to its bases, calls through this, self and super to
the nearest class up the chain that defines the method, and ``overrides`` from a method to the one it
overrides. One fixture per language the connectors mark as code; a language the coverage table marks
``no`` gets no such edge out of its files, and its row says why."""

from dataclasses import dataclass, field

import pytest

from evo_agents.kg.build import build_project
from evo_agents.kg.connectors._files import LANGUAGE_BY_SUFFIX
from evo_agents.kg.extract import graphify as gfy
from evo_agents.kg.extract.languages import CROSS_FILE_CALLS, INHERITANCE, OVERRIDES
from evo_agents.kg.project import load_project_at
from evo_agents.kg.schema import HUB_RELATIONS
from evo_agents.kg.serve import Session
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, git, write


@dataclass
class Case:
    """Edges as ("<path>::<qualname>", "<path>::<qualname>"): ``inherits`` from a class to its base,
    ``calls`` through this, self or super, ``overrides``; ``absent`` names (rel, src, dst) that must not be."""

    files: dict[str, str]
    inherits: list[tuple[str, str]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    overrides: list[tuple[str, str]] = field(default_factory=list)
    absent: list[tuple[str, str, str]] = field(default_factory=list)


CASES = {
    "python": Case(
        {
            "pykin/__init__.py": "",
            "pykin/a.py": (
                "class Shape:\n    def run(self):\n        raise NotImplementedError\n\n\n"
                "class Base(Shape):\n    def run(self):\n        return 1\n\n"
                "    def helper(self):\n        return 2\n\n\n"
                "class Unrelated:\n    pass\n"
            ),
            "pykin/b.py": (
                "from pykin.a import Base\n\n\n"
                "class Child(Base):\n    def run(self):\n        return super().run() + self.helper()\n\n"
                "    def go(self):\n        return self.run()\n\n\n"
                "class Grand(Child):\n    def deep(self):\n"
                "        return super().helper() + super(Grand, self).run()\n\n\n"
                "class Plain(Base):\n    def go(self):\n        return self.run()\n\n    @classmethod\n"
                "    def make(cls):\n        return cls.helper()\n\n\n"
                "class Quiet(Base):\n    def run(self):\n        return 0\n\n\n"
                "class Stray(Unrelated):\n    pass\n"
            ),
        },
        inherits=[
            ("pykin/b.py::Child", "pykin/a.py::Base"),
            ("pykin/b.py::Grand", "pykin/b.py::Child"),
            ("pykin/b.py::Plain", "pykin/a.py::Base"),
            ("pykin/b.py::Quiet", "pykin/a.py::Base"),
            ("pykin/a.py::Base", "pykin/a.py::Shape"),
        ],
        calls=[
            ("pykin/b.py::Child.run", "pykin/a.py::Base.run"),
            ("pykin/b.py::Child.run", "pykin/a.py::Base.helper"),
            ("pykin/b.py::Child.go", "pykin/b.py::Child.run"),
            ("pykin/b.py::Grand.deep", "pykin/a.py::Base.helper"),  # Child has none: the nearest up the chain
            ("pykin/b.py::Grand.deep", "pykin/b.py::Child.run"),
            ("pykin/b.py::Plain.go", "pykin/a.py::Base.run"),
            ("pykin/b.py::Plain.make", "pykin/a.py::Base.helper"),
        ],
        overrides=[
            ("pykin/b.py::Child.run", "pykin/a.py::Base.run"),
            ("pykin/b.py::Quiet.run", "pykin/a.py::Base.run"),
            ("pykin/a.py::Base.run", "pykin/a.py::Shape.run"),
        ],
        absent=[
            ("inherits", "pykin/b.py::Stray", "pykin/a.py::Unrelated"),  # b.py does not import Unrelated
            ("calls", "pykin/b.py::Child.run", "pykin/b.py::Child.run"),
            ("calls", "pykin/b.py::Child.run", "pykin/a.py::Shape.run"),
        ],
    ),
    "typescript": Case(
        {
            "tskin/a.ts": (
                "export interface Shape {\n  run(): number;\n}\n\n"
                "export class Base implements Shape {\n  run(): number {\n    return 1;\n  }\n"
                "  helper(): number {\n    return 2;\n  }\n}\n\nexport class Unrelated {}\n"
            ),
            "tskin/b.ts": (
                'import { Base, Shape } from "./a";\n\n'
                "@Component({ selector: 'kin-child' })\n"
                "export class Child extends Base implements Shape {\n  run(): number {\n"
                "    return super.run() + this.helper();\n  }\n  go(): number {\n    return this.run();\n  }\n}\n\n"
                "export class Plain extends Base {\n  go(): number {\n    return this.run();\n  }\n}\n\n"
                "export class Quiet extends Base {\n  run(): number {\n    return 0;\n  }\n}\n\n"
                "export class Stray extends Unrelated {}\n"
            ),
        },
        inherits=[
            ("tskin/b.ts::Child", "tskin/a.ts::Base"),
            ("tskin/b.ts::Child", "tskin/a.ts::Shape"),
            ("tskin/b.ts::Plain", "tskin/a.ts::Base"),
            ("tskin/b.ts::Quiet", "tskin/a.ts::Base"),
            ("tskin/a.ts::Base", "tskin/a.ts::Shape"),
        ],
        calls=[
            ("tskin/b.ts::Child.run", "tskin/a.ts::Base.run"),
            ("tskin/b.ts::Child.run", "tskin/a.ts::Base.helper"),
            ("tskin/b.ts::Plain.go", "tskin/a.ts::Base.run"),
        ],
        overrides=[
            ("tskin/b.ts::Child.run", "tskin/a.ts::Base.run"),
            ("tskin/b.ts::Child.run", "tskin/a.ts::Shape.run"),
            ("tskin/b.ts::Quiet.run", "tskin/a.ts::Base.run"),
            ("tskin/a.ts::Base.run", "tskin/a.ts::Shape.run"),
        ],
        absent=[
            ("inherits", "tskin/b.ts::Stray", "tskin/a.ts::Unrelated"),
            ("calls", "tskin/b.ts::Child.run", "tskin/b.ts::Child.run"),
        ],
    ),
    "javascript": Case(
        {
            "jskin/a.js": (
                "class Base {\n  run() {\n    return 1;\n  }\n  helper() {\n    return 2;\n  }\n}\n"
                "class Unrelated {}\nmodule.exports = { Base, Unrelated };\n"
            ),
            "jskin/b.js": (
                'const { Base } = require("./a");\n\n'
                "class Child extends Base {\n  run() {\n    return super.run() + this.helper();\n  }\n}\n\n"
                "class Stray extends Unrelated {}\nmodule.exports = { Child, Stray };\n"
            ),
        },
        inherits=[("jskin/b.js::Child", "jskin/a.js::Base")],
        calls=[("jskin/b.js::Child.run", "jskin/a.js::Base.run"), ("jskin/b.js::Child.run", "jskin/a.js::Base.helper")],
        overrides=[("jskin/b.js::Child.run", "jskin/a.js::Base.run")],
        absent=[("inherits", "jskin/b.js::Stray", "jskin/a.js::Unrelated")],
    ),
    "vue": Case(
        {
            "vuekin/base.ts": (
                "export class Base {\n  run(): number {\n    return 1;\n  }\n"
                "  helper(): number {\n    return 2;\n  }\n}\n"
            ),
            "vuekin/Child.vue": (
                '<template><div /></template>\n<script lang="ts">\nimport { Base } from "./base";\n'
                "export default class Child extends Base {\n  run(): number {\n"
                "    return super.run() + this.helper();\n  }\n}\n</script>\n"
            ),
        },
        inherits=[("vuekin/Child.vue::Child", "vuekin/base.ts::Base")],
        calls=[
            ("vuekin/Child.vue::Child.run", "vuekin/base.ts::Base.run"),
            ("vuekin/Child.vue::Child.run", "vuekin/base.ts::Base.helper"),
        ],
        overrides=[("vuekin/Child.vue::Child.run", "vuekin/base.ts::Base.run")],
    ),
    "go": Case(
        {
            "gokin/a/a.go": "package a\n\ntype Base struct{}\n\nfunc (b *Base) Run() int { return 1 }\n",
            "gokin/b/b.go": 'package b\n\nimport "example.com/kin/gokin/a"\n\ntype Child struct {\n\ta.Base\n}\n\n'
            "func (c *Child) Run() int { return c.Base.Run() }\n",
        },
        absent=[
            ("inherits", "gokin/b/b.go::Child", "gokin/a/a.go::Base"),
            ("overrides", "gokin/b/b.go::Child.Run", "gokin/a/a.go::Base.Run"),
        ],
    ),
    "java": Case(
        {
            "javakin/com/kin/a/Shape.java": "package com.kin.a;\n\npublic interface Shape {\n    int run();\n}\n",
            "javakin/com/kin/a/Base.java": "package com.kin.a;\n\npublic class Base implements Shape {\n"
            "    public int run() {\n        return 1;\n    }\n\n"
            "    public int helper() {\n        return 2;\n    }\n\n"
            "    public int other() {\n        return 3;\n    }\n}\n",
            "javakin/com/kin/a/Unrelated.java": "package com.kin.a;\n\npublic class Unrelated {\n}\n",
            "javakin/com/kin/b/Child.java": "package com.kin.b;\n\nimport com.kin.a.Base;\nimport com.kin.a.Shape;\n\n"
            "public class Child extends Base implements Shape {\n    public int run() {\n"
            "        return super.run() + this.helper() + other();\n    }\n}\n\n"
            "class Quiet extends Base {\n    public int run() {\n        return 0;\n    }\n}\n\n"
            "class Stray extends Unrelated {\n}\n",
        },
        inherits=[
            ("javakin/com/kin/b/Child.java::Child", "javakin/com/kin/a/Base.java::Base"),
            ("javakin/com/kin/b/Child.java::Child", "javakin/com/kin/a/Shape.java::Shape"),
            ("javakin/com/kin/b/Child.java::Quiet", "javakin/com/kin/a/Base.java::Base"),
            ("javakin/com/kin/a/Base.java::Base", "javakin/com/kin/a/Shape.java::Shape"),  # same package
        ],
        calls=[
            ("javakin/com/kin/b/Child.java::Child.run", "javakin/com/kin/a/Base.java::Base.run"),
            ("javakin/com/kin/b/Child.java::Child.run", "javakin/com/kin/a/Base.java::Base.helper"),
            ("javakin/com/kin/b/Child.java::Child.run", "javakin/com/kin/a/Base.java::Base.other"),
        ],
        overrides=[
            ("javakin/com/kin/b/Child.java::Child.run", "javakin/com/kin/a/Base.java::Base.run"),
            ("javakin/com/kin/b/Child.java::Child.run", "javakin/com/kin/a/Shape.java::Shape.run"),
            ("javakin/com/kin/b/Child.java::Quiet.run", "javakin/com/kin/a/Base.java::Base.run"),
            ("javakin/com/kin/a/Base.java::Base.run", "javakin/com/kin/a/Shape.java::Shape.run"),
        ],
        absent=[("inherits", "javakin/com/kin/b/Child.java::Stray", "javakin/com/kin/a/Unrelated.java::Unrelated")],
    ),
    "kotlin": Case(
        {
            "ktkin/A.kt": "package com.kin.ka\n\ninterface Shape {\n    fun run(): Int\n}\n\n"
            "open class Base : Shape {\n    override fun run(): Int {\n        return 1\n    }\n\n"
            "    open fun helper(): Int {\n        return 2\n    }\n}\n\n"
            "object Util {\n    fun twice(): Int {\n        return 2\n    }\n}\n",
            "ktkin/B.kt": "package com.kin.kb\n\nimport com.kin.ka.Base\nimport com.kin.ka.Util\n\n"
            "class Child : Base() {\n    override fun run(): Int {\n        return super.run() + this.helper()\n"
            "    }\n\n    fun go(): Int {\n        return Util.twice()\n    }\n}\n",
        },
        inherits=[("ktkin/B.kt::Child", "ktkin/A.kt::Base"), ("ktkin/A.kt::Base", "ktkin/A.kt::Shape")],
        calls=[
            ("ktkin/B.kt::Child.run", "ktkin/A.kt::Base.run"),
            ("ktkin/B.kt::Child.run", "ktkin/A.kt::Base.helper"),
            ("ktkin/B.kt::Child.go", "ktkin/A.kt::Util.twice"),  # an object's method, in a method: not this
        ],
        overrides=[
            ("ktkin/B.kt::Child.run", "ktkin/A.kt::Base.run"),
            ("ktkin/A.kt::Base.run", "ktkin/A.kt::Shape.run"),
        ],
    ),
    "rust": Case(
        {
            "rskin/src/a.rs": "pub trait Shape {\n    fn area(&self) -> i32;\n\n"
            "    fn describe(&self) -> i32 {\n        1\n    }\n}\n",
            "rskin/src/b.rs": "use crate::a::Shape;\n\npub struct Square {}\n\nimpl Shape for Square {\n"
            "    fn area(&self) -> i32 {\n        self.describe()\n    }\n}\n",
        },
        inherits=[("rskin/src/b.rs::Square", "rskin/src/a.rs::Shape")],
        calls=[("rskin/src/b.rs::Square.area", "rskin/src/a.rs::Shape.describe")],
        overrides=[("rskin/src/b.rs::Square.area", "rskin/src/a.rs::Shape.area")],
    ),
    "ruby": Case(
        {
            "rbkin/a.rb": "module Greet\n  def hello\n    1\n  end\nend\n\nclass Base\n  def run\n    1\n  end\n\n"
            "  def helper\n    2\n  end\nend\n",
            "rbkin/b.rb": 'require_relative "a"\n\nclass Child < Base\n  include Greet\n\n  def run\n'
            "    super + self.helper + self.hello\n  end\nend\n",
        },
        inherits=[("rbkin/b.rb::Child", "rbkin/a.rb::Base"), ("rbkin/b.rb::Child", "rbkin/a.rb::Greet")],
        calls=[
            ("rbkin/b.rb::Child.run", "rbkin/a.rb::Base.run"),
            ("rbkin/b.rb::Child.run", "rbkin/a.rb::Base.helper"),
            ("rbkin/b.rb::Child.run", "rbkin/a.rb::Greet.hello"),
        ],
        overrides=[("rbkin/b.rb::Child.run", "rbkin/a.rb::Base.run")],
    ),
    "php": Case(
        {
            "phpkin/a.php": "<?php\nnamespace Kin\\A;\n\ninterface Shape {\n    public function run();\n}\n\n"
            "class Base implements Shape {\n    public function run() {\n        return 1;\n    }\n\n"
            "    public function helper() {\n        return 2;\n    }\n\n"
            "    public static function make() {\n        return 3;\n    }\n}\n\n"
            "trait Loud {\n    public function shout() {\n        return 4;\n    }\n}\n",
            "phpkin/b.php": "<?php\nnamespace Kin\\B;\n\nuse Kin\\A\\Base;\nuse Kin\\A\\Loud;\n\n"
            "class Child extends Base {\n"
            "    use Loud;\n\n    public function run() {\n"
            "        return parent::run() + $this->helper() + self::make() + $this->shout();\n    }\n}\n",
        },
        inherits=[
            ("phpkin/b.php::Child", "phpkin/a.php::Base"),
            ("phpkin/b.php::Child", "phpkin/a.php::Loud"),
            ("phpkin/a.php::Base", "phpkin/a.php::Shape"),
        ],
        calls=[
            ("phpkin/b.php::Child.run", "phpkin/a.php::Base.run"),
            ("phpkin/b.php::Child.run", "phpkin/a.php::Base.helper"),
            ("phpkin/b.php::Child.run", "phpkin/a.php::Base.make"),
            ("phpkin/b.php::Child.run", "phpkin/a.php::Loud.shout"),
        ],
        overrides=[
            ("phpkin/b.php::Child.run", "phpkin/a.php::Base.run"),
            ("phpkin/a.php::Base.run", "phpkin/a.php::Shape.run"),
        ],
    ),
    "csharp": Case(
        {
            "cskin/A.cs": "namespace Kin.A\n{\n    public interface IShape\n    {\n        int Run();\n    }\n\n"
            "    public class Base : IShape\n    {\n"
            "        public virtual int Run()\n        {\n            return 1;\n"
            "        }\n\n        public int Helper()\n        {\n            return 2;\n        }\n\n"
            "        public int Other()\n        {\n            return 3;\n        }\n    }\n}\n",
            "cskin/B.cs": "using Kin.A;\n\nnamespace Kin.B\n{\n    public class Child : Base\n    {\n"
            "        public override int Run()\n        {\n            return base.Run() + this.Helper() + Other();\n"
            "        }\n    }\n}\n",
        },
        inherits=[("cskin/B.cs::Child", "cskin/A.cs::Base"), ("cskin/A.cs::Base", "cskin/A.cs::IShape")],
        calls=[
            ("cskin/B.cs::Child.Run", "cskin/A.cs::Base.Run"),
            ("cskin/B.cs::Child.Run", "cskin/A.cs::Base.Helper"),
            ("cskin/B.cs::Child.Run", "cskin/A.cs::Base.Other"),
        ],
        overrides=[
            ("cskin/B.cs::Child.Run", "cskin/A.cs::Base.Run"),
            ("cskin/A.cs::Base.Run", "cskin/A.cs::IShape.Run"),
        ],
    ),
    "swift": Case(
        {
            "swiftkin/A.swift": "class Base {\n    func run() -> Int {\n        return 1\n    }\n}\n",
            "swiftkin/B.swift": (
                "class Child: Base {\n    override func run() -> Int {\n        return super.run()\n    }\n}\n"
            ),
        },
        absent=[
            ("inherits", "swiftkin/B.swift::Child", "swiftkin/A.swift::Base"),
            ("calls", "swiftkin/B.swift::Child.run", "swiftkin/A.swift::Base.run"),
        ],
    ),
    "dart": Case(
        {
            "dartkin/a.dart": "class Base {\n  int run() => 1;\n}\n",
            "dartkin/b.dart": "import 'a.dart';\n\nclass Child extends Base {\n  int run() => super.run();\n}\n",
        },
    ),
    "shell": Case({"shkin/a.sh": "#!/bin/bash\nhelper() {\n  echo 1\n}\n"}),
    "sql": Case({"sqlkin/a.sql": "CREATE TABLE t (id integer);\n"}),
}
# Within one Swift file the chain is known: the in-file fixture links what the two-file one cannot.
# (graphify 0.9.73 drops a Swift call written as the right operand after super.area(), hence the local.)
SWIFT_ONE_FILE = {
    "swiftkin/One.swift": "class Shape {\n    func area() -> Int {\n        return 1\n    }\n\n"
    "    func side() -> Int {\n        return 2\n    }\n}\n\nclass Square: Shape {\n"
    "    override func area() -> Int {\n        let side = self.side()\n        return super.area() + side\n    }\n}\n",
}
IMPACT_SEEDS = ("pykin/a.py::Base.run", "tskin/a.ts::Base.run", "javakin/com/kin/a/Base.java::Base.run")


def sym(ref: str) -> str:
    return f"symbol:kin:{ref}"


@pytest.fixture(scope="module")
def kin(tmp_path_factory):
    """One build of a repo holding every fixture: its edges as (rel, src, dst) -> status, its symbols, and
    kg_impact at depth 1 of a few base methods."""
    base = tmp_path_factory.mktemp("inheritance")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("EVO_KG_HOME", str(base / "kg-home"))
        repo = base / "kin"
        for case in CASES.values():
            for path, text in case.files.items():
                write(repo / path, text)
        for path, text in SWIFT_ONE_FILE.items():
            write(repo / path, text)
        git(repo, "init", "-q", "-b", "main")
        commit(repo, "init")
        root = base / "kin-harness"
        write(
            root / "harness.yaml",
            "name: kin\nworkspace: ..\nknowledge_file: knowledge.yaml\nrepos:\n  - name: kin\n    path: kin\n",
        )
        write(
            root / "knowledge.yaml",
            "version: 1\nproject: kin\npolicy:\n  levels: [public, internal]\n  sinks:\n    - id: agent\n"
            "      kind: agent-session\n      clearance: {level: internal}\nsources:\n  - id: kin\n"
            "    connector: git\n    repo: kin\n    label: {level: internal, integrity: U}\n",
        )
        project = load_project_at(root, base / "kg-home")
        assert all(r.ok for r in sync_project(project))
        report = build_project(project, verify=True, cold=True)
        assert report.ok, report.errors
        store = Store.for_project(project)
        live = "tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)"
        rows = store.db.execute(
            f"SELECT rel, src, dst, status FROM edges WHERE rel IN ('calls', 'inherits', 'overrides') AND {live}",
            {"b": report.build_id},
        ).fetchall()
        symbols = {
            r[0]
            for r in store.db.execute(
                f"SELECT node_id FROM nodes WHERE kind = 'Symbol' AND {live}", {"b": report.build_id}
            )
        }
        store.close()
        session = Session(project, "agent")
        impact = {seed: session.call("kg_impact", {"id": sym(seed), "depth": 1}) for seed in IMPACT_SEEDS}
    return {(rel, src, dst): status for rel, src, dst, status in rows}, symbols, impact


def needs_graphify(lang: str) -> None:
    if lang != "python" and not gfy.available():
        pytest.skip("graphify extra not installed")


def in_files(ref: str, files) -> bool:
    return any(ref.startswith(sym(p) + "::") for p in files)


def test_inherits_and_overrides_are_hub_relations():
    assert HUB_RELATIONS["inherits"] == ("Symbol", "Symbol")
    assert HUB_RELATIONS["overrides"] == ("Symbol", "Symbol")
    assert "implements" in HUB_RELATIONS  # the requirement chain's relation stays its own


def test_every_code_language_has_an_inheritance_and_override_row():
    assert set(LANGUAGE_BY_SUFFIX.values()) == set(INHERITANCE) == set(OVERRIDES) == set(CASES) == set(CROSS_FILE_CALLS)
    assert not INHERITANCE["go"].supported and "no inheritance" in INHERITANCE["go"].note
    for table in (INHERITANCE, OVERRIDES):
        for lang, row in table.items():
            assert row.note, lang


@pytest.mark.parametrize("lang", sorted(CASES))
def test_inherits_across_files(lang, kin):
    needs_graphify(lang)
    edges, symbols, _ = kin
    case, row = CASES[lang], INHERITANCE[lang]
    for child, parent in case.inherits:
        expected = "parsed" if child.split("::")[0] == parent.split("::")[0] else "resolved"
        assert edges.get(("inherits", sym(child), sym(parent))) == expected, (child, parent)
    for rel, src, dst in case.absent:
        assert (rel, sym(src), sym(dst)) not in edges, (rel, src, dst)
    crossing = {
        (s, d)
        for rel, s, d in edges
        if rel == "inherits" and in_files(s, case.files) and s.split("::")[0] != d.split("::")[0]
    }
    if row.supported:
        assert crossing == {(sym(c), sym(p)) for c, p in case.inherits if c.split("::")[0] != p.split("::")[0]}
    else:
        assert row.note and not case.inherits and not crossing
    if lang in ("dart", "sql"):
        assert not any(in_files(s, case.files) for s in symbols)  # no symbols, nothing to inherit


@pytest.mark.parametrize("lang", sorted(CASES))
def test_super_and_this_calls_reach_the_nearest_definition(lang, kin):
    needs_graphify(lang)
    edges, _, _ = kin
    case = CASES[lang]
    for caller, callee in case.calls:
        assert edges.get(("calls", sym(caller), sym(callee))) in ("resolved", "parsed"), (caller, callee)
    for (rel, src, dst), _status in edges.items():
        assert not (rel == "calls" and src == dst), src  # never a call of a method to itself


@pytest.mark.parametrize("lang", sorted(CASES))
def test_overrides_across_files(lang, kin):
    needs_graphify(lang)
    edges, _, _ = kin
    case, row = CASES[lang], OVERRIDES[lang]
    for method, parent in case.overrides:
        expected = "parsed" if method.split("::")[0] == parent.split("::")[0] else "resolved"
        assert edges.get(("overrides", sym(method), sym(parent))) == expected, (method, parent)
    found = {(s, d) for rel, s, d in edges if rel == "overrides" and in_files(s, case.files)}
    assert found == {(sym(m), sym(p)) for m, p in case.overrides}
    if not row.supported:
        assert row.note and not found


def test_inherits_super_and_this_call_within_one_swift_file(kin):
    needs_graphify("swift")
    edges, _, _ = kin
    one = "swiftkin/One.swift"
    assert edges.get(("inherits", sym(f"{one}::Square"), sym(f"{one}::Shape"))) == "parsed"
    assert edges.get(("overrides", sym(f"{one}::Square.area"), sym(f"{one}::Shape.area"))) == "parsed"
    assert ("calls", sym(f"{one}::Square.area"), sym(f"{one}::Shape.area")) in edges  # super.area()
    assert ("calls", sym(f"{one}::Square.area"), sym(f"{one}::Shape.side")) in edges  # self.side()


@pytest.mark.parametrize("seed", IMPACT_SEEDS)
def test_impact_of_an_overridden_method_lists_overrides_and_this_callers(seed, kin):
    needs_graphify("python" if seed.endswith(".py::Base.run") else "typescript")
    _, _, impact = kin
    hop1 = {(r["node"]["id"], r["rel"]) for r in impact[seed]["structuredContent"]["impacted"] if r["hop"] == 1}
    reached = {node for node, _ in hop1}
    child = {"pykin": "pykin/b.py", "tskin": "tskin/b.ts"}.get(seed.split("/")[0], "javakin/com/kin/b/Child.java")
    assert (sym(f"{child}::Quiet.run"), "overrides") in hop1  # overrides it and calls nothing
    assert sym(f"{child}::Child.run") in reached  # overrides it and calls it through super
    if not seed.startswith("java"):
        assert (sym(f"{child}::Plain.go"), "calls") in hop1  # this.run() in a subclass that keeps run
