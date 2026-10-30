"""A merge approval bound to a head and a scope (plan super-board-parity-2, criterion a3).

An approval of a merge holds for one head of the pull or merge request, the commit it names, and one scope: the hash
(``scope_of``) of the files the request touches, the migrations among them, and the lines of its description that say
needs-you. Two decisions take one: a decision of category merge an agent asks naming its pull request (``evo-agents
worker ask --category merge --pull N``: the worker reads the head and the scope from GitHub), and a decision the gate
of autonomy asks for the key ``merge ...`` (``server.autonomy_gates``), the head and scope the hub read. The decision
keeps it in its column ``approval`` (schema 0184): ``{"pull", "head", "scope"}``.

A push that moves the head, or one that changes what the request touches, leaves the approval behind: it no longer
opens the gate (``holds``), and the question is asked again at the new head. A decision asked before 0184, or one that
named no pull request, has no approval and opens the gate as it did; one that has an approval never opens it for
another head, nor when the head of the act could not be read.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping

MIGRATION = re.compile(r"(^|/)(migrations?|alembic)/|\.sql$", re.IGNORECASE)
NEEDS_YOU = re.compile(r"needs[-_ ]?you", re.IGNORECASE)
HEAD = re.compile(r"^[0-9a-f]{7,64}$")
PULL = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*[#!][1-9][0-9]{0,9}$")  # owner/name#12, a repo!3
SCOPE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHORT = 12  # the characters of a head or a scope a sentence shows


def migrations(files: Iterable[str]) -> list[str]:
    """The migrations among ``files``: a path under a directory migrations, migration or alembic, or a SQL file."""
    return sorted({path for path in files if MIGRATION.search(path)})


def needs_you(body: str | None) -> list[str]:
    """The lines of a request's description that say needs-you, stripped, in their order."""
    return [line.strip() for line in (body or "").splitlines() if NEEDS_YOU.search(line)]


def scope_of(files: Iterable[str], body: str | None = None) -> str:
    """The hash of what a request touches: its files, the migrations among them and its needs-you lines; the order of
    the files and their repeats change nothing."""
    touched = sorted(set(files))
    shown = {"files": touched, "migrations": migrations(touched), "needs_you": needs_you(body)}
    text = json.dumps(shown, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def approval(head: str | None, scope: str | None, pull: str | None = None) -> dict | None:
    """What a decision keeps of the request it approves; None without a head, an approval of nothing to bind."""
    if not head:
        return None
    return {"pull": pull, "head": head.lower(), "scope": scope}


def holds(kept: Mapping | None, head: str | None, scope: str | None) -> bool:
    """Whether a decision's approval ``kept`` opens the gate for an act at ``head`` with ``scope``: always for a
    decision without one (asked before schema 0184, or naming no request), else only at the same head and scope."""
    if not kept:
        return True
    return bool(head) and kept.get("head") == head.lower() and kept.get("scope") == scope


def short(value: str | None) -> str:
    if not value:
        return "unknown"
    return value.removeprefix("sha256:")[:SHORT]


def line(kept: Mapping | None) -> str:
    """The sentence the person asked reads under the question: what the answer is bound to."""
    if not kept:
        return ""
    what = f" of {kept['pull']}" if kept.get("pull") else ""
    scope = f" and scope {short(kept.get('scope'))}" if kept.get("scope") else ""
    return (
        f"This answer holds for head {short(kept['head'])}{what}{scope} alone: a push that moves the head, or changes "
        "the files, the migrations or the needs-you lines, asks again."
    )


def stale(kept: Mapping | None, head: str | None, scope: str | None) -> str:
    """Why ``kept`` no longer opens the gate, for a refusal; empty when it does."""
    if holds(kept, head, scope):
        return ""
    if not head:
        return f"it was given for head {short(kept['head'])}, and the head of this act could not be read"
    if kept.get("head") != head.lower():
        return f"it was given for head {short(kept['head'])}, and the head is {short(head)} now"
    return f"it was given for scope {short(kept.get('scope'))}, and the scope is {short(scope)} now"
