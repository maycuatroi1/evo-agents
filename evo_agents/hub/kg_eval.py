"""Questions for ``evo-agents kg eval`` built from the plans a hub keeps (``evo-agents kg eval-questions``).

Each step that is done and whose evidence names commits becomes one question: the step's title (or its ``what``)
asks, and the answers are the files those commits changed in a checkout of the step's repo, as ``repo:path``. A
merge commit counts what it changed against its first parent, so a pull request's merge names the files of the whole
pull request. The commit hashes are read as the handoff reads them (``evo_agents.hub.handoff.commits_of``).

A step is left out, with the reason, when it has no repo (its own, or the plan's only one), when no checkout of that
repo is given, when none of its commits is in the checkout, or when its commits change more than ``max_files`` files:
such a step names too much to be one question.

Only reads: GET of the hub's plans, and ``git diff`` in the checkouts.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

from evo_agents.hub.handoff import commits_of
from evo_agents.hub.mirror import plans_path
from evo_agents.hub.plans import step_key
from evo_agents.kg.evaluate import DEFAULT_MAX_FILES, PLAN_STEP_TYPE, PLAN_TEXTS

GIT_TIMEOUT = 30.0


def _git(checkout: Path, *args: str) -> str | None:
    """The output of a read-only git command, or None when it fails (a commit the checkout lacks, an ambiguous hash)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def changed_files(checkout: Path, sha: str) -> list[str] | None:
    """The paths a commit changed against its first parent (all of them for a root commit); None when the checkout
    does not have the commit."""
    full = _git(checkout, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if full is None:
        return None
    full = full.strip()
    if _git(checkout, "rev-parse", "--verify", "--quiet", f"{full}^1") is not None:
        out = _git(checkout, "diff", "-z", "--name-only", "--no-renames", f"{full}^1", full)
    else:
        out = _git(checkout, "diff-tree", "-z", "--root", "-r", "--name-only", "--no-commit-id", "--no-renames", full)
    return None if out is None else [path for path in out.split("\0") if path]  # -z: paths verbatim, not quoted


def _step_repo(body: dict, step: dict) -> str | None:
    if isinstance(step.get("repo"), str) and step["repo"]:
        return step["repo"]
    repos = [r.get("repo") for r in body.get("repos") or [] if isinstance(r, dict)]
    return repos[0] if len(repos) == 1 and isinstance(repos[0], str) else None


def _text(step: dict, text: str) -> str:
    value = step.get(text) if text in PLAN_TEXTS else None
    return " ".join(value.split()) if isinstance(value, str) else ""


def step_question(
    project: str, plan_id: str, body: dict, index: int, checkouts: dict[str, Path], *, text: str, max_files: int
) -> tuple[dict | None, str | None]:
    """The question one step of a plan gives, or None and why it gives none (None, None: not done or no commit)."""
    step = body["steps"][index]
    if not isinstance(step, dict) or step.get("status") != "done":
        return None, None
    commits = commits_of(str(step.get("evidence") or ""))
    if not commits:
        return None, None
    name = f"{plan_id}#{step_key(step, index)}"
    repo = _step_repo(body, step)
    if repo is None or repo not in checkouts:
        return None, f"{name}: no checkout of repo {repo or '(none named)'}"
    files: list[str] = []
    named: list[str] = []
    for sha in commits:
        changed = changed_files(checkouts[repo], sha)
        if changed is not None:
            named.append(sha)
            files += [path for path in changed if path not in files]
    if not named:
        return None, f"{name}: none of its {len(commits)} commit(s) is in the checkout of {repo}"
    if not files or len(files) > max_files:
        return None, f"{name}: its commits change {len(files)} file(s), not 1 to {max_files}"
    question = _text(step, text) or _text(step, "title")
    if not question:
        return None, f"{name}: no {text}"
    entry = {"id": name, "question": question, "type": PLAN_STEP_TYPE, "project": project, "repo": repo}
    return {**entry, "files": [f"{repo}:{path}" for path in files], "commits": named}, None


def plan_questions(
    hub,
    project: str,
    checkouts: dict[str, Path],
    *,
    plans: Iterable[str] = (),
    text: str = "title",
    max_files: int = DEFAULT_MAX_FILES,
) -> tuple[list[dict], list[str]]:
    """The questions of every plan of ``project`` the hub shows (or only ``plans``), and why each step with commits
    gave none. ``hub`` is anything with ``call(method, path)``, as ``evo_agents.hub.client.Hub``."""
    wanted = set(plans)
    questions: list[dict] = []
    skipped: list[str] = []
    for summary in hub.call("GET", plans_path(project)):
        plan_id = summary.get("plan_id")
        if wanted and plan_id not in wanted:
            continue
        body = hub.call("GET", plans_path(project, plan_id)).get("body") or {}
        steps = body.get("steps") if isinstance(body.get("steps"), list) else []
        for index in range(len(steps)):
            question, why = step_question(project, plan_id, body, index, checkouts, text=text, max_files=max_files)
            if question is not None:
                questions.append(question)
            elif why is not None:
                skipped.append(why)
    return questions, skipped
