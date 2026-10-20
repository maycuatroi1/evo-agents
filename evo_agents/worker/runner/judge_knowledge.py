"""The checks a judge run runs for a change of knowledge.yaml besides the plan's verify (``JudgeInputs.knowledge``, which
``evo_agents.hub.server.change_tiers`` gives a change of kind kg_source whose pull request changed the file): first
``evo-agents harness validate`` in the worktree at the commit judged, as code the worker does not trust
(``verify.run_untrusted``), then a trial sync of each source the change edits, by the worker's own code.

A trial sync loads the project from a view of the harness as the commit judged has it, written under the run's
directory: harness.yaml with each repo's path the worker's checkout of it, knowledge.yaml byte for byte, its ontology
when it names one (as ``evo_agents.worker.kg_sync`` writes its view of origin's default branch), with a corpus of its
own there. It syncs the source as the worker's sync of the graph would: a git source fetched (``fetch: true``) under
the daemon's lock of the repo's checkout, and failed when that sync would skip it (``kg_sync.skipped_reason``: no ref,
a ref other than origin/<branch>, a ``path:``, a repo harness.yaml does not name) or the worker has no checkout of its
repo; a harness source over the worktree itself. Any other connector, an exec command included, never runs. Nothing is
pushed, and the corpus goes with the run's directory.

Each check gives a verify result named as ``knowledge_diff.judge_checks`` names it, which the hub expects among the
verdict's: harness validate's exit code; a trial sync's 0 when its source synced, 1 when it did not, with why in its
event.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from evo_agents.harness import parse_yaml
from evo_agents.hub import knowledge_diff
from evo_agents.kg.project import Project, ProjectError, load_project_at
from evo_agents.kg.sync import SyncResult, sync_source
from evo_agents.worker import kg_sync
from evo_agents.worker.runner.verify import verify_event

if TYPE_CHECKING:
    from evo_agents.worker.runner.judge import JudgeRun

TRIAL_DIR = "kg-trial"


def wanted(inputs: dict) -> dict | None:
    """The knowledge checks of a judge run's inputs: the sources to sync on trial and the name of each check as the
    hub expects it (harness validate first, then one per source, in their order); None for a change without them."""
    found = inputs.get("knowledge")
    if not isinstance(found, dict):
        return None
    sources = [item for item in found.get("sources") or [] if isinstance(item, str)]
    checks = [item for item in found.get("checks") or [] if isinstance(item, str)]
    if len(checks) != len(sources) + 1:
        checks = knowledge_diff.judge_checks(sources)
    return {"sources": sources, "checks": checks}


def checkouts_of(run) -> dict[str, Path]:
    """The worker's checkouts of the run's project, by repo name."""
    prefix = f"{run.project}/"
    found = run.daemon.checkouts.items()
    return {key[len(prefix) :]: Path(entry["path"]) for key, entry in found if key.startswith(prefix)}


def trial_project(worktree: Path, checkouts: dict[str, Path], root: Path) -> Project:
    """The project as the commit judged has it, loaded from a view under ``root`` (see the module's docstring).
    Raises ProjectError, OSError or yaml.YAMLError when the harness cannot be read."""
    manifest = parse_yaml((worktree / "harness.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(manifest, dict):
        raise ProjectError("harness.yaml at the commit judged is not a mapping")
    view = root / "harness"
    manifest = kg_sync.view_manifest(manifest, checkouts)
    knowledge = Path(manifest["knowledge_file"])
    if knowledge.is_absolute() or ".." in knowledge.parts:
        raise ProjectError(f"harness.yaml names a knowledge_file outside the harness: {knowledge}")
    (view / knowledge).parent.mkdir(parents=True, exist_ok=True)
    (view / "harness.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (view / knowledge).write_bytes((worktree / knowledge).read_bytes())
    data = parse_yaml((worktree / knowledge).read_text(encoding="utf-8")) or {}
    ontology = data.get("ontology") if isinstance(data, dict) else None
    if isinstance(ontology, str) and ontology and ".." not in Path(ontology).parts and not Path(ontology).is_absolute():
        relative = knowledge.parent / ontology
        if (worktree / relative).is_file():
            (view / relative).parent.mkdir(parents=True, exist_ok=True)
            (view / relative).write_bytes((worktree / relative).read_bytes())
    return load_project_at(view, root / "home")


def _sync(project: Project, source: str) -> SyncResult:
    corpus = project.corpus()
    try:
        return sync_source(project, source, corpus=corpus)
    finally:
        corpus.close()


async def trial(run, project: Project, source: str, worktree: Path, checkouts: dict[str, Path]) -> tuple[int, str]:
    """(0 or 1, what happened) of the trial sync of ``source`` of ``project``."""
    found = next((item for item in project.sources() if isinstance(item, dict) and item.get("id") == source), None)
    if found is None:
        return 1, f"knowledge.yaml at the commit judged has no source {source}"
    connector = found.get("connector")
    if connector == "git":
        repos = {repo["name"] for repo in project.harness.repos() if isinstance(repo.get("name"), str)}
        reason = kg_sync.skipped_reason(found, repos)
        if reason is not None:
            return 1, f"the worker's sync of the graph would skip source {source}: {reason}"
        checkout = checkouts.get(found["repo"])
        if checkout is None:
            return 1, f"this worker has no checkout of repo {found['repo']} of project {run.project}"
        found["fetch"] = True  # as the worker's sync reads it
        async with run.daemon.repo_lock(checkout):
            result = await asyncio.to_thread(_sync, project, source)
    elif connector == "harness":
        found["path"] = str(worktree)  # the harness as the commit judged has it
        result = await asyncio.to_thread(_sync, project, source)
    else:
        return 1, f"source {source} is read by connector {connector}: a Judge syncs git and harness sources alone"
    problem = kg_sync.sync_failure(result)
    if problem is not None:
        return 1, f"the trial sync of source {source} failed: {problem}"
    return 0, f"source {source} synced on trial: {result.items} item(s), {result.tombstones} gone, {result.seconds}s"


async def run_checks(job: JudgeRun, worktree: Path, knowledge: dict) -> list[dict]:
    """Run the knowledge checks of a judge run (see the module's docstring): each one's verify result."""
    run = job.run
    sources, checks = knowledge["sources"], knowledge["checks"]
    run.note(f"Running {len(checks)} check(s) of knowledge.yaml: harness validate and a trial sync of each source.")
    results = []
    started = run.loop.time()
    code, output = await job.run_untrusted(knowledge_diff.JUDGE_VALIDATE, worktree)
    results.append(_result(run, checks[0], code, started, output))
    await job.restore(worktree)
    checkouts = checkouts_of(run)
    try:
        project, why = trial_project(worktree, checkouts, job.directory.path / TRIAL_DIR), None
    except (ProjectError, OSError, yaml.YAMLError, ValueError) as exc:
        project, why = None, f"the harness at the commit judged cannot be loaded: {exc}"
    for source, name in zip(sources, checks[1:], strict=True):
        run.check()
        started = run.loop.time()
        if project is None:
            code, output = 1, why
        else:
            try:
                code, output = await trial(run, project, source, worktree, checkouts)
            except Exception as exc:  # a corpus that cannot be written, a sync already running: the check fails
                code, output = 1, f"the trial sync of source {source} failed: {type(exc).__name__}: {exc}"
        results.append(_result(run, name, code, started, output))
    return results


def _result(run, name: str, code: int, started: float, output: str) -> dict:
    duration_ms = int((run.loop.time() - started) * 1000)
    run.event("system", verify_event(name, code, duration_ms, output))
    return {"command": name, "exit_code": code, "duration_ms": duration_ms}
