"""The merge check of a pull request on github.com (plan super-board-parity-2): the hub judges a pull request a plan
run opened, at its head, before the run merges it, in a repo whose policy has ``merge_check`` on
(``autonomy.MERGE_CHECK``). A repo that leaves it off keeps its behaviour: nothing here reads it.

The agent of the plan run asks with ``evo-agents worker merge-check REPO`` (POST /v1/worker/runs/{id}/merge-check).
The hub reads the pull request with the workers' GitHub App (a token of pull_requests read alone): it must be open,
of the run's branch, and not of a fork. Its row in merge_checks (schema 0187, one a pull request) holds the head read
then; the verdict holds for that head alone, so a check asked at a new head starts again, with the findings of the
old one still open (``findings.carried``), and a judge run of the old head no longer counts. The route opens a wait
of the run (a decision of category ``wait``, as ``evo-agents worker wait`` does): the agent ends its turn, the reaper
parks the run, and no worker slot is held while the Judges work. The verdict answers the wait
(``run_waits.wake``) with what it says; a wait whose verdict never came wakes at WAIT_HOURS, and asking again reads
where the check stands.

``advance``, on each pass of the job hub.merge_queue, queues the judge run of each check that waits for one: the
merge queue's own judge run (``merge_judge.queue_judge_run``: pinned to the policy's judge.worker or the charter's
worker on duty, on Claude Code with a model other than the Builder's, one judge run active a project), tried again
when one ends without a verdict, up to ``judge.JUDGE_ATTEMPTS``. Its claim (``claim``) names the pull request, its head
and its base branch, with ``merge_result``: the worker merges the current base into the head in a scratch worktree, a
conflict fails the check with "needs a rebase", and the plan's verify for the repo and the project's hidden checks run
on that merge result, while the Judge reads the pull request's own diff. Its prompt is ``merge_gate.build_prompt`` for
a pull request (``prompt``), it reads its inputs from GET /v1/worker/runs/{id}/merge-judge and posts its verdict to
POST /v1/worker/runs/{id}/merge-verdict, the merge queue's routes, which hand a run of a check to this module. The
verdict is read as the queue reads its own (``merge_judge.Judged``: the checks, the Judge's dimensions, the findings of
each round with its marks of earlier ones, and the security pass, ``evo_agents.hub.security_pass``). Once the first
Judge passed, ``advance`` weighs the risk of the paths it read (``merge_gate.risk_of``, with the project's graph); a
high one has the second Judge judge it too. The Judge never reads the Builder's transcript.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections import Counter
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import autonomy, findings, judge, merge_gate, runs, tables
from evo_agents.hub.server import audit, change_tiers, pulls, run_events, run_waits
from evo_agents.hub.server import merge_gate as queue
from evo_agents.hub.server import merge_judge as judging
from evo_agents.hub.server.decisions import OptionIn, store_decision
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.protection import github_target
from evo_agents.hub.server.run_state import notify_queued, write_event
from evo_agents.hub.server.runs.models import LINE, REFUSALS, RunId
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

STATES = JUDGING, PASSED, FAILED = ("judging", "passed", "failed")
REBASE = "needs a rebase"
WAIT_HOURS = 24  # a wait whose verdict never came wakes then: asking again reads where the check stands
COMMAND = "evo-agents worker merge-check"

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# The rows


async def row_of(conn: AsyncConnection, check_id: int, *, lock: bool = False):
    m = tables.merge_checks
    query = select(m).where(m.c.id == check_id)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def judged_check(conn: AsyncConnection, run_id: int, *, lock: bool = False):
    """The merge check judge run ``run_id`` judges; None for any other run."""
    m = tables.merge_checks
    query = select(m).where(m.c.judge_run_id == run_id)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def set_row(conn: AsyncConnection, check_id: int, **values) -> None:
    m = tables.merge_checks
    await conn.execute(update(m).values(**values, updated_at=func.now()).where(m.c.id == check_id))


# Asking for it


class MergeCheckIn(BaseModel):
    repo: str = Field(min_length=1, max_length=200, pattern=LINE, description="the repo of the run's pull request")
    number: int | None = Field(None, ge=1, description="the pull request; null: the open one of the run's branch")
    branch: str | None = Field(
        None, min_length=1, max_length=255, pattern=LINE, description="the branch the run pushes, as its worker has it"
    )


class MergeCheck(BaseModel):
    """Where the merge check of a pull request stands, and the wait its verdict answers."""

    id: int
    repo: str
    number: int
    head_sha: str = Field(description="the head the verdict holds for; a new push needs a new check")
    state: str = Field(description="judging, passed or failed")
    reason: str | None
    web_url: str | None
    decision_id: int | None = Field(description="the run's wait the verdict answers; null when the verdict is there")
    wake_at: datetime | None = Field(description="when the wait wakes at the latest")


def _repo_of(project, run, body: MergeCheckIn, plan: dict | None) -> tuple[str, object]:
    """(the run's branch of ``body.repo``, its owner/name on GitHub); 409 or 422 when the check is not for it."""
    settings = project.policy[autonomy.REPOS].get(body.repo)
    facts = project.repos.get(body.repo)
    if settings is None or facts is None:
        raise HTTPException(422, f"project {project.name} has no repo {body.repo}")
    if settings[autonomy.MERGE_CHECK] != autonomy.ON:
        raise HTTPException(
            409,
            f"the policy of project {project.name} has repos.{body.repo}.{autonomy.MERGE_CHECK} off: the hub does not "
            "judge its pull requests before a merge, so merge it as before",
        )
    target = github_target(facts.origin)
    if target is None:
        raise HTTPException(409, f"{body.repo} is not on github.com: the merge check judges a pull request there")
    entry = next((item for item in run.repos or [] if isinstance(item, dict) and item.get("repo") == body.repo), None)
    if entry is None:
        raise HTTPException(422, f"the run has no repo {body.repo}")
    branch = entry.get("branch") or (merge_gate.plan_branch(plan, body.repo) if plan else None) or body.branch
    if not branch:
        raise HTTPException(422, f"the run names no branch of {body.repo}: name it with --branch")
    return branch, target


async def _read_pull(app, target, branch: str, number: int | None) -> dict:
    """The pull request ``number`` of owner/name ``target``, else the open one of ``branch``; 404, 409 or 503 when
    GitHub has none, refuses, or does not answer."""
    if app is None:
        raise HTTPException(503, "the hub has no GitHub App of its workers: it cannot read the pull request")

    async def read(token: str) -> dict | None:
        if number is not None:
            return await pulls.pull(app, token, target.owner, target.name, number)
        return await pulls.find_pull(app, token, target.owner, target.name, branch)

    try:
        found = await pulls.with_token(app, target.owner, target.name, "pulls", read)
    except GitHubRefused as exc:
        raise HTTPException(409, f"GitHub refused to show the pull request: {exc}") from None
    except GitHubUnavailable as exc:
        raise HTTPException(503, f"GitHub did not answer: {exc}; ask again in a minute") from None
    if not isinstance(found, dict):
        raise HTTPException(404, f"{target.owner}/{target.name} has no open pull request of {branch}: open it first")
    return found


def pull_refusal(pull: dict, target, branch: str) -> str | None:
    """Why the hub does not judge ``pull`` for a run whose branch is ``branch``; None when it does."""
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    number = pull.get("number")
    if pull.get("state") != "open":
        return f"pull request #{number} is {pull.get('state') or 'not open'}: the hub judges an open one"
    if head.get("ref") != branch:
        return f"pull request #{number} is of branch {head.get('ref')}, not of the run's branch {branch}"
    if "repo" in head and not (
        isinstance(head["repo"], dict)
        and str(head["repo"].get("full_name") or "").lower() == f"{target.owner}/{target.name}".lower()
    ):
        return f"pull request #{number} comes from a fork: the hub judges a branch of {target.owner}/{target.name}"
    if not judge.is_sha(head.get("sha")) or type(number) is not int:
        return f"GitHub gave pull request #{number} no head commit"
    return None


async def _upsert(conn: AsyncConnection, project, run_id: int, run, repo: str, pull: dict, branch: str):
    """The row of ``pull`` at its head now: a new one, or the one there, started again at a new head."""
    m, r = tables.merge_checks, tables.runs
    head, base = pull["head"]["sha"], (pull.get("base") or {}).get("ref") or "main"
    model = (await conn.execute(select(r.c.model).where(r.c.id == run_id))).scalar_one_or_none()
    seen = {
        "run_id": run_id,
        "plan_id": run.plan_id,
        "model": model,
        "title": pull.get("title"),
        "description": merge_gate.kept_description(pull.get("body")),
        "web_url": pull.get("html_url"),
        "source_branch": branch,
        "base_branch": base,
    }
    query = select(m).where(m.c.project_id == project.id, m.c.repo == repo, m.c.number == pull["number"])
    row = (await conn.execute(query.with_for_update())).one_or_none()
    if row is None:
        values = {"project_id": project.id, "repo": repo, "number": pull["number"], "head_sha": head, **seen}
        check_id = (await conn.execute(insert(m).values(values).returning(m.c.id))).scalar_one()
        return await row_of(conn, check_id)
    if row.head_sha != head:  # a new head: the old verdict holds no more, its open findings stay
        fresh = {"head_sha": head, "base_sha": None, "state": JUDGING, "reason": None, "risk": None}
        fresh |= {"verdict": findings.carried(row.verdict), "judge_run_id": None, "judge_key": None}
        await set_row(conn, row.id, **seen, **fresh, judge_attempts=0)
    elif row.state == FAILED and not (row.verdict or {}).get("first"):  # no Judge said it: try again
        await set_row(conn, row.id, **seen, state=JUDGING, reason=None, judge_attempts=0)
    else:
        await set_row(conn, row.id, **seen)
    return await row_of(conn, row.id)


async def _open_wait(conn: AsyncConnection, run_id: int, run, row, project: str) -> tuple[int, datetime]:
    """The wait of run ``run_id`` the verdict of ``row`` answers: the open one there, else a new one."""
    d = tables.decisions
    if row.decision_id is not None:
        found = (
            await conn.execute(select(d.c.run_id, d.c.state, d.c.wake_at).where(d.c.id == row.decision_id))
        ).one_or_none()
        if found is not None and found.run_id == run_id and found.state == "open":
            return row.decision_id, found.wake_at
    wake_at = datetime.now(UTC) + timedelta(hours=WAIT_HOURS)
    what = f"pull request #{row.number} of {row.repo} at {row.head_sha[:12]}"
    ask = run_waits.WaitAsk(
        category=runs.WAIT_DECISION_CATEGORY,
        question=f"The hub's merge check of {what}: the Judges' verdict before the run merges it.",
        context="\n".join(
            [
                "## What the run waits for",
                "",
                f"- The verdict of the merge check of {what} in project {project}.",
                f"- At the latest, its time: {wake_at:%Y-%m-%d %H:%M} UTC.",
                "",
                "The hub answers this wait when the Judges have spoken, and the agent goes on in its session. Nobody "
                "needs to answer it; Go on now wakes the run sooner, and Stop waiting has it go on without the verdict.",
            ]
        ),
        options=[OptionIn(key=key, label=label, description=text) for key, label, text in run_waits.OPTIONS],
    )
    view = await store_decision(conn, run_id, run, ask, wake={"wake_at": wake_at, "wake_on": None})
    await set_row(conn, row.id, decision_id=view.id)
    return view.id, wake_at


@worker_router.post(
    "/runs/{run_id}/merge-check",
    response_model=MergeCheck,
    responses={**REFUSALS, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def ask_check(request: Request, run_id: RunId, body: MergeCheckIn, user: CurrentUser) -> MergeCheck:
    """Have the hub judge the run's pull request on github.com at its head before the run merges it: where the check
    stands, and the wait of the run its verdict answers while it judges."""
    engine = request.app.state.engine
    async with engine.begin() as conn:
        _, run = await held_plan_run(conn, user, run_id)
        project = await queue._load(conn, run.project_id)
        plan = await queue.plan_body(conn, run.project_id, run.plan_id)
        branch, target = _repo_of(project, run, body, plan)
    pull = await _read_pull(getattr(request.app.state, "github_app", None), target, branch, body.number)
    why = pull_refusal(pull, target, branch)
    if why is not None:
        raise HTTPException(409, why)
    async with engine.begin() as conn:
        _, run = await held_plan_run(conn, user, run_id, lock=True)
        row = await _upsert(conn, project, run_id, run, body.repo, pull, branch)
        decision_id = wake_at = None
        if row.state == JUDGING:
            decision_id, wake_at = await _open_wait(conn, run_id, run, row, project.name)
            row = await row_of(conn, row.id)
    log.info("merge check asked", extra={"run_id": run_id, "check_id": row.id, "state": row.state})
    return MergeCheck(
        id=row.id,
        repo=row.repo,
        number=row.number,
        head_sha=row.head_sha,
        state=row.state,
        reason=row.reason,
        web_url=row.web_url,
        decision_id=decision_id,
        wake_at=wake_at,
    )


# The gate of the agent's merge command


class MergeGateIn(BaseModel):
    repo: str = Field(min_length=1, max_length=200, pattern=LINE, description="owner/name of the pull request's repo")
    number: int = Field(ge=1, description="the pull request the merge command merges")
    head: str | None = Field(
        None, description="the head of the pull request as the hook read it now; null when it could not"
    )


class MergeGate(BaseModel):
    gated: bool = Field(description="false: the repo does not have merge_check on, so the command is not the hub's")
    passed: bool = Field(description="the hub holds a passed verdict for the head named, and the command may go on")
    head_sha: str | None = Field(description="the head the hub's verdict holds for; the merge pins it")
    reason: str | None = Field(description="why the command does not pass")
    command: str = Field(description="the command that asks for the check")


def _slug(target) -> str:
    return f"{target.owner}/{target.name}".lower()


def gate_of(row, number: int, repo: str, head: str | None) -> tuple[bool, str | None]:
    """(whether the merge may go on, why not) for pull request ``number`` of ``repo`` whose head now is ``head``, given
    its merge check ``row`` (None when the run never asked)."""
    ask = f"ask with `{COMMAND} {repo}`"
    if row is None:
        return False, f"the hub has no merge check of pull request #{number}: {ask}, and merge when it passes"
    if row.state == JUDGING:
        return False, f"the hub still judges pull request #{number} at {row.head_sha[:12]}: wait for the verdict"
    if row.state == FAILED:
        return (
            False,
            f"the merge check of pull request #{number} failed at {row.head_sha[:12]}: {row.reason or 'no reason'}",
        )
    if head is None:
        return (
            False,
            f"the hook could not read the head of pull request #{number}: the hub's verdict holds for {row.head_sha[:12]} alone",
        )
    if head != row.head_sha:
        return (
            False,
            f"pull request #{number} is at {head[:12]} now and the hub's verdict holds for {row.head_sha[:12]}: {ask} again",
        )
    return True, None


@worker_router.post(
    "/runs/{run_id}/merge-gate", response_model=MergeGate, responses={**REFUSALS, 422: {"model": ErrorBody}}
)
async def merge_gate_act(request: Request, run_id: RunId, body: MergeGateIn, user: CurrentUser) -> MergeGate:
    """The word of the hub on a merge command of the run's agent (K5): whether the pull request's repo has the merge
    check on, and when it has, whether a passed verdict holds for the head the command merges."""
    head = body.head.lower() if body.head and judge.is_sha(body.head.lower()) else None
    async with request.app.state.engine.begin() as conn:
        await run_events.claimed_run(conn, user, run_id)
        r = tables.runs
        project_id = (await conn.execute(select(r.c.project_id).where(r.c.id == run_id))).scalar_one()
        project = await queue._load(conn, project_id)
        name = next(
            (
                repo
                for repo, facts in project.repos.items()
                if (target := github_target(facts.origin)) is not None and _slug(target) == body.repo.lower()
            ),
            None,
        )
        settings = project.policy[autonomy.REPOS].get(name) if name else None
        command = f"{COMMAND} {name or body.repo}"
        if settings is None or settings[autonomy.MERGE_CHECK] != autonomy.ON:
            return MergeGate(gated=False, passed=True, head_sha=None, reason=None, command=command)
        m = tables.merge_checks
        query = select(m).where(m.c.project_id == project_id, m.c.repo == name, m.c.number == body.number)
        row = (await conn.execute(query)).one_or_none()
    passed, why = gate_of(row, body.number, name, head)
    return MergeGate(
        gated=True, passed=passed, head_sha=row.head_sha if row is not None else None, reason=why, command=command
    )


# The verdict's end: the wait answered


def woke_text(decision_id: int, row) -> str:
    """The message that wakes the agent with the verdict of check ``row``."""
    what = f"pull request #{row.number} of {row.repo}"
    lines = [
        f"Decision #{decision_id} (wait) woke: the merge check of {what} {row.state} at head {row.head_sha[:12]}"
        + (f": {row.reason}" if row.reason else "."),
    ]
    if row.state == PASSED:
        lines.append(
            f"The verdict holds for that head alone: merge it with `gh pr merge {row.number} --match-head-commit "
            f"{row.head_sha}`; after another push, ask again with `{COMMAND} {row.repo}`."
        )
    else:
        lines.append(f"Do not merge it as it is: fix it, push, and ask again with `{COMMAND} {row.repo}`.")
    return runs.clip("\n".join(lines), runs.MAX_ANSWER_BYTES)


async def settle(conn: AsyncConnection, row, state: str, reason: str) -> None:
    """End check ``row`` ``state`` (passed or failed) with ``reason``, and answer the wait of the run that asked."""
    await set_row(conn, row.id, state=state, reason=reason, judge_run_id=None, judge_key=None)
    if row.decision_id is None:
        return
    d = tables.decisions
    plan_id = (await conn.execute(select(d.c.plan_id).where(d.c.id == row.decision_id))).scalar_one_or_none()
    settled = await row_of(conn, row.id)
    await run_waits.wake(conn, row.decision_id, row.project_id, plan_id, woke_text(row.decision_id, settled))


# The judge run: its claim, its prompt, its inputs and its verdict


async def claim(conn: AsyncConnection, view) -> dict | None:
    """What the claim of judge run ``view`` tells its worker when it judges a merge check, with a new key of its own;
    None for any other run."""
    row = await judged_check(conn, view.id, lock=True)
    if row is None:
        return None
    charter = await queue.charter_of(conn, row.project_id)
    found = {
        "role": "judge",
        "protected_paths": queue.protected_of(charter),
        "branch": row.source_branch,
        "forge": "github",
        "base_branch": row.base_branch,
        "head_sha": row.head_sha,
        "pr_url": row.web_url,
        "merge_result": True,  # the checks run on the head merged with the current base
    }
    if row.state == JUDGING:
        key = secrets.token_urlsafe(32)
        await set_row(conn, row.id, judge_key=_digest(key))
        found["judge_key"] = key
    return found


def _pull(row) -> dict:
    return {
        "iid": row.number,
        "title": row.title,
        "description": row.description,
        "web_url": row.web_url,
        "source_branch": row.source_branch,
        "target_branch": row.base_branch,
    }


async def prompt(conn: AsyncConnection, view) -> str | None:
    """The prompt of judge run ``view`` when it judges a merge check; None for any other run."""
    row = await judged_check(conn, view.id)
    if row is None:
        return None
    return merge_gate.build_prompt(
        view.project,
        repo=row.repo,
        folder=None,
        mr=_pull(row),
        base=f"origin/{row.base_branch}",
        head=row.head_sha,
        plan=await queue.plan_body(conn, row.project_id, row.plan_id),
        second=queue.next_round(row.verdict, row.risk) == "second",
        risk=(row.risk or {}).get("reasons") or [],
        earlier=findings.ledger_of(row.verdict),
        forge="github",
    )


async def held(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """(run row, check row) of the judge run ``run_id`` of a merge check the worker of ``user`` holds, whose check
    waits for its verdict, asked with the run's key; as ``merge_judge.held_judge`` refuses, 409 for a check that waits
    for no verdict of it."""
    run = await judging.held_judge(conn, user, run_id)
    row = await judged_check(conn, run_id, lock=True)
    if row is None or row.state != JUDGING:
        raise HTTPException(409, f"run {run_id} judges no pull request that waits for its verdict")
    judging.require_key(run_id, key, row.judge_key)
    return run, row


async def inputs(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """What judge run ``run_id`` of a merge check reads (``merge_judge.MergeJudgeInputs``), with the run's key."""
    _, row = await held(conn, user, run_id, key)
    charter = await queue.charter_of(conn, row.project_id)
    hidden = judging.hidden_of(charter)
    verify = merge_gate.verify_of(await queue.plan_body(conn, row.project_id, row.plan_id), row.repo)
    await set_row(conn, row.id, verdict={**(row.verdict or {}), "hidden_count": len(hidden)})
    text = (
        f"The Judge read its inputs: {len(verify)} verify command(s) of the plan and {len(hidden)} hidden check(s) of "
        "the project, which only this run reads; they run on the pull request's head merged with its base."
    )
    await write_event(conn, run_id, {"text": text, "merge_check": row.id})
    return judging.MergeJudgeInputs(
        queue_id=row.id,
        repo=row.repo,
        iid=row.number,
        branch=row.source_branch,
        base_branch=row.base_branch,
        head_sha=row.head_sha,
        second=queue.next_round(row.verdict, row.risk) == "second",
        verify=verify,
        protected_paths=queue.protected_of(charter),
        hidden_checks=hidden,
    )


def _conflict_entry(run_id: int, model, body) -> dict:
    where = ", ".join(body.conflicts[:10]) + (
        f" and {len(body.conflicts) - 10} more" if len(body.conflicts) > 10 else ""
    )
    why = f"{REBASE}: merging its base into {body.head_sha[:12]} conflicts" + (f" in {where}" if where else "")
    return {"run_id": run_id, "model": model, "head_sha": body.head_sha, "passed": False, "failures": [why]}


async def verdict(conn: AsyncConnection, user: Principal, run_id: int, body, key: str | None):
    """Take the verdict of judge run ``run_id`` of a merge check (``merge_judge.MergeVerdictIn``), with the run's
    key: a fail ends the check, the first Judge's pass waits for ``advance`` to weigh the risk, the second's ends it."""
    run, row = await held(conn, user, run_id, key)
    second = queue.next_round(row.verdict, row.risk) == "second"
    if body.conflicts is not None:  # the worker could not merge the base into the head
        entry, ledger = _conflict_entry(run_id, run.model, body), findings.ledger_of(row.verdict)
    else:
        hidden_count = (row.verdict or {}).get("hidden_count")
        if not isinstance(hidden_count, int):
            hidden_count = len(judging.hidden_of(await queue.charter_of(conn, row.project_id)))
        found = judging.Judged(
            body=body,
            second=second,
            expected=merge_gate.verify_of(await queue.plan_body(conn, row.project_id, row.plan_id), row.repo),
            hidden_count=hidden_count,
            head=row.head_sha,
            ledger=findings.ledger_of(row.verdict),
            run_id=run_id,
        )
        entry, ledger = found.entry(run_id, run.model), found.ledger
    stored = {**(row.verdict or {}), ("second" if second else "first"): entry, "findings": ledger}
    if not second:
        stored["paths"] = list(body.paths)
    await set_row(
        conn, row.id, verdict=stored, base_sha=body.base_sha, judge_run_id=None, judge_key=None, judge_attempts=0
    )
    row = await row_of(conn, row.id)
    if not entry["passed"]:
        await settle(conn, row, FAILED, "; ".join(entry.get("failures") or []) or "the Judge failed it")
    elif second:
        await settle(conn, row, PASSED, "the Judge and the second Judge passed it")
    else:
        await set_row(conn, row.id, reason="the Judge passed it; the hub weighs its risk next")
    row = await row_of(conn, row.id)
    said = "passed" if entry["passed"] else "failed"
    text = f"The merge check took the verdict: the Judge {said} pull request #{row.number}; it is {row.state} now."
    await write_event(conn, run_id, {"text": text, "merge_check": row.id, "passed": entry["passed"]})
    log.info("merge check verdict", extra={"run_id": run_id, "check_id": row.id, "passed": entry["passed"]})
    return judging.MergeVerdict(queue_id=row.id, passed=entry["passed"], state=row.state, reason=row.reason)


# The job's pass


async def advance(engine: AsyncEngine, *, blobs=None, data_dir=None) -> dict:
    """One pass over the merge checks that judge, on each pass of the job hub.merge_queue: how many ended in each
    outcome (an empty dict when none judges)."""
    m = tables.merge_checks
    outcomes: Counter = Counter()
    async with engine.begin() as conn:
        ids = (await conn.execute(select(m.c.id).where(m.c.state == JUDGING).order_by(m.c.id))).scalars().all()
    reader = change_tiers.GraphReader.at(blobs, data_dir) if ids else None
    for check_id in ids:
        try:
            await _advance_one(engine, check_id, reader, outcomes)
        except Exception:  # one check failing leaves the others their pass
            log.exception("a merge check failed", extra={"check_id": check_id})
            outcomes["check_errors"] += 1
    return {f"checks_{key}": value for key, value in sorted(outcomes.items())}


async def _advance_one(engine: AsyncEngine, check_id: int, reader, outcomes) -> None:
    async with engine.begin() as conn:
        row = await row_of(conn, check_id)
        if row is None or row.state != JUDGING:
            return
        project = await queue._load(conn, row.project_id)
    settings = project.policy[autonomy.REPOS].get(row.repo)
    first = (row.verdict or {}).get("first") or {}
    risk = None
    if first.get("passed") and row.risk is None:
        risk = await _risk(engine, project, row.repo, (row.verdict or {}).get("paths") or [], reader)
    async with engine.begin() as conn:
        locked = await row_of(conn, check_id, lock=True)
        if locked is None or locked.state != JUDGING or locked.head_sha != row.head_sha:
            return
        if settings is None or settings[autonomy.MERGE_CHECK] != autonomy.ON:
            await settle(conn, locked, FAILED, f"the policy has {autonomy.MERGE_CHECK} off for {row.repo} now")
            outcomes["off"] += 1
            return
        if risk is not None and locked.risk is None:
            await set_row(conn, check_id, risk=risk)
            locked = await row_of(conn, check_id)
        await _judge_next(conn, project, locked, outcomes)


async def _risk(engine: AsyncEngine, project, repo: str, paths: list[str], reader) -> dict:
    """The risk of the ``paths`` the first Judge read, with what the project's graph says they reach."""
    named = change_tiers.named_paths(repo, paths)
    targets = [f"{name}:{path}" for name, path in named]
    graph = await change_tiers.impact_as_owner(engine, project.id, targets, reader, owner_id=project.owner_id)
    async with engine.begin() as conn:
        unknown = await change_tiers.unknown_paths(conn, project.id, named, graph)
    return merge_gate.risk_of(
        repo,
        [path for _, path in named],
        risk_paths=project.policy["merge"]["risk_paths"],
        protected=queue.protected_of(project.charter),
        impacted=graph.impacted if graph is not None else (),
        unknown=unknown,
    )


async def _judge_next(conn: AsyncConnection, project, row, outcomes) -> None:
    """Settle ``row`` when its Judges have spoken, else queue its next judge run, or end it once JUDGE_ATTEMPTS runs
    ended without a verdict."""
    step = queue.next_round(row.verdict, row.risk)
    if step == "ready":
        high = (row.risk or {}).get("high")
        why = "the Judge and the second Judge passed it" if high else "the Judge passed it, and its risk is not high"
        await settle(conn, row, PASSED, why)
        outcomes["passed"] += 1
        return
    if step == "failed":
        await settle(conn, row, FAILED, "the Judges' verdict at this head did not pass it")
        outcomes["failed"] += 1
        return
    if row.judge_run_id is not None:
        r = tables.runs
        ended = (await conn.execute(select(r.c.state).where(r.c.id == row.judge_run_id))).scalar_one_or_none()
        if ended not in runs.TERMINAL_STATES:
            return
        attempts = row.judge_attempts + 1
        why = f"judge run #{row.judge_run_id} ended {ended} without a verdict"
        await set_row(conn, row.id, judge_run_id=None, judge_key=None, judge_attempts=attempts)
        row = await row_of(conn, row.id)
        if attempts >= judge.JUDGE_ATTEMPTS:
            await settle(conn, row, FAILED, f"{attempts} judge runs ended without a verdict; the last: {why}")
            outcomes["failed"] += 1
            return
    second = step == "second"
    run_id, model, chosen = await judging.queue_judge_run(
        conn,
        project,
        repo=row.repo,
        branch=row.source_branch,
        title=f"{'Second Judge' if second else 'Judge'} of pull request #{row.number} of {row.repo}: {row.title or ''}",
        builder_model=row.model,
        built=True,
        second=second,
    )
    if run_id is None:
        if row.reason != f"waits: {chosen}":
            await set_row(conn, row.id, reason=f"waits: {chosen}")
        outcomes["waiting"] += 1
        return
    await set_row(conn, row.id, judge_run_id=run_id, judge_key=None, reason=None)
    which = "the second Judge" if second else "the Judge"
    text = (
        f"Queued by the merge check of project {project.name} as {which} of pull request #{row.number} of {row.repo} "
        f"at {row.head_sha[:12]}, on Claude Code ({model or 'the default model'}: {chosen}). It merges the base into "
        "the head and runs the plan's verify and the project's hidden checks there, then reads the pull request and "
        "its diff, never the Builder's transcript."
    )
    await write_event(conn, run_id, {"text": text, "pull_request": row.number, "merge_check": row.id})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=project.owner_id,
        token_id=None,
        action=audit.MERGE_JUDGE,
        target=f"{project.name}/{row.repo} #{row.number} run:{run_id} {'second' if second else 'first'} model={model}",
        project_id=project.id,
    )
    outcomes["judging"] += 1
