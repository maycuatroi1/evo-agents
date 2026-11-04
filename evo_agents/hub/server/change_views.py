"""The Curator's changes as the hub keeps them (``evo_agents.hub.server.changes`` describes the design): their models,
reading and writing a change, the refusals that keep the Curator's plans as the hub made them, and what a run of the
Curator gets, its judge run's key and prompt included."""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, findings, judge, security_pass, tables, tiers
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server.runs.models import OBJECT_NAME, VerifyResult

log = logging.getLogger(__name__)

JudgeKey = Annotated[
    str | None,
    Header(
        alias=judge.JUDGE_KEY_HEADER,
        max_length=200,
        description="the judge run's own key, which its claim handed the daemon; the worker token alone is refused",
    ),
]


# Models


class Change(BaseModel):
    """What an accepted proposal of tier 0 to 2 became, and where it stands."""

    id: int
    project: str
    proposal_id: int
    plan_id: str | None = Field(description="the Curator's plan on the hub; null when the draft could not become one")
    repo: str | None
    branch: str | None = Field(description="curator/..., the one branch its runs push")
    forge: judge.Forge | None
    tier: int = Field(ge=0, le=3, description="its proposal's tier; 3 once a sign of score hacking showed")
    state: judge.ChangeState
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


class ChangeClose(BaseModel):
    reason: str = Field(min_length=1, max_length=2000, description="why it is closed: its reason and ledger say it")


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
    knowledge: dict | None = Field(
        None,
        description="a change of kind kg_source whose pull request changed knowledge.yaml: its files, the sources it "
        "edits and the checks the worker runs for it besides the verify (harness validate, a trial sync of each "
        "source), whose results the verdict's verify carries under those names; null for any other change",
    )


class HiddenResult(BaseModel):
    index: int = Field(ge=1, le=curator.MAX_HIDDEN_CHECKS, description="the check's place in the charter, from 1")
    exit_code: int
    duration_ms: int | None = Field(None, ge=0)


class Sign(BaseModel):
    kind: judge.SignKind
    path: str = Field(min_length=1, max_length=1000)
    line: int | None = Field(None, ge=0)
    text: str = Field("", max_length=judge.MAX_SIGN_TEXT)


class SecurityFinding(BaseModel):
    """A vulnerability the security pass kept after its filter."""

    path: str | None = Field(None, max_length=findings.MAX_PATH_CHARS)
    line: int | None = Field(None, ge=1)
    text: str = Field(min_length=1, max_length=findings.MAX_TEXT_CHARS)


class SecurityPass(BaseModel):
    """What the security pass of a Judge run (``evo_agents.hub.security_pass``) said of the change, and what it cost:
    the dimension ``security``, its findings with path and line, and the turns the pass took."""

    verdict: judge.Verdict
    reason: str = Field("", max_length=2000)
    findings: list[SecurityFinding] = Field(default_factory=list, max_length=findings.MAX_NEW)
    candidates: int = Field(0, ge=0, description="the findings the first turn raised, before the filter")
    turns: int = Field(0, ge=0, description="agent turns the pass took: one that finds, one for each candidate")
    cost_usd: float | None = Field(None, ge=0, description="what those turns cost, when the runtime reports it")


def security_dimension(security: SecurityPass | None) -> dict:
    """The dimension ``security`` of a verdict from the pass; a verdict without the pass fails it."""
    if security is None:
        return {"verdict": "fail", "reason": "the security pass gave no result"}
    return {"verdict": security.verdict, "reason": security.reason}


def security_findings(security: SecurityPass | None) -> list[dict]:
    """The findings of the pass as ``findings.read_new`` reads them."""
    found = [] if security is None else [item.model_dump() for item in security.findings]
    return findings.read_new([{**item, "dimension": security_pass.DIMENSION} for item in found])


class VerdictIn(BaseModel):
    verdict: judge.Verdict | None = Field(description="the Judge agent's; null when it gave none")
    security: SecurityPass | None = Field(
        None, description="the security pass of the run: a verdict without it fails the dimension security"
    )
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


def changes_query():
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


async def change_with_origin(conn: AsyncConnection, project_id: int, change_id: int, *, lock: bool = False):
    """Change ``change_id`` of the project with its repo's origin, as the job reads a change it moves on, or None."""
    c, pr = tables.curator_changes, tables.project_repos
    query = (
        select(c, pr.c.origin)
        .join_from(c, pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == c.c.repo), isouter=True)
        .where(c.c.id == change_id, c.c.project_id == project_id)
    )
    if lock:
        query = query.with_for_update(of=c)
    return (await conn.execute(query)).one_or_none()


async def change_view(conn: AsyncConnection, change_id: int) -> Change:
    row = (await conn.execute(changes_query().where(tables.curator_changes.c.id == change_id))).one()
    return Change(**row._mapping)


async def set_change(conn: AsyncConnection, change_id: int, *, ledger_extra: dict | None = None, **values) -> None:
    """Set ``values`` on change ``change_id``; a move to another state adds its lines to the proposal's ledger
    (``ledger.change_moved``, with ``ledger_extra``)."""
    c = tables.curator_changes
    old = None
    if "state" in values:
        old = (await conn.execute(select(c.c.state).where(c.c.id == change_id).with_for_update())).scalar_one()
    await conn.execute(update(c).values(**values, updated_at=func.now()).where(c.c.id == change_id))
    if old is not None and old != values["state"]:
        await ledger_lines.change_moved(conn, change_id, old, values["state"], ledger_extra)


async def charter_of(conn: AsyncConnection, project_id: int) -> dict:
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none() or {}


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
    steps = new_body.get("steps")
    steps = steps if isinstance(steps, list) else []
    if any(isinstance(step, dict) and step.get("repo") not in (None, change.repo) for step in steps):
        return why
    if old_body is not None and judge.progress_free(old_body) != judge.progress_free(new_body):
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


@dataclass(frozen=True)
class CuratorPolicy:
    """A run of the Curator, and what its charter lets it have: its role, the owner's git secret it uses on other
    forges than GitHub, the env secrets it gets, and the charter's protected paths."""

    role: str  # reviewer, builder, judge or replay
    git_secret: str | None
    env_secrets: tuple[str, ...]
    protected: tuple[str, ...]
    change_id: int | None = None


async def curator_policy(
    conn: AsyncConnection, project_id: int, kind: str, plan_id: str | None
) -> CuratorPolicy | None:
    """The policy of a run of ``kind`` on ``plan_id`` when it is a run of the Curator (a review run, a judge run, a
    replay run, a history run, a probe run, or any run of a Curator's plan); None for any other run."""
    roles = {"review": "reviewer", "judge": "judge", "replay": "replay", "history": "history", "probe": "probe"}
    role = roles.get(kind, "builder")
    change = None if kind == "review" else await change_of_plan(conn, project_id, plan_id)
    if change is None and (role == "builder" or (role == "judge" and not plan_id)):  # no plan: the merge queue's
        return None
    charter = await charter_of(conn, project_id)
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
    protected paths its watchdog compares the worktree with, and for a Builder, a Judge or a replay the change, its
    branch and forge, for a Judge, a replay and a probe the change's head, for a replay its task and its own key
    (``replays.replay_claim``), and for a probe run its probe and its own key (``probe_runs.probe_claim``). None for
    any other run."""
    from evo_agents.hub.server.probe_runs import probe_claim  # it reads the changes of this module
    from evo_agents.hub.server.replays import replay_claim  # it reads the changes of this module

    policy = await curator_policy(conn, project_id, view.kind, view.plan_id or None)
    if policy is None:
        return None
    found, change = {"role": policy.role, "protected_paths": list(policy.protected)}, None
    if policy.change_id is not None:
        change = (
            await conn.execute(select(tables.curator_changes).where(tables.curator_changes.c.id == policy.change_id))
        ).one()
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
    return found


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def issue_judge_key(conn: AsyncConnection, change_id: int) -> str:
    """A new key of the judge run of change ``change_id``, which the run's claim hands its daemon: the hub keeps its
    SHA-256 alone, and a key issued before it no longer counts."""
    key = secrets.token_urlsafe(32)
    c = tables.curator_changes
    await conn.execute(update(c).values(judge_key=key_digest(key)).where(c.c.id == change_id))
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


# A proposal's tier


async def raise_tier(conn: AsyncConnection, change, signs: list[dict]) -> None:
    """Put the change's proposal at tier 3, with the reason, once a diff of it shows signs."""
    pr = tables.proposals
    reasons = (await conn.execute(select(pr.c.tier_reasons).where(pr.c.id == change.proposal_id))).scalar_one()
    reason = judge.raised_reason(signs)
    kept = list(reasons or [])
    if reason not in kept:
        kept.append(reason)
    await conn.execute(update(pr).values(tier=3, tier_reasons=kept).where(pr.c.id == change.proposal_id))
