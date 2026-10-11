"""What the code graph covers per language: the coverage table docs, tools and tests read.

One row per language the connectors mark as code (``connectors._files.LANGUAGE_BY_SUFFIX``). A ``no``
carries the reason, measured on graphify 0.9.73 with a two-file fixture in tests/kg/test_cross_file.py,
which also checks that such a language really gets no cross-file edge.
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


def language_of(path: str) -> str | None:
    """The coverage row of a file, by its suffix."""
    return LANGUAGE.get(PurePosixPath(path).suffix.lower())
