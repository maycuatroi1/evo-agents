"""What the code graph covers per language: the coverage tables docs, tools and tests read.

One row per language the connectors mark as code (``connectors._files.LANGUAGE_BY_SUFFIX``) in each table:
calls across files, inheritance (``inherits`` edges and calls through this, self or super) and
``overrides`` edges. A ``no`` carries the reason, measured on graphify 0.9.73 with a two-file fixture in
tests/kg/test_cross_file.py and tests/kg/test_inheritance.py, which also check that such a language really
gets no cross-file edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from evo_agents.kg.extract.xref import LANGUAGE


@dataclass(frozen=True)
class Coverage:
    supported: bool
    note: str


CROSS_FILE_CALLS: dict[str, Coverage] = {
    "python": Coverage(
        True,
        "from m import f, import m [as x] then x.f(), from pkg import mod then mod.f(), relative imports,"
        " imports inside a function body, star imports; a parameter or local of the same name hides the import",
    ),
    "typescript": Coverage(
        True,
        "named, default and namespace imports and require(), relative or through the paths and baseUrl of the"
        " nearest tsconfig.json; a receiver typed with an imported class (parameter, field, inject())",
    ),
    "javascript": Coverage(True, "as TypeScript: named, default and namespace imports, require()"),
    "vue": Coverage(True, "the imports of the component's script block, as TypeScript"),
    "go": Coverage(
        True,
        "pkg.F() through an import whose path ends with the package's directory; F() of another file of the"
        " same directory",
    ),
    "java": Coverage(
        True,
        "single-type, wildcard and static imports, fully qualified names, classes of the file's own package;"
        " a receiver whose declared type is such a class",
    ),
    "kotlin": Coverage(True, "imports with or without alias, wildcard imports, the file's own package"),
    "rust": Coverage(
        True,
        "a name brought in by use crate::, self:: or super:: called bare; graphify reports no path calls"
        " such as a::f() or Type::new(), so those link nothing",
    ),
    "ruby": Coverage(True, "a method or Constant.method of a file loaded with require_relative or require"),
    "php": Coverage(True, "use and use function, the file's own namespace, a file loaded with require or include"),
    "csharp": Coverage(True, "using, using static, using aliases, the file's namespace and its parents"),
    "swift": Coverage(
        False,
        "the files of one Swift module see each other without an import, so no file names where a callee"
        " lives; graphify 0.9.73 reports the call with no other evidence",
    ),
    "dart": Coverage(
        False,
        "graphify 0.9.73's Dart extractor reports definitions and imports but no calls, in the file or out of it",
    ),
    "shell": Coverage(True, "a function of a file loaded with source or ., relative to the script or the repo root"),
    "sql": Coverage(
        False,
        "graphify 0.9.73 needs tree-sitter-sql, which the graphify extra does not install: a .sql file gets no"
        " symbols, so it has no caller or callee",
    ),
}


NO_CLASSES = "the language has no classes"
NO_SYMBOLS = (
    "graphify 0.9.73's Dart extractor marks no definition as callable and puts methods under the file, not"
    " their class, so a Dart file gets no symbols to link"
)
SWIFT_MODULE = (
    "within one file only: the files of one Swift module see each other without an import, so nothing in a"
    " file says where a base class of another file lives"
)

INHERITANCE: dict[str, Coverage] = {
    "python": Coverage(
        True,
        "bases through the file's imports or of the same file; self.m() and cls.m() to an inherited method,"
        " super().m() and super(C, self).m(), along the C3 order",
    ),
    "typescript": Coverage(
        True,
        "extends and implements of a class and extends of an interface, read off the header (graphify 0.9.73"
        " reports none for JS and TS); this.m() and super.m()",
    ),
    "javascript": Coverage(True, "extends, read off the class header; this.m() and super.m()"),
    "vue": Coverage(True, "as TypeScript, in the component's script block"),
    "go": Coverage(
        False,
        "Go has no inheritance: embedding a struct promotes its methods but is not a subclass, and is not followed",
    ),
    "java": Coverage(True, "extends and implements; this.m(), super.m(), and a bare m() the class or a parent defines"),
    "kotlin": Coverage(
        True, "the supertypes after the colon; this.m(), super.m(), super<T>.m(), and a bare m() of the class chain"
    ),
    "rust": Coverage(
        True, "impl Trait for Type makes the type inherit the trait; self.m() to a trait's default method"
    ),
    "ruby": Coverage(
        True,
        "the superclass and included modules; self.m() and super inside a method; a bare m() links nothing,"
        " since graphify 0.9.73 reports no call without a receiver that it cannot resolve in the file",
    ),
    "php": Coverage(True, "extends, implements and use of a trait; $this->m(), self::m(), static::m(), parent::m()"),
    "csharp": Coverage(
        True, "the base class and interfaces after the colon; this.M(), base.M(), and a bare M() of the class chain"
    ),
    "swift": Coverage(False, SWIFT_MODULE),
    "dart": Coverage(False, NO_SYMBOLS),
    "shell": Coverage(False, NO_CLASSES),
    "sql": Coverage(False, NO_CLASSES),
}

OVERRIDES: dict[str, Coverage] = {
    **{
        lang: Coverage(
            True,
            "a method to the method of the same name nearest up the chain of each base of its class, an"
            " interface's or trait's declaration included",
        )
        for lang, row in INHERITANCE.items()
        if row.supported
    },
    "go": INHERITANCE["go"],
    "swift": Coverage(False, SWIFT_MODULE),
    "dart": Coverage(False, NO_SYMBOLS),
    "shell": Coverage(False, NO_CLASSES),
    "sql": Coverage(False, NO_CLASSES),
}


def language_of(path: str) -> str | None:
    """The coverage row of a file, by its suffix."""
    return LANGUAGE.get(PurePosixPath(path).suffix.lower())
