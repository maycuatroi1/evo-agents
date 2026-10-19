"""The Curator's replays as the hub models them: the suite of replay tasks a project's own history gives, which files of
a change each arm of a replay runs with, the prompt of a replay run's agent, and what the two arms came to. Pure
functions over JSON values, standard library only, so the api, the hub's worker, the worker daemon and the tests share
them. ``evo_agents.hub.replay_suite`` builds the suites, ``evo_agents.hub.server.replays`` queues the replay runs and
keeps what they found, ``evo_agents.worker.runner.replay`` runs one; ``docs/curator.md``, "Replays", the design.

A task (``task_of``) is a step of a plan of the project that is done, whose verify is one shell line, whose repo is
known (its own, or the plan's only one), and whose evidence names the commits it made (``handoff.commits_of``), or
else a run of it reported one. Its base commit is the first parent of the oldest of those commits: the code as it was
before the step. A plan of the Curator gives no task, since its steps change the harness under test. A task's split
(``split_of``) comes from the HMAC-SHA256 of its plan and step under a secret of the hub (EVO_HUB_SESSION_SECRET): one
task in HOLDOUT_EVERY is held out. Without the secret nobody can tell from the code which tasks are held out, the
agents of the runs the replays measure among them. It is the same for every run of the job on a hub; once in the suite
a task keeps its split, so a new secret splits only the tasks added after it, and the suite only grows.

A replay runs one task for one change, twice, each time in a new directory with a repository of the task's repo that
holds the history up to the base commit and nothing after it: arm ``a`` with the harness of the default branch of the
change's repo, arm ``b`` with the harness of the change's branch. The harness is what the change touches among
``harness_path``: an AGENTS.md or CLAUDE.md file, and the files of a skill (a directory under a directory named
``skills``), each skill whole (``skill_dir``). The agent does the step's what (``replay_prompt``), with the runtime and
model of the charter's Builder; then the worker runs the step's verify on what it did as code it does not trust, as a
judge run does. An arm passes when every verify command exited 0. ``compare`` adds up the arms of the replays of one
change. The Judge reads none of it, and decides nothing of it.

``verdict`` says whether a change's replays won, from the replays of the holdout split with both arms whose arm b ran
at the head of the change's pull request: with fewer than MIN_HOLDOUT of them they do not win; with them, arm b wins
when it passed more tasks than arm a, or as many with a mean cost or a mean number of turns at least MIN_SAVING lower
(each mean over the replays where both arms name it). ``harness_only`` says whether a diff edits a harness alone: the
files of one skill, AGENTS.md and CLAUDE.md files, nothing else. A change of such a diff whose replays won is tier 0
(``evo_agents.hub.tiers.replay_tier``).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from pathlib import PurePosixPath

from evo_agents.hub.handoff import commits_of
from evo_agents.hub.plans import step_key
from evo_agents.hub.runs import MAX_PROMPT_BYTES, clip

SPLITS = ("select", "holdout")  # the tasks the Curator may look at, and those it is measured on
HOLDOUT_EVERY = 3  # one task in three, by the HMAC-SHA256 of its plan and step, is held out
SPLIT_PURPOSE = b"evo-agents replay split"  # keeps the secret's MAC of a task apart from its other uses
ARMS = ("a", "b")  # the harness of the default branch, and the harness of the change's branch
STATES = ("queued", "running", "done", "failed")  # a replay of one task for one change
SOURCES = ("evidence", "run")  # where a task's commits were read: the step's evidence, else a run of the step
REPLAY_STATES = ("pr_pending", "judge_pending", "judging", "judged", "open")  # the states of a change replayed
DEFAULT_TASKS = 3  # tasks each change is replayed on, by default
MAX_TASKS = 20
REPLAY_ATTEMPTS = 2  # replay runs of one task for one change that end without both arms before it fails
REPLAY_KEY_HEADER = "X-Evo-Replay-Key"  # a replay run's own credential, which its claim hands the daemon alone
HARNESS_FILES = ("AGENTS.md", "CLAUDE.md")
SKILLS_DIR = "skills"
MAX_COMMITS = 20  # commits a task keeps, in the order its evidence names them
MAX_WHAT_BYTES = 16 * 1024
MAX_TITLE_CHARS = 300
MAX_VERIFY_CHARS = 2000  # a task's verify, one shell line, as a step's report takes it
MAX_REASON_CHARS = 2000
MAX_PATHS = 200  # harness paths an arm names
MIN_HOLDOUT = 6  # holdout tasks with both arms at the change's head its replays need before they can win
MIN_SAVING = 0.10  # with as many tasks passed, arm b's mean cost or turns at least this share lower than arm a's
CURATOR_PLAN_PREFIX = "curator-"  # evo_agents.hub.judge.CURATOR_PLAN_PREFIX: plans whose steps give no task
SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
SHORT_SHA = re.compile(r"^[0-9a-f]{7,64}$")


def split_of(plan_id: str, key: str, secret: str) -> str:
    """``holdout`` for one task in HOLDOUT_EVERY, by the HMAC-SHA256 of ``<plan>#<step>`` under ``secret``, the hub's;
    ``select`` for the others."""
    message = SPLIT_PURPOSE + b"\0" + f"{plan_id}#{key}".encode()
    digest = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    return SPLITS[1] if int.from_bytes(digest[:8], "big") % HOLDOUT_EVERY == 0 else SPLITS[0]


def harness_path(path: str) -> bool:
    """Whether ``path`` (within its repo) is part of a harness: an AGENTS.md or CLAUDE.md file anywhere, or a file of
    a skill, ``.../skills/<skill>/<file>``."""
    parts = PurePosixPath(path).parts
    if not parts or ".." in parts:
        return False
    return parts[-1] in HARNESS_FILES or skill_dir(path) is not None


def skill_dir(path: str) -> str | None:
    """The directory of the skill ``path`` is a file of (``.../skills/<skill>``, the last ``skills`` directory with a
    file under its skill), or None."""
    parts = PurePosixPath(path).parts
    for index in range(len(parts) - 3, -1, -1):
        if parts[index] == SKILLS_DIR:
            return "/".join(parts[: index + 2])
    return None


def harness_paths(paths: Iterable[str]) -> list[str]:
    """The paths among ``paths`` that are part of a harness, each once, sorted."""
    return sorted({path for path in paths if isinstance(path, str) and harness_path(path)})


def step_repo(body: dict, step: dict) -> str | None:
    """The repo of ``step``: its own, else the plan's only one."""
    if isinstance(step.get("repo"), str) and step["repo"]:
        return step["repo"]
    repos = [entry.get("repo") for entry in body.get("repos") or [] if isinstance(entry, dict)]
    return repos[0] if len(repos) == 1 and isinstance(repos[0], str) and repos[0] else None


def _one_line(value, limit: int) -> str | None:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text[:limit] if text else None


def task_of(plan_id: str, body: dict, index: int, run_commits: Iterable[str] = (), *, secret: str) -> dict | None:
    """The task step ``index`` of the plan ``body`` gives (see the module's docstring), its commits those its evidence
    names, else ``run_commits``, those the runs of the step reported, split under ``secret``; None when it gives
    none."""
    steps = body.get("steps") if isinstance(body.get("steps"), list) else []
    step = steps[index] if 0 <= index < len(steps) else None
    if plan_id.startswith(CURATOR_PLAN_PREFIX) or not isinstance(step, dict) or step.get("status") != "done":
        return None
    verify = step.get("verify")
    what = step.get("what")
    if not isinstance(verify, str) or "\n" in verify.strip() or not verify.strip() or len(verify) > MAX_VERIFY_CHARS:
        return None
    if not isinstance(what, str) or not what.strip() or len(what.encode()) > MAX_WHAT_BYTES:
        return None
    repo = step_repo(body, step)
    commits = commits_of(str(step.get("evidence") or ""))
    source = SOURCES[0]
    if not commits:
        commits, source = [sha for sha in dict.fromkeys(run_commits) if SHORT_SHA.match(sha or "")], SOURCES[1]
    if repo is None or not commits:
        return None
    return {
        "plan_id": plan_id,
        "step_key": step_key(step, index),
        "repo": repo,
        "title": _one_line(step.get("title"), MAX_TITLE_CHARS),
        "what": what.strip(),
        "verify": verify.strip(),
        "commits": commits[:MAX_COMMITS],
        "source": source,
        "split": split_of(plan_id, step_key(step, index), secret),
    }


def replay_prompt(project: str, task: dict, *, folder: str, harness_folder: str | None = None) -> str:
    """The prompt of the agent of one arm of a replay: the rules, then the step's title, what and verify. ``folder``
    holds the task's repo; ``harness_folder``, when the change is in another repo, the files of it the arm runs with."""
    lines = [
        f"You are an agent of a plan run of project {project} on the evo-agents hub, doing one step of its plan.",
        "",
        "Rules:",
        f"- The repository {task.get('repo')} is in {folder}/ under the current directory, at the commit the step "
        "starts from. Do the step there, as the step says, and leave your work in that directory: nothing you do "
        "leaves this machine, so do not push, open a pull request or ask anybody.",
        "- `evo-agents worker` commands do not work in this run: there is no plan to report to.",
        "- When the step is done, end your turn with a few lines saying what you did. The worker then runs the step's "
        "verify on your work.",
    ]
    if harness_folder:
        lines.append(f"- {harness_folder}/ holds files of another repository of the project that this work follows.")
    lines += ["", f"# Step {task.get('step_key')}: {task.get('title') or ''}".rstrip(), "", str(task.get("what") or "")]
    lines += ["", f"Verify: {task.get('verify')}"]
    return clip("\n".join(lines) + "\n", MAX_PROMPT_BYTES)


def base_of(commits: list[tuple[str, list[str], int]]) -> str | None:
    """The base commit of a task from ``commits``, each (sha, its parents, its committer time) as a checkout has them:
    the first parent of the oldest, the one with the earliest committer time (the first named among equals); None when
    there is none, or when the oldest has no parent."""
    if not commits:
        return None
    oldest = min(enumerate(commits), key=lambda item: (item[1][2], item[0]))[1]
    return oldest[1][0] if oldest[1] else None


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def compare(results: Iterable[dict]) -> dict:
    """What the arms of the replays of one change came to: for each arm, the replays that reported it, how many
    passed, and the cost, turns, tool errors, largest context and agent time added up over them (null where none
    reported one); and ``both``, the replays with both arms."""
    arms: dict[str, dict] = {}
    both = 0
    for row in results:
        found = [row.get(f"result_{arm}") for arm in ARMS]
        both += all(isinstance(item, dict) for item in found)
        for arm, item in zip(ARMS, found, strict=True):
            if not isinstance(item, dict):
                continue
            total = arms.setdefault(arm, {"replays": 0, "passed": 0})
            total["replays"] += 1
            total["passed"] += bool(item.get("passed"))
            for key in ("cost_usd", "turns", "tool_errors", "duration_ms"):
                value = _number(item.get(key))
                if value is not None:
                    total[key] = round(total.get(key, 0) + value, 6)
            peak = _number(item.get("peak_context_tokens"))
            if peak is not None:
                total["peak_context_tokens"] = max(total.get("peak_context_tokens", 0), peak)
    empty = {"replays": 0, "passed": 0}
    shown = {arm: {**empty, **arms.get(arm, {})} for arm in ARMS}
    for total in shown.values():
        for key in ("cost_usd", "turns", "tool_errors", "duration_ms", "peak_context_tokens"):
            total.setdefault(key, None)
    return {**shown, "both": both}


def harness_only(paths: Iterable[str]) -> str | None:
    """What ``paths`` (within one repo) edit when they edit a harness alone: the files of one skill, AGENTS.md and
    CLAUDE.md files, and nothing else, in words; None otherwise, or for no path."""
    paths = sorted(set(paths))
    if not paths or not all(harness_path(path) for path in paths):
        return None
    skills = sorted({found for found in map(skill_dir, paths) if found})
    if len(skills) > 1:
        return None
    files = [path for path in paths if skill_dir(path) is None]
    named = [f"the files of skill {skills[0]}"] if skills else []
    named += [", ".join(files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")] if files else []
    return " and ".join(named)


def _means(pairs: list[tuple[dict, dict]], key: str) -> tuple[float | None, float | None, float | None]:
    """(arm a's mean, arm b's mean, b's saving: 1 - b / a) of ``key`` over the pairs where both arms name it."""
    values = [(_number(a.get(key)), _number(b.get(key))) for a, b in pairs]
    values = [(x, y) for x, y in values if x is not None and y is not None]
    if not values:
        return None, None, None
    mean_a = round(sum(x for x, _ in values) / len(values), 6)
    mean_b = round(sum(y for _, y in values) / len(values), 6)
    return mean_a, mean_b, round(1 - mean_b / mean_a, 6) if mean_a > 0 else None


def verdict(rows: Iterable[dict], head_sha: str | None) -> dict:
    """Whether the replays ``rows`` of one change (each its ``split``, ``state``, ``result_a`` and ``result_b``) won
    at ``head_sha``, the head of its pull request (see the module's docstring): {won, reason, tasks, head_sha, a, b,
    saving, paths}, ``a`` and ``b`` each arm's tasks passed and mean cost and turns, ``paths`` the harness files arm b
    ran with."""
    pairs = [
        (row["result_a"], row["result_b"])
        for row in rows
        if row.get("split") == SPLITS[1]
        and row.get("state") == "done"
        and isinstance(row.get("result_a"), dict)
        and isinstance(row.get("result_b"), dict)
        and head_sha is not None
        and row["result_b"].get("harness_sha") == head_sha
    ]
    arms: dict[str, dict] = {arm: {"passed": 0} for arm in ARMS}
    saving: dict[str, float | None] = {}
    for key in ("cost_usd", "turns"):
        arms["a"][key], arms["b"][key], saving[key] = _means(pairs, key)
    for index, arm in enumerate(ARMS):
        arms[arm]["passed"] = sum(bool(pair[index].get("passed")) for pair in pairs)
    paths = sorted({path for _, b in pairs for path in b.get("harness_paths") or [] if isinstance(path, str)})
    found = {"tasks": len(pairs), "head_sha": head_sha, **arms, "saving": saving, "paths": paths[:MAX_PATHS]}
    passed_a, passed_b = arms["a"]["passed"], arms["b"]["passed"]
    if len(pairs) < MIN_HOLDOUT:
        why = f"{len(pairs)} holdout task(s) replayed with both arms at the head, of the {MIN_HOLDOUT} a win needs"
        return {**found, "won": False, "reason": why}
    if passed_b != passed_a:
        word = "more" if passed_b > passed_a else "fewer"
        why = f"arm b passed {word} of the {len(pairs)} holdout tasks than arm a ({passed_b} against {passed_a})"
        return {**found, "won": passed_b > passed_a, "reason": why}
    lower = [
        f"{'cost' if key == 'cost_usd' else 'turns'} {round(share * 100, 1)}% lower"
        for key, share in saving.items()
        if share is not None and share >= MIN_SAVING
    ]
    if lower:
        why = f"arm b passed as many of the {len(pairs)} holdout tasks as arm a ({passed_b}), with a mean " + (
            " and ".join(lower)
        )
        return {**found, "won": True, "reason": why}
    why = (
        f"arm b passed as many of the {len(pairs)} holdout tasks as arm a ({passed_b}), with neither its mean cost "
        f"nor its mean turns {round(MIN_SAVING * 100)}% lower"
    )
    return {**found, "won": False, "reason": why}
