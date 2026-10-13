"""Calls across files: an edge from the calling symbol to the called one when the caller's file names it
(an import, a require or source line, a package the language puts in scope), and none on a shared name
alone. One two-file fixture per language the connectors mark as code; a language the coverage table
marks ``no`` must get no cross-file edge, and its row must say why."""

import pytest

from evo_agents.kg.build import build_project
from evo_agents.kg.connectors._files import LANGUAGE_BY_SUFFIX
from evo_agents.kg.extract import graphify as gfy
from evo_agents.kg.extract.languages import CROSS_FILE_CALLS, language_of
from evo_agents.kg.project import load_project_at
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, git, write

# language -> (files, calls that must be edges, calls that must not). A call is (caller, callee) as
# "<path>::<qualname>"; each fixture's second file calls into the first.
FIXTURES = {
    "python": (
        {
            "pyapp/__init__.py": "",
            "pyapp/a.py": (
                "def helper():\n    return 1\n\n\ndef other():\n    return 2\n\n\n"
                "class Repo:\n    def find(self, key):\n        return key\n"
            ),
            "pyapp/b.py": (
                "from pyapp.a import helper\nimport pyapp.a as amod\nfrom . import a\n\n\n"
                "def absolute():\n    return helper()\n\n\n"
                "def module_alias():\n    return amod.other()\n\n\n"
                "def relative():\n    return a.Repo().find(1)\n\n\n"
                "def local_import():\n    from pyapp.a import other as o\n\n    return o()\n\n\n"
                "def no_evidence():\n    return other()\n\n\n"
                "def shadowed(helper):\n    return helper()\n"
            ),
        },
        [
            ("pyapp/b.py::absolute", "pyapp/a.py::helper"),
            ("pyapp/b.py::module_alias", "pyapp/a.py::other"),
            ("pyapp/b.py::relative", "pyapp/a.py::Repo"),
            ("pyapp/b.py::local_import", "pyapp/a.py::other"),
        ],
        [("pyapp/b.py::no_evidence", "pyapp/a.py::other"), ("pyapp/b.py::shadowed", "pyapp/a.py::helper")],
    ),
    "typescript": (
        {
            "ts/tsconfig.json": (  # JSON with a comment and trailing commas, as tsconfig allows
                '{\n  // aliases\n  "compilerOptions": {"baseUrl": ".", "paths": {"@lib/*": ["src/*"]},},\n}\n'
            ),
            "ts/src/a.ts": (
                "export function helper(x: number) { return x + 1; }\n"
                "export function other() { return 2; }\n"
                "export class Repo { find(id: string) { return id; } }\n"
            ),
            "ts/src/b.ts": (
                'import { helper, Repo as R } from "./a";\n'
                'import * as A from "@lib/a";\n'
                "export function caller() { return helper(1) + A.other(); }\n"
                'export function useRepo(r: R) { return r.find("x"); }\n'
                "export function noEvidence() { return other(); }\n"
            ),
        },
        [
            ("ts/src/b.ts::caller", "ts/src/a.ts::helper"),
            ("ts/src/b.ts::caller", "ts/src/a.ts::other"),
            ("ts/src/b.ts::useRepo", "ts/src/a.ts::Repo.find"),
        ],
        [("ts/src/b.ts::noEvidence", "ts/src/a.ts::other")],
    ),
    "javascript": (
        {
            "js/a.js": "function helper(x) { return x + 1; }\nfunction other() { return 2; }\n"
            "module.exports = { helper, other };\n",
            "js/b.js": 'const { helper } = require("./a");\nconst lib = require("./a.js");\n'
            "function caller() { return helper(1) + lib.other(); }\n"
            "function noEvidence() { return other(); }\nmodule.exports = { caller, noEvidence };\n",
        },
        [("js/b.js::caller", "js/a.js::helper"), ("js/b.js::caller", "js/a.js::other")],
        [("js/b.js::noEvidence", "js/a.js::other")],
    ),
    "vue": (
        {
            "vue/util.ts": (
                "export function helper(x: number) { return x + 1; }\nexport function stray() { return 0; }\n"
            ),
            "vue/Widget.vue": '<template><div>{{ caller() }}</div></template>\n<script setup lang="ts">\n'
            'import { helper } from "./util";\nfunction caller() { return helper(1); }\n'
            "function noEvidence() { return stray(); }\n</script>\n",
        },
        [("vue/Widget.vue::caller", "vue/util.ts::helper")],
        [("vue/Widget.vue::noEvidence", "vue/util.ts::stray")],
    ),
    "go": (
        {
            "go/a/a.go": "package a\n\nfunc Helper(x int) int { return x + 1 }\n\nfunc Other() int { return 2 }\n",
            "go/b/b.go": 'package b\n\nimport (\n\tal "example.com/poly/go/a"\n)\n\n'
            "func Caller() int { return al.Helper(1) }\n\nfunc NoEvidence() int { return Other() }\n",
        },
        [("go/b/b.go::Caller", "go/a/a.go::Helper")],
        [("go/b/b.go::NoEvidence", "go/a/a.go::Other")],
    ),
    "java": (
        {
            "java/com/acme/a/Helper.java": "package com.acme.a;\n\npublic class Helper {\n"
            "    public static int util(int x) { return x + 1; }\n    public int inst() { return 2; }\n}\n\n"
            "class Hidden {\n    static int util() { return 3; }\n}\n",
            "java/com/acme/b/Caller.java": "package com.acme.b;\n\nimport com.acme.a.Helper;\n\npublic class Caller {\n"
            "    public int run() { Helper h = new Helper(); return Helper.util(1) + h.inst(); }\n"
            "    public int noEvidence() { return Hidden.util(); }\n}\n",
        },
        [
            ("java/com/acme/b/Caller.java::Caller.run", "java/com/acme/a/Helper.java::Helper"),  # new Helper()
            ("java/com/acme/b/Caller.java::Caller.run", "java/com/acme/a/Helper.java::Helper.util"),
            ("java/com/acme/b/Caller.java::Caller.run", "java/com/acme/a/Helper.java::Helper.inst"),
        ],
        [("java/com/acme/b/Caller.java::Caller.noEvidence", "java/com/acme/a/Helper.java::Hidden.util")],
    ),
    "kotlin": (
        {
            "kt/A.kt": "package com.acme.a\n\nfun helper(x: Int): Int = x + 1\n\nfun stray(x: Int): Int = x\n",
            "kt/B.kt": "package com.acme.b\n\nimport com.acme.a.helper\n\nfun caller(): Int = helper(1)\n\n"
            "fun noEvidence(): Int = stray(1)\n",
        },
        [("kt/B.kt::caller", "kt/A.kt::helper")],
        [("kt/B.kt::noEvidence", "kt/A.kt::stray")],
    ),
    "rust": (
        {
            "rs/src/a.rs": "pub fn helper(x: i32) -> i32 { x + 1 }\npub fn stray() -> i32 { 2 }\n",
            "rs/src/b.rs": "use crate::a::helper;\n\npub fn caller() -> i32 { helper(1) }\n\n"
            "pub fn no_evidence() -> i32 { stray() }\n",
        },
        [("rs/src/b.rs::caller", "rs/src/a.rs::helper")],
        [("rs/src/b.rs::no_evidence", "rs/src/a.rs::stray")],
    ),
    "ruby": (
        {
            "rb/a.rb": "def helper(x)\n  x + 1\nend\n\nmodule Util\n  def self.other\n    2\n  end\nend\n\n"
            "def backwards\n  entry\nend\n",
            "rb/b.rb": 'require_relative "a"\n\ndef entry\n  helper(1) + Util.other\nend\n',
        },
        [("rb/b.rb::entry", "rb/a.rb::helper"), ("rb/b.rb::entry", "rb/a.rb::Util.other")],
        [("rb/a.rb::backwards", "rb/b.rb::entry")],  # a.rb loads nothing: b.rb's entry is not in its scope
    ),
    "php": (
        {
            "php/a.php": "<?php\nnamespace App\\A;\n\nfunction helper($x) { return $x + 1; }\n\n"
            "function stray() { return 0; }\n\nclass Repo {\n    public function find($id) { return $id; }\n}\n",
            "php/b.php": "<?php\nnamespace App\\B;\n\nuse App\\A\\Repo;\nuse function App\\A\\helper;\n\n"
            "function caller() { $r = new Repo(); return helper(1); }\n\nfunction no_evidence() { return stray(); }\n",
        },
        [("php/b.php::caller", "php/a.php::helper"), ("php/b.php::caller", "php/a.php::Repo")],
        [("php/b.php::no_evidence", "php/a.php::stray")],
    ),
    "csharp": (
        {
            "cs/A.cs": "namespace Acme.A\n{\n    public static class Helper\n    {\n"
            "        public static int Util(int x) { return x + 1; }\n    }\n}\n\nnamespace Acme.C\n{\n"
            "    public static class Stray\n    {\n        public static int Run() { return 0; }\n    }\n}\n",
            "cs/B.cs": "using Acme.A;\n\nnamespace Acme.B\n{\n    public class Caller\n    {\n"
            "        public int Run() { return Helper.Util(1); }\n"
            "        public int NoEvidence() { return Stray.Run(); }\n"
            "    }\n}\n",
        },
        [("cs/B.cs::Caller.Run", "cs/A.cs::Helper.Util")],
        [("cs/B.cs::Caller.NoEvidence", "cs/A.cs::Stray.Run")],
    ),
    "swift": (
        {
            "swift/A.swift": "func helper(_ x: Int) -> Int { return x + 1 }\n",
            "swift/B.swift": "import Foundation\n\nfunc caller() -> Int { return helper(1) }\n",
        },
        [],
        [("swift/B.swift::caller", "swift/A.swift::helper")],
    ),
    "dart": (
        {
            "dart/a.dart": "int helper(int x) => x + 1;\n",
            "dart/b.dart": "import 'a.dart';\n\nint caller() => helper(1);\n",
        },
        [],
        [("dart/b.dart::caller", "dart/a.dart::helper")],
    ),
    "shell": (
        {
            "sh/a.sh": '#!/bin/bash\nhelper() {\n  echo "$1"\n}\n\nbackwards() {\n  entry\n}\n',
            "sh/b.sh": '#!/bin/bash\nsource "$(dirname "$0")/a.sh"\n\nentry() {\n  helper 1\n}\n',
        },
        [("sh/b.sh::entry", "sh/a.sh::helper")],
        [("sh/a.sh::backwards", "sh/b.sh::entry")],  # a.sh sources nothing
    ),
    "sql": (
        {
            "sql/a.sql": "CREATE FUNCTION helper(x integer) RETURNS integer AS $$ SELECT x + 1 $$ LANGUAGE sql;\n",
            "sql/b.sql": "CREATE FUNCTION caller() RETURNS integer AS $$ SELECT helper(1) $$ LANGUAGE sql;\n",
        },
        [],
        [("sql/b.sql::caller", "sql/a.sql::helper")],
    ),
}
# Beyond the two-file fixtures: the scope a language gives without an import, and a Python src/ layout.
EXTRAS = {
    "pysrc/src/core/log.py": "def get_logger(name):\n    return name\n",
    "pysrc/src/api/view.py": "from core.log import get_logger\n\n\ndef handler():\n    return get_logger('api')\n",
    "go/b/util.go": "package b\n\nfunc Local() int { return 3 }\n",
    "go/b/use.go": "package b\n\nfunc UseLocal() int { return Local() }\n",
    "java/com/acme/b/Peer.java": "package com.acme.b;\n\nclass Peer {\n    int go() { return Twice.twice(); }\n}\n",
    "java/com/acme/b/Twice.java": "package com.acme.b;\n\nclass Twice {\n    static int twice() { return 2; }\n}\n",
}


def sym(ref: str) -> str:
    return f"symbol:poly:{ref}"


@pytest.fixture(scope="module")
def poly(tmp_path_factory):
    """One build of a repo holding every fixture: the calls edges of its latest build, as (src, dst) -> status."""
    base = tmp_path_factory.mktemp("cross-file")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("EVO_KG_HOME", str(base / "kg-home"))
        repo = base / "poly"
        for files, _, _ in FIXTURES.values():
            for path, text in files.items():
                write(repo / path, text)
        for path, text in EXTRAS.items():
            write(repo / path, text)
        git(repo, "init", "-q", "-b", "main")
        commit(repo, "init")
        root = base / "poly-harness"
        write(
            root / "harness.yaml",
            "name: poly\nworkspace: ..\nknowledge_file: knowledge.yaml\nrepos:\n  - name: poly\n    path: poly\n",
        )
        write(
            root / "knowledge.yaml",
            "version: 1\nproject: poly\npolicy:\n  levels: [public, internal]\n  sinks:\n    - id: agent\n"
            "      kind: agent-session\n      clearance: {level: internal}\nsources:\n  - id: poly\n"
            "    connector: git\n    repo: poly\n    label: {level: internal, integrity: U}\n",
        )
        project = load_project_at(root, base / "kg-home")
        assert all(r.ok for r in sync_project(project))
        report = build_project(project, verify=True, cold=True)
        assert report.ok, report.errors
        store = Store.for_project(project)
        live = "tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)"
        rows = store.db.execute(
            f"SELECT src, dst, status FROM edges WHERE rel = 'calls' AND {live}", {"b": report.build_id}
        ).fetchall()
        symbols = {
            r[0]
            for r in store.db.execute(
                f"SELECT node_id FROM nodes WHERE kind = 'Symbol' AND {live}",
                {"b": report.build_id},
            )
        }
        store.close()
    return {(src, dst): status for src, dst, status in rows}, symbols


def test_every_code_language_has_a_cross_file_row():
    assert set(LANGUAGE_BY_SUFFIX.values()) == set(CROSS_FILE_CALLS) == set(FIXTURES)
    for suffix, lang in LANGUAGE_BY_SUFFIX.items():
        assert language_of(f"x{suffix}") == lang


@pytest.mark.parametrize("lang", sorted(FIXTURES))
def test_cross_file_calls(lang, poly):
    if lang != "python" and not gfy.available():
        pytest.skip("graphify extra not installed")
    calls, symbols = poly
    files, expected, absent = FIXTURES[lang]
    row = CROSS_FILE_CALLS[lang]
    for caller, callee in expected:
        assert calls.get((sym(caller), sym(callee))) == "resolved", (caller, callee, sorted(calls))
    for caller, callee in absent:
        assert (sym(caller), sym(callee)) not in calls, (caller, callee)
    crossing = {(s, d) for s, d in calls if s.split("::")[0] != d.split("::")[0]}
    in_fixture = {(s, d) for s, d in crossing if any(s.startswith(sym(p) + "::") for p in files)}
    if row.supported:
        assert expected and in_fixture == {(sym(c), sym(d)) for c, d in expected}
    else:
        # The table says no, with the reason it measured; the build agrees: no edge leaves these files.
        assert row.note and not expected and not in_fixture
    if lang == "sql":
        assert not any(s.startswith(sym("sql/")) for s in symbols)  # no grammar, no symbols


def test_cross_file_calls_in_the_same_package(poly):
    if not gfy.available():
        pytest.skip("graphify extra not installed")
    calls, _ = poly
    assert calls.get((sym("go/b/use.go::UseLocal"), sym("go/b/util.go::Local"))) == "resolved"
    peer = (sym("java/com/acme/b/Peer.java::Peer.go"), sym("java/com/acme/b/Twice.java::Twice.twice"))
    assert calls.get(peer) == "resolved"


def test_cross_file_calls_through_a_src_layout(poly):
    # src/ is on sys.path: `from core.log import ...` names src/core/log.py, whose module path is src.core.log.
    calls, _ = poly
    assert calls.get((sym("pysrc/src/api/view.py::handler"), sym("pysrc/src/core/log.py::get_logger"))) == "resolved"
