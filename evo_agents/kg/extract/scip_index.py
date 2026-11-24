"""``scip`` code backend, the reading side: a SCIP index (https://github.com/sourcegraph/scip) turned into the compact,
type-resolved data one code item carries in ``props.scip``.

A connector of a source declaring ``code: {backend: scip}`` (or ``auto`` with an index in the tree) reads the index
once per run, here, and ``Scip.annotate`` puts on each code item what the index says of its file. The hub never runs
a compiler: the build reads only ``props.scip`` (``extract.scip_facts``).

The index is a protobuf message. ``parse_scip_bytes`` decodes the few fields the graph needs straight from the wire
format (``Index``, ``Document``, ``SymbolInformation``, ``Relationship``, ``Occurrence``), without generated code;
``parse_scip_json`` reads the same message as protobuf JSON, camelCase or snake_case.

The data of a file (``props.scip``)::

    {"defs": [[symbol, qualname, line, end_line], ...],     definitions made in the file, lines from 1
     "refs": [[caller symbol or "", target symbol, line], ...],  a use of a symbol by a definition of the file
     "impl": [[symbol, parent symbol], ...]}                the file's symbols that implement another

A symbol is the SCIP symbol string. ``refs`` holds each (caller, target) pair once, at its first line: an
occurrence the index resolved through an untyped variable, a loop variable, a ``with ... as`` or a callback is an
occurrence of the method's own symbol like any other. Local symbols (``local 7``) are not kept.

Tools: ``scip-python`` (basedpyright) and ``scip-typescript`` index a tree on this machine. They run only for
``backend: scip``, or for ``auto`` when ``EVO_KG_SCIP_TOOLS`` is set; a missing tool, a failed run or a missing index
leaves the items without data, which the build reads with ``python-ast`` or ``graphify-ast``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

DEFAULT_INDEXES = ("index.scip", ".scip/index.scip")
MAX_REFS = 4000  # references one file keeps; a generated file past it keeps its first ones
TOOL_TIMEOUT = 600
TOOLS = {  # the command that indexes a tree into the file named after --output, by language of the files it reads
    "python": ("scip-python", ["index", ".", "--project-name", "{name}", "--output", "{out}"]),
    "typescript": ("scip-typescript", ["index", "--output", "{out}"]),
}
SUFFIXES = {"python": (".py",), "typescript": (".ts", ".tsx", ".js", ".jsx", ".mjs")}
DEFINITION_ROLE = 0x1


# ---------------------------------------------------------------------------------------------
# protobuf wire format


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    shift = value = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7


def _fields(data: bytes):
    """(field number, wire type, value) of each field of a message; a length-delimited value is its bytes."""
    pos, end = 0, len(data)
    while pos < end:
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 2:
            size, pos = _varint(data, pos)
            value, pos = data[pos : pos + size], pos + size
        elif wire == 1:
            value, pos = int.from_bytes(data[pos : pos + 8], "little"), pos + 8
        elif wire == 5:
            value, pos = int.from_bytes(data[pos : pos + 4], "little"), pos + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield number, wire, value


def _packed(value) -> list[int]:
    """A repeated int32 field: packed (one length-delimited value) or, from an older writer, one varint a field."""
    if isinstance(value, int):
        return [value]
    out, pos = [], 0
    while pos < len(value):
        number, pos = _varint(value, pos)
        out.append(number - (1 << 64) if number >= 1 << 63 else number)
    return out


def _blob(value: int | bytes) -> bytes:
    """A length-delimited value; a field of another wire type where one is expected reads as empty."""
    return value if isinstance(value, bytes) else b""


def _text(value: int | bytes) -> str:
    return _blob(value).decode("utf-8", "replace")


def _relationship(data: int | bytes) -> dict:
    out = {"symbol": "", "implementation": False}
    for number, _, value in _fields(_blob(data)):
        if number == 1:
            out["symbol"] = _text(value)
        elif number == 3:
            out["implementation"] = bool(value)
    return out


def _symbol_information(data: int | bytes) -> dict:
    out: dict = {"symbol": "", "relationships": []}
    for number, _, value in _fields(_blob(data)):
        if number == 1:
            out["symbol"] = _text(value)
        elif number == 4:
            out["relationships"].append(_relationship(value))
    return out


def _occurrence(data: int | bytes) -> dict:
    out: dict = {"range": [], "symbol": "", "roles": 0, "enclosing": []}
    for number, _, value in _fields(_blob(data)):
        if number == 1:
            out["range"] += _packed(value)
        elif number == 2:
            out["symbol"] = _text(value)
        elif number == 3:
            out["roles"] = value
        elif number == 7:
            out["enclosing"] += _packed(value)
    return out


def _document(data: int | bytes) -> dict:
    out: dict = {"path": "", "language": "", "occurrences": [], "symbols": []}
    for number, _, value in _fields(_blob(data)):
        if number == 1:
            out["path"] = _text(value)
        elif number == 2:
            out["occurrences"].append(_occurrence(value))
        elif number == 3:
            out["symbols"].append(_symbol_information(value))
        elif number == 4:
            out["language"] = _text(value)
    return out


def parse_scip_bytes(data: bytes) -> dict:
    """The documents of a SCIP ``Index`` message: ``{"documents": [{"path", "language", "occurrences", "symbols"}]}``,
    an occurrence being ``{"range", "symbol", "roles", "enclosing"}`` and a symbol ``{"symbol", "relationships"}``."""
    return {"documents": [_document(v) for number, wire, v in _fields(data) if number == 2 and wire == 2]}


def _pick(obj: dict, *names: str, default=None):
    for name in names:
        if name in obj:
            return obj[name]
    return default


def parse_scip_json(text: str) -> dict:
    """The same shape as ``parse_scip_bytes`` from a SCIP index written as protobuf JSON."""
    documents = []
    for doc in json.loads(text).get("documents") or []:
        occurrences = [
            {
                "range": list(o.get("range") or []),
                "symbol": o.get("symbol") or "",
                "roles": int(_pick(o, "symbolRoles", "symbol_roles", default=0) or 0),
                "enclosing": list(_pick(o, "enclosingRange", "enclosing_range", default=[]) or []),
            }
            for o in doc.get("occurrences") or []
        ]
        symbols = [
            {
                "symbol": s.get("symbol") or "",
                "relationships": [
                    {
                        "symbol": r.get("symbol") or "",
                        "implementation": bool(_pick(r, "isImplementation", "is_implementation", default=False)),
                    }
                    for r in s.get("relationships") or []
                ],
            }
            for s in doc.get("symbols") or []
        ]
        path = _pick(doc, "relativePath", "relative_path", default="")
        documents.append(
            {"path": path, "language": doc.get("language") or "", "occurrences": occurrences, "symbols": symbols}
        )
    return {"documents": documents}


def load_index(path: Path) -> dict:
    """An index file: protobuf JSON when it is a ``.json`` file, protobuf binary otherwise."""
    data = path.read_bytes()
    return parse_scip_json(data.decode("utf-8")) if path.suffix == ".json" else parse_scip_bytes(data)


# ---------------------------------------------------------------------------------------------
# symbols


def descriptors(symbol: str) -> list[tuple[str, str]]:
    """(name, suffix) of each descriptor of a SCIP symbol after its scheme, manager, package and version: the
    suffix is ``/`` for a namespace, ``#`` for a type, ``.`` for a term, ``().`` for a method, ``:`` for a meta."""
    parts = symbol.split(" ", 4)
    if len(parts) < 5:
        return []
    text, out, i = parts[4], [], 0
    while i < len(text):
        name, i = _name(text, i)
        start = i
        if text.startswith("(", i):
            i = text.find(")", i) + 1 or len(text)
        suffix = text[i : i + 1]
        i += 1
        out.append((name, "()." if start != i - 1 and suffix == "." else suffix))
    return out


def _name(text: str, i: int) -> tuple[str, int]:
    if text.startswith("`", i):
        out, i = [], i + 1
        while i < len(text):
            if text[i] == "`":
                if text[i + 1 : i + 2] == "`":
                    out.append("`")
                    i += 2
                    continue
                return "".join(out), i + 1
            out.append(text[i])
            i += 1
        return "".join(out), i
    start = i
    while i < len(text) and text[i] not in "/#.:([!":
        i += 1
    return text[start:i], i


def qualname_of(symbol: str) -> str | None:
    """The name a symbol has inside its file: the types, terms and methods of its descriptors joined by dots
    (``Checkout.pay`` for ``... src/`checkout.ts`/Checkout#pay().``); None for a local symbol or a bare namespace."""
    if symbol.startswith("local "):
        return None
    names = [name for name, suffix in descriptors(symbol) if suffix in ("#", ".", "().") and name]
    return ".".join(names) or None


def symbol_kind(symbol: str) -> str:
    """``function`` for a method or function, ``class`` for a type, ``variable`` for a term, from the last suffix."""
    parsed = descriptors(symbol)
    suffix = parsed[-1][1] if parsed else ""
    return {"().": "function", "#": "class", ".": "variable"}.get(suffix, "other")


# ---------------------------------------------------------------------------------------------
# the compact data of a file


def _span(occurrence: dict) -> tuple[int, int]:
    """(first line, last line) of an occurrence's enclosing range, else of its own range; lines from 1."""
    box = occurrence["enclosing"] or occurrence["range"]
    if not box:
        return 0, 0
    return box[0] + 1, (box[2] if len(box) == 4 else box[0]) + 1


def _definitions(doc: dict) -> list[tuple[str, int, int, str]]:
    """(symbol, line, end line, qualname) of each definition of a document, by line."""
    defs = []
    for o in doc["occurrences"]:
        qual = qualname_of(o["symbol"])
        if o["roles"] & DEFINITION_ROLE and qual and o["range"]:
            line = o["range"][0] + 1
            end = _span(o)[1] if o["enclosing"] else 0
            defs.append((o["symbol"], line, end, qual))
    defs.sort(key=lambda d: (d[1], d[3]))
    out = []
    for i, (symbol, line, end, qual) in enumerate(defs):
        if not end:  # no enclosing range: up to the line before the next definition of the same depth or less
            depth = qual.count(".")
            end = next((n[1] - 1 for n in defs[i + 1 :] if n[3].count(".") <= depth), 10**9)
        out.append((symbol, line, max(end, line), qual))
    return out


def _enclosing(defs: list[tuple[str, int, int, str]], line: int) -> str:
    """The symbol of the innermost definition holding ``line``; "" at the top level of the file."""
    best = ""
    for symbol, start, end, _ in defs:
        if start <= line <= end:
            best = symbol
    return best


def file_data(doc: dict) -> dict | None:
    """The compact data of one document, None when it defines and uses nothing."""
    defs = _definitions(doc)
    refs: dict[tuple[str, str], int] = {}
    for o in doc["occurrences"]:
        target = o["symbol"]
        if o["roles"] & DEFINITION_ROLE or not o["range"] or not target or target.startswith("local "):
            continue
        line = o["range"][0] + 1
        refs.setdefault((_enclosing(defs, line), target), line)
    impl = sorted(
        {
            (s["symbol"], r["symbol"])
            for s in doc["symbols"]
            for r in s["relationships"]
            if r["implementation"] and r["symbol"] and not s["symbol"].startswith("local ")
        }
    )
    if not (defs or refs or impl):
        return None
    ordered = sorted(((c, t, ln) for (c, t), ln in refs.items()), key=lambda r: (r[2], r[0], r[1]))[:MAX_REFS]
    return {
        "defs": [[s, q, a, b if b < 10**9 else a] for s, a, b, q in defs],
        "refs": [list(r) for r in ordered],
        "impl": [list(p) for p in impl],
    }


def files_data(index: dict) -> dict[str, dict]:
    """The compact data of each document of an index, by relative path."""
    out = {}
    for doc in index["documents"]:
        data = file_data(doc)
        if data and doc["path"]:
            out[doc["path"]] = data
    return out


# ---------------------------------------------------------------------------------------------
# finding or making the index


def _tools_allowed(backend: str) -> bool:
    return backend == "scip" or bool(os.environ.get("EVO_KG_SCIP_TOOLS"))


def _run_tool(root: Path, language: str, name: str, out: Path) -> bool:
    """Index the tree of ``root`` with the tool of ``language`` when it is on the path; False when it is not or fails."""
    tool, args = TOOLS[language]
    if shutil.which(tool) is None:
        return False
    argv = [tool, *(a.format(name=name, out=out) for a in args)]
    try:
        done = subprocess.run(argv, cwd=root, capture_output=True, timeout=TOOL_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and out.is_file()


def _generated(root: Path, backend: str) -> dict[str, dict]:
    """The data of the files the local tools index, each tool apart and each only when its files are in the tree."""
    out: dict[str, dict] = {}
    if not _tools_allowed(backend):
        return out
    for language, suffixes in SUFFIXES.items():
        if not any(
            p.suffix in suffixes for p in root.rglob("*") if "node_modules" not in p.parts and ".git" not in p.parts
        ):
            continue
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "index.scip"
            if _run_tool(root, language, root.name, target):
                out.update(files_data(load_index(target)))
    return out


def find_index(root: Path, source_code: dict | None) -> Path | None:
    """The index file of a source: ``code.scip_index`` below ``root``, else the first of the default names."""
    declared = (source_code or {}).get("scip_index")
    names = [declared] if declared else list(DEFAULT_INDEXES)
    for name in names:
        path = (root / name).resolve()
        if path.is_file() and path.is_relative_to(root.resolve()):
            return path
    return None


class Scip:
    """The SCIP data one run of a file connector reads once: ``annotate`` puts a code item's data in its props."""

    def __init__(self, source: dict, root: Path):
        code = source.get("code") or {}
        self.backend = code.get("backend") or "auto"
        self.files: dict[str, dict] = {}
        self.note: str | None = None
        if self.backend not in ("scip", "auto"):
            return
        try:
            path = find_index(root, code)
            if path is not None:
                self.files = files_data(load_index(path))
            elif self.backend == "scip" and code.get("scip_index"):
                self.note = f"scip index {code['scip_index']} not found"
            if not self.files:
                self.files = _generated(root, self.backend)
        except (OSError, ValueError, IndexError, UnicodeDecodeError) as exc:
            self.note = f"scip index unreadable: {exc}"

    def annotate(self, item: dict) -> dict:
        """``item`` with ``props.scip`` when it is a code file the index covers; as it was otherwise."""
        path = (item.get("props") or {}).get("path") or ""
        data = self.files.get(path) if item.get("kind") == "code" else None
        if data is not None and PurePosixPath(path).suffix:
            item["props"]["scip"] = data
        return item
