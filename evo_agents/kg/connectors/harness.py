"""Builtin ``harness`` connector: the harness directory itself, working tree included.

The harness is where plans, contracts, bindings and the ontology are edited, often before they are
committed, so this connector reads the files on disk. Revisions are git blob SHAs of the content, so an
unchanged file keeps its revision whether or not it is committed.

Kinds: manifest (harness.yaml), contracts, knowledge, plan, binding, ontology, registry (other root
YAML files such as deployments.yaml), markdown, yaml.
"""

from __future__ import annotations

import os
from pathlib import Path

from evo_agents import __version__
from evo_agents.kg.connectors import _git
from evo_agents.kg.connectors._files import (
    DEFAULT_EXCLUDE,
    asset_item,
    decode,
    file_item,
    git_blob_sha,
    matches,
    selected,
)
from evo_agents.kg.ids import epoch_to_iso
from evo_agents.kg.protocol import finalize_item, hello

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "state",
    ".claude",
    ".agents",
    ".pytest_cache",
    ".playwright-mcp",
    "proposals",
}


def harness_kind(path: str) -> str | None:
    parts = path.split("/")
    name = parts[-1]
    if not name.endswith((".yaml", ".yml", ".md", ".mdx")):
        return None
    if name.endswith((".md", ".mdx")):
        return "markdown"
    if len(parts) == 1:
        return {
            "harness.yaml": "manifest",
            "contracts.yaml": "contracts",
            "knowledge.yaml": "knowledge",
            "ontology.yaml": "ontology",
        }.get(name, "registry")
    if parts[0] == "plans":
        return "plan"
    if parts[0] == "bindings":
        return "binding"
    return "yaml"


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            full = Path(dirpath) / name
            yield full, full.relative_to(root).as_posix()


def run(ctx):
    sid = ctx.source_id
    yield hello("harness", __version__, list_complete=True, rev_exact=True)
    root = Path(ctx.source.get("path") or ctx.harness_root or ".").expanduser()
    if not (root / "harness.yaml").is_file():
        message = f"{root} is not a harness (no harness.yaml)"
        yield {"type": "error", "failure": "config", "message": message}
        yield {"type": "closed", "status": "error", "exception": message}
        return

    clean: set[str] = set()
    times: dict[str, str] = {}
    origin = ""
    if _git.is_repo(root):
        try:
            head = _git.resolve(root, "HEAD")
            clean = _git.tracked_clean(root)
            times = _git.last_change_times(root, head)
            origin = _git.git(root, "remote", "get-url", "origin").strip()
        except _git.GitError:
            pass
    branch = ctx.source.get("branch") or "main"
    include, exclude = ctx.source.get("include"), ctx.source.get("exclude")

    assets = ctx.source.get("assets", True)
    count = 0
    for full, rel in walk(root):
        kind = harness_kind(rel)
        wanted = kind is not None and selected(rel, include or ["*.md", "*.mdx", "*.yaml", "*.yml"], exclude)
        if not wanted and not (assets and not matches(rel, DEFAULT_EXCLUDE + list(exclude or []))):
            continue
        data = full.read_bytes()
        rev_time = times.get(rel) if rel in clean else None
        rev_time = rev_time or epoch_to_iso(full.stat().st_mtime)
        uri = _git.web_url(origin, branch, rel) if origin else None
        text = decode(data) if wanted else None
        if text is None:
            item = asset_item(
                f"{sid}:file:{rel}", rel, rev=git_blob_sha(data), rev_time=rev_time, uri=uri, size=len(data)
            )
        else:
            item = file_item(
                f"{sid}:file:{rel}", rel, text, rev=git_blob_sha(data), rev_time=rev_time, uri=uri, kind=kind
            )
        item["props"]["repo"] = root.name
        yield finalize_item(item)
        count += 1
    yield {"type": "listing", "scope": f"{sid}:file:", "complete": True, "count": count}
    yield {"type": "closed", "status": "ok"}
