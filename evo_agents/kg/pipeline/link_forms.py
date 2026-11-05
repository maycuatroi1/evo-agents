"""The written forms of a mention the link stage reads (``pipeline.link``), and the small rules on them: which
name in backticks may be a symbol, which path is a placeholder, which URLs form a collection."""

from __future__ import annotations

import posixpath
import re

URL = re.compile(r"https?://[^\s<>()\[\]{}`'\"|]+")
BACKTICK = re.compile(r"`([^`\n]{2,200})`")
QUALIFIED_SYMBOL = re.compile(r"(?<![\w/.-])(?:([A-Za-z0-9_.-]+):)?((?:[\w.-]+/)*[\w.-]+\.\w{1,6})::([A-Za-z_][\w.]*)")
REPO_PATH = re.compile(r"(?<![\w/.:@-])([A-Za-z0-9_.-]+):((?:[\w.-]+/)*[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/])")
PATH = re.compile(r"(?<![\w/.:@-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/])")
# A path whose file name holds spaces (`reports/Backend tập trung cho agent.md`), up to its first extension: linked only
# when it is a file of the project as written.
SPACED_PATH = re.compile(
    r"(?<![\w/.:@-])((?:[\w.-]+/)+[\w.-][^\n/`'\"()\[\]<>|*]{0,150}?\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/])"
)
# A path whose directories hold spaces (`research_notes/Browser cho agent/literature.md`): the first segment has none,
# so prose before it stays out; it ends at the first file name it can, and may start at an earlier path, so its
# tails are tried too (SPACED_START).
SPACED_DIR_PATH = re.compile(
    r"(?<![\w/.:@-])([\w.-]+/(?:[^\n/`'\"()\[\]<>|*]{1,150}/)+?[\w.-][^\n/`'\"()\[\]<>|*]{0,150}?\.[A-Za-z][A-Za-z0-9]{0,5})"
    r"(?![\w/])"
)
SPACED_START = re.compile(r"(?<![\w/.:@-])[\w.-]+/")
# A document at a repo's root named in capitals (CLUSTER.md, AGENTS.md): one of the step's or the item's repo.
ROOT_DOC = re.compile(r"(?<![\w/.:@-])([A-Z][A-Z0-9_-]*\.md)(?![\w/])")
DOI = re.compile(r"(?<![\w/.])10\.\d{4,9}/[^\s)\]>,;]+")
MD_LINK = re.compile(r"\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")
REPO_SHA = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.-]+)@([0-9a-f]{7,40})\b")
BARE_SHA = re.compile(r"(?<![\w/.@-])([0-9a-f]{7,40})(?![\w-])")
PLAN_STEP = re.compile(r"(?<![\w/.-])([a-z0-9][a-z0-9-]{2,})#(\d+)\b")
GITHUB_PULL = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")  # owner/repo#57
PULL_WORD = re.compile(r"\b(?:PR|[Pp]ull [Rr]equest) ?#(\d+)\b")  # "PR #57" in the text of a step of that repo
TOKEN = re.compile(r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9])(?![\w-])")
SYMBOLISH = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*(?:\(\))?$")
# `cd ~/github/evo-cli`, `cd /srv/app`, `cd "$HOME/x"`: a shell command moving into a tree named by its last segment.
CD = re.compile(r"(?<![\w-])cd\s+['\"]?((?:~|/|\$)[^\s;&|()'\"]*)")
REPO_BEFORE = re.compile(r"([A-Za-z0-9_.-]+)(?:'s)?[\s`'\"]*$")  # `evo-cli tests/x.py`, evo-cli's `tests/x.py`
REPO_AFTER = re.compile(r"^[`'\"]*\s+(?:of|in|from|của|trong)\s+[`'\"]?([A-Za-z0-9_.-]+)")  # tests/x.py of evo-cli
PLACEHOLDERS = frozenset({"x", "xx", "xxx", "y", "z", "n", "foo", "bar", "baz", "name", "example", "placeholder"})
PLAN_FILE = re.compile(r"(^|/)plans/(active|completed)/([^/]+\.ya?ml)$")
LOCKFILE = re.compile(
    r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock|uv\.lock|Cargo\.lock|go\.sum)$"
)


def is_placeholder(path: str) -> bool:
    """Whether a path's file name is a placeholder: its words (split at ``.``, ``_``, ``-``, the extension too) are
    ``test`` and placeholders such as ``x``, ``X.Y.Z``, ``name``, at least one a placeholder."""
    words = [w.lower() for w in re.split(r"[._-]", posixpath.basename(path.rstrip("/"))) if w]
    return any(w in PLACEHOLDERS for w in words) and all(w in PLACEHOLDERS or w == "test" for w in words[:-1])


def url_prefix(url: str) -> str:
    """The collection a URL is in: forge URLs up to the kind after owner/repo (``.../pull``, ``.../commit``,
    ``.../blob``, past GitLab's ``-``), other URLs without their last segment (``https://hub/chats`` of
    ``https://hub/chats/30``). A mention in a collection some item's URL is in that matches no item is a dangling
    link; any other URL (a host alone, ``/v1/health`` of the hub, a forge's ``actions/runs``) is just outside the
    project."""
    parts = url.split("/")
    if len(parts) < 4 or not parts[3]:
        return url
    if any(marker in parts[5:6] for marker in ("blob", "tree", "-")) or parts[2] in ("github.com", "gitlab.com"):
        kind = 6 if parts[5:6] == ["-"] else 5
        return "/".join(parts[: kind + 1])
    return "/".join(parts[:-1])


def looks_like_symbol(name: str) -> bool:
    """Whether a name in backticks may be a symbol: written as code (``_``, a dot, camelCase or a capital), not a
    plain lower-case word such as `settings` or `coverage`, which prose uses for the thing and not the code, nor a
    dunder alone."""
    if name.startswith("__") and name.endswith("__"):
        return False  # `__init__` names a protocol every class may have, not one symbol
    return "_" in name or "." in name or not name.islower()


def unique(entries: list) -> str | None:
    """The one node index entries (or node ids) name; None when they name none or several."""
    nodes = {e["node"] if isinstance(e, dict) else e for e in entries}
    return next(iter(nodes)) if len(nodes) == 1 else None
