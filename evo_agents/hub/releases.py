"""The daily release of a project as the hub models it: its schedule, the check the hub makes at the schedule's local
time, and the cycle a check opens. Pure functions over JSON values and datetimes, standard library only, so the api,
the hub's worker and the command line share them. ``evo_agents.hub.server.releases`` holds the routes,
``evo_agents.hub.server.release_check`` the check and the job ``hub.release_schedules`` built on them, and
``evo_agents.hub.server.release_github`` what the check reads of GitHub.

A project has at most one release schedule, apart from the schedules and the charter of the night shift: the Curator's
pause, resume and circuit breaker never touch it, and pausing it never touches the night shift. An admin of the project
sets it (``evo-agents hub release set``), and that admin is its owner: the runs of its cycles are dispatched as them,
pinned to no worker, so any worker of theirs, or one shared with them, that runs release runs may take them. The
schedule names a repo of the project on GitHub, a local time and an IANA time zone (DEFAULT_AT in DEFAULT_TIMEZONE),
the ref whose commit a release would ship (DEFAULT_TARGET_REF, the tag nightly, which the deploy of the hub moves once
main runs), the pattern of the release tags (a literal prefix then ``*``, DEFAULT_TAG_PATTERN), the workflow that
publishes a tag (DEFAULT_WORKFLOW), the paths whose files make the package (DEFAULT_PATHS: an entry ending in ``/`` is
a directory, any other one a file or a directory of that name) and the cap of what one cycle may cost
(DEFAULT_CYCLE_USD).

Each minute the job looks at every schedule whose local time came in the last CATCH_UP_MINUTES (``due_day``) and that
has no check of that local day yet. A check reads, without any model, the newest tag the pattern matches
(``latest_tag``: the highest version X.Y.Z after the prefix) and the commit of the target ref, and compares the files
of the two commits under the packaging paths (``changed_paths``). It opens a cycle, and queues its first run, of kind
release in mode cut, only when at least one packaging path changed, no cycle of the schedule is unfinished (UNFINISHED)
and the schedule is not paused; that run's cost cap is what is left of the cycle's cap. Every check is kept with its
outcome (CHECK_OUTCOMES) and why, and the scheduled check of a local day happens once: a second pass of the job, or a
hub that restarted, finds it and does nothing, while a check that failed (outcome ``error``: GitHub did not answer, the
App is not installed, no tag matches) is tried again on the next minutes, ERROR_TRIES times in all that day. An admin
may also check now (``evo-agents hub release run``): the same gates, recorded as a manual check, which a day may hold
any number of.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

RELEASE_KIND = "release"  # the kind of a run of a cycle (runs.RUN_KINDS)
RUN_MODES = ("cut", "fix")  # a run that cuts a new version, and one that fixes a red tag forward
CYCLE_STATES = ("open", "stopped", "published", "closed")
UNFINISHED = ("open", "stopped")  # a schedule has at most one cycle in these; the check opens none while it does
CHECK_TRIGGERS = ("schedule", "manual")  # the job at the schedule's time, or an admin's `release run`
# opened: a cycle opened and its first run queued; unchanged: no packaging path changed since the newest tag; busy: a
# cycle of the schedule is unfinished; paused: the schedule is paused; owner: its owner no longer holds the admin role
# on the project; error: the check could not read what it compares (the reason says what).
CHECK_OUTCOMES = ("opened", "unchanged", "busy", "paused", "owner", "error")

DEFAULT_AT = "06:00"
DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"
DEFAULT_TARGET_REF = "nightly"
DEFAULT_TAG_PATTERN = "v*"
DEFAULT_WORKFLOW = "release.yml"
DEFAULT_PATHS = ("evo_agents/", "plugins/", "web/", ".claude-plugin/", "pyproject.toml", "uv.lock")
DEFAULT_CYCLE_USD = 200.0
CYCLE_USD = (1.0, 10_000.0)  # the cap of one cycle an admin may set

CATCH_UP_MINUTES = 60  # the job makes the day's check this long after the schedule's time, when it missed the minute
ERROR_TRIES = 3  # checks of one day that may fail before the job waits for the next day
RUNTIME = "claude-code"  # the runtime of a release run: it reports its cost, which the cycle's cap counts
RUN_MINUTES = 120  # the agent time of one release run
GRACE_SECONDS = 300  # a release run's timeout is its agent time plus this
MAX_ATTEMPTS = 1  # a release run the hub lost is not tried again: the cycle decides what comes next

MAX_PATHS = 50
MAX_PATH_CHARS = 200
MAX_REF_CHARS = 200
MAX_REASON_CHARS = 2000
MAX_TITLE_CHARS = 200
SHOWN_CHECKS = 10  # the latest checks a status lists

AT = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")  # HH:MM, 24 hours
TIME_ZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$")  # checked against Postgres's list too
TAG_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{0,50}\*$")  # a literal prefix, then *
REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
WORKFLOW = re.compile(r"^[A-Za-z0-9._-]{1,100}\.ya?ml$")
# X.Y.Z; matched with fullmatch, since $ also matches before a final newline. The pattern keeps ^ and $ for the API
# and the database, whose engines do not take \Z.
VERSION = re.compile(r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
OBJECT_NAME = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")  # a commit, as git names it


def path_problem(path: object) -> str | None:
    """Why ``path`` cannot be a packaging path, or None."""
    if not isinstance(path, str) or not path.strip():
        return "a packaging path is a non-empty text"
    if len(path) > MAX_PATH_CHARS or CONTROL.search(path):
        return f"packaging path {path[:40]!r} is over {MAX_PATH_CHARS} characters or holds a control character"
    if path.startswith("/") or ".." in path.split("/") or "*" in path or path != path.strip():
        return f"packaging path {path!r} is not relative to the repo's root: no leading /, no .., no *, no spaces"
    return None


def paths_problem(paths: list[str]) -> str | None:
    """Why ``paths`` cannot be a schedule's packaging paths, or None."""
    if not paths:
        return "a release schedule names at least one packaging path"
    if len(paths) > MAX_PATHS:
        return f"a release schedule names at most {MAX_PATHS} packaging paths"
    for path in paths:
        problem = path_problem(path)
        if problem:
            return problem
    if len(set(paths)) != len(paths):
        return "the packaging paths name one path twice"
    return None


def ref_problem(ref: str) -> str | None:
    """Why ``ref`` cannot be a target ref (``nightly``, ``main``, ``refs/heads/main``), or None."""
    if not REF.match(ref) or ".." in ref or "//" in ref or ref.startswith("/") or ref.endswith(("/", ".lock", ".")):
        return f"{ref!r} is not a git ref name: letters, digits, . _ / -, no .., no // and no / at either end"
    return None


def target_refs(ref: str) -> tuple[str, ...]:
    """The full refs ``ref`` may name, the first that exists winning: itself when it is one, else the tag, then the
    branch."""
    return (ref,) if ref.startswith("refs/") else (f"refs/tags/{ref}", f"refs/heads/{ref}")


def tag_prefix(pattern: str) -> str:
    """The literal part of a tag pattern: ``v`` of ``v*``."""
    return pattern[:-1]


def tag_version(name: str, pattern: str) -> tuple[int, int, int] | None:
    """The version of tag ``name`` under ``pattern``: (0, 24, 1) of ``v0.24.1`` under ``v*``; None for a tag the pattern
    does not match or whose rest is not X.Y.Z (a pre-release, a word)."""
    prefix = tag_prefix(pattern)
    found = VERSION.fullmatch(name[len(prefix) :]) if name.startswith(prefix) else None
    return None if found is None else (int(found.group(1)), int(found.group(2)), int(found.group(3)))


def latest_tag(tags: dict[str, str], pattern: str) -> tuple[str, str] | None:
    """(name, commit) of the tag of highest version among ``tags`` ({name: commit}) that ``pattern`` matches."""
    versioned = [(tag_version(name, pattern), name) for name in tags]
    found = max(((version, name) for version, name in versioned if version is not None), default=None)
    return None if found is None else (found[1], tags[found[1]])


def under(path: str, entry: str) -> bool:
    """Whether file ``path`` of a repo is under packaging path ``entry``: inside it when it ends in ``/``, else that
    file itself or inside a directory of that name."""
    if entry.endswith("/"):
        return path.startswith(entry)
    return path == entry or path.startswith(entry + "/")


def changed_paths(base: dict[str, str], head: dict[str, str], entries: list[str]) -> list[str]:
    """The packaging paths of ``entries`` whose files differ between the trees ``base`` and ``head`` ({path: blob}):
    a file added, removed, or with other content."""
    changed = []
    for entry in entries:
        before = {path: blob for path, blob in base.items() if under(path, entry)}
        after = {path: blob for path, blob in head.items() if under(path, entry)}
        if before != after:
            changed.append(entry)
    return changed


def due_day(local_now: datetime, at: str, catch_up: int = CATCH_UP_MINUTES) -> date | None:
    """The local day whose check is due at ``local_now`` (a naive local time) for a schedule at ``at``: the day whose
    ``at`` came in the last ``catch_up`` minutes, that moment included; None when none did."""
    hours, minutes = (int(part) for part in at.split(":"))
    for day in (local_now.date(), local_now.date() - timedelta(days=1)):  # the day before, for a time near midnight
        moment = datetime.combine(day, time(hours, minutes))
        if moment <= local_now < moment + timedelta(minutes=catch_up):
            return day
    return None


def short(commit: str | None) -> str:
    return (commit or "")[:12]


def cut_title(repo: str, base_tag: str | None, target_ref: str, target_commit: str) -> str:
    """The title of a cycle's cut run: what it releases, from where."""
    since = f" since {base_tag}" if base_tag else ""
    title = f"Release {repo}: cut {target_ref} at {short(target_commit)}{since}"
    return title[:MAX_TITLE_CHARS]


def fix_title(repo: str, red_tag: str) -> str:
    """The title of a fix run: the red tag it fixes forward."""
    return f"Release {repo}: fix {red_tag} forward with the next patch"[:MAX_TITLE_CHARS]


def run_values(*, project_id: int, owner_id: int, repo: str, title: str, budget: dict) -> dict:
    """The columns of a run of a cycle (kind release): dispatched as the cycle's owner by the release schedule, pinned
    to no worker, headless, with ``budget`` (``run_budget``) and tried once."""
    return {
        "kind": RELEASE_KIND,
        "project_id": project_id,
        "title": title,
        "dispatched_by": owner_id,
        "dispatched_via": "release",
        "pinned_worker_id": None,
        "requested_runtime": RUNTIME,
        "runtime": RUNTIME,
        "model": None,
        "mode": "headless",
        "approval": "auto",
        "timeout_s": budget["max_seconds"] + GRACE_SECONDS,
        "max_attempts": MAX_ATTEMPTS,
        "repos": [{"repo": repo}],
        "budget": budget,
    }


def run_budget(left_usd: float) -> dict:
    """The caps of a release run when ``left_usd`` of its cycle's cap is left: that cost, and RUN_MINUTES of agent
    time."""
    return {"max_usd": round(max(left_usd, 0.0), 4), "max_seconds": RUN_MINUTES * 60}


def clip_reason(text: str) -> str:
    """``text`` as a check's or a cycle's reason: one line, at most MAX_REASON_CHARS."""
    return " ".join(CONTROL.sub(" ", text).split())[:MAX_REASON_CHARS] or "no reason given"
