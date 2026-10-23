"""The tier rules of the Curator: how far a change the Reviewer proposes may go without its owner. They are code of the
hub, not words in a prompt, so no agent ever sets the tier of its own change. Pure functions, standard library only,
so the api, the command line and the tests share them; ``docs/curator.md`` and ``evo_agents.hub.server.proposals``
are built on them.

A tier is one of ``curator.TIERS``. A proposal of tier 0, 1 or 2 that the owner accepts becomes the Curator's plan,
which the night shift builds on a branch of its own and the Judge judges (``judge.PLANNED_TIERS``): 0, the hub may
merge its pull request once CI and the Judge pass (when the charter lets it); 1, its pull request stays open for the
owner to merge; 2, the same as 1, and the proposal also reaches the owner's Inbox. 3, the night shift builds nothing
and the owner alone makes the change. ``tier_of`` computes it from these:

- the kind of change the proposal names (CHANGE_KINDS), each with its tier; the kinds that loosen or remove a test,
  change a plan's verify, change CI or lint, touch credentials or permissions, or change the Curator's own rules
  (TOP_KINDS) are always tier 3;
- the paths it would edit, each ``repo:path``: a path that matches a protected glob of the charter, or one of
  HARD_PATHS (CI, lint and test configuration, plan copies), makes it tier 3; a migration or schema path makes it at
  least tier 2 (SCHEMA_PATHS); any path that is neither documentation nor a test makes it at least tier 1;
- the paths those reach in the project's knowledge graph (``kg_impact``), when the project has one: a reached path
  that is protected or one of HARD_PATHS makes it tier 3 too;
- what that raise cannot know (``unknown``): a path the graph has no file for, one whose file the graph holds older
  than the default branch's (the latest measure of the graph's health says so), or every path while the graph of a
  project that has one cannot be read, makes it at least tier 1, the reason saying which;
- for kind ``kg_source`` (KNOWLEDGE_KIND), knowledge.yaml at a repo's root (KNOWLEDGE_PATHS) is not code: tier 0 while
  the pull request's diff, read key by key (``knowledge``, from ``evo_agents.hub.knowledge_diff``), edits nothing but
  SOURCE_KEYS of git and harness sources the file has; anything else it changes there, or a file the hub could not
  read at both ends, makes it at least tier 1, as knowledge.yaml is under any other kind;
- a fix proposal (``fix_of``: the merged change whose probe recurred on one model two weekly reports in a row,
  ``evo_agents.hub.server.probe_fixes``) is at least tier 1 (FIX_TIER), whatever its kind says: the owner merges it.

The tier is the highest of them, and ``Tier.reasons`` says which rule gave each raise, in words for the owner. The hub
computes it for a proposal from its paths, then again from the paths its pull request really changes, which only ever
raises it (``evo_agents.hub.server.change_tiers``); a protected path stays tier 3 whatever else says.

One rule lowers the tier a change merges with, never its proposal's (``replay_tier``): a change of a kind of
REPLAY_KINDS whose pull request edits a harness alone (the files of one skill, AGENTS.md and CLAUDE.md files:
``evo_agents.hub.replay.harness_only``) and whose replays on the project's holdout tasks won (``replay.verdict``) is
tier 0, whatever its kind, its code paths or the graph gave it, unless a rule made it tier 3 or it is a fix proposal.
Without replays that won, or with any other file in its diff, it keeps the tier these rules give.

A protected glob is matched against the path within its repo (``.github/workflows/**`` protects that directory of every
repo of the project); a glob written ``repo:glob`` protects that repo's paths alone. ``*`` matches within one
directory, ``**`` across directories (``**/`` also matches none), ``?`` one character; nothing else is special.

A proposal the owner rejected comes back the same: its fingerprint (``fingerprint``: the kind and the paths, or the
kind and the title when it names no path) is the rejected one's. Within REJECTED_DAYS of that rejection the hub drops
it, unless its evidence counts at least EVIDENCE_FACTOR times the rejected one's (``drops``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

REJECTED_DAYS = 30  # a proposal like one rejected this recently is dropped
EVIDENCE_FACTOR = 2  # unless its evidence is this many times the rejected one's

# kind: (tier, what it covers). The owner's report of the Curator's design names these classes of change.
CHANGE_KINDS: dict[str, tuple[int, str]] = {
    "docs": (0, "documentation or figures that drifted from the code, such as a version or a path in CLUSTER.md"),
    "memory": (0, "memories on the hub: merging duplicates, marking ones that contradict each other"),
    "test_add": (0, "new tests only, for a fixed bug or for behaviour without a test; no existing test changes"),
    "kg_source": (
        0,
        "the ref, fetch, refresh, include, exclude or allow of a git or harness source already in knowledge.yaml, "
        "as the pull request's diff shows key by key",
    ),
    "fix": (1, "a bug fix that comes with its test"),
    "refactor": (1, "a refactor that keeps the behaviour, or a lint warning fixed"),
    "lint": (1, "a new lint rule, or a principle moved into an invariant a machine checks"),
    "skill": (1, "a skill of the project, or a learned skill reviewed with evidence of its use"),
    "cli": (1, "a new command for a chain of commands people type again and again"),
    "release_prep": (1, "preparing a release: changelog, version, the release pull request"),
    "revert": (1, "a git revert of a change the Curator merged whose figures got worse after it; the hub proposes it"),
    "feature": (2, "a new feature"),
    "api_change": (2, "a change of an API, a command line or a JSON key"),
    "schema_change": (2, "a change of a database schema or a migration"),
    "global_config": (2, "a global skill, a hook or a global CLAUDE.md, which every session on every machine reads"),
    "dependency_major": (2, "a new major version of a dependency"),
    "operation": (2, "a deploy, a migration on real data, deleting data, sending outside, spending money"),
    "test_loosen": (3, "loosening, skipping or removing a test or an assertion"),
    "verify_change": (3, "changing the verify of a plan"),
    "ci_change": (3, "changing a CI workflow or the lint configuration"),
    "credentials": (3, "credentials, the permissions of a GitHub App, permissions on the hub"),
    "curator_rules": (3, "the charter, these tier rules, the Judge's prompt or its checks"),
}
TOP_KINDS = frozenset(kind for kind, (tier, _) in CHANGE_KINDS.items() if tier == 3)

# Paths that make a change tier 3 whatever its kind: CI, lint and test configuration, and the copies of plans.
HARD_PATHS = (
    ".github/workflows/**",
    ".github/actions/**",
    ".gitlab-ci.yml",
    ".gitlab-ci/**",
    ".circleci/**",
    ".pre-commit-config.yaml",
    "ruff.toml",
    ".ruff.toml",
    "pytest.ini",
    "tox.ini",
    "**/conftest.py",
    "eslint.config.*",
    "**/eslint.config.*",
    ".eslintrc*",
    "**/.eslintrc*",
    "plans/**",
)
SCHEMA_PATHS = ("**/migrations/**", "**/*.sql")  # at least tier 2
DOC_PATHS = ("**/*.md", "**/*.rst", "**/*.txt", "docs/**")  # no raise: tier 0 when the kind allows it
TEST_PATHS = (
    "tests/**",
    "test/**",
    "**/tests/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/*.test.*",
    "**/*.spec.*",
    "**/e2e/**",
)
KNOWLEDGE_KIND = "kg_source"
KNOWLEDGE_PATHS = ("knowledge.yaml",)  # the knowledge config at the root of a repo of the project
SOURCE_KEYS = ("ref", "fetch", "refresh", "include", "exclude", "allow")  # what a change of kind kg_source edits
SOURCE_CONNECTORS = ("git", "harness")  # of the sources it may edit
SHOWN_CHANGES = 3  # of what a knowledge.yaml change does besides SOURCE_KEYS, named in a reason
REPLAY_KINDS = ("docs", "skill", "global_config")  # the kinds whose change replays that won make tier 0
FIX_TIER = 1  # the lowest tier of a fix proposal: the hub never merges one alone
MAX_PATHS = 100  # paths one proposal names
MAX_PATH_CHARS = 500
PATH = re.compile(r"^[^\x00-\x1f\x7f\\]+$")


@dataclass(frozen=True)
class Tier:
    """The tier ``tier_of`` gave, and why: one line per rule that raised it, the first naming the kind."""

    tier: int
    reasons: list[str] = field(default_factory=list)


# Paths


def normalize_path(path: str) -> str | None:
    """``path`` as a repo-relative POSIX path: no leading ``./`` or ``/``, no empty, ``.`` or ``..`` part, no
    backslash or control character; None when it cannot be one."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS or not PATH.match(path):
        return None
    parts = [part for part in path.strip().lstrip("/").split("/")]
    while parts and parts[0] == ".":
        parts.pop(0)
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    return "/".join(parts)


@lru_cache(maxsize=1024)
def _pattern(glob: str) -> re.Pattern:
    """The regular expression of a glob: ``**/`` any directories or none, ``**`` anything, ``*`` within a
    directory, ``?`` one character."""
    out, index = [], 0
    while index < len(glob):
        if glob.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif glob[index] == "*":
            out.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(glob[index]))
            index += 1
    return re.compile("^" + "".join(out) + "$")


def matches(glob: str, repo: str, path: str) -> bool:
    """Whether ``repo``'s ``path`` matches ``glob``, a glob of the charter (``repo:glob`` for one repo only)."""
    scope, sep, rest = glob.partition(":")
    if sep and rest and "/" not in scope and "*" not in scope:
        if scope != repo:
            return False
        glob = rest
    glob = glob.lstrip("/")
    return bool(_pattern(glob).match(path)) or (glob.endswith("/**") and path == glob[:-3])


def _first(globs, repo: str, path: str) -> str | None:
    return next((glob for glob in globs if matches(glob, repo, path)), None)


def split_target(text: str) -> tuple[str, str] | None:
    """``("repo", "path")`` of ``repo:path``; None when either part is missing or the path is not a repo path."""
    repo, sep, path = (text or "").partition(":")
    found = normalize_path(path) if sep else None
    if not repo.strip() or found is None:
        return None
    return repo.strip(), found


# The rules


def _knowledge_raises(repo: str, path: str, knowledge) -> list[tuple[int, str]]:
    """The raises of knowledge.yaml under kind kg_source: a note before the pull request's diff is known (``knowledge``
    None), at least tier 1 for a file the hub could not read at both ends or one that changes more than SOURCE_KEYS."""
    keys = ", ".join(SOURCE_KEYS)
    if knowledge is None:
        return [
            (
                0,
                f"{repo}:{path} is knowledge.yaml: tier 0 while the pull request's diff edits only the {keys} of the "
                "git and harness sources it has, which the hub reads key by key",
            )
        ]
    others = knowledge.get((repo, path))
    if others is None:
        return [(1, f"the hub could not read {repo}:{path} at both ends of the change, key by key: at least tier 1")]
    if not others:
        return []
    shown = "; ".join(others[:SHOWN_CHANGES]) + (
        f"; {len(others) - SHOWN_CHANGES} more" if len(others) > SHOWN_CHANGES else ""
    )
    return [
        (1, f"{repo}:{path} changes more than the {keys} of its git and harness sources ({shown}): at least tier 1")
    ]


def _path_raises(kind: str, repo: str, path: str, protected, unknown, knowledge) -> list[tuple[int, str]]:
    """(level, why) of each rule a path raises the tier by, in the order the reasons name them; level 0 is a note."""
    found = _first(protected, repo, path)
    if found is not None:
        return [(3, f"{repo}:{path} is protected by the charter ({found}): tier 3")]
    found = _first(HARD_PATHS, repo, path)
    if found is not None:
        return [(3, f"{repo}:{path} is CI, lint or test configuration, or a plan's copy ({found}): tier 3")]
    raises = []
    found = _first(SCHEMA_PATHS, repo, path)
    if found is not None:
        raises.append((2, f"{repo}:{path} is a schema or a migration ({found}): at least tier 2"))
    elif kind == KNOWLEDGE_KIND and path in KNOWLEDGE_PATHS:
        raises += _knowledge_raises(repo, path, knowledge)
    elif _first(DOC_PATHS, repo, path) is None and _first(TEST_PATHS, repo, path) is None:
        raises.append((1, f"{repo}:{path} is code, neither documentation nor a test: at least tier 1"))
    why = unknown.get((repo, path))
    if why:
        raises.append(
            (
                1,
                f"the knowledge graph {why}: the raise by what a change reaches in the graph cannot tell what editing "
                f"{repo}:{path} reaches, so at least tier 1",
            )
        )
    return raises


def path_raises(repo: str, path: str, protected=()) -> list[tuple[int, str]]:
    """(level, why) of each path rule of ``tier_of`` that ``repo:path`` raises a change by, whatever its kind: a
    protected path or one of HARD_PATHS 3, a schema or a migration 2, code 1; nothing for documentation or a test. The
    risk score of the merge queue reads it (``evo_agents.hub.merge_gate``), as a merge request names no kind."""
    return _path_raises("", repo, path, protected, {}, None)


def tier_of(
    kind: str,
    paths: list[tuple[str, str]],
    protected: list[str],
    impacted: list[tuple[str, str]] = (),
    *,
    unknown: Mapping[tuple[str, str], str] | None = None,
    knowledge: Mapping[tuple[str, str], Sequence[str] | None] | None = None,
    fix_of: int | None = None,
) -> Tier:
    """The tier of a change of ``kind`` to ``paths`` ((repo, path) each), under the charter's ``protected`` globs,
    whose edits reach ``impacted`` ((repo, path) each, from the knowledge graph); ``unknown`` says, of a path, why the
    graph cannot tell what it reaches, and ``knowledge``, of a knowledge.yaml the pull request changes, what it changes
    besides SOURCE_KEYS (None: it could not be read), None before the diff is known; ``fix_of``, the change a fix
    proposal fixes. See the module's docstring. ValueError for a kind not in CHANGE_KINDS."""
    if kind not in CHANGE_KINDS:
        raise ValueError(f"{kind!r} is not a kind of change; the kinds are {', '.join(CHANGE_KINDS)}")
    tier, what = CHANGE_KINDS[kind]
    reasons = [f"kind {kind} ({what}) is tier {tier}"]

    def raise_to(level: int, why: str) -> None:
        nonlocal tier
        if level > tier:
            tier = level
            reasons.append(why)
        elif (level == 3 or level == 0) and why not in reasons:
            reasons.append(why)

    for repo, path in paths:
        for level, why in _path_raises(kind, repo, path, protected, unknown or {}, knowledge):
            raise_to(level, why)
    named = set(paths)
    for repo, path in impacted:
        if (repo, path) in named:
            continue
        found = _first(protected, repo, path) or _first(HARD_PATHS, repo, path)
        if found is not None:
            raise_to(
                3, f"the change reaches {repo}:{path} in the knowledge graph, which is protected ({found}): tier 3"
            )
    if fix_of is not None:
        raise_to(
            FIX_TIER,
            f"it fixes the rule of change #{fix_of}, whose probe recurred two weeks in a row: at least tier {FIX_TIER}",
        )
    return Tier(tier, reasons)


def replay_tier(kind: str, found: Tier, *, harness: str | None, won: str | None, fix: bool = False) -> Tier:
    """The tier a change of ``kind`` merges with, the rules having given it ``found``: tier 0 when its pull request
    edits ``harness`` alone (what it edits, ``replay.harness_only``; None when it edits more) and its replays won
    (``won``, why; None when they did not), its kind is one of REPLAY_KINDS, no rule made it tier 3 and it is no fix
    proposal (``fix``); ``found`` otherwise."""
    if fix or found.tier in (0, 3) or kind not in REPLAY_KINDS or not harness or not won:
        return found
    return Tier(
        0, [*found.reasons, f"it edits {harness} alone and its replays on the holdout tasks won ({won}): tier 0"]
    )


def fingerprint(kind: str, paths: list[tuple[str, str]], title: str) -> str:
    """What makes two proposals the same for the rule of rejected ones: the kind and the paths, or the kind and the
    title (case and blanks folded) when it names no path; as a hex sha256."""
    if paths:
        key = [kind, sorted({f"{repo}:{path}" for repo, path in paths})]
    else:
        key = [kind, " ".join(title.casefold().split())]
    return hashlib.sha256(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def drops(new_evidence: int, rejected_evidence: int) -> bool:
    """Whether a proposal with ``new_evidence`` items is dropped as the rejected one with ``rejected_evidence``:
    unless it has at least EVIDENCE_FACTOR times as many."""
    return new_evidence < EVIDENCE_FACTOR * max(rejected_evidence, 1)
