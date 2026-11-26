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

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Findings` is
the gateway to the routes, :func:`finding_body` and :func:`proposal_body` turn the options into the bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from types import SimpleNamespace

from evo_agents.clikit import Arg, Result, Text, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource
from evo_agents.hub.cli.curator.resources import PROPOSAL_KEYS
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.review import LENSES, MAX_BODY_BYTES, SEVERITIES
from evo_agents.hub.tiers import CHANGE_KINDS, split_target

FINDING_KEYS = ("id", "project", "run_id", "lens", "severity", "title", "body", "evidence", "created_at", "fix_of")
SESSION_OPTIONS = (
    Arg("--session", metavar="ID", help="the Claude Code session (default: CLAUDE_CODE_SESSION_ID)"),
    Arg("--run", type=int, metavar="ID", help="the session's local run, in place of finding it"),
)


class Findings(ProjectResource):
    """What a session records for the Curator: POST /v1/projects/{p}/curator/findings and .../curator/proposals."""

    collection = "curator"

    def finding(self, body: dict) -> dict:
        return self.call("POST", "findings", body=body)

    def proposal(self, body: dict) -> dict:
        return self.call("POST", "proposals", body=body)


def session_run(hub: Hub, project: str, session: str | None, run: int | None) -> int:
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


def finding_body(args: argparse.Namespace) -> dict:
    """The body of ``curator finding``, its run aside: the options, the body file and the evidence."""
    from evo_agents.worker.agent_input import evidence_items, read_text

    body: dict = {"lens": args.lens, "severity": args.severity, "title": " ".join(args.title.split())}
    if args.body_file:
        text = _input(lambda: read_text(args.body_file, "body", MAX_BODY_BYTES))
        if text is not None:
            body["body"] = text
    body["evidence"] = _input(lambda: evidence_items(args.evidence))
    return body


def proposal_paths(args: argparse.Namespace) -> list[dict]:
    """The ``--path REPO:PATH`` of ``curator propose``; HubError for one that is not."""
    paths = []
    for text in args.path or []:
        found = split_target(text)
        if found is None:
            raise HubError(f"--path takes REPO:PATH, a path within a repo, not {text!r}")
        paths.append({"repo": found[0], "path": found[1]})
    return paths


def proposal_body(args: argparse.Namespace) -> dict:
    """The body of ``curator propose``, its run aside; HubError when it rests on no finding and no evidence."""
    from evo_agents.worker.agent_input import evidence_items, plan_file, probe_file, promotion_file, read_text

    paths = proposal_paths(args)
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
    return body


def recorded(ctx: HubContext, body: dict) -> dict:
    """``body`` with its evidence checked here and the run of this session it is recorded in."""
    _attest(ctx.project, body["evidence"])
    return {**body, "run_id": session_run(ctx.hub, ctx.project, ctx.args.session, ctx.args.run)}


class Finding(HubCommand):
    name = "finding"
    help = "record a finding of your own session, with its evidence, as a review run does"
    arguments = (
        PROJECT,
        Arg("--lens", required=True, choices=list(LENSES), help="the lens it was found through"),
        Arg("--title", required=True, help="what was found, in one line"),
        Arg(
            "--evidence",
            action="append",
            required=True,
            metavar="SPEC",
            help="session:ID[:FIELD:INDEX], transcript:ID[:FILE]:LINE, run:ID:SEQ, code:REPO:PATH[:LINE] or "
            "memory:ID:REV; repeat it",
        ),
        Arg("--severity", choices=SEVERITIES, default="medium", help="low, medium (default) or high"),
        Arg("--body-file", help="markdown that explains it, at most 16 KiB (- for stdin)"),
        *SESSION_OPTIONS,
    )
    output = returns_object(*FINDING_KEYS, schema="Finding")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        _ = ctx.hub, ctx.project  # signed in and the project found before any file is read, as before
        body = recorded(ctx, finding_body(args))
        answer = Findings.of(ctx).finding(body)
        said = (
            f"Finding #{answer.get('id')} recorded in run #{answer.get('run_id')} ({args.lens}, {args.severity}): "
            f"{body['title']}"
        )
        return Result(answer, Text(said))


def proposal_lines(answer: dict) -> list[str]:
    """What ``curator propose`` says of the proposal the hub recorded, or dropped."""
    if answer.get("state") == "dropped":
        return [
            f"Proposal #{answer.get('id')} was dropped: it repeats proposal #{answer.get('duplicate_of')}, which was "
            "rejected lately, and its evidence is not twice as large."
        ]
    said = (
        f"Proposal #{answer.get('id')} recorded in run #{answer.get('run_id')}: tier {answer.get('tier')}, "
        f"{answer.get('state')}."
    )
    return [said, *(f"  {reason}" for reason in answer.get("tier_reasons") or [])]


class Propose(HubCommand):
    name = "propose"
    help = "propose a change from your own session, with its draft plan, as a review run does"
    output = returns_object(*PROPOSAL_KEYS, schema="Proposal")

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        PROJECT.add_to(parser)
        parser.add_argument("--lens", required=True, choices=list(LENSES), help="the lens it comes from")
        parser.add_argument("--kind", required=True, choices=list(CHANGE_KINDS), help="the kind of change")
        parser.add_argument("--title", required=True, help="the change, in one line")
        draft = parser.add_mutually_exclusive_group(required=True)
        draft.add_argument("--plan-file", help="the draft plan, YAML or JSON, in outcome steps")
        draft.add_argument("--promote-file", help="kind memory: the promotion of agents' memories, YAML or JSON")
        parser.add_argument("--path", action="append", metavar="REPO:PATH", help="a file it would edit; repeat it")
        parser.add_argument(
            "--finding", action="append", type=int, metavar="ID", help="a finding it rests on; repeat it"
        )
        parser.add_argument(
            "--evidence", action="append", metavar="SPEC", help="evidence of its own, as finding takes it; repeat it"
        )
        parser.add_argument(
            "--summary-file", help="markdown: the problem, the change, what waiting costs (- for stdin)"
        )
        parser.add_argument(
            "--probe-file",
            help="the probe, YAML or JSON: repo, commit, task and verify; required when the evidence cites a "
            "correction and a path is an AGENTS.md, a CLAUDE.md, a skill or the repo's .claude settings or hooks",
        )
        for option in SESSION_OPTIONS:
            option.add_to(parser)

    def run(self, ctx: HubContext) -> Result:
        _ = ctx.hub, ctx.project  # signed in and the project found before any file is read, as before
        answer = Findings.of(ctx).proposal(recorded(ctx, proposal_body(ctx.args)))
        return Result(answer, Text(*proposal_lines(answer)))


FINDING = Finding()
PROPOSE = Propose()
