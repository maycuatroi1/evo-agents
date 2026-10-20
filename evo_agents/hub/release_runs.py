"""A run of a project's release cycle (kind ``release``) as the hub and the worker share it: the range it releases,
the version it picks, the branch it pushes, and why the hub refuses to land it. Pure functions, standard library only,
so the api, the hub's worker and the daemon share them. ``evo_agents.hub.releases`` describes the schedule and the
cycle; ``evo_agents.hub.server.release_claims`` hands the run its prompt and takes its branch, and
``evo_agents.hub.server.release_land`` lands it.

When a check opens a cycle it reads the range of the release once (``span_of``): the commits from the newest release
tag to the target's commit as GitHub compares them, at most MAX_SPAN_COMMITS, the pull requests their merge commits
name (``pulls_of``) and the migrations the target's tree adds under MIGRATIONS_DIR (``new_migrations``); the cycle
keeps it, and the claim of its cut run builds the prompt from it (``cut_prompt``), so a claim reads nothing of GitHub.
The agent picks the version from the base tag's by the rule of ``bump_of``: the next minor when the range adds a
migration or merges a pull request from a branch FEATURE_PREFIX..., else the next patch, never a new major; writes
one line ``Decision: ...``; runs BUMP_SCRIPT; looks in the range for the three faults that fail only on macOS and
Windows and fixes them; commits on the run's branch; and writes the version, the Decision line and its summary in
``.evo-run/result.json`` (RESULT_VERSION, RESULT_DECISION). It neither pushes nor tags: the daemon pushes HEAD to
``release/<version>`` (``branch_of``), the one branch a release run pushes (``is_release_branch``,
``gitops.check_push``), and tells the hub.

Once the run is done the hub lands it with its own GitHub App: it refuses (``land_problem``) a version that is not
X.Y.Z, changes the major version, is not greater than the version VERSION_FILE names on the default branch, whose tag
exists already, or that VERSION_FILE on the branch does not name; else it opens the pull request of the branch into
the default branch, merges it with a merge commit and tags that commit ``<prefix><version>``. A refusal, or GitHub
failing LAND_TRIES passes in a row, stops the cycle with the reason (LAND_STATES).
"""

from __future__ import annotations

import re

from evo_agents.hub.releases import CONTROL, VERSION, clip_reason, short

BRANCH_PREFIX = "release/"  # the branches a release run pushes: release/<version>
VERSION_FILE = "evo_agents/__init__.py"  # the file whose __version__ the default branch and a release branch name
VERSION_LINE = re.compile(r'^__version__ = "([^"\n]*)"\s*$', re.MULTILINE)
BUMP_SCRIPT = "scripts/bump_version.py"  # sets the version in every file that names it
MIGRATIONS_DIR = "evo_agents/hub/migrations/versions/"  # a file new here makes the release a minor one
FEATURE_PREFIX = "feat/"  # a pull request merged from such a branch makes the release a minor one
RESULT_VERSION = "version"  # the keys of .evo-run/result.json a release run's agent writes, besides "summary"
RESULT_DECISION = "decision"
DECISION_PREFIX = "Decision:"
LAND_STATES = ("tagged", "refused", "failed")  # the hub tagged its merge commit; refused to; GitHub kept failing
LAND_TRIES = 5  # passes in a row GitHub may fail while the hub lands a run before the cycle stops

MAX_SPAN_COMMITS = 250  # what GitHub's comparison lists of a range at most
SPAN_PAGE = 100
MAX_SPAN_PAGES = 3
SHOWN_COMMITS = 120  # of a range, in the prompt
SHOWN_PULLS = 80
SHOWN_MIGRATIONS = 40
MAX_LINE_CHARS = 160  # of each commit's or pull request's title the hub keeps
MAX_URL_CHARS = 500

MERGE_PULL = re.compile(r"^Merge pull request #([1-9][0-9]{0,9}) from [^/\s]+/(\S+)$")
SQUASHED_PULL = re.compile(r"\(#([1-9][0-9]{0,9})\)$")


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """(0, 25, 0) of ``0.25.0``; None for a text that is not X.Y.Z."""
    found = VERSION.match(text or "")
    return None if found is None else (int(found.group(1)), int(found.group(2)), int(found.group(3)))


def version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def bumped(version: tuple[int, int, int], bump: str) -> tuple[int, int, int]:
    """The next ``bump`` (minor or patch) of ``version``; a release never bumps the major."""
    major, minor, patch = version
    return (major, minor + 1, 0) if bump == "minor" else (major, minor, patch + 1)


def branch_of(version: str) -> str:
    """The branch a release run of ``version`` pushes: release/<version>."""
    return f"{BRANCH_PREFIX}{version}"


def is_release_branch(branch: str) -> bool:
    """Whether ``branch`` is the branch of a release run: release/ then a version X.Y.Z."""
    return branch.startswith(BRANCH_PREFIX) and parse_version(branch[len(BRANCH_PREFIX) :]) is not None


def file_version(text: str | None) -> str | None:
    """The version VERSION_FILE's text sets (its ``__version__ = "..."`` line), or None."""
    found = VERSION_LINE.search(text or "")
    return None if found is None else found.group(1)


def one_line(text: object, limit: int = MAX_LINE_CHARS) -> str:
    """``text`` as one line of at most ``limit`` characters, control characters made spaces."""
    return " ".join(CONTROL.sub(" ", str(text or "")).split())[:limit]


def new_migrations(base: dict[str, str], head: dict[str, str]) -> list[str]:
    """The migrations the tree ``head`` ({path: blob}) adds under MIGRATIONS_DIR that ``base`` lacks, by name."""
    names = (path[len(MIGRATIONS_DIR) :] for path in head if path.startswith(MIGRATIONS_DIR) and path not in base)
    return sorted(name for name in names if name.endswith(".py") and "/" not in name)


def commit_of(item: dict) -> dict | None:
    """{sha, title, body} of one commit GitHub lists, the first line its title, the first line after it its body;
    None for an item without a commit's name."""
    sha = item.get("sha")
    commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
    if not isinstance(sha, str) or not sha:
        return None
    lines = [line.strip() for line in str(commit.get("message") or "").splitlines()]
    title = lines[0] if lines else ""
    body = next((line for line in lines[1:] if line), "")
    return {"sha": sha, "title": one_line(title), "body": one_line(body)}


def pulls_of(commits: list[dict]) -> list[dict]:
    """The pull requests the ``commits`` (``commit_of``) of a range name, each {number, branch, title} once: a merge
    commit's ``Merge pull request #N from owner/branch`` (its body the pull request's title), or a squashed one's
    ``... (#N)`` (no branch)."""
    found: dict[int, dict] = {}
    for commit in commits:
        merged = MERGE_PULL.match(commit["title"])
        if merged is not None:
            number = int(merged.group(1))
            found.setdefault(number, {"number": number, "branch": merged.group(2), "title": commit["body"]})
            continue
        squashed = SQUASHED_PULL.search(commit["title"])
        if squashed is not None:
            number = int(squashed.group(1))
            title = commit["title"][: squashed.start()].strip()
            found.setdefault(number, {"number": number, "branch": None, "title": title})
    return list(found.values())


def span_of(commits: list[dict], total: int | None, migrations: list[str], problem: str | None = None) -> dict:
    """The range of a cycle as the hub keeps it: its commits ({sha, title}, oldest first, at most
    MAX_SPAN_COMMITS), how many GitHub counts, the pull requests they name, the migrations it adds, and what kept the
    hub from listing the commits (None when nothing did)."""
    kept = commits[:MAX_SPAN_COMMITS]
    return {
        "commits": [{"sha": item["sha"], "title": item["title"]} for item in kept],
        "total": total if total is not None else len(kept),
        "pulls": pulls_of(kept),
        "migrations": list(migrations),
        "problem": None if problem is None else clip_reason(problem),
    }


def bump_of(span: dict | None) -> tuple[str, str]:
    """(minor or patch, why) by the rule, of what ``span`` holds."""
    span = span or {}
    migrations = [name for name in span.get("migrations") or [] if isinstance(name, str)]
    if migrations:
        return "minor", f"the range adds the migration {', '.join(migrations[:3])}"
    features = [
        item
        for item in span.get("pulls") or []
        if isinstance(item, dict) and str(item.get("branch") or "").startswith(FEATURE_PREFIX)
    ]
    if features:
        named = ", ".join(f"#{item['number']} from {item['branch']}" for item in features[:3])
        return "minor", f"the range merges {named}"
    if span.get("problem"):
        return "patch", "no migration is new, and the hub could not list the pull requests: check them with git"
    return "patch", "the range adds no migration and merges no pull request from a branch feat/..."


def land_problem(
    version: str, main_version: str | None, head_version: str | None, *, branch: str, tag: str, tag_exists: bool
) -> str | None:
    """Why the hub refuses to land the release ``version`` pushed on ``branch``, whose VERSION_FILE names
    ``head_version``, while the default branch's names ``main_version`` and the tag ``tag`` exists or not; None when
    it lands it."""
    found = parse_version(version)
    if found is None:
        return f"{version!r} is not a version X.Y.Z"
    main = parse_version(main_version)
    if main is None:
        return (
            f"{VERSION_FILE} on the default branch names no version X.Y.Z ({main_version!r}): nothing to compare with"
        )
    if found[0] != main[0]:
        return f"{version} changes the major version of {main_version}, the default branch's: a release never does"
    if found <= main:
        return f"{version} is not greater than {main_version}, the version {VERSION_FILE} names on the default branch"
    if tag_exists:
        return f"the tag {tag} exists already: a release tag is never made twice"
    if head_version != version:
        return f"{VERSION_FILE} on {branch} names {head_version!r}, not {version}: {BUMP_SCRIPT} did not run on it"
    return None


def _range_lines(span: dict | None, base_tag: str | None, base_commit: str | None, target: str) -> list[str]:
    """What the prompt says of the range: its size, its migrations, its pull requests and its commits."""
    span = span or {}
    since = f"the newest release tag {base_tag} at {short(base_commit)}" if base_tag else "the first commit"
    commits = [item for item in span.get("commits") or [] if isinstance(item, dict)]
    total = span.get("total") if isinstance(span.get("total"), int) else len(commits)
    lines = [f"- From {since} to {target}: {total} commits."]
    migrations = [str(name) for name in span.get("migrations") or []]
    shown = ", ".join(migrations[:SHOWN_MIGRATIONS]) or "none"
    more = f" and {len(migrations) - SHOWN_MIGRATIONS} more" if len(migrations) > SHOWN_MIGRATIONS else ""
    lines.append(f"- New migrations under {MIGRATIONS_DIR}: {shown}{more}.")
    if span.get("problem"):
        lines.append(f"- The hub could not list the commits ({span['problem']}): read them with git.")
        return lines
    pulls = [item for item in span.get("pulls") or [] if isinstance(item, dict)]
    lines.append(f"- Pull requests merged in the range: {len(pulls) or 'none the hub read'}.")
    for item in pulls[:SHOWN_PULLS]:
        where = f" from {item['branch']}" if item.get("branch") else ""
        lines.append(f"  - #{item['number']}{where}: {one_line(item.get('title')) or '(no title)'}")
    if len(pulls) > SHOWN_PULLS:
        lines.append(f"  - and {len(pulls) - SHOWN_PULLS} more")
    lines.append("- Commits, oldest first:")
    lines += [f"  - {short(item.get('sha'))} {one_line(item.get('title'))}" for item in commits[:SHOWN_COMMITS]]
    if total > min(len(commits), SHOWN_COMMITS):
        lines.append(f"  - and {total - min(len(commits), SHOWN_COMMITS)} more")
    return lines


def _closing(prefix: str, summary: str) -> list[str]:
    """The steps every release prompt ends with: commit, never push or tag, and the result file."""
    return [
        "4. Commit all of it on the run's branch, the first line of each commit saying what it does (the bump: "
        "`Release <version>: ...`). The repo is public: no commit message names a customer or a person outside the "
        "project. Do not push, tag or open a pull request: the worker pushes the branch "
        f"{BRANCH_PREFIX}<version>, and the hub opens its pull request into the default branch, merges it with a "
        f"merge commit and tags that commit {prefix}<version>.",
        f'5. Write .evo-run/result.json as a JSON object with "{RESULT_VERSION}" (X.Y.Z), "{RESULT_DECISION}" (the '
        f"{DECISION_PREFIX} line, which the public pull request quotes: it names no customer or person either) and "
        f'"summary" ({summary}). Keep .evo-run/ out of your commits.',
    ]


FAULTS = [
    "   - a test module that imports SQLAlchemy or psycopg at its top without `pytest.importorskip` before it;",
    '   - a file written with `write_text`, or `open` in text mode, without `newline="\\n"` whose bytes a test then '
    "compares: on Windows its lines end in CRLF;",
    "   - a `sqlite3` connection that is never closed (`with sqlite3.connect(...)` commits but does not close it): "
    "Windows keeps its file locked.",
]


def cut_prompt(
    *,
    project: str,
    run_id: int,
    cycle_id: int,
    repo: str,
    target_ref: str,
    target_commit: str,
    base_tag: str | None,
    base_commit: str | None,
    tag_prefix: str,
    span: dict | None,
) -> str:
    """The prompt of the cut run of cycle ``cycle_id``: the range, the rule of the version, the bump, the three faults,
    the commit and the result file."""
    base = parse_version(base_tag[len(tag_prefix) :]) if base_tag and base_tag.startswith(tag_prefix) else None
    bump, why = bump_of(span)
    suggested = version_text(bumped(base, bump)) if base else None
    target = f"{target_ref} at {short(target_commit)}"
    rule = (f"1. Pick the version from {base_tag}'s, {version_text(base)}: " if base else "1. Pick the version: ") + (
        f"the next minor (X.Y+1.0) when the range adds a migration under {MIGRATIONS_DIR} or merges a pull request "
        f"from a branch {FEATURE_PREFIX}..., else the next patch (X.Y.Z+1); never a new major."
    )
    if suggested:
        rule += f" By what the hub read, that is {suggested}, a {bump}: {why}. Check it against git."
    rule += (
        f" When {VERSION_FILE} at HEAD names that version or a later one already, take the next one of the same kind "
        f"after it. Write one line that starts with `{DECISION_PREFIX} ` and says the version and why."
    )
    lines = [
        f"You are the release agent of project {project}, run #{run_id}: cut the next release of {repo} from {target} "
        f"for cycle #{cycle_id} of its daily release. Nobody answers a question during this run: decide, act and "
        "report.",
        "",
        "The range of the release, as the hub read it when it opened the cycle:",
        *_range_lines(span, base_tag, base_commit, target),
        f"`git log --oneline {base_tag + '..HEAD' if base_tag else 'HEAD'}` in the worktree lists the whole range.",
        "",
        f"Your worktree of {repo} is at {short(target_commit)}, on a branch of the run, with origin fetched.",
        "",
        rule,
        f"2. Run `python {BUMP_SCRIPT} <version>` in the worktree.",
        f"3. In the diff of the range (`git diff {base_tag or 'HEAD~1'} HEAD`), look for three faults that fail only "
        "on macOS and Windows, and fix each one you find:",
        *FAULTS,
        "   Run the tests of each file you change.",
        *_closing(tag_prefix, "what you changed, and which of the three faults you found and fixed, or that none"),
    ]
    return "\n".join(lines) + "\n"


def fix_prompt(
    *,
    project: str,
    run_id: int,
    cycle_id: int,
    repo: str,
    red_tag: str,
    start_commit: str,
    tag_prefix: str,
    failures: str | None = None,
) -> str:
    """The prompt of a fix run of cycle ``cycle_id``: the red tag stays, and the next patch fixes it forward;
    ``failures`` is what the hub read of its CI."""
    red = parse_version(red_tag[len(tag_prefix) :]) if red_tag.startswith(tag_prefix) else None
    following = version_text(bumped(red, "patch")) if red else "the next patch"
    lines = [
        f"You are the release agent of project {project}, run #{run_id}: the CI of the release tag {red_tag} of {repo} "
        f"failed, and cycle #{cycle_id} of its daily release fixes it forward. The tag {red_tag} stays as it is: you "
        "cut the next patch with the fix. Nobody answers a question during this run: decide, act and report.",
        "",
        f"Your worktree of {repo} is at {short(start_commit)}, the commit of {red_tag}, on a branch of the run, with "
        "origin fetched.",
        "",
        *(["What failed:", failures.strip(), ""] if failures else []),
        f"1. The version is {following}, the next patch after {red_tag}. Write one line that starts with "
        f"`{DECISION_PREFIX} ` and says the version and why.",
        f"2. Run `python {BUMP_SCRIPT} <version>` in the worktree.",
        "3. Find why the CI failed and fix it forward. Never delete a test, never skip one other than with "
        "`pytest.importorskip` or a skip on one platform with its reason, and never make an assert always true. Look "
        "also for these three faults that fail only on macOS and Windows:",
        *FAULTS,
        "   Run the tests of each file you change.",
        *_closing(tag_prefix, "why the CI failed and what you changed"),
    ]
    return "\n".join(lines) + "\n"
