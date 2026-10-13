"""Class hierarchy evidence for the graphify code backend: each class's bases, and the calls made through
``this``, ``self`` or ``super``.

graphify reports a class's supertypes as ``inherits``, ``implements`` and ``mixes_in`` edges, to a node of
the same file or to a bare name (Ruby's ``include`` as a raw call marked ``is_mixin``), and none for JS and
TS, whose ``extends`` and ``implements`` clauses are read off the class header here. Each base becomes a
list of ``via`` entries, as a call does: ``["local", "", qualname]`` first when this file defines the
name, then what the file's imports and packages put in scope (``xref.name_via``).

A call through ``this`` or ``self`` becomes ``["self", class, name]``, one through ``super``
``["super", class, name]``: the link stage looks ``name`` up along the chain of ``class``, a class of this
file. graphify gives the receiver for Java, C# and Python; for the other languages it gives none, and the
call's line says which keyword the call went through. graphify resolves ``super.m()`` inside the file to
the class's own ``m``; such an edge becomes a ``super`` reference instead. A bare call in a method of a
Java, Kotlin, C# or Swift class may be a member the class inherits, so its ``via`` tries that first.
"""

from __future__ import annotations

import re

from evo_agents.kg.extract import xref

SUPERTYPES = {"mixes_in": 0, "inherits": 1, "implements": 2}  # a trait's methods come before the parent's
JS_FAMILY = {"typescript", "javascript", "vue"}
IMPLICIT_THIS = {"java", "kotlin", "csharp", "swift"}
SELF_RECEIVERS = {"this", "self", "$this"}
SUPER_RECEIVERS = {"super", "base"}
GENERIC = r"(?:<(?:[^<>{}]|<[^<>{}]*>)*>)?"
JS_CLASS = re.compile(
    rf"\bclass\s+([A-Za-z_$][\w$]*)\s*{GENERIC}\s*(?:extends\s+([\w$.]+)\s*{GENERIC})?"
    rf"\s*(?:implements\s+([^{{;]+?))?\s*\{{"
)
JS_INTERFACE = re.compile(rf"\binterface\s+([A-Za-z_$][\w$]*)\s*{GENERIC}\s*extends\s+([^{{;]+?)\s*\{{")
THIS_DOT, SUPER_DOT = r"\bthis\s*\.\s*", r"\bsuper\s*\.\s*"
# Per language, the patterns a member's name follows in a call through this or self, and through super.
KEYWORDS: dict[str, tuple[str, str | None]] = {
    **dict.fromkeys(("typescript", "javascript", "vue", "java"), (THIS_DOT, SUPER_DOT)),
    "kotlin": (r"\bthis(?:@\w+)?\s*\.\s*", r"\bsuper(?:<[\w.]+>)?(?:@\w+)?\s*\.\s*"),
    "csharp": (THIS_DOT, r"\bbase\s*\.\s*"),
    "swift": (r"\bself\s*\.\s*", SUPER_DOT),
    "python": (r"\bself\s*\.\s*", r"\bsuper\s*\([^)]*\)\s*\.\s*"),
    "php": (r"(?:\$this\s*->|\b(?:self|static)\s*::)\s*", r"\bparent\s*::\s*"),
    "ruby": (r"\bself\s*\.\s*", None),
    "rust": (r"\bself\s*\.\s*", None),
}
PHP_STATIC = {"parent": "super", "self": "self", "static": "self"}  # graphify names the keyword as the callee
RUBY_SUPER = re.compile(r"(?<![\w.:@$])super\b")


def _strip_generics(text: str) -> str:
    while True:
        stripped = re.sub(r"<[^<>]*>", "", text)
        if stripped == text:
            return text
        text = stripped


def _names(clause: str | None) -> list[list[str]]:
    """``A, ns.B<T>`` -> [[A], [ns, B]]."""
    out = []
    for part in _strip_generics(clause or "").split(","):
        part = part.strip()
        if re.fullmatch(r"[\w$]+(?:\.[\w$]+)*", part):
            out.append(part.split("."))
    return out


class Members:
    """One file's classes: where a method belongs, what each class extends, and the calls through
    ``this``, ``self`` and ``super`` of its methods."""

    def __init__(self, text: str, path: str, symbols: list[dict], scopes: xref.Scopes):
        self.lang = xref.language(path) or ""
        self.lines = text.splitlines()
        self.text = text
        self.symbols = symbols
        self.scopes = scopes
        self.quals = {s["qualname"] for s in symbols}
        self.owner = {s["qualname"]: s["parent"] for s in symbols if s["kind"] == "method" and s["parent"]}
        self.lines_of = {s["qualname"]: s["line"] for s in symbols}
        self.this_pat, self.super_pat = KEYWORDS.get(self.lang, (None, None))
        self._called: dict[tuple, set[str]] = {}

    def line(self, number: int) -> str:
        return self.lines[number - 1] if 0 < number <= len(self.lines) else ""

    # -- bases -------------------------------------------------------------------------------------

    def bases(self, result: dict, quals: dict[str, str]) -> dict[str, list[list[list]]]:
        """Each class's bases, nearest first, as ``via`` lists; a base without evidence is left out."""
        if self.lang == "go":
            return {}  # Go has no inheritance: embedding a struct is not subclassing it
        found = self._js_bases() if self.lang in JS_FAMILY else self._graph_bases(result, quals)
        out = {}
        for qual, chains in found.items():
            vias = [via for chain in chains if (via := self._base_via(chain))]
            if vias:
                out[qual] = vias
        return out

    def _base_via(self, chain: list[str]) -> list[list]:
        joined = ".".join(chain)
        local = [["local", "", joined]] if joined in self.quals else []
        return local + xref.name_via(chain, self.scopes)

    def _js_bases(self) -> dict[str, list[list[str]]]:
        classes = [s for s in self.symbols if s["kind"] == "class"]
        out: dict[str, list[list[str]]] = {}
        for pattern, clauses in ((JS_CLASS, (2, 3)), (JS_INTERFACE, (2,))):
            for m in pattern.finditer(self.text):
                line = self.text.count("\n", 0, m.start()) + 1
                named = [s for s in classes if s["name"] == m.group(1)]
                if not named:
                    continue
                owner = min(named, key=lambda s: (abs(s["line"] - line), s["line"]))["qualname"]
                for group in clauses:
                    out.setdefault(owner, []).extend(_names(m.group(group)))
        return out

    def _graph_bases(self, result: dict, quals: dict[str, str]) -> dict[str, list[list[str]]]:
        classes = {s["qualname"] for s in self.symbols if s["kind"] == "class"}
        edges = [
            (SUPERTYPES[e["relation"]], i, e)
            for i, e in enumerate(result.get("edges", []))
            if e.get("relation") in SUPERTYPES and quals.get(e["source"]) in classes
        ]
        nodes = {n["id"]: n for n in result.get("nodes", [])}
        out: dict[str, list[list[str]]] = {}
        for _, _, e in sorted(edges, key=lambda t: t[:2]):
            owner = quals[e["source"]]
            if e["target"] in quals:
                out.setdefault(owner, []).append(quals[e["target"]].split("."))
            else:
                out.setdefault(owner, []).append(_reference(e, nodes.get(e["target"]) or {}))
        mixins: dict[str, list[list[str]]] = {}
        for call in result.get("raw_calls") or []:
            owner = quals.get(call.get("caller_nid"))
            if call.get("is_mixin") and owner in classes and call.get("callee"):
                mixins.setdefault(owner, []).append(re.split(r"::|\.", call["callee"]))
        for owner, modules in mixins.items():  # Ruby looks in the modules a class includes, last first, then up
            out[owner] = [*reversed(modules), *out.get(owner, [])]
        return {owner: [c for c in chains if c and all(c)] for owner, chains in out.items()}

    # -- calls through this, self and super ---------------------------------------------------------

    def _has(self, pattern: str | None, line_no: int, name: str) -> bool:
        """Whether the line calls ``name`` through the keyword ``pattern`` matches."""
        if not pattern:
            return False
        if (pattern, line_no) not in self._called:
            self._called[pattern, line_no] = set(re.findall(pattern + r"([\w$]+)", self.line(line_no)))
        return name in self._called[pattern, line_no]

    def call_vias(self, call: dict, caller: str, line_no: int) -> list[list[list]]:
        """The ``via`` lists of one raw call graphify could not resolve in the file."""
        cls, name = self.owner.get(caller), call.get("callee") or ""
        if call.get("is_mixin") or not name:
            return []
        if cls and self.lang == "php" and name in PHP_STATIC and not call.get("is_member_call"):
            members = re.findall(rf"\b{name}\s*::\s*(\w+)\s*\(", self.line(line_no))
            return [[[PHP_STATIC[name], cls, m]] for m in dict.fromkeys(members)]
        receiver = call.get("receiver")
        if cls and (receiver in SELF_RECEIVERS or call.get("rust_self_type")):
            return [[["self", cls, name]]]
        if cls and receiver in SUPER_RECEIVERS:
            return [[["super", cls, name]]]
        named = any(call.get(k) for k in ("receiver", "receiver_type", "kotlin_object_receiver", "qualified_prefix"))
        if cls and call.get("is_member_call") and not named:
            kinds = [k for k, p in (("self", self.this_pat), ("super", self.super_pat)) if self._has(p, line_no, name)]
            return [[[kind, cls, name]] for kind in kinds]
        via = xref.call_via(call, self.scopes)
        if cls and self.lang in IMPLICIT_THIS and not call.get("is_member_call") and not named:
            via = [["self", cls, name], *via]  # a bare call in a method: a member, inherited or not, comes first
        return [via] if via else []

    def member_edge(self, caller: str, callee: str, line_no: int) -> tuple[list[list[list]], bool]:
        """For a call graphify resolved in the file: the references to look up along the caller's class
        chain instead, and whether graphify's edge also stands. graphify matches a member by its name in the
        file, so it takes ``super.m()`` for the class's own ``m`` and ``this.m()`` for an ``m`` of any class
        of the file. A call the line makes through super, or through this to a method of another class,
        goes to the chain; the edge stands when the line also calls the name another way."""
        cls = self.owner.get(caller)
        if not cls:
            return [], True
        name = callee.rpartition(".")[2]
        own = self.owner.get(callee) == cls
        through_this = self._has(self.this_pat, line_no, name)
        vias = [[["super", cls, name]]] if self._has(self.super_pat, line_no, name) else []
        if through_this and not own:
            vias.append([["self", cls, name]])
        if not vias:
            return [], True
        bare = line_no != self.lines_of.get(callee) and self._bare(self.line(line_no), name)  # not its header
        return vias, (through_this and own) or bare

    def _bare(self, line: str, name: str) -> bool:
        return re.search(rf"(?<![\w$.>:]){re.escape(name)}\s*\(", line) is not None

    def ruby_supers(self) -> list[dict]:
        """``super`` in a Ruby method calls the method of the same name up the chain; graphify reports none.
        A method's body runs from its ``def`` line to the line before the next definition."""
        if self.lang != "ruby":
            return []
        starts = sorted({s["line"] for s in self.symbols})
        out = []
        for s in self.symbols:
            cls = self.owner.get(s["qualname"])
            if not cls:
                continue
            end = next((n for n in starts if n > s["line"]), len(self.lines) + 1)
            for number in range(s["line"], end):
                if RUBY_SUPER.search(self.line(number).split("#", 1)[0]):
                    out.append({"caller": s["qualname"], "line": number, "via": [["super", cls, s["name"]]]})
                    break
        return out


def _reference(edge: dict, node: dict) -> list[str]:
    """The name a supertype edge points to outside the file: graphify's label, with the qualifier C# and
    Ruby keep in the edge's metadata."""
    meta = edge.get("metadata") or {}
    if meta.get("ruby_superclass_ref"):
        return re.split(r"::", meta["ruby_superclass_ref"].lstrip(":"))
    label = re.sub(r"<.*$", "", str(meta.get("ref_token") or node.get("label") or ""))
    qualifier = str(meta.get("ref_qualifier") or "").removeprefix("global::")
    return [p for p in re.split(r"\.|::|\\", f"{qualifier}.{label}" if qualifier else label) if p]
