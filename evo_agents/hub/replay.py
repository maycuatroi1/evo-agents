"""The Curator's replays as the hub models them: the suite of replay tasks a project's own history gives, which files of
a change each arm of a replay runs with, the prompt of a replay run's agent, and what the two arms came to. Pure
functions over JSON values, standard library only, so the api, the hub's worker, the worker daemon and the tests share
them. ``evo_agents.hub.replay_suite`` builds the suites, ``evo_agents.hub.server.replays`` queues the replay runs and
keeps what they found, ``evo_agents.worker.runner.replay`` runs one; ``docs/curator.md``, "Replays", the design.

A task (``task_of``) is a step of a plan of the project that is done, whose verify is one shell line, whose repo is
known (its own, or the plan's only one), and whose evidence names the commits it made (``handoff.commits_of``), or
else a run of it reported one. Its base commit is the first parent of the oldest of those commits: the code as it was
before the step. A plan of the Curator gives no task, since its steps change the harness under test. A task's split
(``split_of``) comes from the SHA-256 of its plan and step: one task in HOLDOUT_EVERY is held out. It is the same on
every hub and for every run of the job; once in the suite a task keeps its split, and the suite only grows.

A replay runs one task for one change, twice, each time in a new directory with a repository of the task's repo that
holds the history up to the base commit and nothing after it: arm ``a`` with the harness of the default branch of the
change's repo, arm ``b`` with the harness of the change's branch. The harness is what the change touches among
``harness_path``: an AGENTS.md or CLAUDE.md file, and the files of a skill (a directory under a directory named
``skills``), each skill whole (``skill_dir``). The agent does the step's what (``replay_prompt``), with the runtime and
model of the charter's Builder; then the worker runs the step's verify on what it did as code it does not trust, as a
judge run does. An arm passes when every verify command exited 0. ``compare`` adds up the arms of the replays of one
change. The Judge reads none of it, and decides nothing of it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import PurePosixPath

from evo_agents.hub.handoff import commits_of
from evo_agents.hub.plans import step_key
from evo_agents.hub.runs import MAX_PROMPT_BYTES, clip

SPLITS = ("select", "holdout")  # the tasks the Curator may look at, and those it is measured on
HOLDOUT_EVERY = 3  # one task in three, by the SHA-256 of its plan and step, is held out
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
CURATOR_PLAN_PREFIX = "curator-"  # evo_agents.hub.judge.CURATOR_PLAN_PREFIX: plans whose steps give no task
SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
SHORT_SHA = re.compile(r"^[0-9a-f]{7,64}$")


def split_of(plan_id: str, key: str) -> str:
    """``holdout`` for one task in HOLDOUT_EVERY, by the SHA-256 of ``<plan>#<step>``; ``select`` for the others."""
    digest = hashlib.sha256(f"{plan_id}#{key}".encode()).digest()
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


def task_of(plan_id: str, body: dict, index: int, run_commits: Iterable[str] = ()) -> dict | None:
    """The task step ``index`` of the plan ``body`` gives (see the module's docstring), its commits those its evidence
    names, else ``run_commits``, those the runs of the step reported; None when it gives none."""
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
        "split": split_of(plan_id, step_key(step, index)),
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
