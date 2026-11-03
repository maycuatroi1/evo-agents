"""``evo-agents hub curator finding|propose``: what a member's own session finds and proposes, recorded on the hub as
a review run's agent records it (``worker finding|propose``), in the session's local run (the curator skill).

The session is the Claude Code session the command runs in: ``--session``, else the one of CLAUDE_CODE_SESSION_ID,
else the newest session of the working directory (``local_sync.live_session``); ``--run`` names its local run
directly. The bodies are those of ``worker finding|propose``, read from the same flags and files
(``evo_agents.worker.agent_input``); code evidence is checked against this machine's checkout of its repo when there is
one (``kg.evaluate.project_checkouts``), which adds the commit it is at. POST /v1/projects/{p}/curator/findings and
.../curator/proposals take them (``evo_agents.hub.server.proposals``): the hub resolves the evidence, gives a proposal
its tier, its ledger line and, with the charter's ``answer_by`` agent, its thread, as for the night's review run. A
fix proposal (``--fix-of``) is the night's alone. Standard library and PyYAML only, like the client.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from evo_agents.hub.cli_client import _client_command, _print_json, _project_path, _signed_in
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.plan_cli import _project
from evo_agents.hub.review import LENSES, MAX_BODY_BYTES, SEVERITIES
from evo_agents.hub.tiers import CHANGE_KINDS, split_target

FINDING_KEYS = ("id", "project", "run_id", "lens", "severity", "title", "body", "evidence", "created_at", "fix_of")


def session_run(hub, project: str, session: str | None, run: int | None) -> int:
    """The local run of this machine's session of ``project`` the command runs in, or ``run`` when given."""
    if run is not None:
        return run
    from evo_agents.hub import local_sync

    env = dict(os.environ)
    if session:
        env[local_sync.SESSION_VARIABLE] = session
    found = local_sync.live_session(hub.url, project, env)
    run_id = found[1].get("run_id") if found else None
    if not isinstance(run_id, int):
        raise HubError(
            f"no live Claude Code session of project {project} here: run this inside the session after its first "
            "message (Claude Code names it in CLAUDE_CODE_SESSION_ID), or pass --session ID or --run ID"
        )
    return run_id


def _input(read):
    """``read()`` from ``agent_input``, its WorkerStateError as the HubError this command prints."""
    from evo_agents.worker.home import WorkerStateError

    try:
        return read()
    except WorkerStateError as exc:
        raise HubError(str(exc)) from None


def _attest(project: str, evidence: list[dict]) -> None:
    """Check each piece of code evidence against this machine's checkout of its repo, when it has one."""
    if not any(item["kind"] == "code" for item in evidence):
        return
    from evo_agents.kg.evaluate import project_checkouts
    from evo_agents.worker.agent_input import attest

    checkouts = project_checkouts(project)
    named = {item["repo"] for item in evidence if item["kind"] == "code"}
    if not named <= set(checkouts):
        return  # the hub resolves the evidence of a repo with no checkout here against the repo and its graph
    workspaces = {name: SimpleNamespace(worktree=path) for name, path in checkouts.items()}
    _input(lambda: asyncio.run(attest(evidence, workspaces)))


@_client_command
def cmd_finding(args) -> int:
    from evo_agents.worker.agent_input import evidence_items, read_text

    hub, _ = _signed_in()
    project = _project(args)
    body: dict = {"lens": args.lens, "severity": args.severity, "title": " ".join(args.title.split())}
    if args.body_file:
        text = _input(lambda: read_text(args.body_file, "body", MAX_BODY_BYTES))
        if text is not None:
            body["body"] = text
    body["evidence"] = _input(lambda: evidence_items(args.evidence))
    _attest(project, body["evidence"])
    body["run_id"] = session_run(hub, project, args.session, args.run)
    answer = hub.call("POST", f"{_project_path(project)}/curator/findings", body)
    if args.json:
        _print_json(answer)
        return 0
    print(
        f"Finding #{answer.get('id')} recorded in run #{answer.get('run_id')} ({args.lens}, {args.severity}): "
        f"{body['title']}"
    )
    return 0


@_client_command
def cmd_propose(args) -> int:
    from evo_agents.worker.agent_input import evidence_items, plan_file, probe_file, promotion_file, read_text

    hub, _ = _signed_in()
    project = _project(args)
    paths = []
    for text in args.path or []:
        found = split_target(text)
        if found is None:
            raise HubError(f"--path takes REPO:PATH, a path within a repo, not {text!r}")
        paths.append({"repo": found[0], "path": found[1]})
    evidence = _input(lambda: evidence_items(args.evidence or []))
    if not evidence and not args.finding:
        raise HubError("a proposal rests on --finding ID, --evidence SPEC, or both: give at least one")
    body: dict = {
        "lens": args.lens,
        "kind": args.kind,
        "title": " ".join(args.title.split()),
        "paths": paths,
        "finding_ids": list(dict.fromkeys(args.finding or [])),
        "evidence": evidence,
    }
    if args.plan_file:
        body["plan"] = _input(lambda: plan_file(args.plan_file))
    else:
        body["promotion"] = _input(lambda: promotion_file(args.promote_file))
    if args.summary_file:
        text = _input(lambda: read_text(args.summary_file, "summary", MAX_BODY_BYTES))
        if text is not None:
            body["summary"] = text
    if args.probe_file:
        body["probe"] = _input(lambda: probe_file(args.probe_file))
    _attest(project, evidence)
    body["run_id"] = session_run(hub, project, args.session, args.run)
    answer = hub.call("POST", f"{_project_path(project)}/curator/proposals", body)
    if args.json:
        _print_json(answer)
        return 0
    if answer.get("state") == "dropped":
        print(
            f"Proposal #{answer.get('id')} was dropped: it repeats proposal #{answer.get('duplicate_of')}, which was "
            "rejected lately, and its evidence is not twice as large."
        )
        return 0
    print(
        f"Proposal #{answer.get('id')} recorded in run #{answer.get('run_id')}: tier {answer.get('tier')}, "
        f"{answer.get('state')}."
    )
    for reason in answer.get("tier_reasons") or []:
        print(f"  {reason}")
    return 0


def _session_options(parser) -> None:
    parser.add_argument("--session", metavar="ID", help="the Claude Code session (default: CLAUDE_CODE_SESSION_ID)")
    parser.add_argument("--run", type=int, metavar="ID", help="the session's local run, in place of finding it")


def register_session(csub, with_project) -> None:
    from evo_agents.hub.curator_cli import PROPOSAL_KEYS

    finding = csub.add_parser(
        "finding", help="record a finding of your own session, with its evidence, as a review run does"
    )
    with_project(finding)
    finding.add_argument("--lens", required=True, choices=list(LENSES), help="the lens it was found through")
    finding.add_argument("--title", required=True, help="what was found, in one line")
    finding.add_argument(
        "--evidence",
        action="append",
        required=True,
        metavar="SPEC",
        help="session:ID[:FIELD:INDEX], transcript:ID[:FILE]:LINE, run:ID:SEQ, code:REPO:PATH[:LINE] or "
        "memory:ID:REV; repeat it",
    )
    finding.add_argument("--severity", choices=SEVERITIES, default="medium", help="low, medium (default) or high")
    finding.add_argument("--body-file", help="markdown that explains it, at most 16 KiB (- for stdin)")
    _session_options(finding)
    json_option(finding, returns_object(*FINDING_KEYS, schema="Finding"))
    finding.set_defaults(func=cmd_finding)

    propose = csub.add_parser(
        "propose", help="propose a change from your own session, with its draft plan, as a review run does"
    )
    with_project(propose)
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
    _session_options(propose)
    json_option(propose, returns_object(*PROPOSAL_KEYS, schema="Proposal"))
    propose.set_defaults(func=cmd_propose)
