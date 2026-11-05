"""The commands of a run's agent under ``evo-agents worker`` (``cli`` registers them): ``step``, ``ask``, ``wait``,
``notify`` and ``plan`` of a plan run's agent, ``finding`` and ``propose`` of a review run's, ``put`` of an author run's,
and the parsers of the other modules of a run's agent (``AGENT_PARSERS``). ``cli`` says what each one does.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evo_agents.hub import credential_requests, decision_asks, proofs, runs
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.worker import (
    backup_cli,
    chat_cli,
    cli_support,
    curator_cli,
    merge_check_cli,
    output_cli,
    playbook_cli,
    probe_cli,
    pull_heads,
    step_verify,
    triage_cli,
)
from evo_agents.worker.agent_input import (
    attest,
    decision_extras,
    decision_option,
    evidence_items,
    payload_contains,
    plan_file,
    probe_file,
    promotion_file,
    read_text,
    wait_until,
)
from evo_agents.worker.cli_support import worker_command
from evo_agents.worker.home import (
    WorkerConfig,
    WorkerHome,
    WorkerStateError,
)
from evo_agents.worker.run_kinds import kind_refusal, kinds_of

NO_RECOMMENDED = (
    "warning: no --recommended: the person may answer on a phone with one tap when you recommend an option "
    "(--recommended KEY) and say why (--recommended-reason TEXT). The decision is asked without one."
)


# The commands of a plan run's agent

RUN_VARIABLE = "EVO_RUN_ID"
AGENT_STEP_STATUSES = ("in_progress", "done", "pending")  # what POST /v1/worker/runs/{id}/steps/{key} takes
AGENT_NOTICE_KINDS = ("push_default_branch", "merge_default_branch")  # the hub sends plan_finished and run_failed
MAX_VERIFY_COMMANDS = 50
MAX_VERIFY_CHARS = 2000
MAX_EVIDENCE_CHARS = 16 * 1024  # a step report's evidence, as the hub takes it
# The parsers of a run's agent in group chats, of `repo open` of a chat run's, of a triage thread's for a failed run, of
# a review run's on the playbook, and of any run's outputs
AGENT_PARSERS = (chat_cli.register_chat, chat_cli.register_repo, triage_cli.register_triage)
AGENT_PARSERS += (playbook_cli.register_playbook, output_cli.register_output, backup_cli.register_backup)
AGENT_PARSERS += (probe_cli.register_probe,)  # a review run's probe of a merged change
AGENT_PARSERS += (merge_check_cli.register_merge_check,)  # a plan run's pull request judged at its head
AGENT_PARSERS += (curator_cli.register_curator,)  # a proposal's thread answers the proposal


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
    hubapi = cli_support.hubapi()
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


def run_workspaces(agent: _AgentRun) -> dict:
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
    if args.proof and args.status != "done":
        raise WorkerStateError(f"{proofs.FLAG} goes with done only")
    return verify


def _step_proofs(args, body: dict, step, worktree) -> list[dict] | None:
    """The proofs of ``worker step KEY done``, one for each criterion of the plan's acceptance the step names, each
    test found in the repo's worktree; None for a step that names none and was given none. WorkerStateError naming
    the criteria left without one, before anything is verified or pushed."""
    try:
        given = proofs.parse_all(args.proof or [])
    except proofs.ProofProblem as exc:
        raise WorkerStateError(str(exc)) from None
    wanted = proofs.criteria(body, step)
    if not wanted and not given:
        return None
    why = proofs.problem(wanted, given)
    if why is not None:
        raise WorkerStateError(f"step {args.key} is not done: {why}. Nothing was verified, committed or pushed")
    for item in given:
        missing = proofs.missing_test(item, worktree)
        if missing:
            raise WorkerStateError(f"the proof of {item['id']}: {missing}. Nothing was verified, committed or pushed")
    return proofs.stored(given)


def _step_workspaces(agent: _AgentRun, repo: str | None) -> dict:
    """The run's worktrees by repo, once ``--repo``, when given, names one of them."""
    workspaces = run_workspaces(agent)
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


def _named_repo(name: str | None, key: str, workspaces: dict) -> str:
    """The repo a done or verify report of step ``key`` is about; WorkerStateError when none is named."""
    if name is None:
        raise WorkerStateError(f"name the repo of step {key} with --repo: one of {', '.join(workspaces)}")
    return name


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
        wait = step_verify.verify_jobs.WAIT if args.wait is None else None if args.wait < 0 else args.wait
        if args.status == "verify":
            name = _named_repo(name, args.key, workspaces)
            return await step_verify.verify_step(agent, args.key, name, workspaces[name].worktree, verify, wait)
        report: dict = {"status": args.status}
        if name is not None:
            report["repo"] = name
        if args.evidence is not None:
            report["evidence"] = args.evidence
        pushed = None
        if args.status == "done":
            name = _named_repo(name, args.key, workspaces)
            given = _step_proofs(args, body, step, workspaces[name].worktree)
            if given is not None:
                report["proofs"] = given
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


def credential_request(args) -> dict | None:
    """The credential request of ``worker ask`` (``--env-var``, ``--secret-name``, ``--hub-account``), as the hub takes
    it; None when the agent asks none. Refused as the hub would refuse it, before it is asked."""
    env_var, secret_name = getattr(args, "env_var", None), getattr(args, "secret_name", None)
    hub_account = bool(getattr(args, "hub_account", False))
    if env_var is None and secret_name is None and not hub_account:
        return None
    if args.category != credential_requests.CATEGORY:
        raise WorkerStateError(
            f"--env-var, --secret-name and --hub-account ask for a credential: use --category "
            f"{credential_requests.CATEGORY}"
        )
    if args.option or args.wait_option:
        raise WorkerStateError(
            "a credential request takes the hub's options (paste the value, the owner's hub account, both, refuse): "
            "leave out --option and --wait-option"
        )
    refusal = credential_requests.request_refusal(env_var, secret_name, hub_account)
    if refusal is not None:
        raise WorkerStateError(refusal)
    request: dict = {"hub_account": hub_account}
    if env_var is not None:
        request |= {"env_var": env_var, "secret_name": secret_name or credential_requests.default_secret_name(env_var)}
    return request


def asked_approval(category: str, ref: str) -> dict:
    """The approval ``worker ask --pull`` binds its decision to: the pull request's head and scope as gh reads them in
    the run's directory (``pull_heads``)."""
    if category != "merge":
        raise WorkerStateError("--pull binds a merge approval to a pull request's head: use --category merge")
    bound = pull_heads.read(ref, None, Path.cwd())
    if bound is None:
        raise WorkerStateError(f"gh could not read pull request {ref} here: `gh pr view {ref}` says why")
    return bound


def _asked_options(args) -> list[dict]:
    """The options of ``worker ask`` that asks no credential: 2 to 6, each with a key of its own."""
    options = [decision_option(text) for text in args.option or []]
    keys = [option["key"] for option in options]
    fewest, most = runs.DECISION_OPTIONS
    if not fewest <= len(options) <= most or len(set(keys)) != len(keys):
        raise WorkerStateError(f"a decision offers {fewest} to {most} options, each with a key of its own")
    for key in args.wait_option or []:
        if key not in keys:
            raise WorkerStateError(f"--wait-option names one of the options: {', '.join(keys)}")
        options[keys.index(key)]["waits"] = True
    if all(option.get("waits") for option in options):
        raise WorkerStateError("a decision needs an option that answers it: --wait-option names every one")
    return options


@worker_command
def cmd_ask(args) -> int:
    agent = agent_run("ask", runs.RESUME_KINDS)
    request = credential_request(args)
    if request is None:
        options = _asked_options(args)
        keys = [option["key"] for option in options]
    else:  # the hub gives it these options
        options = []
        given = credential_requests.options(request.get("env_var"), request.get("secret_name"), request["hub_account"])
        keys = [option["key"] for option in given]
    if args.recommended is not None and args.recommended not in keys:
        raise WorkerStateError(f"--recommended names one of the options: {', '.join(keys)}")
    body: dict = {"category": args.category, "question": args.question, "options": options}
    if request is not None:
        body["credential_request"] = request
    context = read_text(args.context_file, "context", runs.MAX_DECISION_CONTEXT_BYTES) if args.context_file else None
    if context is not None:
        body["context"] = context
    if args.recommended is not None:
        body["recommended"] = args.recommended
    body.update(decision_extras(args.recommended, args.recommended_reason, args.reversible, args.undo, args.evidence))
    if args.step is not None:
        body["step_key"] = args.step
    if getattr(args, "pull", None) is not None:
        body["approval"] = asked_approval(args.category, args.pull)
    body.update(output_cli.attached(agent.home, agent.run_id, args.attach))  # the outputs the question carries
    if args.recommended is None:
        print(NO_RECOMMENDED, file=sys.stderr)

    async def work(hub) -> dict:
        return await hub.decision(agent.run_id, body)

    answer = with_hub(agent, work)
    decision_id = noted_decision(agent, answer, credential=request is not None)
    if request is not None and answer.get("state") == "answered":  # the hub found the owner's secret and answered
        print(f"Decision #{decision_id} was answered by the hub at once: {answer.get('answer_text') or ''}".rstrip())
        print(
            "The worker takes the run's leases again as the answer reaches the run: read them with "
            f"`{credential_requests.ENV_COMMAND}` before the command that needs them; nothing waits on the owner."
        )
        return 0
    print(f"Decision #{decision_id} is open: {' '.join(args.question.split())}")
    if request is not None:
        print(
            "Once the owner gives what it asks for, the worker takes the run's leases again: read them with "
            f"`{credential_requests.ENV_COMMAND}` before the command that needs them."
        )
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


def noted_decision(agent: _AgentRun, answer, *, credential: bool = False) -> int:
    """The id of the decision the hub answered with, noted in the run's file of decisions, which the daemon waits on;
    a credential request is noted as one, so the daemon takes the run's leases again once it is answered."""
    decision_id = answer.get("id") if isinstance(answer, dict) else None
    if not isinstance(decision_id, int):
        raise HubError("the hub did not answer with the decision's id")
    line = {"id": decision_id, "asked_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if credential:
        line[credential_requests.NOTED] = True
    noted = json.dumps(line)
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
    decision_id = noted_decision(agent, answer)
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
    workspaces = run_workspaces(agent)

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
    workspaces = run_workspaces(agent)

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


def _register_ask(wsub) -> None:
    """The parser of `evo-agents worker ask`."""
    ask = wsub.add_parser(
        "ask", help="inside a plan run or an author run: ask the run's owner a decision; prints its id"
    )
    ask.add_argument("--category", required=True, choices=runs.ASKED_DECISION_CATEGORIES, help="what kind of decision")
    ask.add_argument("--question", required=True, help="the question, in a sentence or two")
    ask.add_argument("--context-file", help="a markdown file of context for the owner, at most 16 KiB (- for stdin)")
    ask.add_argument(
        "--option",
        action="append",
        metavar="KEY=LABEL[:DESCRIPTION]",
        help="an option; repeat it, 2 to 6 times; none for a credential request, which takes the hub's",
    )
    ask.add_argument(
        "--env-var",
        metavar="NAME",
        help="with --category credential: the variable of your environment the run needs, such as an API key; the "
        "owner pastes its value, which you then read with `evo-agents worker env --run N`",
    )
    ask.add_argument(
        "--secret-name",
        metavar="NAME",
        help="with --env-var: the name of the owner's secret that keeps it (default: the variable in lower case, - "
        "for _)",
    )
    ask.add_argument(
        "--hub-account",
        action="store_true",
        help="with --category credential: the owner's account on the hub, so `evo-agents hub ...` runs as them",
    )
    ask.add_argument("--recommended", metavar="KEY", help="the key of the option you recommend")
    ask.add_argument(
        "--recommended-reason",
        metavar="TEXT",
        help=f"why you recommend that option, at most {decision_asks.MAX_REASON_CHARS} characters; needs --recommended",
    )
    ask.add_argument(
        "--reversible",
        choices=("yes", "no"),
        help="for an act: whether it can be undone (--undo says yes)",
    )
    ask.add_argument(
        "--undo", metavar="TEXT", help=f"how to undo the act, at most {decision_asks.MAX_UNDO_CHARS} characters"
    )
    ask.add_argument(
        "--evidence",
        action="append",
        metavar="REF",
        help=f"what the question rests on, repeat it (at most {decision_asks.MAX_EVIDENCE}): {decision_asks.FORMS}",
    )
    ask.add_argument(
        "--wait-option",
        action="append",
        metavar="KEY",
        help="an option that says not yet: choosing it leaves the decision open and the run waiting, and you hear "
        "nothing; repeat it for another",
    )
    ask.add_argument("--step", help="the step of the run's plan it is about (an author run: once it put the plan)")
    ask.add_argument(
        "--pull",
        metavar="REF",
        help="with --category merge: the pull request to merge (number, URL or branch); the answer holds for its head "
        "and scope now alone, and a push that changes them asks again",
    )
    output_cli.attach_option(ask, "the question")
    ask.set_defaults(func=cmd_ask)


def _register_step(wsub) -> None:
    """The parser of `evo-agents worker step`."""
    step = wsub.add_parser(
        "step", help="inside a plan run: report a step of the plan; done runs its verify commands again and pushes"
    )
    step.add_argument("key", help="the step's id, or its order")
    step.add_argument(
        "status",
        choices=(*AGENT_STEP_STATUSES, "verify"),
        help="in_progress, done, pending to hand it back, or verify to run the verify as a job of the worker alone",
    )
    step.add_argument("--repo", help="the run's repo the step is in (default: the plan's repo of the step)")
    step.add_argument("--evidence", help="what was done and how it was checked; a line per decision taken")
    step.add_argument(
        proofs.FLAG,
        action="append",
        metavar="ID=VALUE",
        help="with done: the proof of a criterion of the plan's acceptance the step names (a4=path::test_name, "
        "a4=COMMAND or a4=n/a: why); repeat it, once for each; done is refused without one for each",
    )
    step.add_argument(
        "--verify",
        action="append",
        metavar="COMMAND",
        help="a command that checks the step, run in the repo's worktree; repeat it; done and verify need one",
    )
    step.add_argument(
        "--wait",
        type=float,
        metavar="SECONDS",
        help=f"how long done and verify wait on the verify (default {step_verify.verify_jobs.WAIT:g}; -1: until it "
        f"ends); it goes on after, and they exit {step_verify.verify_jobs.EXIT_RUNNING}",
    )
    step.set_defaults(func=cmd_step)


def _register_review(wsub) -> None:
    """``worker finding`` and ``worker propose``, which the agent of a review run runs; outside one they refuse."""
    from evo_agents.hub.review import LENSES, SEVERITIES
    from evo_agents.hub.tiers import CHANGE_KINDS

    finding = wsub.add_parser("finding", help="inside a review run: record a finding with its evidence; prints its id")
    finding.add_argument("--lens", required=True, choices=list(LENSES), help="the lens it was found through")
    finding.add_argument("--title", required=True, help="what was found, in one line")
    finding.add_argument(
        "--evidence",
        action="append",
        required=True,
        metavar="SPEC",
        help="session:ID[:FIELD:INDEX], transcript:ID[:FILE]:LINE, run:ID:SEQ or code:REPO:PATH[:LINE]; repeat it",
    )
    finding.add_argument("--severity", choices=SEVERITIES, default="medium", help="low, medium (default) or high")
    finding.add_argument("--body-file", help="markdown that explains it, at most 16 KiB (- for stdin)")
    finding.add_argument(
        "--fix-of",
        type=int,
        metavar="CHANGE",
        help="the change the prompt asked you to fix: why you propose no fix of its rule; the hub adds the probe "
        "results",
    )
    finding.set_defaults(func=cmd_finding)

    propose = wsub.add_parser(
        "propose", help="inside a review run: propose a change with its draft plan; prints its id, tier and state"
    )
    propose.add_argument("--lens", required=True, choices=list(LENSES), help="the lens it comes from")
    propose.add_argument("--kind", required=True, choices=list(CHANGE_KINDS), help="the kind of change")
    propose.add_argument("--title", required=True, help="the change, in one line")
    draft = propose.add_mutually_exclusive_group(required=True)
    draft.add_argument("--plan-file", help="the draft plan, YAML or JSON, in outcome steps")
    draft.add_argument("--promote-file", help="kind memory: the promotion of agents' memories, YAML or JSON")
    propose.add_argument("--path", action="append", metavar="REPO:PATH", help="a file it would edit; repeat it")
    propose.add_argument("--finding", action="append", type=int, metavar="ID", help="a finding it rests on; repeat it")
    propose.add_argument(
        "--evidence", action="append", metavar="SPEC", help="evidence of its own, as finding takes it; repeat it"
    )
    propose.add_argument("--summary-file", help="markdown: the problem, the change, what waiting costs (- for stdin)")
    propose.add_argument(
        "--probe-file",
        help="the probe, YAML or JSON: repo, commit, task and verify; required when the evidence cites a correction "
        "and a path is an AGENTS.md, a CLAUDE.md, a skill or the repo's .claude settings or hooks",
    )
    propose.add_argument(
        "--fix-of",
        type=int,
        metavar="CHANGE",
        help="the change the prompt asked you to fix, whose probe recurred two weeks in a row; at least tier 1, and "
        "the hub adds the probe results as evidence",
    )
    propose.set_defaults(func=cmd_propose)


def _register_wait(wsub) -> None:
    """The parser of `evo-agents worker wait`."""
    wait = wsub.add_parser(
        "wait",
        help="inside a plan run: wait for a time or an event of the hub rather than ask the owner when it came; "
        "prints the wait's id",
        description="Wait for a time or an event of the hub rather than ask the owner when it came. A step of another "
        "plan done: --on plan.step --contains plan=northwind-launch --contains step=8 --contains status=done --until "
        "+12h. A GitHub Actions run (the CI of a tag, a deploy) that completes or has a job fail: --on "
        "github.workflow_run --contains repo=OWNER/NAME --contains run_id=ID --until +90m, ID the databaseId of `gh run "
        "list`; the hub reads that run every minute and wakes on its first failed job, or, with --contains "
        "status=completed, on its end alone.",
    )
    wait.add_argument("--reason", required=True, help="what the run waits for and why, for the owner to read")
    wait.add_argument(
        "--until",
        metavar="TIME",
        help=f"wake at this time at the latest: ISO 8601 with a timezone (2026-10-30T09:00Z) or a span (+6h, +90m, "
        f"+5d), at most {runs.MAX_WAIT_DAYS} days ahead; without it, {runs.MAX_WAIT_DAYS} days",
    )
    wait.add_argument(
        "--on",
        metavar="KIND",
        help="wake sooner on an event of the hub of this kind: plan.step (a step's status of a plan set or changed), "
        "github.workflow_run (a GitHub Actions run that completed or had a job fail), tasks.status, canvas.baseline, "
        "canvas.claim, signal.posted, srs.version.approved, or plugin.<plugin>.<name>",
    )
    wait.add_argument(
        "--contains",
        action="append",
        metavar="KEY=VALUE",
        help="the event's payload has KEY equal to VALUE (read as JSON when it is), such as plan=SLUG, step=8 and "
        "status=done for plan.step, or repo=OWNER/NAME, run_id=ID and status=completed, failed=true or attempt=2 for "
        "github.workflow_run; repeat it",
    )
    wait.add_argument("--step", help="the step of the run's plan it is about")
    wait.set_defaults(func=cmd_wait)


def _register_notify(wsub) -> None:
    """The parser of `evo-agents worker notify`."""
    notify = wsub.add_parser(
        "notify", help="inside a plan run: tell the run's owner of a push or merge into a default branch"
    )
    notify.add_argument("--kind", required=True, choices=AGENT_NOTICE_KINDS)
    notify.add_argument("--title", required=True, help="one line")
    notify.add_argument("--body", help="what happened, at most 16 KiB")
    notify.add_argument("--repo", help="the run's repo")
    notify.add_argument("--branch", help="the branch pushed or merged into")
    notify.add_argument("--commit", action="append", metavar="SHA", help="a commit pushed or merged; repeat it")
    notify.set_defaults(func=cmd_notify)


def _register_plan(wsub) -> None:
    """The parser of `evo-agents worker plan`."""
    plan = wsub.add_parser(
        "plan", help="inside a plan run or an author run: print the run's plan as the hub holds it now"
    )
    from evo_agents.hub.plan_cli import PLAN_KEYS

    json_option(plan, returns_object(*PLAN_KEYS, schema="Plan"), help="the hub's answer as JSON")  # seam hub-cli-v1
    plan.set_defaults(func=cmd_plan)


def _register_put(wsub) -> None:
    """The parser of `evo-agents worker put`."""
    from evo_agents.hub.plan_cli import WRITTEN

    put = wsub.add_parser(
        "put",
        help="inside an author run: put the plan you wrote on the hub, as the member who asked for it; replacing it "
        "needs --if-revision",
    )
    put.add_argument("file", metavar="FILE", help="the plan, YAML or JSON, as plan.schema.json has it (- for stdin)")
    put.add_argument(
        "--if-revision",
        type=int,
        metavar="REVISION",
        help="the revision on the hub this replaces, as `evo-agents worker plan --json` read it; leave it out to "
        "create the plan",
    )
    json_option(put, WRITTEN, help="the hub's answer as JSON")  # seam hub-cli-v1
    put.set_defaults(func=cmd_put)


def register_agent(wsub) -> None:
    """The parsers of the commands of a run's agent: step, ask, wait, notify, finding, propose, plan and put."""
    # The agent of a plan run runs these, and of an author run ask and plan; outside one they refuse.
    for part in (_register_step, _register_ask, _register_wait, _register_notify, _register_review):
        part(wsub)
    _register_plan(wsub)
    _register_put(wsub)  # the agent of an author run runs this; outside one it refuses
