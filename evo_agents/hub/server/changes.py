"""The Curator's changes on the hub: an accepted proposal as a plan, its Builder, its pull request, its Judge and its
merge; the repos whose ruleset keeps the Curator off their default branch; and the job that moves the changes on.
``evo_agents.hub.judge`` holds the model.

Accepting a proposal of tier 0 or 1 (``proposals.answer_proposal_as``) makes its draft a plan on the hub in the same
transaction (``plan_from_proposal``), as the admin who accepted it, labelled as the proposal: the Curator's plan
``curator-<proposal>-<slug>``, in the one repo the proposal names, on the branch ``curator/<proposal>-<slug>``, and a
row of curator_changes, state ``planned``. A proposal that names several repos, a repo the project does not have or
one without an origin cannot become one: its change is recorded ``open``, without a plan, with the reason, and nothing
of it runs. A write of a Curator's plan that names another repo or branch for it, or changes anything but the progress
of its steps (status, done_at, evidence and note) and its status, is refused (``plan_write_refusal``, from
``plans._store``): its what, goal, context, verify and acceptance stay as the hub made them. A plan whose id starts with
``curator-`` is made this way alone, never by hand. No member dispatches a run of a Curator's plan, step or plan
(``refuse_manual_dispatch``): the night shift alone runs it.

The night shift (``curator.gate_of`` and ``_fire``) queues, after the night's review run, the judge run of a change
waiting for one (``queue_judge``), then the Builder of a planned change (``queue_builder``): a plan run of the
Curator's plan, pinned to the worker on duty, as the schedule's owner, within the night's caps. A change on GitHub gets
its Builder only once the hub checked the repo's ruleset within PROTECTION_DAYS and found it keeps the Curator's App
off the default branch (curator_repo_checks); one on GitLab only once the charter names the owner's git secret
(``git_secret``) and the secret is there. A Builder never runs while a judge run of its change is active, nor a judge
run while a run of the plan is.

When the Builder ends done with every step of the plan done (``builder_ended``), the change waits for its pull
request (``pr_pending``) on GitHub, or for its Judge (``judge_pending``) on GitLab, where the push opened the merge
request. The job ``curator.changes`` (``advance``) opens the pull request with the workers' App, reads its files and
runs ``judge.hack_signs`` on them: a sign fails the change at once and puts its proposal at tier 3. Those files also
give the proposal its tier again (``_retier``: ``tiers.tier_of`` over the paths the pull request really changes, which
only ever raises it), so a proposal of docs whose pull request changes code is tier 1 from then on. A list of files
GitHub cuts short is a sign too. A judge run reads what it needs with GET /v1/worker/runs/{id}/judge, the hidden checks
of the charter included, and posts its verdict with POST /v1/worker/runs/{id}/verdict. Both take, besides the worker's
token, the run's own key (``judge.JUDGE_KEY_HEADER``), which the hub makes when the run is claimed, hands the daemon in
the claim alone and keeps as a SHA-256 (``issue_judge_key``): the worker token, which code on the worker's machine can
read, neither reads a hidden check nor writes a verdict. The hub passes the change only as ``judge.final_verdict``
says, and a sign of the worker's detector puts the proposal at tier 3 too; the paths the worker read in the diff give
the proposal its tier again. A judge run that ends without a verdict queues another, JUDGE_ATTEMPTS in all, then leaves
the change open. The job then writes the Judge's check run on the pull request with the workers' App, and merges it
when ``judge.merge_decision`` says so, reading everything again from GitHub first (the ruleset with the Curator's App,
the files and the tier they give, the checks the ruleset requires and the head commit's message), and merging at the
head the Judge passed; anything else leaves the pull request open for its owner, with the reason (state ``open``).
While the project's night shift is paused the hub merges nothing: a judged change waits for it. Every WATCH_EVERY
it reads such a pull request again: merged by hand, its change is ``merged`` (when, by whom, at which commit), closed
without a merge, ``closed``. It also checks again, every RECHECK_HOURS, each ruleset it checked before. Each move of a
change adds its lines to its proposal's ledger (``evo_agents.hub.server.ledger``), and the job curator.outcomes counts
the figures of a merged change again after the charter's outcome_days (``evo_agents.hub.server.outcomes``).

GET /v1/projects/{p}/curator/changes (reader) lists the project's changes the caller may read (its proposal, and its
plan when it has one, through the project's hub sink), newest first. GET .../curator/protection
(reader) lists the project's repos with the last check of their ruleset, and POST .../curator/protection/{repo}/check
(admin) checks one now, with the Curator's App, audited as curator.protection.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, judge, runs, tables, tiers
from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import audit
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.run_state import RunStep, notify_queued, write_event
from evo_agents.hub.server.runs import (
    LINE,
    NOT_HELD,
    OBJECT_NAME,
    Pinned,
    RunId,
    VerifyResult,
    _activity,
    _insert_run,
    _lock_plan,
    _unfit,
    _worker_of,
)
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

PLAN = "curator.plan"  # an accepted proposal became a plan: "<project>/<plan> proposal:<id> change:<id> branch=<b>"
BUILD = "curator.build"  # the night shift queued a change's Builder: "<project>/<plan> run:<id> change:<id>"
JUDGE = "curator.judge"  # the night shift queued a change's judge run: "<project>/<plan> run:<id> change:<id> ..."
PULL = "curator.pull"  # the hub opened a change's pull request: "<project>/<plan> change:<id> pr=<url>"
MERGE = "curator.merge"  # the hub merged a change's pull request: "<project>/<plan> change:<id> pr=<url> sha=<sha>"
PROTECTION = "curator.protection"  # a repo's ruleset was checked: "<project> repo=<name> protected=<bool>"
PROTECTION_DAYS = 2  # a ruleset check older than this no longer lets a Builder run
RECHECK_HOURS = 24  # the job checks a ruleset again once its last check is this old
CI_WAIT = timedelta(hours=6)  # CI still running this long after the verdict leaves the pull request open
WATCH_EVERY = timedelta(hours=1)  # how often the job reads a pull request left open for its owner
MAX_WATCH = 20  # pull requests left open one pass of the job reads
MAX_LIST = 200
NIGHT_SHIFT = "night_shift"

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}

RepoName = Annotated[str, Path(min_length=1, max_length=200, pattern=LINE)]
JudgeKey = Annotated[
    str | None,
    Header(
        alias=judge.JUDGE_KEY_HEADER,
        max_length=200,
        description="the judge run's own key, which its claim handed the daemon; the worker token alone is refused",
    ),
]
PAUSED = "the night shift of the project is paused: the hub merges nothing while it is"


# Models


class Change(BaseModel):
    """What an accepted proposal of tier 0 or 1 became, and where it stands."""

    id: int
    project: str
    proposal_id: int
    plan_id: str | None = Field(description="the Curator's plan on the hub; null when the draft could not become one")
    repo: str | None
    branch: str | None = Field(description="curator/..., the one branch its runs push")
    forge: Literal[judge.FORGES] | None
    tier: int = Field(ge=0, le=3, description="its proposal's tier; 3 once a sign of score hacking showed")
    state: Literal[judge.CHANGE_STATES]
    reason: str | None = Field(description="why it stays as it is, such as why its pull request stays open")
    builder_run_id: int | None
    judge_run_id: int | None
    pr_number: int | None
    pr_url: str | None
    base_branch: str | None
    head_sha: str | None = Field(description="the commit the Judge judges, and the hub merges")
    passed: bool | None = Field(description="the Judge's verdict; null before it")
    verdict: dict | None = Field(description="the verdict: the Judge's reasons, the checks run, the signs found")
    merged_at: datetime | None
    merge_sha: str | None
    created_at: datetime
    updated_at: datetime


class ChangeList(BaseModel):
    project: str
    changes: list[Change] = Field(description="newest first")


class RepoCheck(BaseModel):
    """A repo of the project and the last check of its ruleset."""

    repo: str
    origin: str | None
    forge: Literal[judge.FORGES] | None = Field(description="null without an origin the Curator could push to")
    github_repo: str | None = Field(description="owner/name on GitHub; null elsewhere")
    protected: bool | None = Field(
        description="a ruleset keeps the Curator's App off its default branch; null: never checked"
    )
    default_branch: str | None
    reason: str | None
    rulesets: list[dict] = Field(default_factory=list, description="each {id, enforcement, can_bypass}")
    checked_at: datetime | None
    checked_by: str | None = Field(description="who asked; null for the hub's own check again")


class Protection(BaseModel):
    project: str
    repos: list[RepoCheck]


class JudgeInputs(BaseModel):
    """What a judge run reads: the change, the proposal, the plan's verify commands, the charter's protected paths,
    and the project's hidden checks, which no other run reads."""

    change_id: int
    repo: str
    branch: str
    base_branch: str | None
    head_sha: str | None = Field(description="the commit to judge; null on GitLab: the branch's tip")
    proposal: dict = Field(description="id, title, kind, tier, summary, paths")
    verify: list[str] = Field(description="the verify of each step of the plan, as the Builder had them")
    protected_paths: list[str]
    hidden_checks: list[str] = Field(description="commands run in the worktree; never logged, never shown")


class HiddenResult(BaseModel):
    index: int = Field(ge=1, le=curator.MAX_HIDDEN_CHECKS, description="the check's place in the charter, from 1")
    exit_code: int
    duration_ms: int | None = Field(None, ge=0)


class Sign(BaseModel):
    kind: Literal[tuple(judge.SIGN_KINDS)]
    path: str = Field(min_length=1, max_length=1000)
    line: int | None = Field(None, ge=0)
    text: str = Field("", max_length=judge.MAX_SIGN_TEXT)


class VerdictIn(BaseModel):
    verdict: Literal[judge.VERDICTS] | None = Field(description="the Judge agent's; null when it gave none")
    reasons: str | None = Field(None, max_length=judge.MAX_REASONS_CHARS)
    head_sha: str = Field(pattern=OBJECT_NAME, description="the commit the worktree was at")
    base_sha: str | None = Field(None, pattern=OBJECT_NAME)
    verify: list[VerifyResult] = Field(default_factory=list, max_length=50)
    hidden: list[HiddenResult] = Field(default_factory=list, max_length=curator.MAX_HIDDEN_CHECKS)
    signs: list[Sign] = Field(default_factory=list, max_length=judge.MAX_SIGNS)
    paths: list[Annotated[str, Field(min_length=1, max_length=tiers.MAX_PATH_CHARS)]] = Field(
        default_factory=list,
        max_length=judge.MAX_PATHS,
        description="every path the diff judged touches; they give the proposal its tier again, which only raises it",
    )


# Reading changes


def _changes():
    c, p = tables.curator_changes, tables.projects
    return select(
        c.c.id,
        p.c.name.label("project"),
        c.c.proposal_id,
        c.c.plan_id,
        c.c.repo,
        c.c.branch,
        c.c.forge,
        c.c.tier,
        c.c.state,
        c.c.reason,
        c.c.builder_run_id,
        c.c.judge_run_id,
        c.c.pr_number,
        c.c.pr_url,
        c.c.base_branch,
        c.c.head_sha,
        c.c.passed,
        c.c.verdict,
        c.c.merged_at,
        c.c.merge_sha,
        c.c.created_at,
        c.c.updated_at,
    ).join_from(c, p, p.c.id == c.c.project_id)


async def change_of_plan(conn: AsyncConnection, project_id: int, plan_id: str | None, *, lock: bool = False):
    """The change whose plan is ``plan_id`` in the project, or None: a plan is the Curator's when it has one."""
    if not plan_id:
        return None
    c = tables.curator_changes
    query = select(c).where(c.c.project_id == project_id, c.c.plan_id == plan_id)
    if lock:
        query = query.with_for_update()
    return (await conn.execute(query)).one_or_none()


async def _view(conn: AsyncConnection, change_id: int) -> Change:
    row = (await conn.execute(_changes().where(tables.curator_changes.c.id == change_id))).one()
    return Change(**row._mapping)


async def _set(conn: AsyncConnection, change_id: int, *, ledger_extra: dict | None = None, **values) -> None:
    """Set ``values`` on change ``change_id``; a move to another state adds its lines to the proposal's ledger
    (``ledger.change_moved``, with ``ledger_extra``)."""
    c = tables.curator_changes
    old = None
    if "state" in values:
        old = (await conn.execute(select(c.c.state).where(c.c.id == change_id).with_for_update())).scalar_one()
    await conn.execute(update(c).values(**values, updated_at=func.now()).where(c.c.id == change_id))
    if old is not None and old != values["state"]:
        await ledger_lines.change_moved(conn, change_id, old, values["state"], ledger_extra)


def _reason(text: str | None) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    return flat[:2000] or None


async def _charter(conn: AsyncConnection, project_id: int) -> dict:
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none() or {}


async def _origins(conn: AsyncConnection, project_id: int) -> dict[str, str | None]:
    pr = tables.project_repos
    rows = await conn.execute(select(pr.c.name, pr.c.origin).where(pr.c.project_id == project_id).order_by(pr.c.name))
    return {row.name: row.origin for row in rows}


# An accepted proposal becomes a plan


async def _record(conn: AsyncConnection, access: ProjectAccess, user: Principal, proposal, values: dict) -> int:
    c = tables.curator_changes
    values = {"project_id": access.project_id, "proposal_id": proposal.id, "tier": proposal.tier, **values}
    change_id = (await conn.execute(insert(c).values(**values).returning(c.c.id))).scalar_one()
    plan = values.get("plan_id") or "no plan"
    branch = values.get("branch") or "-"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=PLAN,
        target=f"{access.name}/{plan} proposal:{proposal.id} change:{change_id} branch={branch}",
        project_id=access.project_id,
    )
    return change_id


async def _make_plan(conn: AsyncConnection, access: ProjectAccess, user: Principal, proposal) -> dict | str:
    """The values of the change the draft of ``proposal`` becomes, its plan stored; or why it cannot become one."""
    draft = proposal.plan if isinstance(proposal.plan, dict) else {}
    origins = await _origins(conn, access.project_id)
    named = {
        item["repo"] for item in proposal.paths or [] if isinstance(item, dict) and isinstance(item.get("repo"), str)
    }
    for section in ("steps", "repos"):
        for item in draft.get(section) or []:
            if isinstance(item, dict) and isinstance(item.get("repo"), str):
                named.add(item["repo"])
    if not named and len(origins) == 1:
        named = set(origins)
    if len(named) != 1:
        return f"a Curator's plan works in one repo, and the proposal names {', '.join(sorted(named)) or 'no repo'}"
    (repo,) = named
    if repo not in origins:
        return f"project {access.name} has no repo {repo}"
    forge = judge.forge_of(origins[repo])
    if forge is None:
        return f"repo {repo} has no origin a Builder could push a branch to"
    draft_id = str(draft.get("id") or proposal.title)
    plan_id = judge.curator_plan_id(proposal.id, draft_id)
    branch = judge.curator_branch(proposal.id, draft_id)
    body = judge.curator_plan(
        draft, plan_id=plan_id, repo=repo, branch=branch, proposal_id=proposal.id, project=access.name
    )
    try:
        plan_routes._schema_checked(body, plan_id)
        label = access.push_label(proposal.label)
        async with conn.begin_nested():  # a store refused after its insert takes nothing with it
            stored = await plan_routes._store(
                conn,
                access,
                user,
                None,
                area="active",
                label=label,
                body=body,
                summary=f"made by the Curator from proposal #{proposal.id}",
                action=plan_routes.PLAN_CREATE,
                made_by_curator=True,
            )
    except plan_routes.PlanError as exc:
        return exc.message
    except HTTPException as exc:
        return str(exc.detail)
    if stored is None:
        return f"project {access.name} has a plan {plan_id} already"
    return {"plan_id": plan_id, "repo": repo, "branch": branch, "forge": forge}


async def plan_from_proposal(conn: AsyncConnection, access: ProjectAccess, user: Principal, proposal) -> int:
    """Make the draft of ``proposal`` (a row of proposals, as ``proposals._proposals`` reads it) the Curator's plan,
    as ``user``, and record its change; its id. A draft that cannot become one (see the module's docstring) is
    recorded as a change left open, with why, and no plan: nothing of it runs."""
    made = await _make_plan(conn, access, user, proposal)
    if isinstance(made, str):
        why = _reason(f"it cannot become the Curator's plan: {made}")
        change_id = await _record(conn, access, user, proposal, {"state": "open", "reason": why})
        await ledger_lines.change_recorded(conn, change_id)
        log.info("curator plan not made", extra={"project": access.name, "proposal_id": proposal.id, "why": made})
        return change_id
    change_id = await _record(conn, access, user, proposal, made)
    await ledger_lines.change_recorded(conn, change_id)
    log.info("curator plan made", extra={"project": access.name, "plan_id": made["plan_id"], "change_id": change_id})
    return change_id


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
    or a change of anything but the progress of its steps and its status (``judge.progress_free``). None for any other
    plan, and for a write that keeps them."""
    if old_body is None and not made_by_curator and judge.is_curator_plan_id(plan_id):
        return (
            f"plan ids that start with {judge.CURATOR_PLAN_PREFIX} belong to the Curator: the hub makes such a plan "
            "from an accepted proposal alone; give the plan another id. Nothing was written"
        )
    change = await change_of_plan(conn, project_id, plan_id)
    if change is None:
        return None
    why = (
        f"plan {plan_id} is the Curator's: it works on {change.branch} of {change.repo} alone, never on another branch "
        "or repo and least of all on a default branch, and a write of it changes the status, done_at, evidence and "
        "note of its steps and its status alone, its what, goal, context, verify and acceptance staying as the hub "
        "made them; nothing was written"
    )
    repos = new_body.get("repos")
    if not isinstance(repos, list) or len(repos) != 1 or not isinstance(repos[0], dict):
        return why
    if repos[0].get("repo") != change.repo or repos[0].get("branch") != change.branch:
        return why
    steps = new_body.get("steps") if isinstance(new_body.get("steps"), list) else []
    if any(isinstance(step, dict) and step.get("repo") not in (None, change.repo) for step in steps):
        return why
    if old_body is not None and judge.progress_free(old_body) != judge.progress_free(new_body):
        return why
    return None


async def refuse_manual_dispatch(conn: AsyncConnection, project_id: int, plan_id: str) -> None:
    """409 when plan ``plan_id`` is the Curator's: the night shift alone runs it."""
    if await change_of_plan(conn, project_id, plan_id) is not None:
        raise HTTPException(
            409,
            f"plan {plan_id} is the Curator's: only the night shift of the project's charter runs it, on its worker on "
            "duty, on its own branch; nothing was dispatched",
        )


# What a run of the Curator gets


@dataclass(frozen=True)
class CuratorPolicy:
    """A run of the Curator, and what its charter lets it have: its role, the owner's git secret it uses on other
    forges than GitHub, the env secrets it gets, and the charter's protected paths."""

    role: str  # reviewer, builder or judge
    git_secret: str | None
    env_secrets: tuple[str, ...]
    protected: tuple[str, ...]
    change_id: int | None = None


async def curator_policy(
    conn: AsyncConnection, project_id: int, kind: str, plan_id: str | None
) -> CuratorPolicy | None:
    """The policy of a run of ``kind`` on ``plan_id`` when it is a run of the Curator (a review run, a judge run, or
    any run of a Curator's plan); None for any other run."""
    change = None
    if kind == "review":
        role = "reviewer"
    elif kind == "judge":
        role = "judge"
        change = await change_of_plan(conn, project_id, plan_id)
    else:
        change = await change_of_plan(conn, project_id, plan_id)
        if change is None:
            return None
        role = "builder"
    charter = await _charter(conn, project_id)
    secret = charter.get("git_secret")
    return CuratorPolicy(
        role=role,
        git_secret=secret if isinstance(secret, str) and secret else None,
        env_secrets=tuple(name for name in charter.get("env_secrets") or [] if isinstance(name, str)),
        protected=tuple(path for path in charter.get("protected_paths") or [] if isinstance(path, str)),
        change_id=None if change is None else change.id,
    )


async def run_curator(conn: AsyncConnection, project_id: int, view) -> dict | None:
    """What the claim of run ``view`` (a ``runs.Run``) tells its worker of the Curator: its role, the charter's
    protected paths its watchdog compares the worktree with, and for a Builder or a Judge the change, its branch and
    forge, and for a Judge the commit to judge. None for any other run."""
    policy = await curator_policy(conn, project_id, view.kind, view.plan_id or None)
    if policy is None:
        return None
    found = {"role": policy.role, "protected_paths": list(policy.protected)}
    if policy.change_id is not None:
        change = (
            await conn.execute(select(tables.curator_changes).where(tables.curator_changes.c.id == policy.change_id))
        ).one()
        found.update(
            change_id=change.id,
            branch=change.branch,
            forge=change.forge,
            base_branch=change.base_branch,
            head_sha=change.head_sha if policy.role == "judge" else None,
            pr_url=change.pr_url,
        )
        if policy.role == "judge" and change.state == "judging" and change.judge_run_id == view.id:
            found["judge_key"] = await issue_judge_key(conn, change.id)
    return found


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def issue_judge_key(conn: AsyncConnection, change_id: int) -> str:
    """A new key of the judge run of change ``change_id``, which the run's claim hands its daemon: the hub keeps its
    SHA-256 alone, and a key issued before it no longer counts."""
    key = secrets.token_urlsafe(32)
    c = tables.curator_changes
    await conn.execute(update(c).values(judge_key=_key_digest(key)).where(c.c.id == change_id))
    return key


async def judge_prompt(conn: AsyncConnection, view) -> str:
    """The prompt of judge run ``view``: ``judge.build_judge_prompt`` over its change, its proposal and the plan at
    the revision the run was queued at."""
    r, pr, rev = tables.runs, tables.proposals, tables.plan_revisions
    project_id = (await conn.execute(select(r.c.project_id).where(r.c.id == view.id))).scalar_one()
    change = await change_of_plan(conn, project_id, view.plan_id)
    if change is None:
        return judge.build_judge_prompt(
            view.project, {}, {}, "", branch="", base="the default branch", head=None, pr_url=None
        )
    proposal = (
        await conn.execute(
            select(pr.c.id, pr.c.title, pr.c.kind, pr.c.tier, pr.c.summary).where(pr.c.id == change.proposal_id)
        )
    ).one()
    body = (
        await conn.execute(
            select(rev.c.body).where(
                rev.c.project_id == project_id, rev.c.plan_id == view.plan_id, rev.c.revision == view.plan_revision
            )
        )
    ).scalar_one_or_none() or {}
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


# The night shift


async def _owner_secret(conn: AsyncConnection, owner_id: int, project_id: int, name: str | None) -> bool:
    """Whether the owner has a live git secret ``name`` bound to the project."""
    if not name:
        return False
    s, b = tables.secrets, tables.secret_bindings
    bound = exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id)
    query = select(
        exists().where(
            s.c.owner_id == owner_id,
            s.c.name == name,
            s.c.kind == "git",
            s.c.deleted_at.is_(None),
            (s.c.expires_at.is_(None)) | (s.c.expires_at > func.now()),
            bound,
        )
    )
    return bool((await conn.execute(query)).scalar_one())


async def _checked(conn: AsyncConnection, project_id: int, repo: str) -> bool:
    """Whether the hub checked the repo's ruleset within PROTECTION_DAYS and found it keeps the Curator off."""
    rc = tables.curator_repo_checks
    query = select(
        exists().where(
            rc.c.project_id == project_id,
            rc.c.repo == repo,
            rc.c.protected.is_(True),
            rc.c.checked_at > func.now() - timedelta(days=PROTECTION_DAYS),
        )
    )
    return bool((await conn.execute(query)).scalar_one())


def _run_values(due, gate, *, kind: str, plan_id: str, revision: int, title: str, runtime: str, model, repos) -> dict:
    budget = gate.budget
    return {
        "kind": kind,
        "project_id": due.project_id,
        "plan_id": plan_id,
        "title": title,
        "plan_revision": revision,
        "dispatched_by": due.owner_id,
        "dispatched_via": "schedule",
        "pinned_worker_id": gate.worker.id,
        "requested_runtime": runtime,
        "runtime": runtime,
        "model": model,
        "mode": "headless",
        "approval": "auto",
        "timeout_s": budget["max_seconds"] + curator.BUDGET_GRACE_SECONDS,
        "repos": repos,
        "schedule_id": due.id,
        "schedule_night": gate.night,
        "budget": budget,
    }


def _caps(budget: dict) -> str:
    return (
        f"cost cap {curator.money(budget['max_usd'])}, {budget['max_turns']} turns, "
        f"{budget['max_seconds'] // 60} minutes of agent time"
    )


async def queue_builder(conn: AsyncConnection, due, gate) -> int | None:
    """Queue the Builder of the oldest planned change of the project the night shift may build now; its run's id, or
    None (see the module's docstring). Called under the schedule's row lock."""
    c = tables.curator_changes
    planned = (
        await conn.execute(select(c).where(c.c.project_id == due.project_id, c.c.state == "planned").order_by(c.c.id))
    ).all()
    if not planned:
        return None
    charter, access, worker = due.body, gate.access, gate.worker
    builder = charter.get("builder") or {}
    runtime = builder.get("runtime") or curator.DEFAULT_RUNTIME
    skipped = []
    for change in planned:
        held = await plan_routes._held(conn, access, change.plan_id)
        if held is None or not access.visible(held.label, plan_routes._sink(access, None)):
            skipped.append(f"{change.plan_id}: not on the hub")
            continue
        if change.forge == "github" and not await _checked(conn, due.project_id, change.repo):
            skipped.append(f"{change.plan_id}: the ruleset of {change.repo} was not checked")
            continue
        if change.forge == "gitlab" and not await _owner_secret(
            conn, due.owner_id, due.project_id, charter.get("git_secret")
        ):
            skipped.append(f"{change.plan_id}: the charter names no git secret of the owner for {change.repo}")
            continue
        await _lock_plan(conn, due.project_id, change.plan_id)
        activity = await _activity(conn, due.project_id, change.plan_id)
        judging = (
            await conn.execute(
                select(
                    exists().where(
                        tables.runs.c.project_id == due.project_id,
                        tables.runs.c.plan_id == change.plan_id,
                        tables.runs.c.kind == "judge",
                        tables.runs.c.state.in_(runs.ACTIVE_STATES),
                    )
                )
            )
        ).scalar_one()
        if activity.plan_run is not None or activity.steps or judging:
            skipped.append(f"{change.plan_id}: has an active run")
            continue
        steps = held.body.get("steps") if isinstance(held.body.get("steps"), list) else []
        if not any(runs.unready_reason(held.body, step) is None for step in steps):
            skipped.append(f"{change.plan_id}: no ready step")
            continue
        repos = [{"repo": change.repo, "branch": change.branch}]
        problems = _unfit(worker, due.project, "plan", [change.repo], runtime)
        if problems:
            skipped.append(f"{change.plan_id}: {'; '.join(problems)}")
            continue
        values = _run_values(
            due,
            gate,
            kind="plan",
            plan_id=change.plan_id,
            revision=held.revision,
            title=runs.step_title(held.body),
            runtime=runtime,
            model=builder.get("model"),
            repos=repos,
        )
        try:
            run_id = await _insert_run(conn, values)
        except IntegrityError:  # another run of the plan got in first
            skipped.append(f"{change.plan_id}: has an active run")
            continue
        await _set(conn, change.id, builder_run_id=run_id, reason=None)
        text = (
            f"Queued by the night shift of project {due.project} as the Builder of the Curator's change #{change.id} "
            f"(proposal #{change.proposal_id}, tier {change.tier}), as {due.owner} on worker {worker.name}, on "
            f"{change.branch} of {change.repo} alone: {_caps(gate.budget)}."
        )
        await write_event(conn, run_id, {"text": text, "change_id": change.id, **gate.budget})
        await notify_queued(conn, run_id)
        await audit.record(
            conn,
            actor_id=due.owner_id,
            token_id=None,
            action=BUILD,
            target=f"{due.project}/{change.plan_id} run:{run_id} change:{change.id}",
            project_id=due.project_id,
        )
        log.info("curator builder queued", extra={"project": due.project, "run_id": run_id, "change_id": change.id})
        return run_id
    log.info("no curator change to build", extra={"project": due.project, "why": "; ".join(skipped)})
    return None


async def queue_judge(conn: AsyncConnection, due, gate) -> int | None:
    """Queue the judge run of the oldest change of the project that waits for one; its id, or None. The Judge runs on
    Codex when the project's policy clears Codex for the change's label and the worker on duty has it, else on Claude
    Code with a model other than the Builder's (``judge.judge_runtime``). Called under the schedule's row lock."""
    c, p = tables.curator_changes, tables.proposals
    waiting = (
        await conn.execute(
            select(c, p.c.label.label("proposal_label"), p.c.title.label("proposal_title"))
            .join_from(c, p, p.c.id == c.c.proposal_id)
            .where(c.c.project_id == due.project_id, c.c.state == "judge_pending")
            .order_by(c.c.id)
            .limit(1)
        )
    ).one_or_none()
    if waiting is None:
        return None
    access, worker, charter = gate.access, gate.worker, due.body
    held = await plan_routes._held(conn, access, waiting.plan_id)
    if held is None:
        await _set(conn, waiting.id, state="open", reason="its plan is no longer on the hub")
        return None
    await _lock_plan(conn, due.project_id, waiting.plan_id)
    activity = await _activity(conn, due.project_id, waiting.plan_id)
    if activity.plan_run is not None or activity.steps:
        return None  # a Builder of the change still runs: never both at once
    usable = {name for name in runs.RUNTIMES if _available(worker, name)}
    cleared = judge.codex_cleared(access.rules, waiting.proposal_label)
    runtime, model, why = judge.judge_runtime(
        charter.get("judge") or {}, charter.get("builder") or {}, codex_allowed=cleared, worker_runtimes=usable
    )
    problems = _unfit(worker, due.project, "judge", [waiting.repo], runtime)
    if problems:
        log.info("no judge run yet", extra={"project": due.project, "why": "; ".join(problems)})
        return None
    values = _run_values(
        due,
        gate,
        kind="judge",
        plan_id=waiting.plan_id,
        revision=held.revision,
        title=f"Judge of the Curator's change #{waiting.id}: {waiting.proposal_title}"[:200],
        runtime=runtime,
        model=model,
        repos=[{"repo": waiting.repo, "branch": waiting.branch}],
    )
    values["max_attempts"] = 1  # a judge run that ends without a verdict is tried again by the change, not the reaper
    try:
        run_id = await _insert_run(conn, values)
    except IntegrityError:  # another judge run of the project is active
        return None
    await _set(conn, waiting.id, state="judging", judge_run_id=run_id, reason=None)
    head = waiting.head_sha[:12] if waiting.head_sha else f"the tip of {waiting.branch}"
    text = (
        f"Queued by the night shift of project {due.project} as the Judge of the Curator's change #{waiting.id} "
        f"(proposal #{waiting.proposal_id}, tier {waiting.tier}) at {head}, on {runtime}"
        f"{f' ({model})' if model else ''}: {why}. It reads the proposal, the diff, the plan's verify and the "
        f"project's hidden checks, never the Builder's transcript: {_caps(gate.budget)}."
    )
    await write_event(conn, run_id, {"text": text, "change_id": waiting.id, **gate.budget})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=due.owner_id,
        token_id=None,
        action=JUDGE,
        target=f"{due.project}/{waiting.plan_id} run:{run_id} change:{waiting.id} runtime={runtime}",
        project_id=due.project_id,
    )
    log.info("curator judge queued", extra={"project": due.project, "run_id": run_id, "change_id": waiting.id})
    return run_id


def _available(worker: Pinned, name: str) -> bool:
    from evo_agents.hub.server.runs import available

    return available(worker.runtimes.get(name))


# When a run of a change ends


def _all_done(body) -> bool:
    steps = body.get("steps") if isinstance(body, dict) else None
    return (
        isinstance(steps, list)
        and bool(steps)
        and all(isinstance(step, dict) and step.get("status") == "done" for step in steps)
    )


async def builder_ended(conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str) -> None:
    """What the end of plan run ``found`` does to its change, when its plan is the Curator's: done with every step
    of the plan done, the change waits for its pull request (GitHub) or its Judge (GitLab); otherwise it stays
    planned, with why."""
    if new not in runs.TERMINAL_STATES or found.project_id is None:
        return
    change = await change_of_plan(conn, found.project_id, found.plan_id, lock=True)
    if change is None or change.state != "planned" or old == "parked":
        return
    pl = tables.plans
    body = (
        await conn.execute(select(pl.c.body).where(pl.c.project_id == found.project_id, pl.c.plan_id == found.plan_id))
    ).scalar_one_or_none()
    if new == "done" and _all_done(body):
        state = "pr_pending" if change.forge == "github" else "judge_pending"
        await _set(conn, change.id, state=state, builder_run_id=found.run_id, reason=None)
        what = (
            "the hub opens its pull request"
            if state == "pr_pending"
            else "its merge request is open; its Judge comes next"
        )
        await write_event(
            conn, found.run_id, {"text": f"The Curator's change #{change.id} is built: {what}.", "change_id": change.id}
        )
        return
    why = found.error or reason if new != "done" else "its plan has steps not done"
    await _set(
        conn, change.id, builder_run_id=found.run_id, reason=_reason(f"Builder run #{found.run_id} ended {new}: {why}")
    )
    if new == "failed":
        await ledger_lines.build_failed(conn, change, found.run_id, new, why)


async def judge_ended(conn: AsyncConnection, found: RunStep, old: str, new: str, *, reason: str) -> None:
    """What the end of judge run ``found`` does: a run that ends without the verdict of its change queues another
    (the change waits for a Judge again), JUDGE_ATTEMPTS in all, then leaves the change open; a failure sends the
    owner the notice run_failed."""
    if new not in runs.TERMINAL_STATES or found.project_id is None:
        return
    from evo_agents.hub.server.notifications import notify, run_link

    if new == "failed":
        error = found.error or reason
        await notify(
            conn,
            user_id=found.dispatcher_id,
            kind="notice",
            notice_kind="run_failed",
            project_id=found.project_id,
            run_id=found.run_id,
            title=f"Judge run #{found.run_id} of {found.plan_id} failed",
            body=error,
            details={"run_kind": "judge", "plan_id": found.plan_id, "error": error},
            link=run_link(found.project, found.run_id),
        )
    change = await change_of_plan(conn, found.project_id, found.plan_id, lock=True)
    if change is None or change.state != "judging" or change.judge_run_id != found.run_id:
        return
    attempts = change.judge_attempts + 1
    why = found.error or reason
    if attempts >= judge.JUDGE_ATTEMPTS:
        await _set(
            conn,
            change.id,
            state="open",
            judge_attempts=attempts,
            reason=_reason(f"{attempts} judge runs ended without a verdict, the last {new}: {why}"),
        )
        return
    await _set(
        conn,
        change.id,
        state="judge_pending",
        judge_attempts=attempts,
        reason=_reason(f"judge run #{found.run_id} ended {new} without a verdict ({why}): another one comes"),
    )


# The judge run's side


async def _held_judge(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """(run row, change row) of the judge run ``run_id`` the worker of ``user`` holds, whose change waits for its
    verdict, asked with the key the run's claim handed its daemon; 404 for any other run, 409 for a change that waits
    for no verdict of it, 403 without the key."""
    worker_id = (await _worker_of(conn, user))[0]
    r = tables.runs
    row = (
        await conn.execute(
            select(r.c.id, r.c.state, r.c.worker_id, r.c.kind, r.c.project_id, r.c.plan_id, r.c.plan_revision)
            .where(r.c.id == run_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if row.kind != "judge":
        raise HTTPException(404, f"run {run_id} is not a judge run: only a judge run reads what a Judge reads")
    change = await change_of_plan(conn, row.project_id, row.plan_id, lock=True)
    if change is None or change.state != "judging" or change.judge_run_id != run_id:
        raise HTTPException(409, f"run {run_id} judges no change that waits for its verdict")
    if not key or change.judge_key is None or not hmac.compare_digest(_key_digest(key), change.judge_key):
        raise HTTPException(
            403,
            f"run {run_id} is a judge run: what it reads and its verdict take the run's own key "
            f"({judge.JUDGE_KEY_HEADER}), which its claim handed the daemon; the worker token alone reads no hidden "
            "check and writes no verdict",
        )
    return row, change


async def _plan_verify(conn: AsyncConnection, row) -> list[str]:
    rev = tables.plan_revisions
    body = (
        await conn.execute(
            select(rev.c.body).where(
                rev.c.project_id == row.project_id, rev.c.plan_id == row.plan_id, rev.c.revision == row.plan_revision
            )
        )
    ).scalar_one_or_none()
    return judge.verify_commands(body or {})


@worker_router.get("/runs/{run_id}/judge", response_model=JudgeInputs, responses=REFUSALS)
async def judge_inputs(
    request: Request, run_id: RunId, user: CurrentUser, response: Response, judge_key: JudgeKey = None
) -> JudgeInputs:
    """What the judge run this worker holds reads: its change, its proposal, the plan's verify commands, the
    charter's protected paths and the project's hidden checks; with the run's own key."""
    async with request.app.state.engine.begin() as conn:
        row, change = await _held_judge(conn, user, run_id, judge_key)
        charter = await _charter(conn, row.project_id)
        hidden = [item for item in (charter.get("judge") or {}).get("hidden_checks") or [] if isinstance(item, str)]
        await _set(conn, change.id, hidden_count=len(hidden))
        pr = tables.proposals
        proposal = (
            await conn.execute(
                select(pr.c.id, pr.c.title, pr.c.kind, pr.c.tier, pr.c.summary, pr.c.paths).where(
                    pr.c.id == change.proposal_id
                )
            )
        ).one()
        verify = await _plan_verify(conn, row)
        await write_event(
            conn,
            run_id,
            {
                "text": f"The Judge read its inputs: {len(verify)} verify command(s) of the plan and {len(hidden)} "
                "hidden check(s) of the project, which only this run reads.",
                "change_id": change.id,
            },
        )
    response.headers.update(NO_STORE)
    log.info("judge inputs read", extra={"run_id": run_id, "change_id": change.id, "hidden": len(hidden)})
    return JudgeInputs(
        change_id=change.id,
        repo=change.repo,
        branch=change.branch,
        base_branch=change.base_branch,
        head_sha=change.head_sha,
        proposal=dict(proposal._mapping),
        verify=verify,
        protected_paths=[item for item in charter.get("protected_paths") or [] if isinstance(item, str)],
        hidden_checks=hidden,
    )


async def _retier(conn: AsyncConnection, change, paths: list[str], charter: dict) -> int:
    """The tier of the change's proposal again, from ``paths``, those the diff really touches, with the charter's
    protected paths (``tiers.tier_of``): raised when they give a higher one, with the reasons, never lowered. The
    tier the change has now."""
    pr = tables.proposals
    row = (
        await conn.execute(
            select(pr.c.kind, pr.c.tier, pr.c.tier_reasons).where(pr.c.id == change.proposal_id).with_for_update()
        )
    ).one()
    named = []
    for path in paths:
        normal = tiers.normalize_path(path)
        named.append((change.repo, normal if normal is not None else "(a path that is no repo path)"))
    protected = [item for item in charter.get("protected_paths") or [] if isinstance(item, str)]
    try:
        found = tiers.tier_of(row.kind, sorted(set(named)), protected)
    except ValueError:
        found = tiers.Tier(3, [f"kind {row.kind} is not a kind of change: tier 3"])
    if found.tier <= row.tier:
        return max(row.tier, change.tier)
    kept = list(row.tier_reasons or [])
    for reason in found.reasons[1:]:
        if reason not in kept:
            kept.append(reason)
    kept.append(f"the files the change really touches give it tier {found.tier}, not {row.tier}")
    await conn.execute(update(pr).values(tier=found.tier, tier_reasons=kept).where(pr.c.id == change.proposal_id))
    log.info(
        "curator change raised",
        extra={"change_id": change.id, "proposal_id": change.proposal_id, "tier": found.tier, "was": row.tier},
    )
    return found.tier


async def _raise_tier(conn: AsyncConnection, change, signs: list[dict]) -> None:
    """Put the change's proposal at tier 3, with the reason, once a diff of it shows signs."""
    pr = tables.proposals
    reasons = (await conn.execute(select(pr.c.tier_reasons).where(pr.c.id == change.proposal_id))).scalar_one()
    reason = judge.raised_reason(signs)
    kept = list(reasons or [])
    if reason not in kept:
        kept.append(reason)
    await conn.execute(update(pr).values(tier=3, tier_reasons=kept).where(pr.c.id == change.proposal_id))


@worker_router.post("/runs/{run_id}/verdict", response_model=Change, responses=REFUSALS)
async def record_verdict(
    request: Request, run_id: RunId, body: VerdictIn, user: CurrentUser, judge_key: JudgeKey = None
) -> Change:
    """The verdict of the judge run this worker holds, with the run's own key: the hub passes the change only as
    ``judge.final_verdict`` says, and the paths the diff touches give its proposal its tier again."""
    async with request.app.state.engine.begin() as conn:
        row, change = await _held_judge(conn, user, run_id, judge_key)
        expected = await _plan_verify(conn, row)
        hidden_count = change.hidden_count
        if hidden_count is None:
            charter = await _charter(conn, row.project_id)
            hidden_count = len((charter.get("judge") or {}).get("hidden_checks") or [])
        signs = [sign.model_dump() for sign in body.signs]
        head = change.head_sha or body.head_sha  # GitLab: the tip the Judge read is the one judged
        verify = [item.model_dump(exclude_none=True) for item in body.verify]
        hidden = [item.model_dump(exclude_none=True) for item in body.hidden]
        passed, failures = judge.final_verdict(
            agent=body.verdict,
            verify=verify,
            expected_verify=expected,
            hidden=hidden,
            hidden_count=hidden_count,
            signs=signs,
            head_matches=body.head_sha == head,
        )
        verdict = {
            "run_id": run_id,
            "agent": body.verdict,
            "reasons": body.reasons,
            "head_sha": body.head_sha,
            "base_sha": body.base_sha,
            "verify": verify,
            "hidden": hidden,
            "signs": signs,
            "failures": failures,
            "source": "judge",
        }
        values = {
            "state": "judged",
            "passed": passed,
            "verdict": verdict,
            "head_sha": head,
            "reason": None,
            "judge_key": None,
        }
        if signs:
            await _raise_tier(conn, change, signs)
            values["tier"] = 3
        elif body.paths:
            charter = await _charter(conn, row.project_id)
            values["tier"] = await _retier(conn, change, body.paths, charter)
        await _set(conn, change.id, **values)
        text = f"The Judge {'passed' if passed else 'failed'} the Curator's change #{change.id}"
        text += "." if passed else f": {'; '.join(failures)}."
        await write_event(conn, run_id, {"text": text, "change_id": change.id, "passed": passed})
        view = await _view(conn, change.id)
    log.info("verdict recorded", extra={"run_id": run_id, "change_id": change.id, "passed": passed})
    return view


# Reading


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the Curator of project {access.name} needs a grant on it")


@router.get("/{project}/curator/changes", response_model=ChangeList, responses=REFUSALS)
async def list_changes(request: Request, project: ProjectName, user: CurrentUser) -> ChangeList:
    """The project's Curator changes, newest first, with the plans the caller may read."""
    c, pr = tables.curator_changes, tables.proposals
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        query = (
            _changes()
            .add_columns(pr.c.label.label("proposal_label"))
            .join(pr, pr.c.id == c.c.proposal_id)
            .where(c.c.project_id == access.project_id)
            .order_by(c.c.id.desc())
            .limit(MAX_LIST)
        )
        rows = (await conn.execute(query)).all()
        from evo_agents.hub.server.runs import visible_plans

        readable = set(await visible_plans(conn, access, None))
    through = plan_routes._sink(access, None)
    shown = [
        Change(**{key: value for key, value in row._mapping.items() if key != "proposal_label"})
        for row in rows
        if access.visible(row.proposal_label, through) and (row.plan_id is None or row.plan_id in readable)
    ]
    return ChangeList(project=access.name, changes=shown)


def _repo_check(name: str, origin: str | None, row) -> RepoCheck:
    found = github_repo(origin) if origin else None
    return RepoCheck(
        repo=name,
        origin=origin,
        forge=judge.forge_of(origin),
        github_repo="/".join(found) if found else None,
        protected=None if row is None else row.protected,
        default_branch=None if row is None else row.default_branch,
        reason=None if row is None else row.reason,
        rulesets=[] if row is None else list(row.rulesets or []),
        checked_at=None if row is None else row.checked_at,
        checked_by=None if row is None else row.checked_by_login,
    )


def _checks(project_id: int):
    rc, u = tables.curator_repo_checks, tables.users
    return (
        select(rc, u.c.login.label("checked_by_login"))
        .join_from(rc, u, u.c.id == rc.c.checked_by, isouter=True)
        .where(rc.c.project_id == project_id)
    )


@router.get("/{project}/curator/protection", response_model=Protection, responses=REFUSALS)
async def protection(request: Request, project: ProjectName, user: CurrentUser) -> Protection:
    """The project's repos, and whether the hub checked that a ruleset keeps the Curator off each default branch."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        origins = await _origins(conn, access.project_id)
        checks = {row.repo: row for row in await conn.execute(_checks(access.project_id))}
    return Protection(
        project=access.name, repos=[_repo_check(name, origin, checks.get(name)) for name, origin in origins.items()]
    )


async def _store_check(conn: AsyncConnection, project_id: int, repo: str, owner_repo: str, found, by: int | None):
    rc = tables.curator_repo_checks
    values = {
        "github_repo": owner_repo,
        "default_branch": found.default_branch,
        "protected": found.protected,
        "reason": _reason(found.reason) or "no reason",
        "rulesets": found.rulesets,
        "checked_at": func.now(),
        "checked_by": by,
    }
    statement = pg_insert(rc).values(project_id=project_id, repo=repo, **values)
    await conn.execute(statement.on_conflict_do_update(constraint="curator_repo_checks_pkey", set_=values))


@router.post(
    "/{project}/curator/protection/{repo}/check",
    response_model=RepoCheck,
    responses={**REFUSALS, 422: {"model": ErrorBody}, 502: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def check_protection(request: Request, project: ProjectName, repo: RepoName, user: CurrentUser) -> RepoCheck:
    """Check now, with the Curator's App, whether a ruleset keeps the Curator off the repo's default branch; the
    project's admins alone may."""
    from evo_agents.hub.server.pulls import check_ruleset

    state = request.app.state
    async with state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        if not has_role(access.role, "admin"):
            raise HTTPException(403, f"checking the rulesets of project {access.name} needs the admin role on it")
        origins = await _origins(conn, access.project_id)
    if repo not in origins:
        raise HTTPException(404, f"project {project} has no repo {repo}")
    found_repo = github_repo(origins[repo]) if origins[repo] else None
    if found_repo is None:
        raise HTTPException(422, f"repo {repo} is not on GitHub: the hub checks rulesets on GitHub alone")
    app: GitHubApp | None = getattr(state, "curator_app", None)
    if app is None:
        unset = ", ".join(state.config.curator_app_missing())
        raise HTTPException(503, f"this hub has no Curator App ({unset} not set): it cannot check a ruleset")
    owner, name = found_repo
    try:
        found = await check_ruleset(app, owner, name)
    except GitHubUnavailable as exc:
        raise HTTPException(502, str(exc)) from None
    async with state.engine.begin() as conn:
        await _store_check(conn, access.project_id, repo, f"{owner}/{name}", found, user.user_id)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=PROTECTION,
            target=f"{access.name} repo={repo} protected={str(found.protected).lower()}",
            project_id=access.project_id,
        )
        row = (await conn.execute(_checks(access.project_id).where(tables.curator_repo_checks.c.repo == repo))).one()
    log.info("ruleset checked", extra={"project": project, "repo": repo, "protected": found.protected})
    return _repo_check(repo, origins[repo], row)


# The job


@dataclass(frozen=True)
class _Target:
    owner: str
    name: str


def _target(origin: str | None) -> _Target | None:
    found = github_repo(origin) if origin else None
    return _Target(*found) if found else None


async def _due_changes(engine: AsyncEngine) -> list:
    c, pr = tables.curator_changes, tables.project_repos
    async with engine.begin() as conn:
        query = (
            select(c, pr.c.origin)
            .join_from(c, pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == c.c.repo), isouter=True)
            .where(c.c.state.in_(("pr_pending", "judged")))
            .order_by(c.c.id)
        )
        return (await conn.execute(query)).all()


async def _owner_of(conn: AsyncConnection, project_id: int) -> int | None:
    s = tables.schedules
    query = select(s.c.owner_id).where(s.c.project_id == project_id, s.c.kind == NIGHT_SHIFT)
    return (await conn.execute(query)).scalar_one_or_none()


def _pr_body(project: str, change, proposal_title: str) -> str:
    return (
        f"The Curator of project {project} on the evo-agents hub made this change from proposal "
        f"#{change.proposal_id} (tier {change.tier}): {proposal_title}.\n\n"
        f"Its plan on the hub is {change.plan_id}. The Judge writes its verdict as the check run "
        f'"{judge.JUDGE_CHECK_NAME}"; the hub merges only a change of tier 0 that the Judge passed, with CI green.'
    )


async def _open_pull(engine: AsyncEngine, app: GitHubApp, change) -> str:
    from evo_agents.hub.server import pulls

    target = _target(change.origin)
    if target is None:
        async with engine.begin() as conn:
            await _set(conn, change.id, state="open", reason="its repo has no origin on GitHub")
        return "open"
    async with engine.begin() as conn:
        pr, p = tables.proposals, tables.projects
        title, project = (
            await conn.execute(
                select(pr.c.title, p.c.name)
                .join_from(pr, p, p.c.id == pr.c.project_id)
                .where(pr.c.id == change.proposal_id)
            )
        ).one()
        charter = await _charter(conn, change.project_id)
        rev = tables.plans
        body = (
            await conn.execute(
                select(rev.c.body).where(rev.c.project_id == change.project_id, rev.c.plan_id == change.plan_id)
            )
        ).scalar_one_or_none() or {}

    async def opened(token: str) -> dict:
        found = await pulls.repository(app, token, target.owner, target.name)
        base = found.get("default_branch")
        if not isinstance(base, str) or not base:
            raise GitHubRefused(f"GitHub names no default branch of {target.owner}/{target.name}")
        made = await pulls.open_pull(
            app,
            token,
            target.owner,
            target.name,
            branch=change.branch,
            base=base,
            title=f"Curator: {title}"[:240],
            body=_pr_body(project, change, title),
        )
        return {"pull": made, "base": base}

    try:
        found = await pulls.with_token(app, target.owner, target.name, "open", opened)
    except GitHubRefused as exc:
        async with engine.begin() as conn:
            await _set(conn, change.id, reason=_reason(f"the pull request could not be opened: {exc}"))
        return "refused"
    made, base = found["pull"], found["base"]
    head = (made.get("head") or {}).get("sha")
    number, url = made.get("number"), made.get("html_url")
    if not judge.is_sha(head) or type(number) is not int:
        raise GitHubUnavailable("GitHub answered without the pull request's number and head")

    async def read(token: str) -> tuple[list[dict], str | None]:
        return await _files_of(app, token, target, number, made.get("changed_files"))

    files, cut = await pulls.with_token(app, target.owner, target.name, "read", read)
    diff = judge.from_github_files(files)
    signs = judge.hack_signs(
        diff,
        repo=change.repo,
        protected=[item for item in charter.get("protected_paths") or [] if isinstance(item, str)],
        verify_commands=judge.verify_commands(body),
    )
    if cut is not None:  # first, so the cap on signs never drops it
        signs.insert(0, {"kind": "diff_unreadable", "path": "(the pull request's files)", "line": None, "text": cut})
    async with engine.begin() as conn:
        locked = await change_of_plan(conn, change.project_id, change.plan_id, lock=True)
        if locked is None or locked.state != "pr_pending":
            return "moved"
        values = {"pr_number": number, "pr_url": url, "base_branch": base, "head_sha": head, "reason": None}
        values["tier"] = await _retier(conn, locked, judge.changed_paths(diff), charter)
        if signs:
            _, failures = judge.final_verdict(
                agent=None, verify=[], expected_verify=[], hidden=[], hidden_count=0, signs=signs, head_matches=True
            )
            verdict = {"signs": signs, "failures": failures[:1], "source": "hub", "head_sha": head}
            await _raise_tier(conn, locked, signs)
            values.update(state="judged", passed=False, verdict=verdict, tier=3)
        else:
            values["state"] = "judge_pending"
        await _set(conn, change.id, **values)
        if change.builder_run_id is not None:
            await write_event(
                conn,
                change.builder_run_id,
                {"text": f"The hub opened pull request {url} of the Curator's change #{change.id}.", "pr": url},
            )
        await audit.record(
            conn,
            actor_id=None,
            token_id=None,
            action=PULL,
            target=f"{project}/{change.plan_id} change:{change.id} pr={url}",
            project_id=change.project_id,
        )
    return "signs" if signs else "opened"


async def _files_of(
    app: GitHubApp, token: str, target: _Target, number: int, expected
) -> tuple[list[dict], str | None]:
    """(the files of pull request ``number`` as GitHub lists them, why that list is not the whole of them or None):
    a list GitHub cuts short, or that holds fewer files than the pull request says it changes, is not read in part."""
    from evo_agents.hub.server import pulls

    try:
        files = await pulls.pull_files(app, token, target.owner, target.name, number)
    except pulls.Truncated as exc:
        return [], str(exc)
    if type(expected) is int and expected > len(files):
        return files, f"GitHub lists {len(files)} of the {expected} files the pull request changes"
    return files, None


async def _night_paused(conn: AsyncConnection, project_id: int) -> bool:
    """Whether the project's night shift is paused (a member paused it, or its circuit breaker): it merges nothing."""
    s = tables.schedules
    query = select(exists().where(s.c.project_id == project_id, s.c.kind == NIGHT_SHIFT, s.c.paused_at.is_not(None)))
    return bool((await conn.execute(query)).scalar_one())


async def _wait_paused(engine: AsyncEngine, change) -> str:
    """Leave a judged change as it is while its project's night shift is paused, saying why."""
    async with engine.begin() as conn:
        c = tables.curator_changes
        await conn.execute(update(c).values(reason=PAUSED).where(c.c.id == change.id))
    return "paused"


async def _write_check(engine: AsyncEngine, app: GitHubApp, change) -> str:
    from evo_agents.hub.server import pulls

    target = _target(change.origin)
    verdict = change.verdict or {}
    reasons = list(verdict.get("failures") or [])
    conclusion, title, summary = judge.check_run_output(
        bool(change.passed),
        reasons,
        verdict.get("verify") or [],
        verdict.get("hidden") or [],
        verdict.get("signs") or [],
    )

    async def write(token: str) -> int:
        return await pulls.create_check_run(
            app,
            token,
            target.owner,
            target.name,
            sha=change.head_sha,
            conclusion=conclusion,
            title=title,
            summary=summary,
            external_id=f"curator-change:{change.id}",
        )

    check_id = await pulls.with_token(app, target.owner, target.name, "check", write)
    async with engine.begin() as conn:
        await _set(conn, change.id, check_run_id=check_id)
    return "checked"


async def _ruleset_now(engine: AsyncEngine, curator_app: GitHubApp | None, change, target: _Target):
    """Check the repo's ruleset again now, with the Curator's App, and keep what it found; the ``RulesetCheck``, None
    without the Curator's App."""
    from evo_agents.hub.server.pulls import RulesetCheck, check_ruleset

    if curator_app is None:
        return None
    try:
        found = await check_ruleset(curator_app, target.owner, target.name)
    except GitHubUnavailable:
        raise
    except GitHubRefused as exc:  # check_ruleset turns refusals into a failed check; this is belt and braces
        found = RulesetCheck(False, None, str(exc))
    async with engine.begin() as conn:
        await _store_check(conn, change.project_id, change.repo, f"{target.owner}/{target.name}", found, None)
    return found


async def _merge(engine: AsyncEngine, app: GitHubApp, curator_app: GitHubApp | None, change) -> str:
    from evo_agents.hub.server import pulls

    if not change.passed:
        async with engine.begin() as conn:
            failures = (change.verdict or {}).get("failures") or []
            await _set(
                conn, change.id, state="open", reason=_reason("the Judge did not pass it: " + "; ".join(failures))
            )
        return "open"
    target = _target(change.origin)
    async with engine.begin() as conn:
        charter = await _charter(conn, change.project_id)
        pr = tables.proposals
        tier = (await conn.execute(select(pr.c.tier).where(pr.c.id == change.proposal_id))).scalar_one()
        body = (
            await conn.execute(
                select(tables.plans.c.body).where(
                    tables.plans.c.project_id == change.project_id, tables.plans.c.plan_id == change.plan_id
                )
            )
        ).scalar_one_or_none() or {}
        paused = await _night_paused(conn, change.project_id)
    auto = tuple(item for item in charter.get("auto_merge") or [] if isinstance(item, int))
    if tier != 0 or 0 not in auto:  # no call to GitHub for a change the hub would never merge
        why = (
            f"tier {tier} waits for its owner to merge it"
            if tier != 0
            else "the charter's auto_merge does not name tier 0"
        )
        async with engine.begin() as conn:
            await _set(conn, change.id, state="open", reason=why)
        return "open"
    if paused:
        return await _wait_paused(engine, change)
    ruleset = await _ruleset_now(engine, curator_app, change, target)

    async def read(token: str) -> dict:
        found = await pulls.pull(app, token, target.owner, target.name, change.pr_number)
        files, cut = await _files_of(app, token, target, change.pr_number, found.get("changed_files"))
        head = (found.get("head") or {}).get("sha")
        runs_found = await pulls.check_runs(app, token, target.owner, target.name, head) if judge.is_sha(head) else None
        statuses = (
            await pulls.combined_status(app, token, target.owner, target.name, head) if judge.is_sha(head) else None
        )
        try:
            commits = await pulls.pull_commits(app, token, target.owner, target.name, change.pr_number)
        except GitHubRefused:
            commits = []
        message = next(
            (
                (item.get("commit") or {}).get("message")
                for item in commits
                if isinstance(item, dict) and item.get("sha") == head
            ),
            None,
        )
        repo = await pulls.repository(app, token, target.owner, target.name)
        return {
            "pull": found,
            "files": files,
            "cut": cut,
            "runs": runs_found,
            "statuses": statuses,
            "message": message,
            "repo": repo,
        }

    seen = await pulls.with_token(app, target.owner, target.name, "read", read)
    found = seen["pull"]
    diff = judge.from_github_files(seen["files"])
    signs = judge.hack_signs(
        diff,
        repo=change.repo,
        protected=[item for item in charter.get("protected_paths") or [] if isinstance(item, str)],
        verify_commands=judge.verify_commands(body),
    )
    if seen["cut"] is not None:  # first, so the cap on signs never drops it
        signs.insert(
            0, {"kind": "diff_unreadable", "path": "(the pull request's files)", "line": None, "text": seen["cut"]}
        )
    async with engine.begin() as conn:
        tier = await _retier(conn, change, judge.changed_paths(diff), charter)
        if tier != change.tier:
            await _set(conn, change.id, tier=tier)
    if isinstance(seen["message"], str):
        ci, ci_reasons = judge.ci_state(
            seen["runs"],
            seen["statuses"],
            required=ruleset.required_checks if ruleset is not None else [],
            head_message=seen["message"],
        )
    else:
        ci, ci_reasons = "unreadable", ["the hub could not read the message of the pull request's head commit"]
    facts = judge.MergeFacts(
        forge=change.forge,
        tier=tier,
        auto_merge=auto,
        repo_checked=ruleset is not None and ruleset.protected,
        passed=bool(change.passed),
        judged_sha=change.head_sha,
        pr_state=found.get("state"),
        pr_merged=found.get("merged") is True,
        pr_head=(found.get("head") or {}).get("sha"),
        pr_base=(found.get("base") or {}).get("ref"),
        default_branch=seen["repo"].get("default_branch"),
        signs=tuple(signs),
        ci=ci,
        ci_reasons=tuple(ci_reasons),
        mergeable=found.get("mergeable") if isinstance(found.get("mergeable"), bool) else None,
    )
    decided, reasons = judge.merge_decision(facts)
    if decided == "wait":
        async with engine.begin() as conn:
            verdict_at = change.updated_at  # when the verdict, then its check run, was written
            if verdict_at is not None and await _older_than(conn, verdict_at, CI_WAIT):
                await _set(
                    conn,
                    change.id,
                    state="open",
                    reason=_reason(f"CI did not finish within 6 hours: {'; '.join(reasons)}"),
                )
                return "open"
            await conn.execute(
                update(tables.curator_changes)
                .values(reason=_reason("; ".join(reasons)))
                .where(tables.curator_changes.c.id == change.id)
            )
        return "wait"
    if decided == "open":
        async with engine.begin() as conn:
            await _set(conn, change.id, state="open", reason=_reason("; ".join(reasons)))
        return "open"

    async with engine.begin() as conn:
        if await _night_paused(conn, change.project_id):  # paused while the hub read GitHub
            paused = True
    if paused:
        return await _wait_paused(engine, change)

    async def merge(token: str) -> str:
        title = f"Merge the Curator's change #{change.id} (proposal #{change.proposal_id}, tier 0)"
        return await pulls.merge_pull(
            app, token, target.owner, target.name, change.pr_number, sha=change.head_sha, title=title
        )

    try:
        merged = await pulls.with_token(app, target.owner, target.name, "merge", merge)
    except GitHubRefused as exc:
        async with engine.begin() as conn:
            await _set(conn, change.id, state="open", reason=_reason(f"GitHub refused the merge: {exc}"))
        return "open"
    async with engine.begin() as conn:
        before = (found.get("base") or {}).get("sha")  # the default branch the pull request merged into
        await _set(
            conn,
            change.id,
            state="merged",
            merged_at=func.now(),
            merge_sha=merged,
            reason=None,
            ledger_extra={"before_sha": before},
        )
        project = (
            await conn.execute(select(tables.projects.c.name).where(tables.projects.c.id == change.project_id))
        ).scalar_one()
        owner_id = await _owner_of(conn, change.project_id)
        if owner_id is not None:
            from evo_agents.hub.server.notifications import notify, run_link

            await notify(
                conn,
                user_id=owner_id,
                kind="notice",
                notice_kind="merge_default_branch",
                project_id=change.project_id,
                run_id=change.builder_run_id,
                title=f"The hub merged pull request #{change.pr_number} of {change.repo} into {change.base_branch}",
                body=(
                    f"The Curator's change #{change.id} (proposal #{change.proposal_id}, tier 0) passed its Judge at "
                    f"{change.head_sha} with CI green; the hub merged {change.pr_url} as {merged}."
                ),
                details={
                    "repo": change.repo,
                    "branch": change.base_branch,
                    "commits": [merged],
                    "change_id": change.id,
                    "pr": change.pr_url,
                },
                link=run_link(project, change.builder_run_id) if change.builder_run_id else None,
            )
        await audit.record(
            conn,
            actor_id=None,
            token_id=None,
            action=MERGE,
            target=f"{project}/{change.plan_id} change:{change.id} pr={change.pr_url} sha={merged}",
            project_id=change.project_id,
        )
    log.info("curator change merged", extra={"change_id": change.id, "sha": merged})
    return "merged"


async def _older_than(conn: AsyncConnection, moment: datetime, span: timedelta) -> bool:
    return bool((await conn.execute(select(func.now() - span > moment))).scalar_one())


async def _recheck(engine: AsyncEngine, curator_app: GitHubApp | None, outcomes: Counter) -> None:
    """Check again the rulesets checked more than RECHECK_HOURS ago."""
    if curator_app is None:
        return
    from evo_agents.hub.server.pulls import check_ruleset

    rc, pr = tables.curator_repo_checks, tables.project_repos
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(rc.c.project_id, rc.c.repo, pr.c.origin)
                .join_from(rc, pr, (pr.c.project_id == rc.c.project_id) & (pr.c.name == rc.c.repo))
                .where(rc.c.checked_at < func.now() - timedelta(hours=RECHECK_HOURS))
                .order_by(rc.c.checked_at)
                .limit(20)
            )
        ).all()
    for row in rows:
        target = _target(row.origin)
        if target is None:
            continue
        found = await check_ruleset(curator_app, target.owner, target.name)
        async with engine.begin() as conn:
            await _store_check(conn, row.project_id, row.repo, f"{target.owner}/{target.name}", found, None)
        outcomes["rechecked"] += 1


async def _left_open(engine: AsyncEngine) -> list:
    """The changes left open for their owner with a pull request on GitHub, not read for WATCH_EVERY."""
    c, pr = tables.curator_changes, tables.project_repos
    async with engine.begin() as conn:
        query = (
            select(c, pr.c.origin)
            .join_from(c, pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == c.c.repo), isouter=True)
            .where(
                c.c.state == "open",
                c.c.forge == "github",
                c.c.pr_number.is_not(None),
                c.c.updated_at < func.now() - WATCH_EVERY,
            )
            .order_by(c.c.updated_at)
            .limit(MAX_WATCH)
        )
        return (await conn.execute(query)).all()


def _github_time(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")) if text else None
    except ValueError:
        return None


async def _member_of(conn: AsyncConnection, login: str | None) -> int | None:
    if not login:
        return None
    u = tables.users
    return (await conn.execute(select(u.c.id).where(func.lower(u.c.login) == login.lower()))).scalar_one_or_none()


async def _watch(engine: AsyncEngine, app: GitHubApp, change) -> str:
    """Read a pull request left open for its owner: merged by hand, the change is merged (and its figures will be
    counted again); closed, it is closed; else it is read again after WATCH_EVERY."""
    from evo_agents.hub.server import pulls

    target = _target(change.origin)

    async def read(token: str) -> dict:
        return await pulls.pull(app, token, target.owner, target.name, change.pr_number)

    try:
        if target is None:
            raise GitHubRefused(f"repo {change.repo} has no origin on GitHub")
        found = await pulls.with_token(app, target.owner, target.name, "read", read)
    except GitHubRefused as exc:  # read again after WATCH_EVERY, not at every pass
        async with engine.begin() as conn:
            await _set(conn, change.id)
        log.warning("a pull request left open could not be read", extra={"change_id": change.id, "why": str(exc)})
        return "refused"
    async with engine.begin() as conn:
        locked = await change_of_plan(conn, change.project_id, change.plan_id, lock=True)
        if locked is None or locked.state != "open":
            return "moved"
        if found.get("merged") is True:
            login = (found.get("merged_by") or {}).get("login")
            sha = found.get("merge_commit_sha")
            extra = {
                "actor": "user",
                "actor_id": await _member_of(conn, login),
                "login": login,
                "before_sha": (found.get("base") or {}).get("sha"),
            }
            await _set(
                conn,
                change.id,
                state="merged",
                merged_at=_github_time(found.get("merged_at")) or func.now(),
                merge_sha=sha if judge.is_sha(sha) else None,
                reason=None,
                ledger_extra=extra,
            )
            return "merged_by_hand"
        if found.get("state") == "closed":
            await _set(conn, change.id, state="closed", reason="its pull request was closed without a merge")
            return "closed"
        await _set(conn, change.id)  # read again after WATCH_EVERY
    return "still_open"


async def advance(engine: AsyncEngine, github_app: GitHubApp | None, curator_app: GitHubApp | None) -> dict:
    """One pass of the job ``curator.changes`` (see the module's docstring); how many changes ended in each outcome.
    GitHub failing leaves a change as it is for the next pass."""
    outcomes: Counter = Counter()
    for change in await _due_changes(engine):
        try:
            if change.forge != "github":
                if change.state == "judged":
                    async with engine.begin() as conn:
                        why = "a merge request on GitLab stays open: the hub never merges one"
                        if not change.passed:
                            why = "the Judge did not pass it: " + "; ".join(
                                (change.verdict or {}).get("failures") or []
                            )
                        await _set(conn, change.id, state="open", reason=_reason(why))
                    outcomes["open"] += 1
                continue
            if github_app is None:
                outcomes["no_app"] += 1
                continue
            if change.state == "pr_pending":
                outcomes[await _open_pull(engine, github_app, change)] += 1
                continue
            if change.check_run_id is None and judge.is_sha(change.head_sha) and _target(change.origin) is not None:
                outcomes[await _write_check(engine, github_app, change)] += 1
            outcomes[await _merge(engine, github_app, curator_app, change)] += 1
        except GitHubUnavailable as exc:
            log.warning("a curator change waits for GitHub", extra={"change_id": change.id, "why": str(exc)})
            outcomes["github"] += 1
        except Exception:  # one change failing leaves the others their turn
            log.exception("a curator change failed", extra={"change_id": change.id})
            outcomes["failed"] += 1
    if github_app is not None:
        for change in await _left_open(engine):
            try:
                found = await _watch(engine, github_app, change)
                if found in ("merged_by_hand", "closed"):
                    outcomes[found] += 1
            except GitHubUnavailable as exc:
                why = {"change_id": change.id, "why": str(exc)}
                log.warning("a pull request left open waits for GitHub", extra=why)
            except Exception:  # one change failing leaves the others their turn
                log.exception("reading a pull request left open failed", extra={"change_id": change.id})
    try:
        await _recheck(engine, curator_app, outcomes)
    except GitHubUnavailable as exc:
        log.warning("rulesets left to check again later", extra={"why": str(exc)})
    return dict(sorted(outcomes.items()))
