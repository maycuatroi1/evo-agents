"""``evo-agents hub curator``: the night shift of a project, as its charter sets it (``evo_agents.hub.curator``).

``curator charter show`` prints the charter at its newest revision, or ``--revision N`` (GET
/v1/projects/{p}/curator/charter); ``--json`` prints it whole, with ``auto_merge`` and ``judge.runtime`` among its keys.
``curator charter set FILE`` writes a new revision from a YAML or JSON file (``-`` reads stdin), which only an admin of
the project may (PUT .../curator/charter); the keys ``show --json`` adds to the body are left out, so what was shown can
be edited and written back. ``curator charter history`` lists every revision.

``curator status`` shows where the night shift stands: the charter's revision, the schedules, whether they are paused,
and the night now with its runs and cost (GET .../curator). ``curator pause`` pauses every schedule of the project and
cancels the runs they queued that are still queued; within a minute nothing of the project's night shift moves.
``curator resume`` lets them run again. Either takes an admin of the project, or the member a schedule dispatches as.

``curator proposal list`` lists the proposals of the project's review runs, newest first, filtered by ``--state``,
``--tier`` (each repeats), ``--lens`` and ``--run`` (GET .../curator/proposals); ``curator proposal show ID`` shows one
with its tier's reasons, its evidence and its draft plan; ``curator proposal accept|reject|defer ID`` answers it, which
only an admin of the project may (POST .../curator/proposals/{id}/answer), with ``--note`` and, for defer, ``--days``.
``curator findings`` lists what the review runs found (GET .../curator/findings), and ``curator figures`` prints the
figures of the latest night, or of ``--night`` (GET .../curator/figures).

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library and PyYAML only, like the client.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlencode

import yaml

from evo_agents.hub.cli_client import _client_command, _print_json, _project_path, _signed_in, _table, _when
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.curator import CHARTER_META, money
from evo_agents.hub.plan_cli import _project
from evo_agents.hub.review import ANSWERS, DEFER_DAYS, LENSES, PROPOSAL_STATES, evidence_text

# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
CHARTER_BODY_KEYS = (
    "goals",
    "window",
    "worker",
    "night_budget_usd",
    "run_budget_usd",
    "run_max_turns",
    "run_minutes",
    "max_runs_per_night",
    "night_plans",
    "max_decisions_per_day",
    "brief_at",
    "auto_merge",
    "protected_paths",
    "circuit_breaker",
    "review",
    "reviewer",
    "builder",
    "judge",
)
CHARTER_KEYS = CHARTER_BODY_KEYS + CHARTER_META
REVISION_KEYS = ("revision", "updated_by", "updated_at", "worker", "worker_id")
STATUS_KEYS = ("project", "charter", "paused", "schedules", "night", "last_review_run")
PROPOSAL_SUMMARY_KEYS = (
    "id",
    "project",
    "run_id",
    "lens",
    "kind",
    "title",
    "tier",
    "state",
    "evidence_count",
    "duplicate_of",
    "answered_by",
    "answered_at",
    "deferred_until",
    "inbox_at",
    "created_at",
)
PROPOSAL_KEYS = PROPOSAL_SUMMARY_KEYS + (
    "summary",
    "paths",
    "impacted",
    "tier_reasons",
    "finding_ids",
    "evidence",
    "plan",
    "note",
)
FINDING_KEYS = ("id", "project", "run_id", "lens", "severity", "title", "body", "evidence", "created_at")
PROPOSAL = returns_object(*PROPOSAL_KEYS, schema="Proposal")
STATUS = returns_object(*STATUS_KEYS, schema="CuratorStatus")
CHARTER = returns_object(*CHARTER_KEYS, schema="Charter")


def _curator_path(project: str, *parts: str) -> str:
    return "/".join((f"{_project_path(project)}/curator", *parts))


def _read_charter(source: str) -> dict:
    """The charter in the YAML or JSON file ``source`` (``-``: stdin), without the keys ``show --json`` adds."""
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        raise HubError(f"cannot read {source}: {exc.strerror or exc}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HubError(f"{source} is neither YAML nor JSON: {exc}") from None
    if not isinstance(data, dict):
        raise HubError(f"{source} holds no mapping: a charter is one, with window, worker and night_budget_usd")
    return {key: value for key, value in data.items() if key not in CHARTER_META}


def _role(role: dict) -> str:
    return role["runtime"] + (f" ({role['model']})" if role.get("model") else "")


def _review(settings: dict) -> str:
    cap = settings.get("budget_usd")
    spend = f"at most {money(cap)}" if cap is not None else "the night's caps"
    return f"{settings.get('lenses', 3)} lenses a night, {settings.get('days', 7)} days of figures, {spend}"


def _print_charter(charter: dict) -> None:
    window = charter["window"]
    print(
        f"Charter of {charter['project']}, revision {charter['revision']}, by {charter['updated_by']} at "
        f"{_when(charter['updated_at'])} UTC"
    )
    run_cap = money(charter["run_budget_usd"]) if charter["run_budget_usd"] is not None else "what the night has left"
    lines = [
        ("window", f"{window['start']} to {window['end']}, {window['timezone']}"),
        ("worker", f"{charter['worker']} (#{charter['worker_id']}), runs dispatched as {charter['schedule_owner']}"),
        ("night", f"{money(charter['night_budget_usd'])}, at most {charter['max_runs_per_night']} runs"),
        ("each run", f"{run_cap}, {charter['run_max_turns']} turns, {charter['run_minutes']} minutes of agent time"),
        ("plans", ", ".join(charter["night_plans"]) or "none"),
        ("decisions", f"at most {charter['max_decisions_per_day']} a day, brief at {charter['brief_at']}"),
        ("auto merge", ", ".join(f"tier {tier}" for tier in charter["auto_merge"]) or "none"),
        ("protected", ", ".join(charter["protected_paths"]) or "none"),
        ("breaker", f"{charter['circuit_breaker']['max_failed_in_a_row']} failed in a row"),
        ("review", _review(charter.get("review") or {})),
        ("reviewer", _role(charter["reviewer"])),
        ("builder", _role(charter["builder"])),
        ("judge", _role(charter["judge"])),
    ]
    checks = charter["judge"].get("hidden_checks")
    lines.append(("hidden checks", "shown to admins only" if checks is None else str(len(checks))))
    width = max(len(label) for label, _ in lines)
    for label, text in lines:
        print(f"  {label:<{width}}  {text}")
    for goal in charter["goals"]:
        print(f"  goal {goal['id']}: {goal['what']}")


@_client_command
def cmd_charter_show(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    query = f"?{urlencode({'revision': args.revision})}" if args.revision is not None else ""
    charter = hub.call("GET", _curator_path(project, "charter") + query)
    if args.json:
        _print_json(charter)
        return 0
    _print_charter(charter)
    return 0


@_client_command
def cmd_charter_set(args) -> int:
    body = _read_charter(args.file)
    hub, _ = _signed_in()
    project = _project(args)
    charter = hub.call("PUT", _curator_path(project, "charter"), body)
    if args.json:
        _print_json(charter)
        return 0
    print(f"Charter of {charter['project']} is at revision {charter['revision']}.")
    print(
        f"The night shift runs {', '.join(charter['night_plans']) or 'no plan'} on {charter['worker']} as "
        f"{charter['schedule_owner']}, {charter['window']['start']} to {charter['window']['end']} "
        f"{charter['window']['timezone']}."
    )
    return 0


@_client_command
def cmd_charter_history(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    revisions = hub.call("GET", _curator_path(project, "charter", "revisions"))
    if args.json:
        _print_json(revisions)
        return 0
    rows = [(row["revision"], row["updated_by"], _when(row["updated_at"]), row["worker"]) for row in revisions]
    if rows:
        _table(("REVISION", "BY", "AT (UTC)", "WORKER"), rows)
    print(f"{len(rows)} revision(s) of the charter of {project}")
    return 0


def _print_status(status: dict) -> None:
    charter = status["charter"]
    if charter is None:
        print(
            f"Project {status['project']} has no charter: an admin writes one with `evo-agents hub curator charter set`"
        )
        return
    state = "paused" if status["paused"] else "on"
    print(f"Night shift of {status['project']}: {state}, charter revision {charter['revision']}")
    for schedule in status["schedules"]:
        paused = (
            f", paused by {schedule['paused_by']} at {_when(schedule['paused_at'])} UTC"
            if schedule["paused_at"]
            else ""
        )
        print(f"  {schedule['kind']}: worker {schedule['worker']}, runs dispatched as {schedule['owner']}{paused}")
    night = status["night"]
    if night is not None:
        where = "inside" if night["in_window"] else "outside"
        active = f", run #{night['active_run_id']} active" if night["active_run_id"] else ""
        print(
            f"  night of {night['night']} ({night['local_time']} local, {where} the window): {night['runs']} of "
            f"{night['max_runs']} runs, {money(night['cost_usd'])} of {money(night['budget_usd'])}{active}"
        )
    last = status.get("last_review_run")
    if last is not None:
        print(
            f"  last review: run #{last['id']} of the night of {last['night']}, {last['state']}: {last['findings']} "
            f"findings, {last['proposals']} proposals (lenses {', '.join(last['lenses']) or '-'})"
        )


@_client_command
def cmd_status(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    status = hub.call("GET", _curator_path(project))
    if args.json:
        _print_json(status)
        return 0
    _print_status(status)
    return 0


def _switch(action: str):
    @_client_command
    def run(args) -> int:
        hub, _ = _signed_in()
        project = _project(args)
        status = hub.call("POST", _curator_path(project, action))
        if args.json:
            _print_json(status)
            return 0
        if action == "pause":
            print(f"Paused the night shift of {project}: it queues no run until `evo-agents hub curator resume`.")
        else:
            print(f"The night shift of {project} runs again inside its window.")
        _print_status(status)
        return 0

    return run


cmd_pause = _switch("pause")
cmd_resume = _switch("resume")


# Proposals and findings


def _query(**values) -> str:
    pairs = []
    for key, value in values.items():
        for item in value if isinstance(value, list) else [value]:
            if item is not None:
                pairs.append((key, item))
    return f"?{urlencode(pairs)}" if pairs else ""


@_client_command
def cmd_proposal_list(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    query = _query(
        state=args.state or [],
        tier=args.tier or [],
        lens=args.lens,
        run_id=args.run,
        limit=args.limit,
        offset=args.offset or None,
    )
    found = hub.call("GET", _curator_path(project, "proposals") + query)
    if args.json:
        _print_json(found)
        return 0
    rows = [
        (f"#{p['id']}", p["tier"], p["state"], p["kind"], p["lens"], f"#{p['run_id']}", p["evidence_count"], p["title"])
        for p in found["proposals"]
    ]
    if rows:
        _table(("ID", "TIER", "STATE", "KIND", "LENS", "RUN", "EVIDENCE", "TITLE"), rows)
    print(f"{len(rows)} of {found['total']} proposal(s) of {project}")
    return 0


def _print_proposal(proposal: dict) -> None:
    print(f"Proposal #{proposal['id']} of {proposal['project']}: {proposal['title']}")
    lines = [
        ("tier", str(proposal["tier"])),
        ("state", proposal["state"] + (f" (repeats #{proposal['duplicate_of']})" if proposal["duplicate_of"] else "")),
        ("kind", proposal["kind"]),
        ("lens", proposal["lens"]),
        ("run", f"#{proposal['run_id']}, {_when(proposal['created_at'])} UTC"),
        ("paths", ", ".join(f"{p['repo']}:{p['path']}" for p in proposal["paths"]) or "none"),
        ("findings", ", ".join(f"#{item}" for item in proposal["finding_ids"]) or "none"),
        ("evidence", f"{proposal['evidence_count']} pieces"),
    ]
    if proposal.get("answered_by"):
        lines.append(("answered", f"by {proposal['answered_by']} at {_when(proposal['answered_at'])} UTC"))
    if proposal.get("deferred_until"):
        lines.append(("deferred", f"until {_when(proposal['deferred_until'])} UTC"))
    if proposal.get("note"):
        lines.append(("note", proposal["note"]))
    width = max(len(label) for label, _ in lines)
    for label, text in lines:
        print(f"  {label:<{width}}  {text}")
    print("  why this tier:")
    for reason in proposal["tier_reasons"]:
        print(f"    {reason}")
    if proposal["evidence"]:
        print("  its own evidence:")
        for item in proposal["evidence"]:
            print(f"    {evidence_text(item)} ({item.get('resolved')})")
    if proposal.get("summary"):
        print("\n" + proposal["summary"].rstrip())
    print("\nDraft plan:")
    print(json.dumps(proposal["plan"], ensure_ascii=False, indent=2))


@_client_command
def cmd_proposal_show(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    proposal = hub.call("GET", _curator_path(project, "proposals", str(args.id)))
    if args.json:
        _print_json(proposal)
        return 0
    _print_proposal(proposal)
    return 0


def _answer(action: str):
    @_client_command
    def run(args) -> int:
        hub, _ = _signed_in()
        project = _project(args)
        body: dict = {"action": action}
        if args.note:
            body["note"] = args.note
        if action == "defer" and args.days is not None:
            body["defer_days"] = args.days
        proposal = hub.call("POST", _curator_path(project, "proposals", str(args.id), "answer"), body)
        if args.json:
            _print_json(proposal)
            return 0
        until = f" until {_when(proposal['deferred_until'])} UTC" if proposal.get("deferred_until") else ""
        print(f"Proposal #{proposal['id']} of {project} is {proposal['state']}{until}: {proposal['title']}")
        return 0

    return run


cmd_accept = _answer("accept")
cmd_reject = _answer("reject")
cmd_defer = _answer("defer")


@_client_command
def cmd_findings(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    query = _query(run_id=args.run, lens=args.lens, limit=args.limit, offset=args.offset or None)
    found = hub.call("GET", _curator_path(project, "findings") + query)
    if args.json:
        _print_json(found)
        return 0
    rows = [
        (f"#{f['id']}", f["severity"], f["lens"], f"#{f['run_id']}", len(f["evidence"]), f["title"])
        for f in found["findings"]
    ]
    if rows:
        _table(("ID", "SEVERITY", "LENS", "RUN", "EVIDENCE", "TITLE"), rows)
    print(f"{len(rows)} of {found['total']} finding(s) of {project}")
    return 0


@_client_command
def cmd_figures(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    found = hub.call("GET", _curator_path(project, "figures") + _query(night=args.night))
    if args.json:
        _print_json(found)
        return 0
    review = f", review run #{found['run_id']}" if found.get("run_id") else ""
    print(
        f"Figures of {found['project']} for the night of {found['night']}{review}: sessions and runs from "
        f"{_when(found['since'])} to {_when(found['until'])} UTC"
    )
    print(json.dumps(found["figures"], ensure_ascii=False, indent=2))
    return 0


def register_curator(hsub) -> None:
    curator = hsub.add_parser(
        "curator", help="the night shift of a project: its charter, status, pause, resume, and its review's proposals"
    )
    csub = curator.add_subparsers(dest="curator_command", required=True)
    project_help = "hub project (default: hub.project in the harness.yaml around the current directory)"

    def with_project(parser) -> None:
        parser.add_argument("--project", help=project_help)

    charter = csub.add_parser("charter", help="the project's charter: show it, write a revision, list revisions")
    chsub = charter.add_subparsers(dest="charter_command", required=True)
    show = chsub.add_parser("show", help="the charter at its newest revision, or an older one")
    with_project(show)
    show.add_argument("--revision", type=int, metavar="N", help="an older revision")
    json_option(show, CHARTER)
    show.set_defaults(func=cmd_charter_show)

    write = chsub.add_parser("set", help="write a new revision from a YAML or JSON file; an admin of the project only")
    write.add_argument("file", metavar="FILE", help="the charter, YAML or JSON; - reads stdin")
    with_project(write)
    json_option(write, CHARTER)
    write.set_defaults(func=cmd_charter_set)

    history = chsub.add_parser("history", help="every revision of the charter, newest first")
    with_project(history)
    json_option(history, returns_array(*REVISION_KEYS, schema="CharterRevision"))
    history.set_defaults(func=cmd_charter_history)

    status = csub.add_parser("status", help="the charter, the schedules and the night now")
    with_project(status)
    json_option(status, STATUS)
    status.set_defaults(func=cmd_status)

    pause = csub.add_parser("pause", help="pause every schedule of the project and cancel the runs they still queue")
    with_project(pause)
    json_option(pause, STATUS)
    pause.set_defaults(func=cmd_pause)

    resume = csub.add_parser("resume", help="let the schedules of the project run again")
    with_project(resume)
    json_option(resume, STATUS)
    resume.set_defaults(func=cmd_resume)

    proposal = csub.add_parser("proposal", help="the proposals of the review runs: list, show, accept, reject, defer")
    psub = proposal.add_subparsers(dest="proposal_command", required=True)
    listed = psub.add_parser("list", help="the proposals, newest first")
    with_project(listed)
    listed.add_argument("--state", action="append", choices=PROPOSAL_STATES, help="any of these states; repeat it")
    listed.add_argument("--tier", action="append", type=int, choices=range(4), help="any of these tiers; repeat it")
    listed.add_argument("--lens", choices=list(LENSES), help="of this lens")
    listed.add_argument("--run", type=int, metavar="ID", help="of this review run")
    listed.add_argument("--limit", type=int, default=50, choices=range(1, 201), metavar="N", help="1 to 200")
    listed.add_argument("--offset", type=int, default=0, metavar="N", help="the proposals to pass over")
    json_option(listed, returns_object("proposals", "total", "limit", "offset", schema="ProposalList"))
    listed.set_defaults(func=cmd_proposal_list)
    shown = psub.add_parser("show", help="one proposal: its tier and why, its evidence, its draft plan")
    shown.add_argument("id", type=int, help="the proposal's id")
    with_project(shown)
    json_option(shown, PROPOSAL)
    shown.set_defaults(func=cmd_proposal_show)
    helps = {
        "accept": "accept the proposal; an admin of the project only",
        "reject": "reject it: one like it is dropped for 30 days, unless its evidence doubles",
        "defer": "defer it: it opens again after --days (7 by default)",
    }
    for action, func in zip(ANSWERS, (cmd_accept, cmd_reject, cmd_defer), strict=True):
        answer = psub.add_parser(action, help=helps[action])
        answer.add_argument("id", type=int, help="the proposal's id")
        answer.add_argument("--note", help="why, in one line")
        if action == "defer":
            answer.add_argument(
                "--days", type=int, choices=range(DEFER_DAYS[0], DEFER_DAYS[1] + 1), metavar="N", help="1 to 90"
            )
        with_project(answer)
        json_option(answer, PROPOSAL)
        answer.set_defaults(func=func)

    findings = csub.add_parser("findings", help="what the review runs found, newest first")
    with_project(findings)
    findings.add_argument("--run", type=int, metavar="ID", help="of this review run")
    findings.add_argument("--lens", choices=list(LENSES), help="of this lens")
    findings.add_argument("--limit", type=int, default=50, choices=range(1, 201), metavar="N", help="1 to 200")
    findings.add_argument("--offset", type=int, default=0, metavar="N", help="the findings to pass over")
    json_option(findings, returns_object("findings", "total", "limit", "offset", schema="FindingList"))
    findings.set_defaults(func=cmd_findings)

    figures = csub.add_parser("figures", help="the figures curator.collect counted for the latest night, or --night")
    with_project(figures)
    figures.add_argument("--night", metavar="YYYY-MM-DD", help="the night, the local date its window opened")
    json_option(
        figures,
        returns_object(
            "project", "night", "since", "until", "run_id", "figures", "created_at", schema="NightFiguresView"
        ),
    )
    figures.set_defaults(func=cmd_figures)
