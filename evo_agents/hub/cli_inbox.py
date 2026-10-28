"""``evo-agents hub inbox``: what waits on you on every hub this machine is signed in to, in one call (``inbox``).

``hub inbox`` lists, hub by hub, the open decisions of every project you read (a credential request says to answer
it with ``--secret``), the runs in review, waiting or parked on no decision, failed or lost in the last ``--since``
hours and not run again, and running with no event for ``--stale`` minutes, then the unread notices that ask for an
act and the Curator's proposals that wait; notices that only report are counted. Yours come first. Each item ends with
the command that acts on it, with ``EVO_HUB_HOME=<dir>`` in front when it is on a hub other than the one a bare
``evo-agents hub`` uses. Decisions of category wait are the hub's to answer and only listed with ``--waits``.
``--hub`` keeps the hubs whose URL holds its text, ``--project`` one project.

``hub inbox --show REF`` reads one item whole, without its project or its hub: ``582``, ``d582`` or ``decision:582`` is a
decision, ``r1092``, ``run:1092`` or ``#1092`` a run, and a link of the web (``.../inbox?decision=582``,
``.../p/lumen/runs/948``) either, on the link's hub. It prints the decision (question, context, options, the
recommendation, the credential request), its run (state, plan and step, worker, last event, lease, cause), the leases
the run holds (to its owner), the run's last events, a diagnosis and the commands that act on it.

``--json`` prints ``{"hubs": [...]}`` for the list and the item's object for ``show``. Exit 1 when no hub could be
read, or ``show`` finds nothing. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys

from evo_agents.hub import inbox
from evo_agents.hub.cli_client import client_command, print_json
from evo_agents.hub.client import EXIT_FAILED, HubError
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.plan_cli import project_of_args
from evo_agents.hub.run_cli import format_event

TITLE_CHARS = 110  # of an item's title in the list
LABELS = {
    "decision": "DECISION",
    "credential": "CREDENTIAL",
    "review": "REVIEW",
    "failed": "FAILED",
    "lost": "LOST",
    "waiting": "WAITING",
    "parked": "PARKED",
    "stale": "STALE",
    "notice": "NOTICE",
    "proposal": "PROPOSAL",
}
INBOX_KEYS = ("hubs",)
SHOW_KEYS = ("hub", "project", "decision", "run", "leases", "events", "diagnosis", "next")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _age(minutes: int | None) -> str:
    if minutes is None:
        return "-"
    if minutes < 120:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


def _where(item: dict) -> str:
    parts = [item["project"] or "-"]
    if item.get("run_id"):
        kind = f" ({item['category']})" if item["kind"] not in ("decision", "credential", "notice", "proposal") else ""
        parts.append(f"run #{item['run_id']}{kind}")
    if item.get("plan_id"):
        parts.append(item["plan_id"] + (f" step {item['step_key']}" if item.get("step_key") else ""))
    return ", ".join(parts)


def inbox_lines(hubs: list[inbox.HubInbox]) -> list[str]:
    """What the terminal prints of the list."""
    lines: list[str] = []
    for hub in hubs:
        if hub.error:
            lines += [f"{hub.url}: cannot read it: {hub.error}", ""]
            continue
        counted = {}
        for item in hub.items:
            counted[item["kind"]] = counted.get(item["kind"], 0) + 1
        summary = ", ".join(f"{n} {LABELS[kind].lower()}" for kind, n in counted.items()) or "nothing waits on you"
        lines.append(f"{hub.url} ({hub.login or 'token'}): {summary}; {hub.reports} unread notice(s) only report")
        for item in hub.items:
            who = "" if item["mine"] else f" [asked of {item['asked_of']}]"
            if item.get("also"):
                who += f" and {len(item['also'])} more alike: " + ", ".join(f"#{n}" for n in item["also"][:6])
            number = f"#{item['id']}"
            lines.append(f"  {LABELS[item['kind']]:<10} {number:<6} {_age(item['age_min']):>4}  {_where(item)}{who}")
            lines.append(f"      {_clip(item['title'] or '-', TITLE_CHARS)}")
            lines.append(f"      -> {item['next']}")
        lines.append("")
    return lines


def inbox_json(hubs: list[inbox.HubInbox]) -> dict:
    """What ``--json`` prints of the list (INBOX_KEYS)."""
    return {"hubs": [hub.to_json() for hub in hubs]}


@client_command
def cmd_inbox(args) -> int:
    if args.show:
        return show(args)
    found = inbox.logins(args.hub)
    if not found:
        print("error: not signed in to any hub: evo-agents hub login --url https://...", file=sys.stderr)
        return EXIT_FAILED
    hubs = inbox.gather(found, project=args.project, stale=args.stale, since=args.since, waits=args.waits)
    if args.json:
        print_json(inbox_json(hubs))
    else:
        print("\n".join(inbox_lines(hubs)).rstrip())
    return EXIT_FAILED if all(hub.error for hub in hubs) else 0


def _decision_lines(decision: dict) -> list[str]:
    lines = [
        f"Decision #{decision['id']} ({decision['category']}, {decision['state']}), asked of {decision.get('asked_of')}",
        f"  {decision['question']}",
    ]
    if decision.get("context"):
        lines += [f"  context: {line}" for line in decision["context"].splitlines()[:12]]
    for option in decision.get("options") or ():
        mark = " (recommended)" if option.get("recommended") or option["key"] == decision.get("recommended") else ""
        lines.append(f"  [{option['key']}] {option['label']}{mark}")
    if decision.get("recommended_reason"):
        lines.append(f"  why: {decision['recommended_reason']}")
    if decision.get("credential_request"):
        request = decision["credential_request"]
        lines.append(f"  asks for: {request.get('env_var')} (secret {request.get('secret_name') or '-'})")
    if decision.get("answer_option") or decision.get("answer_text"):
        lines.append(f"  answered: {decision.get('answer_option') or '-'} {decision.get('answer_text') or ''}".rstrip())
    return lines


def _run_lines(run: dict) -> list[str]:
    plan = (
        f", plan {run['plan_id']}" + (f" step {run['step_key']}" if run.get("step_key") else "")
        if run.get("plan_id")
        else ""
    )
    lines = [
        f"Run #{run['id']} ({run['kind']}{plan}): {run['state']} on {run.get('worker') or 'no worker'}, "
        f"by {run.get('dispatched_by')}, attempt {run.get('attempt')}/{run.get('max_attempts')}",
        f"  last event {run.get('last_activity_at') or '-'}, lease until {run.get('lease_expires_at') or '-'}",
    ]
    if run.get("failure_cause") or run.get("error"):
        lines.append(
            f"  cause: {run.get('failure_cause') or '-'}; {_clip(' '.join(str(run.get('error') or '').split()), 300)}"
        )
    return lines


def show_lines(found: dict) -> list[str]:
    """What the terminal prints of one item."""
    lines = [f"{found['hub']}, project {found['project']}"]
    if found["decision"]:
        lines += _decision_lines(found["decision"])
    if found["run"]:
        lines += _run_lines(found["run"])
    if found["leases"]:
        names = [lease["name"] + (" (given back)" if lease.get("revoked_at") else "") for lease in found["leases"]]
        lines.append("Leases: " + ", ".join(names))
    if found["events"]:
        lines.append("Last events:")
        lines += [format_event(event) for event in found["events"]]
    if found["diagnosis"]:
        lines.append(f"Diagnosis: {found['diagnosis']}")
    lines += [f"-> {command}" for command in found["next"]]
    return lines


def show(args) -> int:
    try:
        ref = inbox.parse_ref(args.show)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        first = project_of_args(args)
    except HubError:
        first = None  # outside a harness: every project in turn
    found = inbox.detail(ref, inbox.logins(args.hub), first, events=args.events)
    if found is None:
        print(f"error: no hub this machine is signed in to has {ref.what} {ref.id} for you", file=sys.stderr)
        return EXIT_FAILED
    if args.json:
        print_json(found)
    else:
        print("\n".join(show_lines(found)))
    return 0


def register_inbox(hsub) -> None:
    parser = hsub.add_parser(
        "inbox",
        help="what waits on you on every hub you are signed in to: open decisions, runs to approve, failed, lost or "
        "stale runs, notices that ask for an act; each with the command that acts on it",
    )
    parser.add_argument(
        "--show",
        metavar="REF",
        help="one decision or run whole instead: 582, d582, r1092, #1092, or a link such as .../inbox?decision=582",
    )
    parser.add_argument("--hub", metavar="TEXT", help="only the hubs whose URL holds this text")
    parser.add_argument("--project", help="only this project (with --show, the one to look in first)")
    parser.add_argument(
        "--stale",
        type=int,
        default=inbox.STALE_MINUTES,
        metavar="MIN",
        help="a running run with no event for this long is stale (default 30)",
    )
    parser.add_argument(
        "--since",
        type=int,
        default=inbox.SINCE_HOURS,
        metavar="HOURS",
        help="failed and lost runs that ended this recently (default 72)",
    )
    parser.add_argument("--waits", action="store_true", help="list the agents' timed waits too, which the hub answers")
    parser.add_argument(
        "--events",
        type=int,
        default=inbox.EVENTS_SHOWN,
        metavar="N",
        help="with --show, the run's last N events (default 20)",
    )
    shown = returns_object(*SHOW_KEYS)
    json_option(parser, returns_object(*INBOX_KEYS, variants=(("--show", shown),)))
    parser.set_defaults(func=cmd_inbox)
