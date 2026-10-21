"""Builtin ``git`` connector: every selected file of a repo at one ref.

Source config (knowledge.yaml)::

    - id: billing
      connector: git
      repo: billing-service               # a repo name from harness.yaml, or `path:`
      ref: HEAD                           # default HEAD of the checkout
      fetch: true                         # optional: with ref origin/<branch>, fetch that branch first
      include: ["docs/*", "*.py"]         # optional globs; default: markdown, yaml, text, code
      exclude: ["tests/fixtures/*"]
      allow: [".claude/CLAUDE.md"]        # optional globs exempt from DEFAULT_EXCLUDE
      code: {backend: auto}               # python-ast, graphify-ast (needs the graphify extra) or none

Paths matching ``DEFAULT_EXCLUDE`` (build output, lock files, virtualenvs, ``.claude/``, ``.agents/``, ...)
at any depth, and ``state/`` at the root only, are skipped unless they match an ``allow`` glob. The source's
own ``exclude`` always wins over ``allow``, and an allowed path still goes through ``include`` or the
default suffixes; one that is not selected becomes a path-only asset like any other file. Globs use fnmatch,
where ``*`` also matches ``/``.

``fetch: true`` makes the run fetch the ref's branch from ``origin`` (with a timeout, never prompting)
before reading it, so the source follows the remote without anyone fetching the clone by hand. It only
applies to a ref of the form ``origin/<branch>``. A failed fetch (offline, no access) does not fail the
run: it logs a warning and reads the ref as the clone has it. Without the key nothing is fetched.

Item IDs are ``<source>:file:<path>``; the revision is the blob SHA, so it changes exactly when the
content does, and the revision time is that of the last commit on the ref that touched the file. Within one ref
the corpus keeps the record with the latest such time, so a force-push that moves a file back to an older commit
shows only once the file changes again. A change of the source's ``ref``, ``repo`` or ``path`` starts a new epoch
(``evo_agents.kg.corpus``): the next run's records replace those read from the old ref, even where its commits
are newer.

The run's cursor (``read_cursor``) names the commit read, how far it is behind the remote's default branch as the
clone has it, where links at that commit start, and ``input``, a digest of what besides the commit decides the items
(``read_input``). A sync whose source's last run was ok asks ``read_already`` first: when the ref still resolves to
that commit and the input is the same, the run would read the same items, so the sync reads nothing and keeps no log
(``evo_agents.kg.sync``). An item's ``uri`` links to its file by branch, never by commit,
which would change every item's hash on every commit; the tools show the link at the commit read instead
(``evo_agents.kg.freshness``).

Before a file's text becomes an item, ``evo_agents.kg.redact`` replaces each secret in it (a private key in PEM, a
provider's key, the password of a URI, the literal value of a variable named as a secret) with a marker that names
its kind, ``[redacted:password]``; the rest of the text stays, so the file's structure still enters the graph. The
item's ``props.redactions`` counts the markers by kind, and the sync's audit line repeats those counts by item id.

Besides the files, a run reads the history of the ref: an item ``<source>:commit:<sha>`` (kind ``commit``) for each
commit reachable from the commit read, with its parents, its committer time, its author's email (``author_email``, as
the commit records it; the build keeps it on the Commit node, labelled as the source like every other prop) and the
paths it changed that the source does not exclude (a merge's against its first parent, at most MAX_COMMIT_PATHS of
them, ``files`` counting them all); never its message. For a repo whose origin is on GitHub it also reads the pull
requests (``github``): an item ``<source>:pr:<number>`` (kind ``pr``) with the title, the state (open, closed or
merged), the head and base branches and the merge commit, labelled integrity U; ``pulls: false`` on the source turns
that off. A run whose cursor has the input of this run reads only the commits since the cursor's commit and the pull
requests updated since the newest one it saw (``pulls.since`` in the cursor); their listings are then not complete,
so nothing read before goes. Any other run reads the whole history, and its complete listing of commits removes those
the ref no longer reaches. So does a run whose ref no longer reaches the cursor's commit (a force-push or a rewrite
of the history, ``rewritten``), with a warning that says so; the deletion guard holds its removals as any others, so
a rewrite of much of the history waits for ``kg sync --accept-removals``. A pull request is never removed.
"""

from __future__ import annotations

import functools
import hashlib
import json
import sys
from pathlib import Path

from evo_agents import __version__
from evo_agents.kg.connectors import _git, github
from evo_agents.kg.connectors._files import asset_item, decode, excluded, file_item, selected
from evo_agents.kg.protocol import canonical_json, finalize_item, hello, sha256
from evo_agents.kg.redact import redact_file

# Keys of a source that say when or how it runs, or what the build does with its items: they leave the items as they
# are, so changing one does not make the next sync read the commit again.
NOT_READ = frozenset({"refresh", "timeout", "fetch", "deletion", "credentials", "label", "code"})
MAX_COMMIT_PATHS = 100  # paths a commit item names; a commit over that is a sweep, and the graph keeps its first ones


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


def behind_now(ctx, commit: str, base: str) -> int | None:
    """How many commits ``base`` (``origin/main``) has that ``commit`` lacks in the checkout of the source of ``ctx``
    as it is now, without fetching; None when there is no checkout or it has no such branch."""
    try:
        repo = repo_dir(ctx)
    except ValueError:
        return None
    return _git.behind(repo, commit, base) if repo.is_dir() else None


def _repo_entry(ctx) -> dict:
    """The source's repo as harness.yaml lists it; {} for a source with ``path:`` or no harness."""
    return (ctx.harness.repo(ctx.source["repo"]) or {}) if ctx.harness and ctx.source.get("repo") else {}


@functools.cache
def _code() -> str:
    """Digest of the code that makes the items of a commit: this module, the file selection and the redaction. A
    build of evo-agents that changes how files or the history are read, or how they are redacted, reads every commit
    again, whatever its version: its first sync reads the whole history, so a prop a commit item gained (such as
    ``author_email``) reaches the commits already in the corpus too."""
    from evo_agents.kg import redact

    digest = hashlib.sha256(__version__.encode())
    for module in (sys.modules[__name__], _git, github, sys.modules[selected.__module__], redact):
        try:
            digest.update(Path(module.__file__).read_bytes())
        except (OSError, TypeError):  # no source file to read: the version alone stands for it
            digest.update(module.__name__.encode())
    return digest.hexdigest()


def read_input(ctx) -> str:
    """Digest of what, besides the commit, decides the items and the links of a run: the source's config but for the
    keys in NOT_READ, the origin and default branch harness.yaml gives its repo, and the connector's code."""
    entry = _repo_entry(ctx)
    config = {key: value for key, value in ctx.source.items() if key not in NOT_READ}
    parts = [_code(), config, entry.get("origin", ""), entry.get("default_branch")]
    return sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str))


def read_already(ctx) -> tuple[bool, list[str]]:
    """Whether a run now would read what the source's cursor (``ctx.cursor``) says the last one read: its ref resolves
    to the same commit and ``read_input`` is the same. It fetches first when the source asks to, as a run does;
    also the warnings of that fetch."""
    cursor = ctx.cursor if isinstance(ctx.cursor, dict) else {}
    if not cursor.get("commit") or cursor.get("input") != read_input(ctx):
        return False, []
    try:
        repo = repo_dir(ctx)
    except ValueError:
        return False, []
    if not _git.is_repo(repo):
        return False, []
    ref = ctx.source.get("ref") or "HEAD"
    warnings = [m["message"] for m in _fetch(repo, ref)] if ctx.source.get("fetch") else []
    try:
        return _git.resolve(repo, ref) == cursor["commit"], warnings
    except _git.GitError:
        return False, warnings


def _fetch(repo: Path, ref: str):
    """Fetch the branch of an ``origin/<branch>`` ref; a failure is a warning, not a failed run."""
    remote, _, branch = ref.partition("/")
    if remote != "origin" or not branch:
        yield {"type": "log", "level": "warning", "message": f"fetch: true ignored: ref {ref!r} is not origin/<branch>"}
        return
    try:
        _git.fetch(repo, remote, branch)
    except (_git.GitError, OSError) as exc:
        yield {"type": "log", "level": "warning", "message": f"fetch failed, reading {ref} as the clone has it: {exc}"}


def blob_item(item_id: str, path: str, sha: str, data: bytes, *, rev_time: str, uri: str | None, assets: bool):
    """The item of a selected file: its text with its secrets redacted, or, when there is no text to read (binary, too
    large, not UTF-8), a path-only asset, or None without ``assets``."""
    text = decode(data)
    if text is None:
        return asset_item(item_id, path, rev=sha, rev_time=rev_time, uri=uri, size=len(data)) if assets else None
    text, redactions = redact_file(text, path)
    item = file_item(item_id, path, text, rev=sha, rev_time=rev_time, uri=uri)
    if redactions:
        item["props"]["redactions"] = redactions
    return item


def read_cursor(repo: Path, ref: str, commit: str, *, origin: str, branch: str, default: str | None) -> dict:
    """The cursor that ends a run: the commit read and the ref named; ``base``, the remote's default branch as the
    clone has it now (harness.yaml's ``default_branch``, else the clone's ``origin/HEAD``), and ``behind``, how many of
    its commits the commit read lacks, when the clone has that branch; with an origin on a forge, ``url`` of the commit
    and ``blob``, where file links by branch start and where those at the commit read start
    (``evo_agents.kg.freshness`` reads all of it)."""
    cursor: dict = {"commit": commit, "ref": ref}
    default = default or _git.remote_head(repo)
    count = _git.behind(repo, commit, f"origin/{default}") if default else None
    if count is not None:
        cursor.update(base=f"origin/{default}", behind=count)
    url = _git.commit_url(origin, commit) if origin else None
    if url:
        cursor["url"] = url
        cursor["blob"] = {"branch": _git.web_url(origin, branch, ""), "commit": _git.web_url(origin, commit, "")}
    return cursor


def prior_cursor(ctx) -> dict:
    """The cursor of the source's last run when that run read with the input this one has, so this run need only read
    the commits and pull requests that came after it; {} otherwise."""
    cursor = ctx.cursor if isinstance(ctx.cursor, dict) else {}
    return cursor if cursor.get("commit") and cursor.get("input") == read_input(ctx) else {}


def commit_item(sid: str, repo_name: str, origin: str, commit: dict, exclude, allow) -> dict:
    """The item of one commit of the history: what it changed, by path, and its author's email when it names one; no
    word of its message."""
    paths = sorted(path for path in commit["paths"] if not excluded(path, exclude, allow))
    props = {
        "repo": repo_name,
        "sha": commit["sha"],
        "parents": commit["parents"],
        "files": len(paths),
        "paths": paths[:MAX_COMMIT_PATHS],
    }
    if commit.get("author_email"):
        props["author_email"] = commit["author_email"]
    if len(commit["parents"]) > 1:
        props["merge"] = True
    item = {
        "id": f"{sid}:commit:{commit['sha']}",
        "kind": "commit",
        "rev": commit["sha"],
        "rev_time": commit["time"],
        "rev_exact": True,
        "title": f"{repo_name}@{commit['sha'][:10]}",
        "props": props,
    }
    uri = _git.commit_url(origin, commit["sha"]) if origin else None
    if uri:
        item["uri"] = uri
    return finalize_item(item)


def rewritten(repo: Path, ref: str, since: str, commit: str) -> str | None:
    """Why the history of ``ref`` cannot be read on from ``since``, the prior cursor's commit: it is not an ancestor
    of ``commit`` (a force-push, a filter-repo, a reset to an older commit) or the clone no longer has it (the same,
    then a gc). None when ``since`` is ``commit`` or one of its ancestors. A commit object that outlived the rewrite
    would still let ``git log since..commit`` run, and list the new history without dropping the old."""
    try:
        if _git.is_ancestor(repo, since, commit):
            return None
        why = f"{since[:10]}, the commit the last run read, is not an ancestor of {commit[:10]}"
    except _git.GitError:
        why = f"{since[:10]}, the commit the last run read, is not in the clone"
    return f"history of {ref} rewritten: {why}; read the whole history, so the commits only on the old one go"


def history_items(ctx, repo: Path, commit: str, repo_name: str, origin: str, prior: dict):
    """The commit items of the ref's history since the prior cursor's commit, then their listing, not complete; or,
    without a prior cursor or when the ref's history was rewritten since (``rewritten``, a warning), of the whole
    history, then a complete listing, so a commit the ref no longer reaches (a force-push, a rewrite, another ref)
    goes, under the deletion guard like a file."""
    exclude, allow = ctx.source.get("exclude"), ctx.source.get("allow")
    since = prior.get("commit")
    why = rewritten(repo, ctx.source.get("ref") or "HEAD", since, commit) if since else None
    if why:
        yield {"type": "log", "level": "warning", "message": why}
        since = None
    commits = _git.history(repo, commit, since)
    for found in commits:
        yield commit_item(ctx.source_id, repo_name, origin, found, exclude, allow)
    yield {"type": "listing", "scope": f"{ctx.source_id}:commit:", "complete": since is None, "count": len(commits)}


def pull_item(sid: str, repo_name: str, project: str, pull: dict) -> dict:
    """The item of one pull request: number, title, state, branches and merge commit; integrity U, as its title is
    free text. The revision is a digest of what it holds, so a pull request that only got a comment keeps it."""
    props = {"repo": repo_name, "github": project, "number": pull["number"], "status": pull["status"]}
    props.update({key: pull[key] for key in ("head", "base", "merge_commit") if pull.get(key)})
    title = pull["title"] or f"#{pull['number']}"
    item = {
        "id": f"{sid}:pr:{pull['number']}",
        "kind": "pr",
        "rev": sha256(canonical_json({"title": title, "props": props})).removeprefix("sha256:"),
        "rev_time": pull["updated"],
        "rev_exact": True,
        "title": title,
        "props": props,
        "label": {"integrity": "U"},
    }
    if pull.get("url"):
        item["uri"] = pull["url"]
    return finalize_item(item)


def pull_items(ctx, repo_name: str, origin: str, prior: dict):
    """The pull request items of a repo on GitHub updated since the prior cursor's ``pulls.since``, then their
    listing; returns the cursor's next ``pulls`` (the prior one when nothing was read). Nothing for a repo elsewhere,
    a source with ``pulls: false``, EVO_KG_PULLS=0 or no token; a log line when GitHub could not be read."""
    key = _git.remote_key(origin) if origin else None
    since = (prior.get("pulls") or {}).get("since") if isinstance(prior.get("pulls"), dict) else None
    kept = {"since": since} if since else None
    if not key or key[0] != "github.com" or ctx.source.get("pulls") is False or not github.enabled(ctx.env):
        return kept
    secret = github.token(ctx.env)
    if secret is None:
        yield {"type": "log", "level": "info", "message": "pulls not read: no GitHub token (GH_TOKEN, gh)"}
        return kept
    try:
        found = github.pulls(key[1], secret, since)
    except github.PullsError as exc:
        yield {"type": "log", "level": "warning", "message": f"pulls not read: {exc}"}
        return kept
    for pull in found.pulls:
        yield pull_item(ctx.source_id, repo_name, key[1], pull)
    yield {"type": "listing", "scope": f"{ctx.source_id}:pr:", "complete": False, "count": len(found.pulls)}
    if found.cut:
        shown = f"the {github.MAX_PAGES * github.PAGE} most recently updated"
        yield {"type": "log", "level": "warning", "message": f"pulls: read {shown} of {key[1]} only"}
    newest = max(filter(None, (since, found.newest)), default=None)
    return {"since": newest} if newest else None


def history_and_pulls(ctx, repo: Path, commit: str, repo_name: str, origin: str):
    """The commit items of the history, then the pull request items; returns the cursor's next ``pulls``."""
    prior = prior_cursor(ctx)
    yield from history_items(ctx, repo, commit, repo_name, origin, prior)
    return (yield from pull_items(ctx, repo_name, origin, prior))


def run(ctx):
    sid = ctx.source_id
    yield hello("git", __version__, list_complete=True, rev_exact=True, history=True)
    try:
        repo = repo_dir(ctx)
        if not _git.is_repo(repo):
            raise ValueError(f"{repo} is not a git checkout")
    except ValueError as exc:
        yield {"type": "error", "failure": "config", "message": str(exc)}
        yield {"type": "closed", "status": "error", "exception": str(exc)}
        return
    ref = ctx.source.get("ref") or "HEAD"
    if ctx.source.get("fetch"):
        yield from _fetch(repo, ref)
    try:
        commit = _git.resolve(repo, ref)
    except _git.GitError as exc:
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
    entry = _repo_entry(ctx)
    origin = entry.get("origin", "")
    branch = ctx.source.get("branch") or entry.get("default_branch") or "main"
    repo_name = ctx.source.get("repo") or repo.name

    count = 0
    batch = 200
    for start in range(0, len(files), batch):
        chunk = files[start : start + batch]
        blobs = _git.cat_blobs(repo, [sha for sha, _ in chunk])
        for sha, path in chunk:
            item = blob_item(
                f"{sid}:file:{path}",
                path,
                sha,
                blobs.get(sha, b""),
                rev_time=times.get(path, fallback_time),
                uri=_git.web_url(origin, branch, path) if origin else None,
                assets=assets,
            )
            if item is None:
                continue  # binary, too large or not UTF-8: outside the listing, like a deleted file
            # No commit in props: it would change every item's hash on every commit.
            item["props"]["repo"] = repo_name
            yield finalize_item(item)
            count += 1
    for sha, path in others:  # known by path only: never read, so no blob fetch
        uri = _git.web_url(origin, branch, path) if origin else None
        item = asset_item(f"{sid}:file:{path}", path, rev=sha, rev_time=times.get(path, fallback_time), uri=uri, size=0)
        item["props"]["repo"] = repo_name
        yield finalize_item(item)
        count += 1
    pulls = yield from history_and_pulls(ctx, repo, commit, repo_name, origin)
    cursor = read_cursor(repo, ref, commit, origin=origin, branch=branch, default=entry.get("default_branch"))
    yield {"type": "state", "cursor": {**cursor, "input": read_input(ctx), **({"pulls": pulls} if pulls else {})}}
    yield {"type": "listing", "scope": f"{sid}:file:", "complete": True, "count": count}
    yield {"type": "closed", "status": "ok"}
