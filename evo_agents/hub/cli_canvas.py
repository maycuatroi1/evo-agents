"""``evo-agents hub canvas list|show|create|edit|undo|evidence|proposal|brainstorm|export|import``: the Lean Canvases
of a project that enabled the core plugin canvas, through its routes under /v1/x/canvas/<project>/
(``docs/plugins.md``, Core plugins). The plugin's code is ``evo_agents.core_plugins.canvas``, which this client never
imports: it speaks HTTP only.

``list`` prints the canvases, ``show SLUG`` a canvas by cell with each claim's code, status, confidence and rev, and
``show SLUG --claim CL-3`` a claim whole: its test card, its evidence (each piece real or why not) and every rev with
who wrote it. ``create SLUG --name NAME`` makes a canvas. ``edit SLUG`` changes one claim (``--claim CL-3`` with the
rev it was read at, ``--base-rev``) or makes one (no ``--claim``; ``--cell`` and ``--text``), or applies ``--file``,
JSON ``{"changes": [...], "reason": "..."}`` as the route takes it, all or nothing. A claim changed since the rev it was
read at refuses the edit: the error lists, per claim, the rev it is at, who changed it and what differs.
``--use-cases`` and ``--signal-keys`` give the whole list of what the claim links, comma-separated. ``undo SLUG
CL-3`` takes back the claim's current rev (``--rev`` names it; read from the hub when left out). ``evidence
add|withdraw|confirm`` keeps a claim's evidence. ``proposal list SLUG`` prints what an agent or the hub proposed (the
open ones unless ``--status`` names others), ``proposal accept SLUG ID`` writes one as the caller's rev and ``proposal
reject SLUG ID`` closes it. ``brainstorm SLUG`` hands @strategist a brainstorm, a task run that only proposes.
``export SLUG`` prints ``evo-canvas/1`` JSON (``--output`` writes a file) and ``import FILE`` reads it back (``-`` for
stdin), keeping the claims' codes; ``--dry-run`` prints what it would do. ``--json`` prints what the hub answered.
Standard library only, like the rest of the client.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object

EXIT_USAGE = 2
CELLS = (
    "problem",
    "customer_segments",
    "unique_value_proposition",
    "solution",
    "channels",
    "revenue_streams",
    "cost_structure",
    "key_metrics",
    "unfair_advantage",
)
STATUSES = ("assumption", "testing", "validated", "invalidated", "retired")
CONFIDENCES = ("low", "medium", "high")
ADDED_KINDS = ("interview", "experiment", "market", "doc", "simulation")
DIRECTIONS = ("supports", "contradicts", "neutral")
SUMMARY_KEYS = (
    "slug",
    "name",
    "description",
    "label",
    "created_by",
    "created_at",
    "updated_at",
    "archived_at",
    "claims",
    "statuses",
)
DETAIL_KEYS = (*SUMMARY_KEYS, "cells")
CLAIM_KEYS = (
    "code",
    "number",
    "cell",
    "position",
    "text",
    "status",
    "confidence",
    "test_card",
    "archived",
    "use_cases",
    "signal_keys",
    "rev",
    "evidence_count",
    "real_support_count",
    "label",
    "created_by",
    "created_at",
    "updated_at",
    "archived_at",
)
CLAIM_DETAIL_KEYS = (*CLAIM_KEYS, "canvas", "revs", "evidence", "hidden_evidence")
EDIT_KEYS = ("claims", "written")
EVIDENCE_KEYS = (
    "id",
    "kind",
    "direction",
    "summary",
    "url",
    "observed_on",
    "synthetic",
    "provenance",
    "created_at",
    "confirmed_by",
    "confirmed_at",
    "withdrawn_at",
    "withdrawn_by",
    "withdrawn_reason",
    "real",
    "not_real_because",
    "basis",
    "rests_on_run",
)
PROPOSAL_STATUSES = ("open", "accepted", "rejected", "stale")
PROPOSAL_KEYS = (
    "id",
    "kind",
    "claim",
    "base_rev",
    "fields",
    "changes",
    "reason",
    "synthetic",
    "provenance",
    "created_at",
    "status",
    "rev",
    "decided_by",
    "decided_at",
    "decided_reason",
    "evidence_id",
)
BRAINSTORM_KEYS = ("run_id", "title")
IMPORT_KEYS = ("dry_run", "canvas", "canvas_created", "created", "edited", "unchanged", "evidence", "problems")


def _path(project: str, *parts: str, query: dict | None = None) -> str:
    path = "/v1/x/canvas/" + "/".join(quote(str(part), safe="") for part in (project, *parts))
    given = {key: value for key, value in (query or {}).items() if value not in (None, False)}
    return f"{path}?{urlencode(given)}" if given else path


def _conflict_line(item: dict) -> str:
    changes = "; ".join(f"{c['field']}: {c['before']!r} -> {c['after']!r}" for c in item.get("changes") or [])
    head = f"  {item['claim']}: at rev {item['rev']}, changed by {item['changed_by']}; you read rev {item['base_rev']}"
    return head + (f": {changes}" if changes else "")


def _problems(exc: HubError) -> None:
    """Each claim of a conflict, or each problem, a refusal of the hub listed, one line each, on stderr."""
    detail = exc.payload.get("detail") if isinstance(exc.payload, dict) else None
    for item in detail or []:
        if isinstance(item, dict) and "changed_by" in item:
            print(_conflict_line(item), file=sys.stderr)
        elif isinstance(item, dict) and "level" in item:
            claim = f" {item['claim']}" if item.get("claim") else ""
            print(f"  {item['level']}{claim}: {item['message']}", file=sys.stderr)


def _call(method: str, path: str, body: dict | None = None):
    hub, _ = signed_in()
    try:
        return hub.call(method, path, body)
    except HubError as exc:
        _problems(exc)
        raise


def _read_json(file: str):
    try:
        text = sys.stdin.read() if file == "-" else Path(file).expanduser().read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, UnicodeDecodeError) as exc:
        raise HubError(f"cannot read {file}: {exc}") from None
    except ValueError as exc:
        raise HubError(f"{file} is no JSON: {exc}") from None


def _out(args, found, show) -> int:
    if args.json:
        print_json(found)
    else:
        show(found)
    return 0


# Reading


@client_command
def cmd_list(args) -> int:
    found = _call("GET", _path(args.project, "canvases", query={"archived": "true" if args.archived else None}))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"No canvas in project {args.project}.")
        return 0
    rows = [
        (
            c["slug"],
            c["claims"],
            ", ".join(f"{n} {s}" for s, n in sorted(c["statuses"].items())) or "-",
            when(c["updated_at"]),
            "archived" if c["archived_at"] else "",
            c["name"],
        )
        for c in found
    ]
    table(("CANVAS", "CLAIMS", "STATUSES", "UPDATED", "", "PRODUCT"), rows)
    return 0


def _claim_line(claim: dict) -> str:
    archived = ", archived" if claim["archived"] else ""
    facts = f"[{claim['status']}, {claim['confidence']}, rev {claim['rev']}{archived}]"
    evidence = (
        f" ({claim['real_support_count']}/{claim['evidence_count']} real support)" if claim["evidence_count"] else ""
    )
    return f"{claim['code']} {facts}{evidence} {claim['text']}"


def _print_canvas(canvas: dict) -> None:
    archived = " (archived)" if canvas["archived_at"] else ""
    print(f"{canvas['slug']}: {canvas['name']}{archived}")
    if canvas["description"]:
        print(f"  {canvas['description']}")
    for cell in canvas["cells"]:
        print(f"{cell['cell'].replace('_', ' ').capitalize()}:")
        for claim in cell["claims"]:
            print(f"  {_claim_line(claim)}")
        if not cell["claims"]:
            print("  -")


def _card_lines(card: dict | None) -> list[str]:
    if not card:
        return ["  no test card"]
    return [f"  test card {name}: {value}" for name, value in card.items() if value is not None]


def _print_claim(claim: dict) -> None:
    print(f"{claim['code']} in {claim['canvas']}, cell {claim['cell']}: {claim['text']}")
    archived = ", archived" if claim["archived"] else ""
    print(f"  status {claim['status']}, confidence {claim['confidence']}, rev {claim['rev']}{archived}")
    for line in _card_lines(claim["test_card"]):
        print(line)
    print(f"  use cases: {', '.join(claim['use_cases']) or '-'}")
    print(f"  signal keys: {', '.join(claim['signal_keys']) or '-'}")
    print("Evidence:" if claim["evidence"] else "No evidence.")
    for item in claim["evidence"]:
        real = "real" if item["real"] else f"not real: {item['not_real_because']}"
        who = item["provenance"]["login"] or item["provenance"]["actor"]
        print(f"  #{item['id']} {item['kind']} {item['direction']} ({real}), by {who}: {item['summary']}")
    if claim["hidden_evidence"]:
        print(f"  and {claim['hidden_evidence']} above what you may read")
    print("Revs:")
    for rev in claim["revs"]:
        author = rev["provenance"]["login"] or "hub"
        if rev["provenance"]["actor"] == "agent":
            author = f"an agent for {author} (run {rev['provenance']['run_id']})"
        fields = ", ".join(change["field"] for change in rev["changes"])
        reason = f" ({rev['reason']})" if rev["reason"] else ""
        print(f"  rev {rev['rev']} {when(rev['created_at'])} {rev['change']} by {author}: {fields}{reason}")


@client_command
def cmd_show(args) -> int:
    if args.claim:
        found = _call("GET", _path(args.project, "canvases", args.slug, "claims", args.claim))
        return _out(args, found, _print_claim)
    query = {"archived": "true" if args.archived else None}
    return _out(args, _call("GET", _path(args.project, "canvases", args.slug, query=query)), _print_canvas)


# Writing


@client_command
def cmd_create(args) -> int:
    body = {"slug": args.slug, "name": args.name, "description": args.description or ""}
    return _out(args, _call("POST", _path(args.project, "canvases"), body), _print_canvas)


def _print_edit(done: dict) -> None:
    written = set(done["written"])
    for claim in done["claims"]:
        mark = "" if claim["code"] in written else " (unchanged)"
        print(f"{_claim_line(claim)}{mark}")


def _test_card(args) -> tuple[bool, dict | None]:
    """(whether the change sets the test card, the card): ``--test-card JSON`` or ``--no-test-card``."""
    if args.no_test_card:
        return True, None
    if args.test_card is None:
        return False, None
    try:
        card = json.loads(args.test_card)
    except ValueError as exc:
        raise HubError(f"--test-card takes a JSON object: {exc}") from None
    if not isinstance(card, dict):
        raise HubError('--test-card takes a JSON object, as {"hypothesis": "...", "deadline": "2026-12-01"}')
    return True, card


def _listed(given: str | None) -> list[str] | None:
    """The comma-separated list an option gives, each item trimmed; None when the option is left out."""
    if given is None:
        return None
    return [item.strip() for item in given.split(",") if item.strip()]


def _change(args) -> dict:
    """The one change the options of ``edit`` give."""
    change = {name: getattr(args, name) for name in ("cell", "text", "status", "confidence", "position")}
    change |= {"use_cases": _listed(args.use_cases), "signal_keys": _listed(args.signal_keys)}
    change = {name: value for name, value in change.items() if value is not None}
    given, card = _test_card(args)
    if given:
        change["test_card"] = card
    if args.archive or args.unarchive:
        change["archived"] = bool(args.archive)
    if args.claim:
        if args.base_rev is None:
            raise HubError("--claim takes --base-rev, the rev you read it at: see `hub canvas show --claim`")
        change.update(claim=args.claim, base_rev=args.base_rev)
    return change


@client_command
def cmd_edit(args) -> int:
    if args.archive and args.unarchive or args.test_card is not None and args.no_test_card:
        print("error: --archive and --unarchive, --test-card and --no-test-card go apart", file=sys.stderr)
        return EXIT_USAGE
    if args.file:
        body = _read_json(args.file)
        body = {"changes": body} if isinstance(body, list) else body
        if args.reason is not None:
            body = {**body, "reason": args.reason}
    else:
        body = {"changes": [_change(args)], "reason": args.reason or ""}
    return _out(args, _call("POST", _path(args.project, "canvases", args.slug, "edit"), body), _print_edit)


@client_command
def cmd_undo(args) -> int:
    rev = args.rev
    if rev is None:
        rev = _call("GET", _path(args.project, "canvases", args.slug, "claims", args.claim))["rev"]
    body = {"revs": [{"claim": args.claim, "rev": rev}], "reason": args.reason or ""}
    return _out(args, _call("POST", _path(args.project, "canvases", args.slug, "undo"), body), _print_edit)


def _print_evidence(item: dict) -> None:
    real = "real" if item["real"] else f"not real: {item['not_real_because']}"
    withdrawn = f", withdrawn: {item['withdrawn_reason']}" if item["withdrawn_at"] else ""
    print(f"#{item['id']} {item['kind']} {item['direction']} ({real}{withdrawn}): {item['summary']}")


@client_command
def cmd_evidence_add(args) -> int:
    body = {"kind": args.kind, "direction": args.direction, "summary": args.summary, "synthetic": args.synthetic}
    body |= {key: value for key, value in (("url", args.url), ("observed_on", args.on)) if value}
    path = _path(args.project, "canvases", args.slug, "claims", args.claim, "evidence")
    return _out(args, _call("POST", path, body), _print_evidence)


@client_command
def cmd_evidence_withdraw(args) -> int:
    path = _path(args.project, "canvases", args.slug, "claims", args.claim, "evidence", args.id, "withdraw")
    return _out(args, _call("POST", path, {"reason": args.reason}), _print_evidence)


@client_command
def cmd_evidence_confirm(args) -> int:
    path = _path(args.project, "canvases", args.slug, "claims", args.claim, "evidence", args.id, "confirm")
    return _out(args, _call("POST", path), _print_evidence)


def _proposal_line(item: dict) -> str:
    what = f"{item['claim']} at rev {item['base_rev']}" if item["kind"] == "change" else "new claim"
    if item["kind"] == "create" and item["claim"]:
        what = f"new claim, now {item['claim']}"
    who = item["provenance"]["login"] or item["provenance"]["actor"]
    if item["provenance"]["actor"] == "agent":
        who = f"an agent for {who} (run {item['provenance']['run_id']})"
    synthetic = ", synthetic" if item["synthetic"] else ""
    return f"#{item['id']} [{item['status']}{synthetic}] {what}, by {who}: {item['reason']}"


def _print_proposal(item: dict) -> None:
    print(_proposal_line(item))
    for change in item["changes"]:
        print(f"  {change['field']}: {change['before']!r} -> {change['after']!r}")
    if item["status"] == "accepted":
        print(f"  accepted by {item['decided_by']} as rev {item['rev']} of {item['claim']}")
    elif item["status"] == "rejected":
        why = f": {item['decided_reason']}" if item["decided_reason"] else ""
        print(f"  rejected by {item['decided_by']}{why}")


@client_command
def cmd_proposal_list(args) -> int:
    query = [("status", status) for status in args.status or ()]
    path = _path(args.project, "canvases", args.slug, "proposals") + (f"?{urlencode(query)}" if query else "")
    found = _call("GET", path)
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"No {' or '.join(args.status or ('open',))} proposal of canvas {args.slug}.")
    for item in found:
        _print_proposal(item)
    return 0


@client_command
def cmd_proposal_accept(args) -> int:
    path = _path(args.project, "canvases", args.slug, "proposals", args.id, "accept")
    return _out(args, _call("POST", path), _print_proposal)


@client_command
def cmd_proposal_reject(args) -> int:
    path = _path(args.project, "canvases", args.slug, "proposals", args.id, "reject")
    return _out(args, _call("POST", path, {"reason": args.reason or ""}), _print_proposal)


def _print_brainstorm(done: dict) -> None:
    print(f"Run #{done['run_id']}: {done['title']}. It proposes; see `hub canvas proposal list` when it ends.")


@client_command
def cmd_brainstorm(args) -> int:
    body = {"focus": args.focus or "", "cells": list(args.cell or ())}
    return _out(args, _call("POST", _path(args.project, "canvases", args.slug, "brainstorm"), body), _print_brainstorm)


@client_command
def cmd_export(args) -> int:
    found = _call("GET", _path(args.project, "canvases", args.slug, "export"))
    text = json.dumps(found, ensure_ascii=False, indent=2) + "\n"
    if not args.output:
        sys.stdout.write(text)
        return 0
    try:
        Path(args.output).expanduser().write_text(text, encoding="utf-8")
    except OSError as exc:
        raise HubError(f"cannot write {args.output}: {exc}") from None
    print(f"Wrote canvas {args.slug}, {len(found['claims'])} claims, to {args.output}.")
    return 0


def _print_import(done: dict) -> None:
    made = " (a new canvas)" if done["canvas_created"] else ""
    lead = "Would import" if done["dry_run"] else "Imported"
    print(f"{lead} canvas {done['canvas']}{made}: {len(done['created'])} new, {len(done['edited'])} changed, ", end="")
    print(f"{len(done['unchanged'])} unchanged, {done['evidence']} pieces of evidence.")
    for name in ("created", "edited"):
        if done[name]:
            print(f"  {name}: {', '.join(done[name])}")
    for problem in done["problems"]:
        claim = f" {problem['claim']}" if problem["claim"] else ""
        print(f"  {problem['level']}{claim}: {problem['message']}")


@client_command
def cmd_import(args) -> int:
    body = {"data": _read_json(args.file), "dry_run": args.dry_run}
    return _out(args, _call("POST", _path(args.project, "canvases", "import"), body), _print_import)


# The parser


def _project(parser) -> None:
    parser.add_argument("--project", required=True, metavar="PROJECT", help="the project that enabled plugin canvas")


def _slug(parser) -> None:
    parser.add_argument("slug", metavar="SLUG", help="the canvas's slug")


def _edit_options(edit) -> None:
    edit.add_argument("--file", metavar="FILE", help='JSON {"changes": [...], "reason": "..."}; - for stdin')
    edit.add_argument("--claim", metavar="CODE", help="the claim to change, as CL-3; left out: a new claim")
    edit.add_argument("--base-rev", type=int, metavar="REV", help="the rev you read the claim at")
    edit.add_argument("--cell", choices=CELLS)
    edit.add_argument("--text", metavar="TEXT")
    edit.add_argument("--status", choices=STATUSES)
    edit.add_argument("--confidence", choices=CONFIDENCES)
    edit.add_argument("--position", type=int, metavar="N", help="its place in the cell, 0 first")
    edit.add_argument("--test-card", metavar="JSON", help="the whole test card, as a JSON object")
    edit.add_argument("--no-test-card", action="store_true", help="take the test card away")
    edit.add_argument(
        "--use-cases",
        metavar="CODES",
        help='the use cases of the SRS that realize it, comma-separated, as "UC ORD-1,UC ORD-2"; "" for none',
    )
    edit.add_argument(
        "--signal-keys", metavar="KEYS", help='the signal keys that measure it, comma-separated; "" for none'
    )
    edit.add_argument("--archive", action="store_true", help="archive the claim")
    edit.add_argument("--unarchive", action="store_true", help="bring an archived claim back")
    edit.add_argument("--reason", metavar="TEXT", help="why, kept with the rev")


def _register_evidence(csub) -> None:
    evidence = csub.add_parser("evidence", help="the evidence of a claim: add, withdraw, confirm")
    esub = evidence.add_subparsers(dest="evidence_command", required=True)
    added = esub.add_parser("add", help="a piece of evidence of a claim, as yours")
    _slug(added)
    added.add_argument("claim", metavar="CODE", help="the claim, as CL-3")
    _project(added)
    added.add_argument("--kind", required=True, choices=ADDED_KINDS)
    added.add_argument("--direction", required=True, choices=DIRECTIONS)
    added.add_argument("--summary", required=True, metavar="TEXT")
    added.add_argument("--url", metavar="URL")
    added.add_argument("--on", metavar="YYYY-MM-DD", help="the day it was taken")
    added.add_argument("--synthetic", action="store_true", help="it rests on simulated people")
    json_option(added, returns_object(*EVIDENCE_KEYS, schema="CanvasEvidence"))
    added.set_defaults(func=cmd_evidence_add)
    for verb, func, help_text in (
        ("withdraw", cmd_evidence_withdraw, "withdraw a piece of evidence, with why; it stays, marked"),
        ("confirm", cmd_evidence_confirm, "vouch for evidence an agent added, so it may validate the claim"),
    ):
        parser = esub.add_parser(verb, help=help_text)
        _slug(parser)
        parser.add_argument("claim", metavar="CODE", help="the claim, as CL-3")
        parser.add_argument("id", type=int, metavar="ID", help="the evidence's id, as show --claim prints it")
        _project(parser)
        if verb == "withdraw":
            parser.add_argument("--reason", required=True, metavar="TEXT")
        json_option(parser, returns_object(*EVIDENCE_KEYS, schema="CanvasEvidence"))
        parser.set_defaults(func=func)


def _register_proposals(csub) -> None:
    """``proposal list|accept|reject`` and ``brainstorm``: what @strategist and the hub propose."""
    proposal = csub.add_parser("proposal", help="what an agent or the hub proposed: list, accept, reject")
    psub = proposal.add_subparsers(dest="proposal_command", required=True)
    listed = psub.add_parser("list", help="the proposals of a canvas, newest first; the open ones by default")
    _slug(listed)
    _project(listed)
    listed.add_argument("--status", action="append", choices=PROPOSAL_STATUSES, help="these statuses; repeatable")
    json_option(listed, returns_array(*PROPOSAL_KEYS, schema="CanvasProposal"))
    listed.set_defaults(func=cmd_proposal_list)
    for verb, func, help_text in (
        ("accept", cmd_proposal_accept, "write an open proposal as your own rev"),
        ("reject", cmd_proposal_reject, "close an open proposal without writing it"),
    ):
        parser = psub.add_parser(verb, help=help_text)
        _slug(parser)
        parser.add_argument("id", type=int, metavar="ID", help="the proposal's id, as proposal list prints it")
        _project(parser)
        if verb == "reject":
            parser.add_argument("--reason", metavar="TEXT", help="why, kept with the proposal")
        json_option(parser, returns_object(*PROPOSAL_KEYS, schema="CanvasProposal"))
        parser.set_defaults(func=func)

    brainstorm = csub.add_parser("brainstorm", help="hand @strategist a brainstorm of a canvas; it only proposes")
    _slug(brainstorm)
    _project(brainstorm)
    brainstorm.add_argument("--focus", metavar="TEXT", help="what to think about")
    brainstorm.add_argument("--cell", action="append", choices=CELLS, help="one cell to look at; repeatable")
    json_option(brainstorm, returns_object(*BRAINSTORM_KEYS, schema="CanvasBrainstorm"))
    brainstorm.set_defaults(func=cmd_brainstorm)


def register_canvas(hsub) -> None:
    canvas = hsub.add_parser("canvas", help="Lean Canvases of a project: claims, revs, evidence (plugin canvas)")
    csub = canvas.add_subparsers(dest="canvas_command", required=True)

    listed = csub.add_parser("list", help="the canvases of the project, with their claims by status")
    _project(listed)
    listed.add_argument("--archived", action="store_true", help="archived canvases too")
    json_option(listed, returns_array(*SUMMARY_KEYS, schema="CanvasSummary"))
    listed.set_defaults(func=cmd_list)

    show = csub.add_parser("show", help="a canvas by cell, or one claim whole with its evidence and revs")
    _slug(show)
    _project(show)
    show.add_argument("--claim", metavar="CODE", help="one claim whole, as CL-3")
    show.add_argument("--archived", action="store_true", help="archived claims too")
    claim_output = returns_object(*CLAIM_DETAIL_KEYS, schema="CanvasClaimDetail")
    json_option(show, returns_object(*DETAIL_KEYS, schema="CanvasDetail", variants=(("--claim", claim_output),)))
    show.set_defaults(func=cmd_show)

    create = csub.add_parser("create", help="a new canvas of a product, with nine empty cells")
    _slug(create)
    _project(create)
    create.add_argument("--name", required=True, metavar="TEXT", help="the product's name")
    create.add_argument("--description", metavar="TEXT")
    json_option(create, returns_object(*DETAIL_KEYS, schema="CanvasDetail"))
    create.set_defaults(func=cmd_create)

    edit = csub.add_parser("edit", help="change claims at the rev you read them, or make new ones; all or nothing")
    _slug(edit)
    _project(edit)
    _edit_options(edit)
    json_option(edit, returns_object(*EDIT_KEYS, schema="CanvasEditResult"))
    edit.set_defaults(func=cmd_edit)

    undo = csub.add_parser("undo", help="take back a claim's current rev")
    _slug(undo)
    undo.add_argument("claim", metavar="CODE", help="the claim, as CL-3")
    _project(undo)
    undo.add_argument("--rev", type=int, metavar="REV", help="the rev to take back; the claim's current when left out")
    undo.add_argument("--reason", metavar="TEXT", help="why, kept with the rev")
    json_option(undo, returns_object(*EDIT_KEYS, schema="CanvasEditResult"))
    undo.set_defaults(func=cmd_undo)

    _register_evidence(csub)
    _register_proposals(csub)

    exported = csub.add_parser("export", help="a canvas as evo-canvas/1 JSON")
    _slug(exported)
    _project(exported)
    exported.add_argument("--output", metavar="FILE", help="write it to FILE instead of stdout")
    exported.set_defaults(func=cmd_export)

    imported = csub.add_parser("import", help="evo-canvas/1 JSON, keeping the claims' codes; all or nothing")
    imported.add_argument("file", metavar="FILE", help="the JSON file; - for stdin")
    _project(imported)
    imported.add_argument("--dry-run", action="store_true", help="print what it would import and write nothing")
    json_option(imported, returns_object(*IMPORT_KEYS, schema="CanvasImportResult"))
    imported.set_defaults(func=cmd_import)
