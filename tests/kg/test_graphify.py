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
