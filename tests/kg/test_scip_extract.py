"""The ``scip`` code backend: a SCIP index read by the connector rides on its code items (``props.scip``), and the build
turns it into ``calls``, ``implements`` and ``references`` edges at ``introspected``, confidence 1, that the base
extractors cannot see (a call through an untyped variable, a loop variable, ``with ... as``, a callback, a method of a
TypeScript interface). Without an index, or without the tools, the build reads the code as before."""

import json
import pathlib

import pytest

from evo_agents.kg.extract import scip_index
from evo_agents.kg.extract.scip_index import parse_scip_bytes, parse_scip_json, qualname_of
from evo_agents.kg.project import load_project_at
from evo_agents.kg.sync import sync_project
from tests.kg import built
from tests.kg.projects import commit, git, write

SCHEMA = pathlib.Path(__file__).parents[2] / "evo_agents/harness/schemas/knowledge.schema.json"

# --- a protobuf writer: the three wire types the SCIP messages use -----------------------------------------------


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        low, n = n & 0x7F, n >> 7
        out.append(low | (0x80 if n else 0))
        if not n:
            return bytes(out)


def text(number: int, value: str) -> bytes:
    raw = value.encode()
    return varint(number << 3 | 2) + varint(len(raw)) + raw


def message(number: int, body: bytes) -> bytes:
    return varint(number << 3 | 2) + varint(len(body)) + body


def number(num: int, value: int) -> bytes:
    return varint(num << 3) + varint(value)


def ints(num: int, values: list[int]) -> bytes:
    return message(num, b"".join(varint(v) for v in values))


def occurrence(rng: list[int], symbol: str, *, definition: bool = False, enclosing: list[int] | None = None) -> bytes:
    body = ints(1, rng) + text(2, symbol) + (number(3, 1) if definition else b"")
    return message(2, body + (ints(7, enclosing) if enclosing else b""))


def info(symbol: str, implements: list[str] = ()) -> bytes:
    rels = b"".join(message(4, text(1, s) + number(3, 1)) for s in implements)
    return message(3, text(1, symbol) + rels)


def document(path: str, *parts: bytes) -> bytes:
    return message(2, text(1, path) + text(4, "x") + b"".join(parts))


def index(*docs: bytes) -> bytes:
    return message(1, text(2, "evo-test")) + b"".join(docs)


# --- the Python fixture ------------------------------------------------------------------------------------------

REPO = "scip-python python app 0.1 `app.models`/Repo#"
SAVE = REPO + "save()."
RUN = "scip-python python app 0.1 `app.service`/run()."
CB = "scip-python python app 0.1 `app.service`/cb()."
MODELS = "class Repo:\n    def save(self):\n        return 1\n"
SERVICE = (
    "from app.models import Repo\n\n\n"
    "def run(items, apply):\n    for r in items:\n        r.save()\n    with open_repo() as r2:\n"
    "        r2.save()\n    apply(cb)\n\n\ndef cb():\n    return Repo\n"
)


def python_index() -> bytes:
    return index(
        document(
            "app/models.py",
            occurrence([0, 6, 10], REPO, definition=True, enclosing=[0, 0, 2, 16]),
            occurrence([1, 8, 12], SAVE, definition=True, enclosing=[1, 4, 2, 16]),
            info(REPO),
            info(SAVE),
        ),
        document(
            "app/service.py",
            occurrence([3, 4, 7], RUN, definition=True, enclosing=[3, 0, 8, 14]),
            occurrence([4, 8, 9], "local 1"),
            occurrence([5, 10, 14], SAVE),  # r.save(): r is the untyped variable of a loop
            occurrence([7, 11, 15], SAVE),  # r2.save(): r2 is bound by with ... as
            occurrence([8, 10, 12], CB),  # apply(cb): a callback
            occurrence([11, 4, 6], CB, definition=True, enclosing=[11, 0, 12, 16]),
            occurrence([12, 11, 15], REPO),  # a use of the class in cb
        ),
    )


# --- the TypeScript fixture --------------------------------------------------------------------------------------

PAYER = "scip-typescript npm shop 1.0 src/`checkout.ts`/Payer#"
PAYER_PAY = PAYER + "pay()."
CHECKOUT = "scip-typescript npm shop 1.0 src/`checkout.ts`/Checkout#"
CHECKOUT_PAY = CHECKOUT + "pay()."
CHARGE = "scip-typescript npm shop 1.0 src/`checkout.ts`/charge()."
TS = (
    "export interface Payer {\n  pay(): void;\n}\nexport class Checkout implements Payer {\n  pay() {}\n}\n"
    "export function charge(p: Payer) {\n  p.pay();\n}\n"
)


def typescript_index() -> bytes:
    return index(
        document(
            "web/checkout.ts",
            occurrence([0, 17, 22], PAYER, definition=True, enclosing=[0, 0, 2, 1]),
            occurrence([1, 2, 5], PAYER_PAY, definition=True, enclosing=[1, 2, 1, 14]),
            occurrence([3, 13, 21], CHECKOUT, definition=True, enclosing=[3, 0, 5, 1]),
            occurrence([4, 2, 5], CHECKOUT_PAY, definition=True, enclosing=[4, 2, 4, 10]),
            occurrence([6, 16, 22], CHARGE, definition=True, enclosing=[6, 0, 8, 1]),
            occurrence([6, 26, 31], PAYER),
            occurrence([7, 4, 7], PAYER_PAY),
            info(PAYER),
            info(PAYER_PAY),
            info(CHECKOUT, [PAYER]),
            info(CHECKOUT_PAY, [PAYER_PAY]),
            info(CHARGE),
        ),
    )


# --- the project ---------------------------------------------------------------------------------------------------


def make(base, home, code: str, files: dict[str, str | bytes], scip: dict[str, bytes] | None = None):
    """A harness with one git source, ``billing``, over a repo of ``files``, and the SCIP index files of ``scip``."""
    repo = base / "billing-service"
    for name, body in files.items():
        write(repo / name, body)
    for name, raw in (scip or {}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(raw)
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "init")
    root = base / "billing-harness"
    write(
        root / "harness.yaml",
        "name: billing\nworkspace: ..\nknowledge_file: knowledge.yaml\n"
        "repos:\n  - name: billing-service\n    path: billing-service\n",
    )
    write(
        root / "knowledge.yaml",
        "version: 1\nproject: billing\npolicy:\n  levels: [public, internal]\n  sinks:\n"
        "    - id: agent\n      kind: agent-session\n      clearance: {level: internal}\n"
        f"sources:\n  - id: billing\n    connector: git\n    repo: billing-service\n"
        f"    label: {{level: internal, integrity: U}}\n    code: {code}\n",
    )
    project = load_project_at(root, home)
    assert all(r.ok for r in sync_project(project))
    return project


def edges(project, rel: str) -> dict[tuple[str, str], dict]:
    return {(e["src"], e["dst"]): e for e in built.edges_of(project) if e["rel"] == rel}


def short(node: str) -> str:
    return node.removeprefix("symbol:billing:")


@pytest.fixture
def no_graphify(monkeypatch):
    import sys

    from evo_agents.kg.extract import graphify as gfy

    monkeypatch.setitem(sys.modules, "graphify", None)
    monkeypatch.setitem(sys.modules, "graphify.extract", None)
    gfy.available.cache_clear()
    yield
    gfy.available.cache_clear()


# --- the reader ----------------------------------------------------------------------------------------------------


def test_wire_format_decodes_the_documents():
    parsed = parse_scip_bytes(python_index())
    paths = [d["path"] for d in parsed["documents"]]
    assert paths == ["app/models.py", "app/service.py"]
    service = parsed["documents"][1]
    run = service["occurrences"][0]
    assert (
        run["symbol"] == RUN and run["roles"] == 1 and run["range"] == [3, 4, 7] and run["enclosing"] == [3, 0, 8, 14]
    )
    ts = parse_scip_bytes(typescript_index())["documents"][0]
    checkout = next(s for s in ts["symbols"] if s["symbol"] == CHECKOUT)
    assert checkout["relationships"] == [{"symbol": PAYER, "implementation": True}]


def test_json_form_reads_the_same_as_the_binary():
    doc = {
        "documents": [
            {
                "relativePath": "web/checkout.ts",
                "occurrences": [
                    {"range": [4, 2, 5], "symbol": CHECKOUT_PAY, "symbolRoles": 1, "enclosingRange": [4, 2, 4, 10]}
                ],
                "symbols": [{"symbol": CHECKOUT, "relationships": [{"symbol": PAYER, "isImplementation": True}]}],
            }
        ]
    }
    parsed = parse_scip_json(json.dumps(doc))
    data = scip_index.files_data(parsed)["web/checkout.ts"]
    assert data["defs"] == [[CHECKOUT_PAY, "Checkout.pay", 5, 5]]
    assert data["impl"] == [[CHECKOUT, PAYER]]


def test_symbols_name_their_qualified_name_and_skip_locals():
    assert qualname_of(SAVE) == "Repo.save" and qualname_of(CB) == "cb"
    assert qualname_of(CHARGE) == "charge" and qualname_of("local 3") is None
    assert qualname_of("scip-python python app 0.1 `a.b`/") is None


def test_file_data_attributes_each_use_to_the_innermost_definition():
    data = scip_index.files_data(parse_scip_bytes(python_index()))["app/service.py"]
    assert data["refs"] == [[RUN, SAVE, 6], [RUN, CB, 9], [CB, REPO, 13]]


# --- the build -----------------------------------------------------------------------------------------------------


def python_project(tmp_path, kg_env, code="{backend: scip, scip_index: index.scip}", scip=None):
    scip = {"index.scip": python_index()} if scip is None else scip
    return make(tmp_path, kg_env, code, {"app/models.py": MODELS, "app/service.py": SERVICE}, scip)


def test_python_calls_through_untyped_variables_loops_with_and_callbacks_are_edges(tmp_path, kg_env, no_graphify):
    project = python_project(tmp_path, kg_env)
    report = built.build(project).to_json()
    assert report["ok"]
    calls = edges(project, "calls")
    key = ("symbol:billing:app/service.py::run", "symbol:billing:app/models.py::Repo.save")
    assert calls[key]["status"] == "introspected" and calls[key]["conf"] == 1.0
    assert ("symbol:billing:app/service.py::run", "symbol:billing:app/service.py::cb") in calls
    refs = edges(project, "references")
    assert ("symbol:billing:app/service.py::cb", "symbol:billing:app/models.py::Repo") in refs
    code = next(s for s in report["coverage"]["sources"] if s["id"] == "billing")["code"]
    assert code["extractors"]["python-ast"] == 2 and code["scip"] == {"items": 2, "without": 0}
    assert code["warning"] is None


def test_typescript_interface_methods_and_implements_are_edges(tmp_path, kg_env, no_graphify):
    project = make(tmp_path, kg_env, "{backend: scip}", {"web/checkout.ts": TS}, {"index.scip": typescript_index()})
    built.build(project)
    nodes = {n["id"] for n in built.nodes_of(project) if n["kind"] == "Symbol"}
    assert {short(n) for n in nodes} == {
        "web/checkout.ts::Payer",
        "web/checkout.ts::Payer.pay",
        "web/checkout.ts::Checkout",
        "web/checkout.ts::Checkout.pay",
        "web/checkout.ts::charge",
    }
    implements = edges(project, "implements")
    assert (
        "symbol:billing:web/checkout.ts::Checkout",
        "symbol:billing:web/checkout.ts::Payer",
    ) in implements
    assert ("symbol:billing:web/checkout.ts::Checkout.pay", "symbol:billing:web/checkout.ts::Payer.pay") in implements
    calls = edges(project, "calls")
    pay = ("symbol:billing:web/checkout.ts::charge", "symbol:billing:web/checkout.ts::Payer.pay")
    assert calls[pay]["status"] == "introspected"
    assert ("symbol:billing:web/checkout.ts::charge", "symbol:billing:web/checkout.ts::Payer") in edges(
        project, "references"
    )


def test_auto_reads_a_default_index_and_falls_back_cleanly_without_one(tmp_path, kg_env, no_graphify):
    with_index = python_project(tmp_path / "a", kg_env, code="{backend: auto}")
    built.build(with_index)
    assert ("symbol:billing:app/service.py::run", "symbol:billing:app/models.py::Repo.save") in edges(
        with_index, "calls"
    )
    without = python_project(tmp_path / "b", kg_env, code="{backend: auto}", scip={})
    report = built.build(without).to_json()
    assert report["ok"] and report["warnings"] == []
    code = next(s for s in report["coverage"]["sources"] if s["id"] == "billing")["code"]
    assert code["extractors"] == {"python-ast": 2} and "scip" not in code
    assert ("symbol:billing:app/service.py::run", "symbol:billing:app/models.py::Repo.save") not in edges(
        without, "calls"
    )


def test_explicit_scip_without_an_index_reads_with_python_ast_and_says_so(tmp_path, kg_env, no_graphify, monkeypatch):
    monkeypatch.setattr(scip_index.shutil, "which", lambda _tool: None)  # no scip-python, no scip-typescript
    project = python_project(tmp_path, kg_env, code="{backend: scip, scip_index: gone.scip}", scip={})
    report = built.build(project).to_json()
    assert report["ok"]
    code = next(s for s in report["coverage"]["sources"] if s["id"] == "billing")["code"]
    assert code["extractors"] == {"python-ast": 2}
    assert code["scip"] == {"items": 0, "without": 2}
    assert code["warning"] == report["warnings"][0].removeprefix("source billing: ")
    assert "no SCIP index data" in code["warning"]
    assert ("symbol:billing:app/models.py::Repo", "symbol:billing:app/models.py::Repo.save") not in edges(
        project, "calls"
    )


# --- the manifest --------------------------------------------------------------------------------------------------


def test_schema_takes_scip_and_stays_compatible():
    schema = json.loads(SCHEMA.read_text())
    code = schema["$defs"]["source"]["properties"]["code"]["properties"]
    assert set(code["backend"]["enum"]) == {"auto", "python-ast", "graphify-ast", "scip", "none"}
    assert code["scip_index"]["type"] == "string"
    jsonschema = pytest.importorskip("jsonschema")
    source = schema["$defs"]["source"]
    for declared in ({"backend": "auto"}, {"backend": "scip", "scip_index": "build/index.scip"}, {}):
        jsonschema.validate({"id": "s", "connector": "git", "code": declared}, source)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"id": "s", "connector": "git", "code": {"backend": "scip", "other": 1}}, source)
