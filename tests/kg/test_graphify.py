import pytest

from evo_agents.kg.extract import graphify as gfy
from evo_agents.kg.pipeline.stages import code_extractor, structure_item

pytestmark = pytest.mark.skipif(not gfy.available(), reason="graphify extra not installed")

SEARCH = """import { rank } from "./rank";
import express from "express";

export class SearchService {
  async search(query: string) {
    return rank(await this.find(query));
  }
  find(query: string) { return [query]; }
}

export function buildRouter(service: SearchService) {
  return express.Router().get("/kb/search", (req, res) => service.search(req.query.q));
}
"""


def test_extract_typescript():
    out = gfy.extract(SEARCH, "api/src/search.ts")
    quals = {s["qualname"]: s for s in out["symbols"]}
    assert quals["SearchService.search"]["kind"] == "method"
    assert quals["SearchService.search"]["parent"] == "SearchService"
    assert quals["buildRouter"]["kind"] == "function"
    assert any(c["caller"] == "buildRouter" and c["callee"] == "SearchService.search" for c in out["calls"])
    assert out["imports"][0][0] == "api/src/rank.ts"  # only the relative import; express is a package


def test_module_constants_of_typescript_are_symbols():
    text = (
        'export const STALL_MS = 15_000;\nconst _RETRY: number = 3;\nexport const SECRETS_PAGE_KEY = ["secrets"];\n'
        "export const useLog = () => STALL_MS;\nfunction f() {\n  const INNER = 1;\n  return INNER;\n}\n"
        "export let STALL_MS = 1;\n"
    )
    out = gfy.extract(text, "web/src/use-run-log.ts")
    found = {s["qualname"]: s for s in out["symbols"] if s["kind"] == "constant"}
    assert list(found) == ["STALL_MS", "_RETRY", "SECRETS_PAGE_KEY"]  # top level, upper case, first declaration
    assert found["STALL_MS"]["line"] == 1 and found["SECRETS_PAGE_KEY"]["line"] == 3
    assert gfy.constants(text, "src/a.py", []) == []  # the python extractor gives python's


def test_structure_stage_uses_graphify_for_typescript():
    record = {
        "id": "web:file:api/src/search.ts",
        "kind": "code",
        "hash": "x",
        "props": {"path": "api/src/search.ts", "repo": "web"},
    }
    assert code_extractor(record, "auto").startswith("graphify-ast@")
    assert code_extractor(record, "python-ast") == "none"
    facts = structure_item(record, "web", SEARCH, "auto")
    ids = {f["id"] for f in facts if f["t"] == "node"}
    assert "symbol:web:api/src/search.ts::SearchService.search" in ids
    refs = [f for f in facts if f["t"] == "ref"]
    assert refs and refs[0]["target"]["type"] == "paths"


# Languages whose extractor marks no node ``_callable``, so their symbols are found by the node's own file.
STRUCTURAL = {
    "go/b/b.go": 'package b\n\nimport al "example.com/poly/go/a"\n\ntype Box struct{}\n\n'
    "func (b Box) Size() int { return al.Helper(1) }\n\nfunc Caller() int { return Box{}.Size() }\n",
    "rs/src/b.rs": "use crate::a::Shape;\n\npub struct Square {}\n\nimpl Shape for Square {\n"
    "    fn area(&self) -> i32 { helper() }\n}\n\nfn helper() -> i32 { 1 }\n",
    "sh/b.sh": "#!/bin/sh\n. ./a.sh\n\nhelper() {\n  echo hi\n}\n\nentry() {\n  helper\n}\n\nentry\n",
}


def _as_on_windows(monkeypatch):
    """Make graphify report each node's file the way it does on Windows: ``str()`` of a WindowsPath."""
    from graphify import extract as gx

    get = gx._get_extractor

    def wrap(fn):
        def run(path):
            result = fn(path)
            for node in result.get("nodes", []):
                if node.get("source_file"):
                    node["source_file"] = "C:" + str(node["source_file"]).replace("/", "\\")
            return result

        run.__name__ = fn.__name__
        return run

    monkeypatch.setattr(gx, "_get_extractor", lambda path: (fn := get(path)) and wrap(fn))


@pytest.mark.parametrize("path", sorted(STRUCTURAL))
def test_structural_symbols_do_not_depend_on_the_os_path_separator(path, monkeypatch):
    text = STRUCTURAL[path]
    posix = gfy.extract(text, path)
    assert posix["symbols"], path
    _as_on_windows(monkeypatch)
    assert gfy.extract(text, path) == posix
