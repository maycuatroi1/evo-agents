"""The autonomy policy of a project (plan gitlab-merge-queue, criterion a2): what the hub may do on its own in the
project's repos, first of all whether it merges their merge requests on GitLab, as keys an admin of the project
writes and the hub keeps as revisions (``evo_agents.hub.server.autonomy_policies``, table autonomy_policies).

A body is an object of sections. ``merge``, ``judge``, ``breaker``, ``audit``, ``loop``, ``context`` and ``routing``
hold the keys of KEYS; ``repos`` maps a repo of the project to the keys of REPO_KEYS. A body holds only the keys its
writer set. A key it leaves out reads as its default (``effective``), so a key a later release adds reads its default
in every revision written before it, and a value an older release took that this one refuses reads as the default too.
A project with no revision has every repo off: the hub merges nothing on its own, as before the policy (criterion a9).

Outside a body a key is named by its dotted path: ``merge.hold_until``, ``repos.<repo>.mode``,
``repos.<repo>.jobs.uat_check``. A repo's name may hold a dot, so ``key_path`` reads a repo key from its known end.
The policy has no key that caps a diff or the merges of a day (the member's answers 4 and 8 of 2026-10-21).

``repos.<repo>.merge_check`` (plan super-board-parity-2) is off by default: on, for a repo on github.com alone, the hub
judges a pull request a run opened at its head before the run merges it (``evo_agents.hub.server.merge_check``).

``loop`` (plan loop-engine, criterion a4) holds the keys of a project's Loop (``evo_agents.hub.server.loop``): whether
it runs, the worker of its author and plan runs, the time zone of its schedule, how many work items of each cycle run
at once and how many runs of a repo, how many runs it hands out an hour and how many work items it may make an hour
before its breaker trips, how long a baseline stays approved before a work item comes of it, the weekday and time of
its weekly look at each goal, whether a frame task must be in an active sprint, the turns and minutes of the author
and plan runs it hands out, and whether a solution goes through use cases of SRS documents (``loop.use_cases``, plan
problem-oriented-hub: always, never, or auto, which does when the plugin srs is on and the project has an SRS
document not archived). It has no estimate and no budget (the member's answer 1 of 2026-10-21, checkpoint of step 1).
``loop.enabled`` is false by default, so a revision written before these keys reads with the Loop off.

``context.answers`` (plan active-context-answers) is false by default: true adds to the prompt of every run of the project
the confirmed answers that match its work (``evo_agents.hub.server.context_answers``). The revision that sets it true is
the first week of the trial that compares the 28 days after with the 28 before.

``routing`` (plan super-board-parity-2, criterion a6) is off by default: ``routing.enabled`` true has the worker rate a
Claude Code run that names no model easy, medium or hard, with one short turn of ``routing.scorer`` on the run's
prompt, and start its agent on the model ``routing.ladder`` gives that difficulty (``evo_agents.worker.model_router``).

Standard library only: the server checks bodies with it, and the client may import it.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from evo_agents.hub.credentials import GITHUB_HOST, normalize_origin

OFF, AUTO = MODES = ("off", "auto")  # repos.<repo>.mode
ON = "on"  # repos.<repo>.merge_check, with OFF
RELEASES = ("immediate", "after_uat", "manual")  # repos.<repo>.prod_release
HOLDS = ("merged", "uat_check", "prod_check")  # merge.hold_until
JOBS = ("uat_check", "prod_deploy", "prod_check", "rollback")  # repos.<repo>.jobs.*
REPOS = "repos"
MAX_BODY_BYTES = 64 * 1024  # of a body as JSON
MAX_REPOS = 1000  # as many as a project lists
MAX_RISK_PATHS = 100
MAX_GLOB_CHARS = 500
MAX_NOTE_CHARS = 1000
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")  # loop.brainstorm.weekday
TIME_OF_DAY = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")  # loop.brainstorm.time, HH:MM on a 24-hour clock
TIME_ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}")  # loop.timezone, as an IANA name
USE_CASE_MODES = ("always", "never", "auto")  # loop.use_cases (loop.model.USE_CASE_MODES)
MAX_TIME_ZONE_CHARS = 64
LABEL = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}(=[A-Za-z0-9][A-Za-z0-9._/-]{0,99})?"
)  # loop.labels, as a placement label

# Why a value is wrong, or None when it is right.
Check = Callable[[Any], str | None]


@dataclass(frozen=True)
class Key:
    """One key of the policy: its dotted name within its section (or within a repo), its default, its check, and
    what it means."""

    name: str
    default: Any
    check: Check
    meaning: str


@dataclass(frozen=True)
class RepoFacts:
    """What the hub knows of a repo of the project: where it lives and its default branch."""

    origin: str | None = None
    default_branch: str | None = None

    @property
    def on_github(self) -> bool:
        return self.origin is not None and urlsplit(normalize_origin(self.origin)).hostname == GITHUB_HOST


# Checks


def _one_of(*options: str) -> Check:
    def check(value):
        return None if isinstance(value, str) and value in options else f"one of {', '.join(options)}"

    return check


def _is_number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _whole(low: int, high: int) -> Check:
    def check(value):
        right = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
        return None if right else f"a whole number from {low} to {high}"

    return check


def _number(low: float, high: float, *, above_low: bool = False) -> Check:
    def check(value):
        if _is_number(value) and (value > low if above_low else value >= low) and value <= high:
            return None
        return f"a number {'above' if above_low else 'from'} {low:g} {'and at most' if above_low else 'to'} {high:g}"

    return check


def _line(max_chars: int) -> Check:
    def check(value):
        right = (
            isinstance(value, str) and 0 < len(value) <= max_chars and value.isprintable() and value == value.strip()
        )
        return None if right else f"text of 1 to {max_chars} characters on one line, no space at either end"

    return check


def _optional(check: Check) -> Check:
    def optional(value):
        why = None if value is None else check(value)
        return None if why is None else f"null or {why}"

    return optional


def _labels(value) -> str | None:
    """A list of placement labels (``worker_placement``), at most ten."""
    right = (
        isinstance(value, list) and len(value) <= 10 and all(isinstance(v, str) and LABEL.fullmatch(v) for v in value)
    )
    return None if right else "a list of at most 10 labels such as os=linux or gpu"


def _flag(value) -> str | None:
    return None if isinstance(value, bool) else "true or false"


def _time_of_day(value) -> str | None:
    return None if isinstance(value, str) and TIME_OF_DAY.fullmatch(value) else "a time of day as HH:MM, 00:00 to 23:59"


def _time_zone(value) -> str | None:
    right = isinstance(value, str) and len(value) <= MAX_TIME_ZONE_CHARS and TIME_ZONE.fullmatch(value) is not None
    return None if right else f"a time zone name such as Asia/Ho_Chi_Minh, at most {MAX_TIME_ZONE_CHARS} characters"


def _globs(value) -> str | None:
    each = _line(MAX_GLOB_CHARS)
    if isinstance(value, list) and len(value) <= MAX_RISK_PATHS and all(each(item) is None for item in value):
        return None
    return f"a list of at most {MAX_RISK_PATHS} globs, each of 1 to {MAX_GLOB_CHARS} characters"


# The keys

LOOP_KEYS = (  # plan loop-engine: the Loop of the project, off by default; no estimate and no budget
    Key("loop.enabled", False, _flag, "true: the project's Loop makes work items and hands out their runs"),
    Key("loop.worker", None, _optional(_line(100)), "the worker of the Loop's author and plan runs; null: none run"),
    Key(
        "loop.pool",
        None,
        _optional(_line(100)),
        "the pool of workers the Loop's author and plan runs go to, in place of loop.worker; null: loop.worker",
    ),
    Key("loop.labels", [], _labels, "the labels a worker of the Loop's author and plan runs holds, with loop.pool"),
    Key(
        "loop.timezone",
        None,
        _optional(_time_zone),
        "the time zone of the Loop's schedule; null: the charter's, else UTC",
    ),
    Key("loop.business.max_running", 2, _whole(0, 100), "work items of the business cycle running at once"),
    Key("loop.development.max_running", 1, _whole(0, 100), "work items of the development cycle running at once"),
    Key("loop.repo_max_running", 1, _whole(1, 100), "plan runs running plus merge requests not merged, per repo"),
    Key("loop.dispatches_per_hour", 6, _whole(0, 1000), "runs the Loop hands out in 60 minutes"),
    Key("loop.max_items_per_hour", 30, _whole(1, 10000), "work items made in 60 minutes; more trips the breaker"),
    Key("loop.stable_hours", 0, _number(0, 720), "hours a baseline stays approved before a work item comes of it"),
    Key("loop.brainstorm.weekday", "monday", _one_of(*WEEKDAYS), "the day of the weekly look at each goal"),
    Key("loop.brainstorm.time", "09:00", _time_of_day, "the time of day of the weekly look at each goal"),
    Key("loop.require_sprint", False, _flag, "true: a frame task is ready only in an active sprint"),
    Key("loop.caps.author.minutes", 60, _whole(1, 1440), "the minutes of an author run the Loop hands out"),
    Key("loop.caps.author.turns", 200, _whole(1, 10000), "the turns of an author run the Loop hands out"),
    Key("loop.caps.plan.minutes", 240, _whole(1, 2880), "the minutes of a plan run the Loop hands out"),
    Key("loop.caps.plan.turns", 600, _whole(1, 10000), "the turns of a plan run the Loop hands out"),
    Key(
        "loop.use_cases",
        "auto",
        _one_of(*USE_CASE_MODES),
        "whether a claim's solution goes through use cases of SRS documents; auto: when the plugin srs is on and the "
        "project has an SRS document not archived",
    ),
)
CONTEXT_KEYS = (  # plan active-context-answers: off by default; the revision that turns it on is the trial's week 1
    Key(
        "context.answers",
        False,
        _flag,
        "true: a run's prompt ends with the confirmed answers (decisions answered, feedback memories) that match its "
        "work",
    ),
)
ROUTING_KEYS = (  # plan super-board-parity-2, criterion a6: off by default; a run's model chosen by its difficulty
    Key(
        "routing.enabled",
        False,
        _flag,
        "true: the worker rates a run that names no model easy, medium or hard and runs it on the ladder's model",
    ),
    Key("routing.scorer", "haiku", _line(200), "the model of the short turn that rates a run's difficulty"),
    Key("routing.ladder.easy", "haiku", _line(200), "the model of a run rated easy"),
    Key("routing.ladder.medium", "sonnet", _line(200), "the model of a run rated medium, and of one the scorer failed"),
    Key("routing.ladder.hard", "opus", _line(200), "the model of a run rated hard"),
)
KEYS = (
    Key(
        "merge.opt_in_label", "evo-merge", _line(255), "the label a person puts on a merge request to ask for the queue"
    ),
    Key("merge.risk_paths", [], _globs, "globs added to the risk paths the code knows"),
    Key(
        "merge.hold_until",
        "merged",
        _one_of(*HOLDS),
        "how far the merge request before must get before the next one of the same target branch starts",
    ),
    Key("merge.pipeline_minutes", 60, _whole(1, 1440), "the longest the hub waits for a pipeline or a job"),
    Key("judge.worker", None, _optional(_line(100)), "the worker that runs the Judge; null: the charter's worker"),
    Key("judge.model", None, _optional(_line(200)), "the first Judge's model; null: a Claude model not the Builder's"),
    Key("breaker.target_red_minutes", 30, _whole(1, 10080), "the target branch red this long trips the breaker"),
    Key("breaker.reverts_in_a_row", 2, _whole(1, 100), "this many automatic reverts in a row trip the breaker"),
    Key(
        "breaker.revert_rate_factor",
        2,
        _number(0, 100, above_low=True),
        "a 14-day revert rate this many times the repo's baseline trips the breaker",
    ),
    Key("breaker.revert_rate_max", 0.10, _number(0, 1), "the revert rate that trips the breaker before a baseline"),
    Key("breaker.baseline_min_merges", 10, _whole(1, 10000), "merges of the first 14 days a baseline needs"),
    Key("breaker.judge_misses_7d", 2, _whole(0, 1000), "Judge passes found wrong in 7 days that the breaker allows"),
    Key("audit.sample_rate", 0.10, _number(0, 1), "the share of merged merge requests an admin is asked to check"),
    *LOOP_KEYS,
    *CONTEXT_KEYS,
    *ROUTING_KEYS,
)
REPO_KEYS = (
    Key("mode", OFF, _one_of(*MODES), "auto: the hub merges the repo's merge requests in scope; off: a person does"),
    Key(
        "target_branch",
        None,
        _optional(_line(255)),
        "the target branch of the repo's queue; null: the repo's default branch on the hub",
    ),
    Key("prod_release", "immediate", _one_of(*RELEASES), "when the hub runs the PROD deploy after the UAT check"),
    Key("after_uat_hours", 4, _number(0, 720), "hours of a green UAT before the PROD deploy, with after_uat"),
    *(Key(f"jobs.{job}", None, _optional(_line(255)), f"the CI job of {job}; null: the repo has none") for job in JOBS),
    Key(
        "merge_check",
        OFF,
        _one_of(OFF, ON),
        "on: a run's pull request on github.com is judged at its head before the run merges it; off: it is not",
    ),
)
TARGET_BRANCH = "target_branch"
MERGE_CHECK = "merge_check"


def _tree(keys) -> dict:
    """The keys as nested objects of their dotted names, a Key at each leaf."""
    tree: dict = {}
    for key in keys:
        *groups, last = key.name.split(".")
        node = tree
        for group in groups:
            node = node.setdefault(group, {})
        node[last] = key
    return tree


TREE = _tree(KEYS)
REPO_TREE = _tree(REPO_KEYS)
SECTIONS = (REPOS, *TREE)


# Checking a body


def shown(value) -> str:
    """``value`` as JSON writes it: a problem names ``false`` where YAML read ``off`` as a boolean."""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _walk(where: str, value, tree: dict, takes: tuple[str, ...]) -> list[str]:
    if not isinstance(value, dict):
        return [f"{where}: an object of {', '.join(takes)}, not {shown(value)}"]
    problems = []
    for name, held in value.items():
        path = f"{where}.{name}" if where else str(name)
        node = tree.get(name)
        if node is None:
            problems.append(f"{path}: no such key; {where or 'the policy'} takes {', '.join(takes)}")
        elif isinstance(node, Key):
            why = node.check(held)
            if why is not None:
                problems.append(f"{path}: {why}, not {shown(held)}")
        else:
            problems.extend(_walk(path, held, node, tuple(node)))
    return problems


def _repo_problems(value, repos: Mapping[str, RepoFacts]) -> list[str]:
    if not isinstance(value, dict):
        return [f"{REPOS}: an object of the project's repos, each with its keys, not {shown(value)}"]
    if len(value) > MAX_REPOS:
        return [f"{REPOS}: at most {MAX_REPOS} repos"]
    problems = []
    for name, keys in value.items():
        where = f"{REPOS}.{name}"
        facts = repos.get(name)
        if facts is None:
            known = ", ".join(sorted(repos)) or "none"
            problems.append(f"{where}: the project has no repo {name} (its repos: {known})")
            continue
        found = _walk(where, keys, REPO_TREE, tuple(REPO_TREE))
        if not found and keys.get("mode") == AUTO and facts.on_github:
            found.append(
                f"{where}.mode: {name} is on github.com, where the Curator's tiers decide what the hub merges; auto "
                "is for a repo on GitLab"
            )
        if not found and keys.get(MERGE_CHECK) == ON and not facts.on_github:
            found.append(
                f"{where}.{MERGE_CHECK}: {name} is not on github.com; the merge check judges a pull request there, "
                "the merge queue a merge request on GitLab"
            )
        problems.extend(found)
    return problems


def problems(body, repos: Mapping[str, RepoFacts]) -> list[str]:
    """Each thing wrong with ``body`` as a policy of a project whose repos are ``repos``; empty when it is right."""
    if not isinstance(body, dict):
        return [f"the policy is an object of sections ({', '.join(SECTIONS)}), not {shown(body)}"]
    try:
        size = len(json.dumps(body, ensure_ascii=False).encode())
    except (TypeError, ValueError):
        return ["the policy holds a value JSON does not write"]
    if size > MAX_BODY_BYTES:
        return [f"the policy is {size} bytes as JSON, over the {MAX_BODY_BYTES} it may take"]
    found = _repo_problems(body[REPOS], repos) if REPOS in body else []
    rest = {name: value for name, value in body.items() if name != REPOS}
    return found + _walk("", rest, TREE, SECTIONS)


# Reading a body: every key at its value or its default


def _read(tree, dotted: str, key: Key):
    node = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return copy.deepcopy(key.default)
        node = node[part]
    return copy.deepcopy(node) if key.check(node) is None else copy.deepcopy(key.default)


def _put(tree: dict, dotted: str, value) -> None:
    *groups, last = dotted.split(".")
    for group in groups:
        tree = tree.setdefault(group, {})
    tree[last] = value


def repo_policy(body: Mapping | None, name: str, facts: RepoFacts | None = None) -> dict:
    """Every key of repo ``name`` as ``body`` sets it, else at its default; the target branch defaults to the repo's
    default branch on the hub (``facts``)."""
    named = (body or {}).get(REPOS)
    own = named.get(name) if isinstance(named, dict) else None
    own = own if isinstance(own, dict) else {}
    values: dict = {}
    for key in REPO_KEYS:
        value = _read(own, key.name, key)
        if key.name == TARGET_BRANCH and value is None and facts is not None:
            value = facts.default_branch
        _put(values, key.name, value)
    return values


def effective(body: Mapping | None, repos: Mapping[str, RepoFacts]) -> dict:
    """The policy ``body`` makes, every key at its value or its default: each repo of the project, and each repo the
    body names that the project no longer lists."""
    body = body or {}
    named = body.get(REPOS) if isinstance(body.get(REPOS), dict) else {}
    result: dict = {REPOS: {name: repo_policy(body, name, repos.get(name)) for name in sorted(set(repos) | set(named))}}
    for key in KEYS:
        _put(result, key.name, _read(body, key.name, key))
    return result


# Naming keys outside a body


def flat(tree: Mapping, prefix: str = "") -> dict[str, Any]:
    """The leaves of ``tree`` by dotted path, in the order the tree holds them."""
    leaves: dict[str, Any] = {}
    for name, value in tree.items():
        path = f"{prefix}.{name}" if prefix else str(name)
        if isinstance(value, dict):
            leaves.update(flat(value, path))
        else:
            leaves[path] = value
    return leaves


def changes(before: Mapping, after: Mapping) -> list[tuple[str, Any, Any]]:
    """(key, value before, value after) of each key whose value differs between two effective policies."""
    old, new = flat(before), flat(after)
    names = list(new) + [name for name in old if name not in new]
    return [(name, old.get(name), new.get(name)) for name in names if old.get(name) != new.get(name)]


def key_path(name: str) -> tuple[str, ...] | None:
    """The path within a body of the key dotted ``name`` names; None when the policy has no such key."""
    if any(key.name == name for key in KEYS):
        return tuple(name.split("."))
    rest = name.removeprefix(f"{REPOS}.")
    if rest == name:
        return None
    for key in REPO_KEYS:
        end = f".{key.name}"
        if rest.endswith(end) and len(rest) > len(end):
            return (REPOS, rest[: -len(end)], *key.name.split("."))
    return None


def _pruned(tree: dict) -> dict:
    """``tree`` without the objects left empty."""
    kept = {}
    for name, value in tree.items():
        if isinstance(value, dict):
            value = _pruned(value)
            if not value:
                continue
        kept[name] = value
    return kept


def with_changes(body: Mapping, sets: Mapping[str, Any], unset) -> tuple[dict, list[str]]:
    """``body`` with the keys of ``sets`` (dotted name -> value) set and those of ``unset`` cleared, objects left
    empty dropped, and each dotted name the policy does not have; the body is unchanged when there is one."""
    unknown = [name for name in (*sets, *unset) if key_path(name) is None]
    if unknown:
        return dict(body), [f"{name}: the policy has no such key" for name in unknown]
    changed = copy.deepcopy(dict(body))
    for name in unset:
        *groups, last = key_path(name)
        node = changed
        for group in groups:
            node = node.get(group) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(last, None)
    for name, value in sets.items():
        *groups, last = key_path(name)
        node = changed
        for group in groups:
            if not isinstance(node.get(group), dict):
                node[group] = {}
            node = node[group]
        node[last] = copy.deepcopy(value)
    return _pruned(changed), []
