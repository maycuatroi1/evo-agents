"""The RunSpec a claimed run is handed (``run_spec``): its prompt, as the module of its kind builds it, its plan,
budget, Curator role, lost attempt, routing and release; the skill of an author run (``author_skill``); and where its
agent runs beyond the core (``Placed``): a plugin's runtime, an environment and agent packs. A run the hub cannot
place fails at its claim, as the reaper, saying why."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import author, autonomy_card, curator, model_router, proofs, run_prompts, runs, tables
from evo_agents.hub.blobs import MAX_GET_TTL
from evo_agents.hub.plans import PlanProblem, step_index
from evo_agents.hub.server import attachment_store, pool_accounts, principals
from evo_agents.hub.server.agent_packs import tickets
from evo_agents.hub.server.loop.runs_of import item_of_run
from evo_agents.hub.server.run_state import move_run
from evo_agents.hub.server.runs.models import (
    ClaimedBudget,
    CuratorSpec,
    LostAttempt,
    PlanCopy,
    PluginRuntime,
    Run,
    RunEnvironment,
    RunPack,
    RunSkill,
    RunSpec,
)
from evo_agents.hub.server.runs.service import plugin_environments, plugin_runtimes
from evo_agents.hub.server.runs.service.profiles import own_home, owner_setup, run_origin, run_profile
from evo_agents.hub.server.runs.service.views import current_plan, run_view, text_or_none
from evo_agents.plugins.run_environments import ContainerEnvironment
from evo_agents.plugins.runtimes import AcpRuntime

log = logging.getLogger("evo_agents.hub.server.runs")  # the package's one logger, as before it was split


@dataclass(frozen=True)
class HeldSkill:
    """The latest version of a global skill, as the hub holds it."""

    name: str
    version: int
    sha256: str
    size: int


def _global_skill(name: str):
    """The latest version of the global skill ``name`` (ignoring case, as names are unique): name, version, sha256,
    size."""
    sk, v = tables.skills, tables.skill_versions
    return (
        select(sk.c.name, v.c.version, v.c.sha256, v.c.size)
        .join_from(sk, v, v.c.skill_id == sk.c.id)
        .where(sk.c.scope == "global", func.lower(sk.c.name) == name.lower())
        .order_by(v.c.version.desc())
        .limit(1)
    )


async def author_skill(conn: AsyncConnection) -> HeldSkill | None:
    """The version of ``author.AUTHOR_SKILL`` an author run claimed now gets; None when the hub has none."""
    row = (await conn.execute(_global_skill(author.AUTHOR_SKILL))).one_or_none()
    return None if row is None else HeldSkill(*row)


def skill_refusal(skill: HeldSkill | None, blobs) -> str | None:
    """Why an author run cannot be handed its skill now, which fails it at its claim; None when it can."""
    name = author.AUTHOR_SKILL
    if skill is None:
        return (
            f"the hub holds no global skill {name}, which the agent of an author run writes the plan with: a hub admin "
            f"publishes it with `evo-agents hub skills publish <agent-skills>/skills/{name}` (scope global), then "
            "dispatch the author run again"
        )
    if blobs is None:
        return (
            f"the hub has no blob store, so it cannot hand the worker the bundle of skill {name} (version "
            f"{skill.version}): configure the blob store of the hub, then dispatch the author run again"
        )
    return None


async def _skill_ticket(blobs, skill: HeldSkill) -> RunSkill:
    """A presigned GET of ``skill``'s bundle, for the worker that claimed the run; it works MAX_GET_TTL."""
    expires_at = datetime.now(UTC) + MAX_GET_TTL  # taken before signing, so never later than the URL
    filename = f"{skill.name}-v{skill.version}.tar.gz"
    url = await asyncio.to_thread(blobs.presign_get, skill.sha256, MAX_GET_TTL, filename=filename)
    return RunSkill(
        name=skill.name, version=skill.version, sha256=skill.sha256, size=skill.size, url=url, expires_at=expires_at
    )


async def dressed(spec: RunSpec, placed: Placed, skill: HeldSkill | None, blobs) -> RunSpec:
    """``spec`` with what its claim placed: the ticket of its ``skill`` (an author run's), the plugin runtime, the
    environment and the packs of ``placed``."""
    if skill is not None:
        spec.skills = [await _skill_ticket(blobs, skill)]
    if placed.runtime is not None:
        spec.plugin_runtime = PluginRuntime(**placed.runtime.to_json())
    if placed.container is not None:
        spec.environment = RunEnvironment(**placed.container.to_json())
    spec.packs = list(placed.packs)
    return spec


@dataclass(frozen=True)
class Placed:
    """Where a claimed run's agent runs, beyond the core: the declaration of the plugin's runtime it asked for, and
    the environment of a plugin its claim picked with its declaration (None for both: the host)."""

    project: str
    runtime: AcpRuntime | None = None
    environment: str | None = None
    container: ContainerEnvironment | None = None
    packs: tuple[RunPack, ...] = ()


async def placed_or_failed(
    conn: AsyncConnection, run_id: int, runtime: str | None, project_id: int, project: str
) -> Placed | None:
    """Placed with the declaration of the plugin's runtime ``runtime`` (None for a runtime of the core) that run
    ``run_id`` asks for; None once the run failed for want of one (``_declared_or_failed``)."""
    if runtime is None:
        return Placed(project)
    declaration = await _declared_or_failed(conn, run_id, runtime, project_id, project)
    return None if declaration is None else Placed(project, declaration)


async def _environment_or_failed(
    conn: AsyncConnection, run_id: int, placed: Placed, rule: dict | None, environments: list[str], project_id: int
) -> Placed | None:
    """``placed`` with the environment run ``run_id`` runs in, picked from its profile's environment axis ``rule``
    among host and the ``environments`` of plugins the worker takes, with the declaration its project has now; None
    once the run failed, as the reaper, for want of one: the plugin was disabled there since."""
    place = await plugin_environments.picked(conn, project_id, rule, environments)
    if place is not None:
        environment, container = place
        return replace(placed, environment=None if container is None else environment, container=container)
    allow = ", ".join(plugin_environments.allowlist({"environment": rule}) or [])
    refused = (
        f"no environment the run's agent profile allows ({allow}) is registered in project {placed.project} now: the "
        "plugin was disabled there, turned off or removed since the run was dispatched, or its version declares the "
        "environment no more; enable it and dispatch the run again"
    )
    await move_run(conn, run_id, "queued", "failed", "reaper", reason=refused, error=refused)
    log.warning("run of a plugin's environment failed at its claim", extra={"run_id": run_id})
    return None


async def settled_or_failed(
    conn: AsyncConnection, run_id: int, placed: Placed, where: tuple, project_id: int, packs: tuple
) -> Placed | None:
    """``placed`` with its environment, from ``where`` (the profile's environment axis, and the environments of plugins
    the worker takes; ``_environment_or_failed``), and with the agent packs the profile attaches, from ``packs`` (them,
    and the blob store; ``_packs_or_failed``); None once the run failed for want of either."""
    rule, environments = where
    attached, blobs = packs
    settled = await _environment_or_failed(conn, run_id, placed, rule, environments, project_id)
    if settled is None or not attached:
        return settled
    return await _packs_or_failed(conn, run_id, settled, project_id, attached, blobs)


async def _packs_or_failed(
    conn: AsyncConnection, run_id: int, placed: Placed, project_id: int, packs: list[dict], blobs
) -> Placed | None:
    """``placed`` with the agent packs ``packs`` of run ``run_id``'s profile, each with a presigned GET of its plugin's
    bundle from the blob store ``blobs``; None once the run failed, as the reaper, for want of one."""
    found, refused = await tickets(conn, blobs, project_id, placed.project, packs)
    if refused is None:
        return replace(placed, packs=tuple(found))
    await move_run(conn, run_id, "queued", "failed", "reaper", reason=refused, error=refused)
    log.warning("run of agent packs failed at its claim", extra={"run_id": run_id})
    return None


async def _declared_or_failed(conn: AsyncConnection, run_id: int, runtime: str, project_id: int, project: str):
    """The declaration of the plugin's runtime ``runtime`` that run ``run_id`` asks for, as project ``project`` has it
    now; None once the run failed, as the reaper, for want of one: the plugin was disabled there since."""
    declaration = await plugin_runtimes.declared(conn, project_id, runtime)
    if declaration is not None:
        return declaration
    plugin = runtime.partition("/")[0]
    refused = (
        f"runtime {runtime} is no longer registered in project {project}: plugin {plugin} was disabled there, turned "
        "off or removed since the run was dispatched, or its version declares the runtime no more; enable it and "
        "dispatch the run again"
    )
    await move_run(conn, run_id, "queued", "failed", "reaper", reason=refused, error=refused)
    log.warning("run of a plugin's runtime failed at its claim", extra={"run_id": run_id, "runtime": runtime})
    return None


OWN_PROMPTS = (
    "review",
    "chat",
    "task",
    "judge",
    "replay",
    "history",
    "probe",
    "release",
    "question",
)  # whose prompt their module builds


async def _own_prompt(conn: AsyncConnection, view: Run, features: tuple[str, ...] = (), ships: bool = False) -> str:
    """The prompt of run ``view`` of one of OWN_PROMPTS, as the module of its kind builds it for a worker whose daemon
    says it does ``features``, a chat run's with merging left to its agent when ``ships``; each module queues runs
    through this package, so it is imported here."""
    if view.kind == "review":
        from evo_agents.hub.server.collect import review_prompt

        return await review_prompt(conn, view)
    if view.kind == "chat":
        from evo_agents.hub.server.chat_runs import chat_prompt

        repos = [repo.model_dump() for repo in view.repos or []]
        return await chat_prompt(conn, view.id, view.branch, repos, features, ships=ships)
    if view.kind == "task":
        from evo_agents.hub.server.task_dispatch import task_prompt

        return await task_prompt(conn, view)
    if view.kind == "question":
        from evo_agents.hub.server.question_dispatch import question_prompt

        return await question_prompt(conn, view)
    if view.kind == "judge":  # the merge queue's on no plan, else the Curator's
        from evo_agents.hub.server.change_views import judge_prompt
        from evo_agents.hub.server.merge_judge import merge_prompt

        return await merge_prompt(conn, view) or await judge_prompt(conn, view)
    if view.kind == "history":
        from evo_agents.hub.history import RUN_PROMPT

        return RUN_PROMPT
    if view.kind == "probe":
        from evo_agents.hub.server.probe_runs import run_prompt as probe_prompt

        return await probe_prompt(conn, view)
    if view.kind == "release":
        from evo_agents.hub.server.release_claims import release_prompt  # it reads runs through this package

        return await release_prompt(conn, view)
    from evo_agents.hub.server.replays import run_prompt

    return await run_prompt(conn, view)


async def run_spec(conn: AsyncConnection, run_id: int, features: tuple[str, ...] = ()) -> RunSpec:
    """The claim of run ``run_id`` by a worker whose daemon says it does ``features`` (``runs.WORKER_FEATURES``): an
    author run's prompt lets its agent ask decisions only on a daemon that does ``runs.AUTHOR_DECISIONS``, and a chat
    run's tells its agent to open the repos it needs only on one that does ``runs.LAZY_CHAT_REPOS``. The prompt of a run
    under an agent profile that is not its project's default ends with the index of its memories
    (``memory_upkeep.with_index``). The prompt of a run with a snapshot of autonomy opens with its card
    (``principals.card``), which it carries as ``autonomy`` too, for the worker to build a step's prompt with."""
    view = await run_view(conn, run_id)
    card = await principals.card(conn, run_id)
    autonomy = None if card is None else card.spec()
    r, revisions = tables.runs, tables.plan_revisions
    project_id = (await conn.execute(select(r.c.project_id).where(r.c.id == run_id))).scalar_one()
    dispatched = select(revisions.c.body).where(
        revisions.c.project_id == project_id,
        revisions.c.plan_id == view.plan_id,
        revisions.c.revision == view.plan_revision,
    )
    body = (await conn.execute(dispatched)).scalar_one_or_none()
    plan = body if body is not None else {"id": view.plan_id, "steps": []}
    copy, verify, criteria = None, None, None
    if view.kind in OWN_PROMPTS:
        prompt = await _own_prompt(conn, view, features, card is not None and card.ships)
        title = view.title
    elif view.kind == "browser":  # a person drives its Chromium: no agent, so no prompt
        prompt, title = "", view.title
    elif view.kind == "author":
        skill = await author_skill(conn)
        repos = [repo.model_dump() for repo in view.repos or []]
        version = skill.version if skill is not None else 0
        target = (view.plan_id, view.plan_revision) if view.plan_id and view.plan_revision is not None else None
        prompt = author.build_author_prompt(
            view.project,
            view.dispatched_by,
            view.request or "",
            repos,
            version,
            plan=target,
            decisions=runs.AUTHOR_DECISIONS in features,
            loop=await item_of_run(conn, view.id) is not None,
        )
        title = view.title
    elif view.kind == "plan":
        current = await current_plan(conn, project_id, view.plan_id)
        body, revision = current if current else (plan, view.plan_revision or 0)  # the plan gone: as dispatched
        copy = PlanCopy(body=body, revision=revision)
        prompt = run_prompts.build_plan_prompt(
            copy.body, [repo.model_dump() for repo in view.repos or []], None, autonomy
        )
        title = view.title
    else:
        try:
            step = plan["steps"][step_index(plan, view.step_key)]
        except (PlanProblem, KeyError, TypeError):
            step = None
        if not isinstance(step, dict):
            step = {"id": view.step_key}
        prompt = run_prompts.build_prompt(plan, step, {"repo": view.repo, "branch": view.branch})
        title = text_or_none(step.get("title"))
        verify = runs.verify_of(step)
        criteria = [key for key, _ in proofs.criteria(plan, step)] or None
    from evo_agents.hub.server.context_answers import with_answers
    from evo_agents.hub.server.memory_upkeep import with_index  # it reads memories through their routes' module

    prompt = await with_answers(conn, run_id, prompt)  # the confirmed answers that match the run, when the policy says
    prompt = await with_index(conn, run_id, prompt)  # the memories of an agent profile, not the project's default
    if view.kind != "plan":  # the plan's prompt has it first already
        prompt = autonomy_card.with_card(prompt, autonomy)
    return RunSpec.model_validate(  # routing and autonomy are dicts the model reads
        dict(
            id=view.id,
            kind=view.kind,
            project=view.project,
            plan_id=view.plan_id,
            step_key=view.step_key,
            title=title,
            plan_revision=view.plan_revision,
            attempt=view.attempt,
            max_attempts=view.max_attempts,
            parent_run_id=view.parent_run_id,
            resume_of_run_id=view.resume_of_run_id,
            session_id=view.session_id,
            lost_attempt=await _lost_attempt(conn, view),
            runtime=view.runtime,
            model=view.model,
            routing=await _routing(conn, project_id, view),
            mode=view.mode,
            approval=view.approval,
            timeout_min=view.timeout_min,
            repo=view.repo,
            branch=view.branch,
            repos=view.repos,
            lease_expires_at=view.lease_expires_at,
            prompt=prompt,
            plan=copy,
            budget=await _claimed_budget(conn, view),
            curator=await _curator_spec(conn, project_id, view),
            verify=verify,
            criteria=criteria,
            session_per_step=view.session_per_step,
            keep_going=runs.KEEP_GOING_TURNS if view.kind == "plan" else 0,
            browser=view.browser,
            browser_identities=view.browser_identities,
            profile=await run_profile(conn, run_id),
            own_home=await own_home(conn, run_id),
            account_pool=await pool_accounts.is_pooled(conn, run_id),
            owner_setup=await owner_setup(conn, run_id),
            origin=await run_origin(conn, run_id),
            release=await _release_spec(conn, view),
            autonomy=autonomy,
            attachments=await _opening_files(conn, view),
            chat_message_id=view.chat_message_id if view.kind == "chat" else None,
        )
    )


async def _routing(conn: AsyncConnection, project_id: int, view: Run) -> dict | None:
    """The routing by difficulty the claim of run ``view`` carries (``model_router.claim_routing``); the project's
    policy is read only for a run of a routed kind and runtime that names no model."""
    if view.model or view.kind not in model_router.ROUTED_KINDS or view.runtime not in model_router.ROUTED_RUNTIMES:
        return None
    from evo_agents.hub.server.autonomy_policies import effective_policy  # it imports the Loop, which reads runs

    return model_router.claim_routing(await effective_policy(conn, project_id), view.kind, view.runtime, view.model)


async def _opening_files(conn: AsyncConnection, view: Run) -> list:
    """The files of the chat message that opened chat run ``view``; none for any other run."""
    if view.kind != "chat" or view.chat_message_id is None:
        return []
    return (await attachment_store.of_messages(conn, [view.chat_message_id])).get(view.chat_message_id, [])


async def _release_spec(conn: AsyncConnection, view: Run):
    """A release run's cycle, mode and start commit (``release_claims.release_claim``); None for any other run."""
    if view.kind != "release":
        return None
    from evo_agents.hub.server.release_claims import release_claim  # it reads runs through this package

    return await release_claim(conn, view.id)


async def _curator_spec(conn: AsyncConnection, project_id: int, view: Run) -> CuratorSpec | None:
    from evo_agents.hub.server.change_views import run_curator  # it reads runs through this package
    from evo_agents.hub.server.history import history_claim  # it queues runs through this package

    found = await run_curator(conn, project_id, view)
    if found is None and view.kind == "judge":  # a judge run of the merge queue is guarded as the Curator's are
        from evo_agents.hub.server.merge_judge import merge_claim

        found = await merge_claim(conn, view)
    if found is not None and view.kind == "history":
        found.update(await history_claim(conn, view.id))
    return None if found is None else CuratorSpec(**found)


async def _lost_attempt(conn: AsyncConnection, view: Run) -> LostAttempt | None:
    """The attempt before plan run ``view`` when the hub lost it (its worker stopped extending the lease) on the worker
    that claims ``view`` now, on the same runtime, with its agent's session known: that worker goes on in the session
    and its worktrees when it still has both. None for any other run, for one with a session for each step, and for one
    with a budget, whose caps count each attempt's agent from nothing."""
    if view.kind != "plan" or view.parent_run_id is None or view.session_per_step or view.budget is not None:
        return None
    r, lost = tables.runs, tables.runs.alias("lost_attempt")
    query = (
        select(lost.c.id, lost.c.session_id)
        .join_from(r, lost, lost.c.id == r.c.parent_run_id)
        .where(
            r.c.id == view.id,
            lost.c.state == "lost",
            lost.c.worker_id == r.c.worker_id,
            lost.c.runtime == r.c.runtime,
            lost.c.session_id.is_not(None),
        )
    )
    row = (await conn.execute(query)).first()
    return None if row is None else LostAttempt(run_id=row.id, session_id=row.session_id)


async def _claimed_budget(conn: AsyncConnection, view: Run) -> ClaimedBudget | None:
    """The budget of run ``view`` with what it spent already: the agent time it counted (a run that resumes a parked
    one starts with that run's), and the cost of the run it resumes, whose session it goes on in."""
    if view.budget is None:
        return None
    spent_usd = 0.0
    if view.resume_of_run_id is not None:
        r = tables.runs
        usage = (await conn.execute(select(r.c.usage).where(r.c.id == view.resume_of_run_id))).scalar_one_or_none()
        spent_usd = curator.run_cost(usage)
    return ClaimedBudget(**view.budget.model_dump(), spent_usd=spent_usd, spent_seconds=view.run_seconds)
