"""The commands of a run's agent: ``worker step``, ``ask``, ``wait``, ``notify``, ``plan``, ``finding``, ``propose`` and
``put`` (``evo_agents.worker.cli`` holds their parsers and re-exports them)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from evo_agents.hub import runs
from evo_agents.hub.client import HubError
from evo_agents.worker import output_cli, step_verify
from evo_agents.worker.agent_input import (
    attest,
    decision_option,
    evidence_items,
    payload_contains,
    plan_file,
    probe_file,
    promotion_file,
    read_text,
    wait_until,
)
from evo_agents.worker.command import load_hubapi, worker_command
from evo_agents.worker.home import WorkerConfig, WorkerHome, WorkerStateError
from evo_agents.worker.run_kinds import kind_refusal, kinds_of

# The commands of a plan run's agent

RUN_VARIABLE = "EVO_RUN_ID"
AGENT_STEP_STATUSES = ("in_progress", "done", "pending")  # what POST /v1/worker/runs/{id}/steps/{key} takes
AGENT_NOTICE_KINDS = ("push_default_branch", "merge_default_branch")  # the hub sends plan_finished and run_failed
MAX_VERIFY_COMMANDS = 50
MAX_VERIFY_CHARS = 2000
MAX_EVIDENCE_CHARS = 16 * 1024  # a step report's evidence, as the hub takes it


@dataclass
class _AgentRun:
    """The plan run an agent's command acts for: the worker's state, its token and the run's record."""

    run_id: int
    home: WorkerHome
    config: WorkerConfig
    token: str
    record: dict


def agent_run(command: str, kinds: tuple[str, ...] = ("plan",)) -> _AgentRun:
    """The run of EVO_RUN_ID on this worker, of one of ``kinds``; WorkerStateError outside one."""
    raw = (os.environ.get(RUN_VARIABLE) or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        raise WorkerStateError(
            f"`evo-agents worker {command}` works only inside {kinds_of(kinds)} of this worker: {RUN_VARIABLE} is "
            "not set. The worker sets it, with EVO_RUN_KIND and EVO_WORKER_HOME, for the agent of each run."
        )
    run_id = int(raw)
    home = WorkerHome()
    config = home.load_config()
    token = home.load_token()
    record = home.load_run(run_id)
    if record is None or record.get("finished_at"):
        raise WorkerStateError(
            f"run {run_id} is not running on this worker (its state is under {home.root}): "
            f"`evo-agents worker {command}` works only inside it"
        )
    kind = record.get("kind") or "step"
    if kind not in kinds:
        raise WorkerStateError(kind_refusal(run_id, kind, kinds, command))
    return _AgentRun(run_id, home, config, token, record)


def with_hub(agent: _AgentRun, work):
    """``await work(hub)`` with the worker's client of the hub; the hub's refusal, or no answer, is a HubError."""
    hubapi = load_hubapi()
    from evo_agents.worker import gitops

    async def go():
        async with hubapi.new_session() as session:
            return await work(hubapi.WorkerHub(agent.config.url, agent.token, session))

    try:
        return asyncio.run(go())
    except hubapi.HubProblem as exc:
        raise exc.hub_error() from None
    except gitops.GitError as exc:
        raise WorkerStateError(str(exc)) from None


def _workspaces(agent: _AgentRun) -> dict:
    from evo_agents.worker import gitops

    found = {}
    for item in agent.record.get("repos") or []:
        try:
            workspace = gitops.Workspace.from_record(item)
        except ValueError:
            continue
        found[workspace.repo] = workspace
    return found


def _plan_step(body: dict, key: str) -> dict | None:
    """The step of ``body`` whose id is ``key`` (ids compare as text), or whose order it is."""
    steps = [step for step in body.get("steps") or [] if isinstance(step, dict)]
    for step in steps:
        if str(step.get("id")) == key:
            return step
    if key.isdigit() and 1 <= int(key) <= len(steps):
        return steps[int(key) - 1]
    return None


def _step_repo(body: dict, step: dict | None, workspaces: dict) -> str | None:
    """The repo a step is in: the plan's repo of the step, or the run's only repo."""
    named = step.get("repo") if isinstance(step, dict) else None
    if isinstance(named, str) and named in workspaces:
        return named
    repos = [entry.get("repo") for entry in body.get("repos") or [] if isinstance(entry, dict)]
    if not isinstance(named, str) and len(repos) == 1 and repos[0] in workspaces:
        return repos[0]
    return next(iter(workspaces)) if len(workspaces) == 1 else None


def _step_verify_commands(args) -> list[str]:
    """The verify commands of ``worker step``, once the status and the commands agree."""
    verify = list(args.verify or [])
    if args.status in ("done", "verify") and not verify:
        raise WorkerStateError(
            f"{'a step is done' if args.status == 'done' else 'verify runs'} only with the commands that check it: "
            "give --verify COMMAND once for each; the worker runs them in the repo's worktree"
        )
    if args.status not in ("done", "verify") and (verify or args.wait is not None):
        raise WorkerStateError("--verify and --wait go with done and verify only")
    if len(verify) > MAX_VERIFY_COMMANDS or any(not c.strip() or len(c) > MAX_VERIFY_CHARS for c in verify):
        raise WorkerStateError(
            f"give 1 to {MAX_VERIFY_COMMANDS} verify commands, each 1 to {MAX_VERIFY_CHARS} characters"
        )
    if args.evidence is not None and (not args.evidence.strip() or len(args.evidence) > MAX_EVIDENCE_CHARS):
        raise WorkerStateError(f"--evidence takes 1 to {MAX_EVIDENCE_CHARS} characters")
    return verify


def _step_workspaces(agent: _AgentRun, repo: str | None) -> dict:
    """The run's worktrees by repo, once ``--repo``, when given, names one of them."""
    workspaces = _workspaces(agent)
    if repo is not None and repo not in workspaces:
        raise WorkerStateError(f"run {agent.run_id} works in {', '.join(workspaces) or 'no repo'}, not in {repo}")
    return workspaces


def _refuse_done_after_conflict(agent: _AgentRun, status: str) -> None:
    """A run whose push went to its side branch reports nothing done any more."""
    conflict = agent.home.load_conflict(agent.run_id)
    if status == "done" and conflict is not None:
        raise WorkerStateError(
            f"run {agent.run_id} ends failed (push_conflict) once this turn ends: {conflict['error']}. Nothing more is "
            "pushed or reported done; end your turn now"
        )


async def _notify_push(hub, agent: _AgentRun, name: str, pushed) -> None:
    """Tell the run's owner of a change pushed to a default branch; the push happened, so a failed notice only warns."""
    from evo_agents.worker import gitops
    from evo_agents.worker.hubapi import HubProblem

    if not (pushed.default and pushed.changed):
        return
    try:
        await hub.notice(agent.run_id, gitops.push_notice(agent.run_id, name, pushed))
        print(f"Notified the run's owner of the push to {pushed.branch}, a default branch.")
    except HubProblem as exc:
        print(
            f"warning: the notice of the push to {pushed.branch} was not sent ({exc}); send it with "
            "`evo-agents worker notify --kind push_default_branch`",
            file=sys.stderr,
        )


@worker_command
def cmd_step(args) -> int:
    agent = agent_run("step")
    verify = _step_verify_commands(args)
    output_cli.request_scan(agent.home, agent.run_id)  # the run's .evo-run/outputs/ goes to the hub with each report
    workspaces = _step_workspaces(agent, args.repo)
    _refuse_done_after_conflict(agent, args.status)

    async def work(hub) -> int:
        body = (await hub.plan(agent.run_id)).get("body") or {}
        step = _plan_step(body, args.key)
        name = args.repo or _step_repo(body, step, workspaces)
        if name is None and args.status in ("done", "verify"):
            raise WorkerStateError(f"name the repo of step {args.key} with --repo: one of {', '.join(workspaces)}")
        wait = step_verify.verify_jobs.WAIT if args.wait is None else None if args.wait < 0 else args.wait
        if args.status == "verify":
            return await step_verify.verify_step(agent, args.key, name, workspaces[name].worktree, verify, wait)
        report: dict = {"status": args.status}
        if name is not None:
            report["repo"] = name
        if args.evidence is not None:
            report["evidence"] = args.evidence
        pushed = None
        if args.status == "done":
            done = await step_verify.done_step(hub, agent, args.key, step, body, name, workspaces[name], verify, wait)
            if isinstance(done, int):
                return done
            pushed, report["verify"], line = done
            await _notify_push(hub, agent, name, pushed)
            report["commit_sha"] = pushed.head
            report["evidence"] = step_verify.with_line(args.evidence, line, MAX_EVIDENCE_CHARS)
        answer = await hub.step(agent.run_id, args.key, report)
        where = f" ({name}@{pushed.head[:12]}, pushed to {pushed.branch})" if pushed is not None else ""
        kept = "" if answer.get("written", True) else ", as the plan had it already"
        print(f"Step {args.key} of plan {answer.get('plan_id')}: {answer.get('status')}{where}{kept}.")
        return 0

    return with_hub(agent, work)


@worker_command
def cmd_ask(args) -> int:
    agent = agent_run("ask", runs.RESUME_KINDS)
    options = [decision_option(text) for text in args.option]
    keys = [option["key"] for option in options]
    fewest, most = runs.DECISION_OPTIONS
    if not fewest <= len(options) <= most or len(set(keys)) != len(keys):
        raise WorkerStateError(f"a decision offers {fewest} to {most} options, each with a key of its own")
    if args.recommended is not None and args.recommended not in keys:
        raise WorkerStateError(f"--recommended names one of the options: {', '.join(keys)}")
    for key in args.wait_option or []:
        if key not in keys:
            raise WorkerStateError(f"--wait-option names one of the options: {', '.join(keys)}")
        options[keys.index(key)]["waits"] = True
    if all(option.get("waits") for option in options):
        raise WorkerStateError("a decision needs an option that answers it: --wait-option names every one")
    body: dict = {"category": args.category, "question": args.question, "options": options}
    context = read_text(args.context_file, "context", runs.MAX_DECISION_CONTEXT_BYTES) if args.context_file else None
    if context is not None:
        body["context"] = context
    if args.recommended is not None:
        body["recommended"] = args.recommended
    if args.step is not None:
        body["step_key"] = args.step
    body.update(output_cli.attached(agent.home, agent.run_id, args.attach))  # the outputs the question carries

    async def work(hub) -> dict:
        return await hub.decision(agent.run_id, body)

    decision_id = _noted_decision(agent, with_hub(agent, work))
    print(f"Decision #{decision_id} is open: {' '.join(args.question.split())}")
    if agent.record.get("kind") == "author":
        print(
            "Ask the other decisions of this turn now, then end your turn with your message to the member in the chat: "
            "what you found, what you decided yourself and the decisions you asked. The run waits until the member "
            "answered each one or replied in the chat, and your next turn in this session takes the answers, each as a "
            f"message naming its decision, #{decision_id} among them."
        )
        return 0
    print(
        "Go on with the work that does not depend on the answer. When none is left, end your turn: the run waits "
        f"for its owner, and the answer comes back in this session as a message naming decision #{decision_id}."
    )
    return 0


def _noted_decision(agent: _AgentRun, answer) -> int:
    """The id of the decision the hub answered with, noted in the run's file of decisions, which the daemon waits on."""
    decision_id = answer.get("id") if isinstance(answer, dict) else None
    if not isinstance(decision_id, int):
        raise HubError("the hub did not answer with the decision's id")
    noted = json.dumps({"id": decision_id, "asked_at": datetime.now(UTC).isoformat(timespec="seconds")})
    with open(agent.home.decisions_path(agent.run_id), "a", encoding="utf-8") as handle:
        handle.write(noted + "\n")
    return decision_id


@worker_command
def cmd_wait(args) -> int:
    agent = agent_run("wait")
    if args.until is None and args.on is None:
        raise WorkerStateError("a wait wakes at a time (--until), on an event of the hub (--on), or both")
    if args.contains and args.on is None:
        raise WorkerStateError("--contains filters the event of --on: name its kind with --on")
    body: dict = {"reason": args.reason}
    if args.until is not None:
        body["until"] = wait_until(args.until, datetime.now(UTC))
    if args.on is not None:
        body["on"] = args.on
    if args.contains:
        body["contains"] = payload_contains(args.contains)
    if args.step is not None:
        body["step_key"] = args.step

    async def work(hub) -> dict:
        return await hub.wait(agent.run_id, body)

    answer = with_hub(agent, work)
    decision_id = _noted_decision(agent, answer)
    wake_at = str(answer.get("wake_at") or "")[:16].replace("T", " ")
    sooner = f", or sooner on an event {args.on} of the project" if args.on is not None else ""
    print(
        f"Wait #{decision_id} is open: the run wakes at {wake_at} UTC at the latest{sooner}. End your turn once no "
        "work that does not depend on it is left: the run waits without the agent, the time does not count toward its "
        f"timeout, and this session goes on with a message naming decision #{decision_id} when it wakes."
    )
    return 0


@worker_command
def cmd_notify(args) -> int:
    agent = agent_run("notify")
    body: dict = {"kind": args.kind, "title": args.title}
    for name in ("body", "repo", "branch"):
        if getattr(args, name) is not None:
            body[name] = getattr(args, name)
    if args.commit:
        body["commits"] = list(dict.fromkeys(args.commit))

    async def work(hub) -> dict:
        return await hub.notice(agent.run_id, body)

    answer = with_hub(agent, work)
    print(f"Notice #{answer.get('id')} sent to the owner of run #{agent.run_id}: {args.title}")
    return 0


@worker_command
def cmd_plan(args) -> int:
    agent = agent_run("plan", ("plan", "author"))

    async def work(hub) -> dict:
        return await hub.plan(agent.run_id)

    view = with_hub(agent, work)
    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0
    import yaml

    print(
        f"# The plan {view.get('plan_id')} of project {view.get('project')} at revision {view.get('revision')}, as "
        "the hub holds it now."
    )
    print(yaml.safe_dump(view.get("body") or {}, allow_unicode=True, sort_keys=False, width=120), end="")
    return 0


# The commands of a review run's agent


@worker_command
def cmd_finding(args) -> int:
    from evo_agents.hub.review import MAX_BODY_BYTES

    agent = agent_run("finding", ("review",))
    body: dict = {"lens": args.lens, "severity": args.severity, "title": " ".join(args.title.split())}
    if args.body_file:
        text = read_text(args.body_file, "body", MAX_BODY_BYTES)
        if text is not None:
            body["body"] = text
    body["evidence"] = evidence_items(args.evidence)
    if args.fix_of is not None:
        body["fix_of"] = args.fix_of
    workspaces = _workspaces(agent)

    async def work(hub) -> dict:
        await attest(body["evidence"], workspaces)
        return await hub.finding(agent.run_id, body)

    answer = with_hub(agent, work)
    print(f"Finding #{answer.get('id')} recorded ({args.lens}, {args.severity}): {body['title']}")
    return 0


@worker_command
def cmd_propose(args) -> int:
    from evo_agents.hub.review import MAX_BODY_BYTES
    from evo_agents.hub.tiers import split_target

    agent = agent_run("propose", ("review",))
    paths = []
    for text in args.path or []:
        found = split_target(text)
        if found is None:
            raise WorkerStateError(f"--path takes REPO:PATH, a path within a repo, not {text!r}")
        paths.append({"repo": found[0], "path": found[1]})
    evidence = evidence_items(args.evidence or [])
    if not evidence and not args.finding and args.fix_of is None:
        raise WorkerStateError(
            "a proposal rests on --finding ID, --evidence SPEC, or both: give at least one (a fix proposal, "
            "--fix-of CHANGE, rests on the probe results the hub adds)"
        )
    body: dict = {
        "lens": args.lens,
        "kind": args.kind,
        "title": " ".join(args.title.split()),
        "paths": paths,
        "finding_ids": list(dict.fromkeys(args.finding or [])),
        "evidence": evidence,
        **({"plan": plan_file(args.plan_file)} if args.plan_file else {"promotion": promotion_file(args.promote_file)}),
    }
    if args.summary_file:
        text = read_text(args.summary_file, "summary", MAX_BODY_BYTES)
        if text is not None:
            body["summary"] = text
    if args.probe_file:
        body["probe"] = probe_file(args.probe_file)
    if args.fix_of is not None:
        body["fix_of"] = args.fix_of
    workspaces = _workspaces(agent)

    async def work(hub) -> dict:
        await attest(body["evidence"], workspaces)
        return await hub.proposal(agent.run_id, body)

    answer = with_hub(agent, work)
    if answer.get("state") == "dropped":
        print(
            f"Proposal #{answer.get('id')} was dropped: it repeats proposal #{answer.get('duplicate_of')}, which the "
            "owner rejected lately, and its evidence is not twice as large."
        )
        return 0
    print(f"Proposal #{answer.get('id')} recorded: tier {answer.get('tier')}, {answer.get('state')}.")
    for reason in answer.get("tier_reasons") or []:
        print(f"  {reason}")
    return 0


# The command of an author run's agent


@worker_command
def cmd_put(args) -> int:
    import yaml

    from evo_agents.hub.plans import MAX_PLAN_BYTES

    agent = agent_run("put", ("author",))
    text = read_text(args.file, "plan", 4 * MAX_PLAN_BYTES) or ""
    try:
        plan = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkerStateError(f"{args.file} is neither YAML nor JSON: {exc}") from None
    if not isinstance(plan, dict):
        raise WorkerStateError(f"{args.file} holds no plan: a plan is a mapping with id, goal and steps")
    request: dict = {"body": json.loads(json.dumps(plan, default=str))}  # dates as text, as the hub keeps JSON
    if args.if_revision is not None:
        request["if_revision"] = args.if_revision

    async def work(hub) -> dict:
        return await hub.put_plan(agent.run_id, request)

    answer = with_hub(agent, work)
    if args.json:
        print(json.dumps(answer, ensure_ascii=False, indent=2))
        return 0
    plan_text = f"plan {answer.get('plan_id')} of project {answer.get('project')}"
    if answer.get("created"):
        print(f"Created {plan_text} on the hub at revision {answer.get('revision')}.")
    elif answer.get("changed"):
        print(f"Revised {plan_text} on the hub: revision {answer.get('revision')}.")
    else:
        print(f"The hub already holds this as {plan_text}, revision {answer.get('revision')}; nothing changed.")
    for warning in answer.get("warnings") or []:
        print(f"warning: {warning.get('path') or '<root>'}: {warning.get('message')}")
    print(f"Pass --if-revision {answer.get('revision')} to put it again.")
    return 0
