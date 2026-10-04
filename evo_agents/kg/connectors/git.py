"""Builtin ``git`` connector: every selected file of a repo at one ref.

Source config (knowledge.yaml)::

    - id: billing
      connector: git
      repo: billing-service               # a repo name from harness.yaml, or `path:`
      ref: HEAD                           # default HEAD of the checkout
      include: ["docs/*", "*.py"]         # optional globs; default: markdown, yaml, text, code
      exclude: ["tests/fixtures/*"]
      allow: [".claude/CLAUDE.md"]        # optional globs exempt from DEFAULT_EXCLUDE
      code: {backend: auto}               # python-ast, graphify-ast (needs the graphify extra) or none

Paths matching ``DEFAULT_EXCLUDE`` (build output, lock files, virtualenvs, ``.claude/``, ``.agents/``,
...) are skipped unless they match an ``allow`` glob. The source's own ``exclude`` always wins over
``allow``, and an allowed path still goes through ``include`` or the default suffixes; one that is not
selected becomes a path-only asset like any other file. Globs use fnmatch, where ``*`` also matches
``/``.

Item IDs are ``<source>:file:<path>``; the revision is the blob SHA, so it changes exactly when the
content does. A git ref can be force-pushed; then the order of revisions is the order syncs observed.
"""

from __future__ import annotations

from pathlib import Path

from evo_agents import __version__
from evo_agents.kg.connectors import _git
from evo_agents.kg.connectors._files import asset_item, decode, excluded, file_item, selected
from evo_agents.kg.protocol import finalize_item, hello


def repo_dir(ctx) -> Path:
    source = ctx.source
    if source.get("path"):
        path = Path(source["path"]).expanduser()
        if not path.is_absolute() and ctx.harness_root:
            path = ctx.harness_root / path
        return path
    if ctx.harness is None:
        raise ValueError("a git source with repo: needs a harness")
    repo = ctx.harness.repo(source["repo"])
    if repo is None:
        raise ValueError(f"repo {source['repo']!r} is not in harness.yaml")
    path = ctx.harness.repo_path(repo)
    if path is None:
        raise ValueError(f"repo {source['repo']!r} has no path in harness.yaml")
    return path


def run(ctx):
    sid = ctx.source_id
    yield hello("git", __version__, list_complete=True, rev_exact=True, history=True)
    try:
        repo = repo_dir(ctx)
        if not _git.is_repo(repo):
            raise ValueError(f"{repo} is not a git checkout")
        commit = _git.resolve(repo, ctx.source.get("ref") or "HEAD")
    except (ValueError, _git.GitError) as exc:
        yield {"type": "error", "failure": "config", "message": str(exc)}
        yield {"type": "closed", "status": "error", "exception": str(exc)}
        return

    include, exclude, allow = ctx.source.get("include"), ctx.source.get("exclude"), ctx.source.get("allow")
    assets = ctx.source.get("assets", True)
    tree = [(sha, path) for sha, path in _git.ls_tree(repo, commit) if not excluded(path, exclude, allow)]
    files = [(sha, path) for sha, path in tree if selected(path, include, exclude, allow)]
    others = [(sha, path) for sha, path in tree if assets and not selected(path, include, exclude, allow)]
    times = _git.last_change_times(repo, commit)
    fallback_time = _git.commit_time(repo, commit)
    branch = ctx.source.get("branch")
    origin = ""
    if ctx.harness and ctx.source.get("repo"):
        entry = ctx.harness.repo(ctx.source["repo"]) or {}
        origin = entry.get("origin", "")
        branch = branch or entry.get("default_branch")
    branch = branch or "main"

    count = 0
    batch = 200
    for start in range(0, len(files), batch):
        chunk = files[start : start + batch]
        blobs = _git.cat_blobs(repo, [sha for sha, _ in chunk])
        for sha, path in chunk:
            data = blobs.get(sha, b"")
            text = decode(data)
            uri = _git.web_url(origin, branch, path) if origin else None
            rev_time = times.get(path, fallback_time)
            if text is None:
                if not assets:
                    continue  # binary, too large or not UTF-8: outside the listing, like a deleted file
                item = asset_item(f"{sid}:file:{path}", path, rev=sha, rev_time=rev_time, uri=uri, size=len(data))
            else:
                item = file_item(f"{sid}:file:{path}", path, text, rev=sha, rev_time=rev_time, uri=uri)
            # No commit in props: it would change every item's hash on every commit.
            item["props"]["repo"] = ctx.source.get("repo") or repo.name
            yield finalize_item(item)
            count += 1
    for sha, path in others:  # known by path only: never read, so no blob fetch
        uri = _git.web_url(origin, branch, path) if origin else None
        item = asset_item(f"{sid}:file:{path}", path, rev=sha, rev_time=times.get(path, fallback_time), uri=uri, size=0)
        item["props"]["repo"] = ctx.source.get("repo") or repo.name
        yield finalize_item(item)
        count += 1
    yield {"type": "state", "cursor": {"commit": commit}}
    yield {"type": "listing", "scope": f"{sid}:file:", "complete": True, "count": count}
    yield {"type": "closed", "status": "ok"}
