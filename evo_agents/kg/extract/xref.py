"""Cross-file call evidence for the graphify code backend: what one file's imports put in scope.

graphify, run on one file, reports the calls it could not resolve inside that file (``raw_calls``: the
callee name, its receiver, sometimes the receiver's type). This module reads the same file's imports,
package declaration and ``require``/``source`` lines, and turns each raw call into ``via`` entries,
``[kind, value, qualname]``, that the link stage tries in order against the identifier index:

    ["tsspec", "./a", "helper"]           a JS or TS module specifier, relative or a tsconfig alias
    ["paths", ["lib/a.rb", ...], "f"]     candidate files (Ruby require, PHP include, Shell source, Rust use)
    ["package", "jvm:com.acme.a", "A.f"]  every file declaring that package or namespace
    ["gopkg", "example.com/m/a", "F"]     a Go import path, matched to a directory of the repo

A call gets a ``via`` only with evidence: the name it calls is bound by an import of this file, or the
language puts it in scope without an import (Go, Java, Kotlin, C# and PHP: the file's own package or
namespace; Ruby, PHP, Shell: a file it requires or sources). A name without such evidence gets none, so
the build never links two files on a shared name alone. Everything here reads one file's text.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
LANGUAGE = {
    ".py": "python",
    **dict.fromkeys((".ts", ".tsx", ".mts", ".cts"), "typescript"),
    **dict.fromkeys((".js", ".jsx", ".mjs", ".cjs"), "javascript"),
    ".vue": "vue",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".dart": "dart",
    ".sh": "shell",
    ".sql": "sql",
}
# The family a language's package names live in: Kotlin and Java share the JVM's.
FAMILY = {"java": "jvm", "kotlin": "jvm", "csharp": "cs", "php": "php"}

ES_IMPORT = re.compile(r"""\bimport\s+(?:type\s+)?([\w$*{},\s]+?)\s+from\s+['"]([^'"]+)['"]""")
REQUIRE = re.compile(r"""\b(?:const|let|var)\s+(\{[^}]*\}|[\w$]+)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)""")
INJECT = re.compile(r"""\b([\w$]+)\s*(?::\s*[\w$.<>]+\s*)?=\s*inject\(\s*([A-Z][\w$]*)""")
TYPED = re.compile(
    r"""\b(?:private|public|protected|readonly|let|const|var)\s+(?:readonly\s+)?([\w$]+)\s*[!?]?\s*:\s*"""
    r"""([A-Z][\w$]*)\b(?!\s*[<.\[])"""
)
JVM_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)", re.M)
JVM_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?(?:\s+as\s+(\w+))?\s*;?\s*$", re.M)
CS_NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
CS_USING = re.compile(r"^\s*(?:global\s+)?using\s+(static\s+)?(?:(\w+)\s*=\s*)?([\w.]+)\s*;", re.M)
PHP_NAMESPACE = re.compile(r"^\s*namespace\s+([\w\\]+)\s*[;{]", re.M)
PHP_USE = re.compile(r"^\s*use\s+(?:function\s+|const\s+)?([\w\\]+?)(?:\\\{([^}]*)\}|\s+as\s+(\w+))?\s*;", re.M)
PHP_REQUIRE = re.compile(r"""\b(?:require|include)(?:_once)?\s*\(?\s*(__DIR__\s*\.\s*)?['"]([^'"]+\.php)['"]""")
RUBY_REQUIRE = re.compile(r"""^\s*(require_relative|require)\s*\(?\s*['"]([^'"]+)['"]""", re.M)
RUST_USE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+([^;]+);", re.M)
SHELL_SOURCE = re.compile(r"^\s*(?:source|\.)\s+(\S.*?)\s*(?:[;#].*)?$", re.M)
SHELL_PREFIX = re.compile(r"^(?:\$\([^)]*\)|\$\{[^}]*\}|\$\w+)/")


def language(path: str) -> str | None:
    return LANGUAGE.get(PurePosixPath(path).suffix.lower())


def module_files(target: str) -> list[str]:
    """Files a JS or TS module specifier, already joined to its directory, may name."""
    stem = re.sub(r"\.(js|jsx|mjs|cjs)$", "", target)
    candidates = [target] if PurePosixPath(target).suffix in (*JS_SUFFIXES, ".vue") else []
    candidates += [stem + s for s in (".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs", ".vue")]
    candidates += [f"{stem}/index{s}" for s in (".ts", ".tsx", ".js")]
    return list(dict.fromkeys(candidates))


@dataclass
class Scopes:
    """What one file puts in scope. ``names``: a local name an import binds, to the heads it stands for,
    each ``[kind, value, qualname prefix]``. ``implicit``: the scopes any other name falls back to, each
    ``[kind, value, prefix]``, in the language's order of precedence. ``types``: a receiver's declared
    type (JS, TS). ``packages``: the packages or namespaces the file declares, as index keys, each with the
    line it starts on: a definition belongs to the last one declared above it."""

    family: str | None = None
    names: dict[str, list[list]] = field(default_factory=dict)
    implicit: list[list] = field(default_factory=list)
    types: dict[str, str] = field(default_factory=dict)
    packages: list[tuple[str, int]] = field(default_factory=list)

    def package_at(self, line: int) -> str | None:
        """The package a definition on this line belongs to."""
        above = [key for key, start in self.packages if start <= line]
        return above[-1] if above else (self.packages[0][0] if self.packages else None)

    def bind(self, name: str, heads: list[list]) -> None:
        if name and heads:
            self.names.setdefault(name, heads)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _splits(family: str, fqn: list[str], rest: list[str]) -> list[list]:
    """``a.b.C`` + ``m``: the package may end at any dot of a qualified name; the longest is tried first."""
    return [["package", f"{family}:{'.'.join(fqn[:i])}", ".".join(fqn[i:] + rest)] for i in range(len(fqn) - 1, 0, -1)]


# -- JS, TS, Vue -------------------------------------------------------------------------------------


def _es_clause(clause: str, spec: str, scopes: Scopes) -> None:
    named = re.search(r"\{([^}]*)\}", clause)
    if named:
        for part in named.group(1).split(","):
            name, _, alias = (p.strip() for p in re.sub(r"^\s*type\s+", "", part).partition(" as "))
            scopes.bind(alias or name, [["tsspec", spec, name]])
        clause = clause[: named.start()] + clause[named.end() :]
    star = re.search(r"\*\s*as\s+([\w$]+)", clause)
    if star:
        scopes.bind(star.group(1), [["tsspec", spec, ""]])
        clause = clause[: star.start()] + clause[star.end() :]
    default = clause.strip(" ,\n\t")
    if re.fullmatch(r"[\w$]+", default):
        # A default import names the module's default export, by convention the class or function of that name.
        scopes.bind(default, [["tsspec", spec, default]])


def _js(text: str, scopes: Scopes) -> None:
    for m in ES_IMPORT.finditer(text):
        _es_clause(m.group(1), m.group(2), scopes)
    for m in REQUIRE.finditer(text):
        lhs, spec = m.group(1), m.group(2)
        if not lhs.startswith("{"):
            scopes.bind(lhs, [["tsspec", spec, ""]])
            continue
        for part in lhs.strip("{} ").split(","):
            name, _, alias = (p.strip() for p in part.partition(":"))
            scopes.bind(alias or name, [["tsspec", spec, name]])
    for pattern in (INJECT, TYPED):
        for m in pattern.finditer(text):
            scopes.types.setdefault(m.group(1), m.group(2))


# -- Go ----------------------------------------------------------------------------------------------


def _go(path: str, go_imports: dict, scopes: Scopes) -> None:
    for alias, import_path in sorted(go_imports.items()):
        scopes.bind(alias, [["gopkg", import_path, ""]])
    scopes.packages.append((f"go:{posixpath.dirname(path)}", 0))
    scopes.implicit.append(["package", scopes.packages[0][0], ""])  # the files of one directory share a package


# -- Java, Kotlin ------------------------------------------------------------------------------------


def _jvm(text: str, scopes: Scopes) -> None:
    for m in JVM_IMPORT.finditer(text):
        fqn = m.group(2).split(".")
        if not m.group(3):
            scopes.bind(m.group(4) or fqn[-1], _splits("jvm", fqn, []))
            continue
        for i in range(len(fqn), 0, -1):  # a wildcard: a package's classes, or a class's members
            prefix = ".".join(fqn[i:])
            scopes.implicit.append(["package", f"jvm:{'.'.join(fqn[:i])}", f"{prefix}." if prefix else ""])
    package = JVM_PACKAGE.search(text)
    if package:
        scopes.packages.append((f"jvm:{package.group(1)}", 0))
        scopes.implicit.insert(0, ["package", scopes.packages[0][0], ""])  # the own package before wildcards


# -- C# ----------------------------------------------------------------------------------------------


def _csharp(text: str, scopes: Scopes) -> None:
    own: list[str] = []
    for m in CS_NAMESPACE.finditer(text):
        parts = m.group(1).split(".")
        scopes.packages.append((f"cs:{m.group(1)}", _line_of(text, m.start())))
        own += [".".join(parts[:i]) for i in range(len(parts), 0, -1)]  # a namespace sees its parents
    scopes.implicit += [["package", f"cs:{name}", ""] for name in dict.fromkeys(own)]
    for m in CS_USING.finditer(text):
        static, alias, fqn = m.group(1), m.group(2), m.group(3).split(".")
        if alias:
            scopes.bind(alias, _splits("cs", fqn, []))
        elif static:
            scopes.implicit += [[kind, value, f"{qual}."] for kind, value, qual in _splits("cs", fqn, [])]
        else:
            scopes.implicit.append(["package", f"cs:{'.'.join(fqn)}", ""])


# -- PHP ---------------------------------------------------------------------------------------------


def _php_heads(fqn: list[str]) -> list[list]:
    return [["package", "php:" + "\\".join(fqn[:i]), ".".join(fqn[i:])] for i in range(len(fqn) - 1, 0, -1)]


def _php(text: str, path: str, scopes: Scopes) -> None:
    for namespace in PHP_NAMESPACE.finditer(text):
        scopes.packages.append((f"php:{namespace.group(1)}", _line_of(text, namespace.start())))
        scopes.implicit.append(["package", scopes.packages[-1][0], ""])
    for m in PHP_USE.finditer(text):
        base = m.group(1).strip("\\").split("\\")
        if not m.group(2):
            scopes.bind(m.group(3) or base[-1], _php_heads(base))
            continue
        for member in m.group(2).split(","):  # a group use: use App\A\{Repo, Other as O};
            name, _, alias = (p.strip() for p in member.partition(" as "))
            if name:
                fqn = base + name.strip("\\").split("\\")
                scopes.bind(alias or fqn[-1], _php_heads(fqn))
    here = posixpath.dirname(path)
    for m in PHP_REQUIRE.finditer(text):
        target = m.group(2).lstrip("/")
        options = [posixpath.normpath(posixpath.join(here, target))]
        if not m.group(1):  # without __DIR__ PHP also tries the include path, whose root a repo controls
            options.append(posixpath.normpath(target))
        scopes.implicit.append(["paths", list(dict.fromkeys(options)), ""])


# -- Ruby --------------------------------------------------------------------------------------------


def _ruby(text: str, path: str, scopes: Scopes) -> None:
    here = posixpath.dirname(path)
    for m in RUBY_REQUIRE.finditer(text):
        target = m.group(2) if m.group(2).endswith(".rb") else f"{m.group(2)}.rb"
        if m.group(1) == "require_relative":
            options = [posixpath.normpath(posixpath.join(here, target))]
        else:  # the load path: the repo root and lib/ are the entries a repo controls
            options = [posixpath.normpath(target), posixpath.normpath(f"lib/{target}")]
        scopes.implicit.append(["paths", options, ""])


# -- Rust --------------------------------------------------------------------------------------------


def _rust_module(path: str) -> tuple[str, list[str]] | None:
    """The crate root (the nearest ``src`` directory above the file) and the file's module path."""
    parts = PurePosixPath(path).parts
    if "src" not in parts[:-1]:
        return None
    i = len(parts) - 1 - parts[::-1].index("src")
    module = [*parts[i + 1 : -1], PurePosixPath(parts[-1]).stem]
    if module[-1] == "mod" or (len(module) == 1 and module[0] in ("lib", "main")):
        module = module[:-1]
    return "/".join(parts[: i + 1]), module


def _rust_files(root: str, module: list[str]) -> list[str]:
    if not module:
        return [f"{root}/lib.rs", f"{root}/main.rs"]
    joined = "/".join(module)
    return [f"{root}/{joined}.rs", f"{root}/{joined}/mod.rs"]


def _split_top(text: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for ch in text:
        depth += {"{": 1, "}": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    return [p.strip() for p in [*parts, current] if p.strip()]


def _rust_uses(spec: str) -> list[tuple[list[str], str]]:
    """``a::{b, c as d, self}`` -> ([a, b], b), ([a, c], d), ([a], a). A glob keeps ``*`` last."""
    spec = spec.strip()
    group = re.match(r"^(.*?)::\s*\{(.*)\}$", spec, re.S)
    if group:
        head = [s.strip() for s in group.group(1).split("::")]
        out = []
        for part in _split_top(group.group(2)):
            for segments, alias in _rust_uses(part):
                out.append((head, head[-1]) if segments == ["self"] else (head + segments, alias))
        return out
    m = re.match(r"^(.*?)(?:\s+as\s+(\w+))?$", spec, re.S)
    segments = [s.strip() for s in m.group(1).split("::")]
    return [(segments, m.group(2) or segments[-1])]


def _rust(text: str, path: str, scopes: Scopes) -> None:
    where = _rust_module(path)
    if where is None:
        return
    root, own = where
    starts = {"crate": [], "self": own, "super": own[:-1]}
    for m in RUST_USE.finditer(text):
        for segments, alias in _rust_uses(m.group(1)):
            if segments[0] not in starts:
                continue  # an external crate, or a module only the file's `mod` lines would explain
            module = starts[segments[0]] + segments[1:]
            if module and module[-1] == "*":
                scopes.implicit.append(["paths", _rust_files(root, module[:-1]), ""])
            elif module:  # an item of the parent module, or a module itself
                item = ["paths", _rust_files(root, module[:-1]), module[-1]]
                scopes.bind(alias, [item, ["paths", _rust_files(root, module), ""]])


# -- Shell -------------------------------------------------------------------------------------------


def _shell(text: str, path: str, scopes: Scopes) -> None:
    here = posixpath.dirname(path)
    for m in SHELL_SOURCE.finditer(text):
        target = m.group(1).replace('"', "").replace("'", "")
        from_script = False
        while SHELL_PREFIX.match(target):  # "$(dirname "$0")/lib.sh", "${DIR}/lib.sh": the script's directory
            target = SHELL_PREFIX.sub("", target, count=1)
            from_script = True
        if not target or "$" in target:
            continue
        options = [posixpath.normpath(posixpath.join(here, target))]
        if not from_script:  # a bare relative path is relative to the caller's directory: try the repo root too
            options.append(posixpath.normpath(target.lstrip("/")))
        scopes.implicit.append(["paths", list(dict.fromkeys(options)), ""])


def scopes_of(text: str, path: str, result: dict) -> Scopes:
    """What one file's text puts in scope; ``result`` is graphify's run on it, for what it read already."""
    lang = language(path)
    scopes = Scopes(family=FAMILY.get(lang or ""))
    if lang in ("typescript", "javascript", "vue"):
        scopes.types.update((result.get("ts_type_table") or {}).get("table") or {})
        _js(text, scopes)
    elif lang == "go":
        _go(path, result.get("go_imports") or {}, scopes)
    elif lang in ("java", "kotlin"):
        _jvm(text, scopes)
    elif lang == "csharp":
        _csharp(text, scopes)
    elif lang == "php":
        _php(text, path, scopes)
    elif lang == "ruby":
        _ruby(text, path, scopes)
    elif lang == "rust":
        _rust(text, path, scopes)
    elif lang == "shell":
        _shell(text, path, scopes)
    return scopes


def _chain(call: dict, scopes: Scopes) -> list[str] | None:
    """The names a raw call goes through: ``[receiver (or its type), ..., callee]``, or ``[callee]``."""
    callee = call.get("callee")
    if not callee:
        return None
    receiver = (
        call.get("receiver_type")
        or call.get("kotlin_object_receiver")
        or call.get("qualified_prefix")
        or call.get("receiver")
    )
    if receiver:
        receiver = scopes.types.get(receiver, receiver)
        return [*(p for p in re.split(r"\.|::|\\", receiver) if p), callee]
    if call.get("is_member_call"):
        return None  # this.m(), a chained or computed receiver: nothing in the file names the target
    return [callee]


def name_via(chain: list[str], scopes: Scopes) -> list[list]:
    """The ``via`` entries of a type name written in this file, ``Base`` or ``a.b.Base``: what an import binds
    to its head, else the whole name as a fully qualified one, then the scopes the language gives without
    an import. Unlike a call, the last segment is the definition itself, not a member of it."""
    head, rest = chain[0], chain[1:]
    via = []
    for kind, value, prefix in scopes.names.get(head, []):
        qual = ".".join(p for p in (prefix, *rest) if p)
        if qual:
            via.append([kind, value, qual])
    if head in scopes.names:
        return via
    if scopes.family and len(chain) > 1:
        via += _splits(scopes.family, chain, [])
    return via + [[kind, value, prefix + ".".join(chain)] for kind, value, prefix in scopes.implicit]


def call_via(call: dict, scopes: Scopes) -> list[list]:
    """The ``via`` entries of one raw call, most specific first; empty when nothing is evidence."""
    chain = _chain(call, scopes)
    if chain is None:
        return []
    if call.get("import_path"):  # Go: graphify read the package's import path off the selector
        return [["gopkg", call["import_path"], ".".join(chain[1:])]]
    head, rest = chain[0], chain[1:]
    via = []
    for kind, value, prefix in scopes.names.get(head, []):
        qual = ".".join(p for p in (prefix, *rest) if p)
        if qual:
            via.append([kind, value, qual])
    if head in scopes.names:
        return via
    if scopes.family and len(chain) > 2:  # a fully qualified name: com.acme.a.Helper.util()
        via += _splits(scopes.family, chain[:-1], chain[-1:])
    return via + [[kind, value, prefix + ".".join(chain)] for kind, value, prefix in scopes.implicit]
