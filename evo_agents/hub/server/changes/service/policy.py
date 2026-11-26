"""The refusals that keep the Curator's plans as the hub made them, and what a run of the Curator gets, its judge
run's key and prompt included (``evo_agents.hub.server.changes.service.job`` describes the design)."""

from __future__ import annotations

import secrets

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import judge, tiers
from evo_agents.hub.server.changes.models.views import CuratorPolicy, key_digest
from evo_agents.hub.server.changes.repository import views as store
from evo_agents.hub.server.changes.repository.views import change_of_plan, charter_of

# The Curator's plans


async def plan_write_refusal(
    conn: AsyncConnection,
    project_id: int,
    plan_id: str,
    old_body: dict | None,
    new_body: dict,
    *,
    made_by_curator: bool = False,
) -> str | None:
    """Why a write of plan ``plan_id`` is refused: a new plan whose id is one of the Curator's, unless the hub makes it
    from a proposal (``made_by_curator``); and for a Curator's plan, another repo or branch, a step in another repo,
    or a change of anything but the progress of its steps and its status (``judge.progress_free``), or than the mend of
    a verify that is no shell command line (``judge.mended_verify``). None for any other plan, and for a write that
    keeps them."""
    if old_body is None and not made_by_curator and judge.is_curator_plan_id(plan_id):
        return (
            f"plan ids that start with {judge.CURATOR_PLAN_PREFIX} belong to the Curator: the hub makes such a plan "
            "from an accepted proposal alone; give the plan another id. Nothing was written"
        )
    change = await change_of_plan(conn, project_id, plan_id)
    if change is None:
        return None
    from evo_agents.hub.server.changes.service import repos as change_repos  # it reads the changes of this module

    allowed = await change_repos.repo_names(conn, change)  # the change's own repo, or each of a change in several
    why = (
        f"plan {plan_id} is the Curator's: it works on {change.branch} of {', '.join(allowed)} alone, never on another branch "
        "or repo and least of all on a default branch, and a write of it changes the status, done_at, evidence and "
        "note of its steps and its status alone, its what, goal, context, verify and acceptance staying as the hub "
        "made them (a verify that is no shell command line may be replaced by one that is and keeps each of its "
        "runnable clauses); nothing was written"
    )
    repos = new_body.get("repos")
    if not isinstance(repos, list) or len(repos) != len(allowed) or not all(isinstance(item, dict) for item in repos):
        return why
    if [item.get("repo") for item in repos] != allowed or any(item.get("branch") != change.branch for item in repos):
        return why
    steps = new_body.get("steps")
    steps = steps if isinstance(steps, list) else []
    if any(isinstance(step, dict) and step.get("repo") not in (None, *allowed) for step in steps):
        return why
    if old_body is not None and judge.progress_free(old_body) != judge.progress_free(
        judge.mended_verify(old_body, new_body)
    ):
        return why
    return None


async def refuse_manual_dispatch(conn: AsyncConnection, project_id: int, plan_id: str) -> None:
    """409 when plan ``plan_id`` is the Curator's: the night shift alone runs it, at any hour."""
    if await change_of_plan(conn, project_id, plan_id) is not None:
        raise HTTPException(
            409,
            f"plan {plan_id} is the Curator's: the night shift runs it itself, at any hour; POST .../curator/plans/"
            f"{plan_id}/build (Run plan on its page) queues its Builder at once; nothing was dispatched",
        )


# What a run of the Curator gets


async def curator_policy(
    conn: AsyncConnection, project_id: int, kind: str, plan_id: str | None
) -> CuratorPolicy | None:
    """The policy of a run of ``kind`` on ``plan_id`` when it is a run of the Curator (a review run, a judge run, a
    replay run, a history run, a probe run, an evolve run, or any run of a Curator's plan); None for any other run."""
    roles = {
        "review": "reviewer",
        "judge": "judge",
        "replay": "replay",
        "history": "history",
        "probe": "probe",
        "evolve": "evolve",
    }
    role = roles.get(kind, "builder")
    change = None if kind == "review" else await change_of_plan(conn, project_id, plan_id)
    if change is None and (role == "builder" or (role == "judge" and not plan_id)):  # no plan: the merge queue's
        return None
    charter = await charter_of(conn, project_id)
    secret = charter.get("git_secret")
    protected = tuple(path for path in charter.get("protected_paths") or [] if isinstance(path, str))
    if role == "builder" and change is not None:
        protected = await _left_protected(conn, project_id, change.proposal_id, protected)
    return CuratorPolicy(
        role=role,
        git_secret=secret if isinstance(secret, str) and secret else None,
        env_secrets=tuple(name for name in charter.get("env_secrets") or [] if isinstance(name, str)),
        protected=protected,
        change_id=None if change is None else change.id,
        read_git_secrets=tuple(
            name for name in charter.get("read_git_secrets") or [] if isinstance(name, str) and name
        ),
    )


async def _left_protected(
    conn: AsyncConnection, project_id: int, proposal_id: int | None, protected: tuple[str, ...]
) -> tuple:
    """The protected globs a Builder's watchdog still stops it on: the charter's, less each file its change's proposal
    lists that a glob naming one file alone (no wildcard) protects, once the owner accepted that proposal at tier 3, as
    the owner then saw the path and why it made the proposal tier 3. Such a glob scoped to that file's repo is left
    out; one with no repo becomes the same glob scoped to each other repo of the project. A glob with a wildcard stays
    whole, as it also covers files the owner never accepted; so does every glob of a proposal not accepted, or accepted
    below tier 3 (a path protected after it was accepted). The Judge and the merge gate still show each protected path
    the diff touches."""
    if proposal_id is None or not protected:
        return protected
    row = await store.accepted_paths(conn, proposal_id)
    if row is None or row.state != "accepted" or row.tier != 3:
        return protected
    listed = {
        (item["repo"], tiers.normalize_path(item["path"]))
        for item in row.paths or []
        if isinstance(item, dict) and isinstance(item.get("repo"), str) and isinstance(item.get("path"), str)
    }
    repos = await store.project_repo_names(conn, project_id)
    left: list[str] = []
    for glob in protected:
        if any(char in glob for char in "*?"):
            left.append(glob)
            continue
        scoped = tiers.split_target(glob)
        if scoped is not None and "/" not in scoped[0]:  # repo:path
            if scoped not in listed:
                left.append(glob)
            continue
        path = tiers.normalize_path(glob)
        accepted_in = {repo for repo, listed_path in listed if listed_path == path}
        if path is None or not accepted_in:
            left.append(glob)
            continue
        left += [f"{repo}:{path}" for repo in repos if repo not in accepted_in]
    return tuple(left)


async def run_curator(conn: AsyncConnection, project_id: int, view) -> dict | None:
    """What the claim of run ``view`` (a ``runs.Run``) tells its worker of the Curator: its role, the charter's
    protected paths its watchdog compares the worktree with, and for a Builder, a Judge or a replay the change, its
    branch and forge, for a Judge, a replay and a probe the change's head, for a replay its task and its own key
    (``replays.replay_claim``), for a probe run its probe and its own key (``probe_runs.probe_claim``), and for an
    evolve run its evolution and its own key (``evolve_runs.evolve_claim``). None for any other run."""
    from evo_agents.hub.server.changes.service import repos as change_repos  # it reads the changes of this module
    from evo_agents.hub.server.probes.service.runs import probe_claim  # it reads the changes of this module
    from evo_agents.hub.server.replays.service import replay_claim  # it reads the changes of this module

    policy = await curator_policy(conn, project_id, view.kind, view.plan_id or None)
    if policy is None:
        return None
    found, change = {"role": policy.role, "protected_paths": list(policy.protected)}, None
    if policy.change_id is not None:
        change = await store.change_row(conn, policy.change_id)
        if policy.role == "judge" and change.judge_run_id == view.id:  # in several repos: the one it judges
            change = await change_repos.in_focus(conn, change)
        found.update(
            change_id=change.id,
            branch=change.branch,
            forge=change.forge,
            base_branch=change.base_branch,
            head_sha=change.head_sha if policy.role in ("judge", "replay", "probe") else None,
            pr_url=change.pr_url,
        )
        if policy.role == "judge" and change.state == "judging" and change.judge_run_id == view.id:
            found["judge_key"] = await issue_judge_key(conn, change.id)
        if policy.role == "replay":
            found.update(await replay_claim(conn, change, view.id))
    if policy.role == "probe":  # a run of a plugin's probe has no change
        found.update(await probe_claim(conn, change, view.id))
    if policy.role == "evolve":  # an evolution has no change while it runs
        from evo_agents.hub.server.evolve.service.runs import (
            evolve_claim,  # it imports the runs service, which imports this
        )

        found.update(await evolve_claim(conn, view.id))
    return found


async def issue_judge_key(conn: AsyncConnection, change_id: int) -> str:
    """A new key of the judge run of change ``change_id``, which the run's claim hands its daemon: the hub keeps its
    SHA-256 alone, and a key issued before it no longer counts."""
    key = secrets.token_urlsafe(32)
    await store.store_judge_key(conn, change_id, key_digest(key))
    return key


async def judge_prompt(conn: AsyncConnection, view) -> str:
    """The prompt of judge run ``view``: ``judge.build_judge_prompt`` over its change, its proposal and the plan at
    the revision the run was queued at."""
    project_id = await store.run_project_id(conn, view.id)
    from evo_agents.hub.server.changes.service import repos as change_repos  # it reads the changes of this module

    change = await change_of_plan(conn, project_id, view.plan_id)
    if change is not None and change.judge_run_id == view.id:  # in several repos: the one it judges
        change = await change_repos.in_focus(conn, change)
    if change is None:
        return judge.build_judge_prompt(
            view.project, {}, {}, "", branch="", base="the default branch", head=None, pr_url=None
        )
    proposal = await store.judged_proposal(conn, change.proposal_id)
    body = await store.plan_revision_body(conn, project_id, view.plan_id, view.plan_revision)
    if getattr(change, "repo_row_id", None) is not None:
        body = change_repos.repo_body(body, change.repo)
    base = f"origin/{change.base_branch}" if change.base_branch else "origin/HEAD"
    return judge.build_judge_prompt(
        view.project,
        dict(proposal._mapping),
        body,
        change.repo,
        branch=change.branch,
        base=base,
        head=change.head_sha,
        pr_url=change.pr_url,
    )
