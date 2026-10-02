"""File classification and fragment building shared by the file-based connectors."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
from pathlib import PurePosixPath

from evo_agents.kg.text import split_markdown

MAX_BYTES = 512 * 1024

KIND_BY_SUFFIX = {
    ".md": "markdown",
    ".mdx": "markdown",
    ".markdown": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".txt": "text",
    ".rst": "text",
    ".py": "code",
    ".ts": "code",
    ".tsx": "code",
    ".js": "code",
    ".jsx": "code",
    ".mjs": "code",
    ".go": "code",
    ".java": "code",
    ".kt": "code",
    ".rs": "code",
    ".rb": "code",
    ".php": "code",
    ".cs": "code",
    ".swift": "code",
    ".dart": "code",
    ".sql": "code",
    ".sh": "code",
    ".vue": "code",
}
LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".dart": "dart",
    ".sql": "sql",
    ".sh": "shell",
    ".vue": "vue",
}
DEFAULT_EXCLUDE = [
    ".git/*",
    "*/.git/*",
    "node_modules/*",
    "*/node_modules/*",
    "vendor/*",
    "*/vendor/*",
    "dist/*",
    "*/dist/*",
    "build/*",
    "*/build/*",
    ".venv/*",
    "*/.venv/*",
    "venv/*",
    "__pycache__/*",
    "*/__pycache__/*",
    "*.min.js",
    "*.min.css",
    "package-lock.json",
    "*/package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "*/pnpm-lock.yaml",
    "uv.lock",
    "poetry.lock",
    ".claude/*",
    ".agents/*",
    "state/*",
    ".playwright-mcp/*",
]


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def selected(path: str, include: list[str] | None, exclude: list[str] | None) -> bool:
    if matches(path, DEFAULT_EXCLUDE + list(exclude or [])):
        return False
    if include:
        return matches(path, include)
    return PurePosixPath(path).suffix.lower() in KIND_BY_SUFFIX


def git_blob_sha(data: bytes) -> str:
    """The SHA-1 git would give this content, so working-tree revisions match committed ones."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def decode(data: bytes) -> str | None:
    if len(data) > MAX_BYTES or b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def python_fragments(text: str) -> list[dict] | None:
    """One fragment per top-level class or function, the rest in ``_module``. Disjoint line ranges."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.splitlines(keepends=True)
    taken = [False] * len(lines)
    frags = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = node.end_lineno or node.lineno
        for i in range(start - 1, end):
            taken[i] = True
        frags.append(
            {
                "anchor": node.name,
                "title": node.name,
                "level": 1,
                "text": "".join(lines[start - 1 : end]),
                "span": f"lines {start}-{end}",
            }
        )
    rest = "".join(line for line, used in zip(lines, taken, strict=True) if not used)
    if rest.strip():
        frags.insert(0, {"anchor": "_module", "level": 0, "text": rest})
    seen: dict[str, int] = {}
    for f in frags:  # redefinitions at module level keep distinct anchors
        n = seen.get(f["anchor"], 0)
        seen[f["anchor"]] = n + 1
        if n:
            f["anchor"] = f"{f['anchor']}-{n}"
    return frags


def file_item(
    item_id: str, path: str, text: str, *, rev: str, rev_time: str, uri: str | None, kind: str | None = None
) -> dict:
    suffix = PurePosixPath(path).suffix.lower()
    kind = kind or KIND_BY_SUFFIX.get(suffix, "text")
    body_format = {"markdown": "markdown", "yaml": "yaml", "code": "code"}.get(kind, "text")
    if kind in ("manifest", "contracts", "knowledge", "plan", "binding", "ontology", "registry"):
        body_format = "yaml"
    body = {"format": body_format, "text": text}
    if suffix in LANGUAGE_BY_SUFFIX:
        body["language"] = LANGUAGE_BY_SUFFIX[suffix]
    item = {
        "id": item_id,
        "kind": kind,
        "rev": rev,
        "rev_time": rev_time,
        "rev_exact": True,
        "title": path,
        "body": body,
        "props": {"path": path},
    }
    if uri:
        item["uri"] = uri
    if body_format == "markdown":
        item["fragments"] = split_markdown(text)
    elif body.get("language") == "python":
        frags = python_fragments(text)
        if frags:
            item["fragments"] = frags
    return item
