"""``hub canvas list|show|create|edit|undo``: the canvases of a project and the claims in their cells."""

from __future__ import annotations

import argparse
import json

from evo_agents.clikit import Arg, Result, Table, UsageError, returns_array, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext, when
from evo_agents.hub.cli.canvas.files import read_json
from evo_agents.hub.cli.canvas.keys import (
    CELLS,
    CLAIM,
    CLAIM_DETAIL_KEYS,
    CONFIDENCES,
    DELIVERIES,
    DETAIL_KEYS,
    EDIT_KEYS,
    PROJECT,
    REASON,
    SLUG,
    STATUSES,
    SUMMARY_KEYS,
)
from evo_agents.hub.cli.canvas.resources import Canvases
from evo_agents.hub.cli.canvas.views import canvas_view, claim_view, edit_view
from evo_agents.hub.cli.features import answer
from evo_agents.hub.client import HubError

ARCHIVED_CANVASES = Arg("--archived", action="store_true", help="archived canvases too")
ARCHIVED_CLAIMS = Arg("--archived", action="store_true", help="archived claims too")


LIST_HEADER = ("CANVAS", "CLAIMS", "STATUSES", "UPDATED", "", "PRODUCT")


def rows(found: list[dict]) -> list[tuple]:
    return [
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


class CanvasList(HubCommand):
    name = "list"
    help = "the canvases of the project, with their claims by status"
    arguments = (PROJECT, ARCHIVED_CANVASES)
    output = returns_array(*SUMMARY_KEYS, schema="CanvasSummary")

    def run(self, ctx: HubContext) -> Result:
        empty = f"No canvas in project {ctx.args.project}."
        return answer(
            ctx, Canvases.of(ctx).list(ctx.args.archived), lambda found: Table(LIST_HEADER, rows(found), empty)
        )


class CanvasShow(HubCommand):
    name = "show"
    help = "a canvas by cell, or one claim whole with its evidence and revs"
    arguments = (SLUG, PROJECT, Arg("--claim", metavar="CODE", help="one claim whole, as CL-3"), ARCHIVED_CLAIMS)
    output = returns_object(
        *DETAIL_KEYS,
        schema="CanvasDetail",
        variants=(("--claim", returns_object(*CLAIM_DETAIL_KEYS, schema="CanvasClaimDetail")),),
    )

    def run(self, ctx: HubContext) -> Result:
        canvases, args = Canvases.of(ctx), ctx.args
        if args.claim:
            return answer(ctx, canvases.claim(args.slug, args.claim), claim_view)
        return answer(ctx, canvases.get(args.slug, args.archived), canvas_view)


class CanvasCreate(HubCommand):
    name = "create"
    help = "a new canvas of a product, with nine empty cells"
    arguments = (
        SLUG,
        PROJECT,
        Arg("--name", required=True, metavar="TEXT", help="the product's name"),
        Arg("--description", metavar="TEXT"),
    )
    output = returns_object(*DETAIL_KEYS, schema="CanvasDetail")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"slug": args.slug, "name": args.name, "description": args.description or ""}
        return answer(ctx, Canvases.of(ctx).create(body), canvas_view)


def test_card(args: argparse.Namespace) -> tuple[bool, dict | None]:
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


def listed(given: str | None) -> list[str] | None:
    """The comma-separated list an option gives, each item trimmed; None when the option is left out."""
    if given is None:
        return None
    return [item.strip() for item in given.split(",") if item.strip()]


def change_of(args: argparse.Namespace) -> dict:
    """The one change the options of ``edit`` give."""
    change = {name: getattr(args, name) for name in ("cell", "text", "status", "confidence", "position", "delivery")}
    change |= {name: listed(getattr(args, name)) for name in ("use_cases", "signal_keys", "problems", "opportunities")}
    change = {name: value for name, value in change.items() if value is not None}
    given, card = test_card(args)
    if given:
        change["test_card"] = card
    if args.archive or args.unarchive:
        change["archived"] = bool(args.archive)
    if args.claim:
        if args.base_rev is None:
            raise HubError("--claim takes --base-rev, the rev you read it at: see `hub canvas show --claim`")
        change.update(claim=args.claim, base_rev=args.base_rev)
    return change


EDIT_OPTIONS = (
    Arg("--file", metavar="FILE", help='JSON {"changes": [...], "reason": "..."}; - for stdin'),
    Arg("--claim", metavar="CODE", help="the claim to change, as CL-3; left out: a new claim"),
    Arg("--base-rev", type=int, metavar="REV", help="the rev you read the claim at"),
    Arg("--cell", choices=CELLS),
    Arg("--text", metavar="TEXT"),
    Arg("--status", choices=STATUSES),
    Arg("--confidence", choices=CONFIDENCES),
    Arg("--delivery", choices=DELIVERIES, help="code (the build path) or action (a task of the plugin tasks)"),
    Arg("--position", type=int, metavar="N", help="its place in the cell, 0 first"),
    Arg("--test-card", metavar="JSON", help="the whole test card, as a JSON object"),
    Arg("--no-test-card", action="store_true", help="take the test card away"),
    Arg(
        "--use-cases",
        metavar="CODES",
        help='the use cases of the SRS that realize it, comma-separated, as "UC ORD-1,UC ORD-2"; "" for none',
    ),
    Arg("--signal-keys", metavar="KEYS", help='the signal keys that measure it, comma-separated; "" for none'),
    Arg(
        "--problems",
        metavar="CODES",
        help='the problems of the project it answers, comma-separated, as "P-1,P-3"; "" for none',
    ),
    Arg(
        "--opportunities",
        metavar="CODES",
        help='the opportunities of the project it links, comma-separated, as "O-1,O-3"; "" for none',
    ),
    Arg("--archive", action="store_true", help="archive the claim"),
    Arg("--unarchive", action="store_true", help="bring an archived claim back"),
    REASON,
)


class CanvasEdit(HubCommand):
    name = "edit"
    help = "change claims at the rev you read them, or make new ones; all or nothing"
    arguments = (SLUG, PROJECT, *EDIT_OPTIONS)
    output = returns_object(*EDIT_KEYS, schema="CanvasEditResult")

    def check(self, args: argparse.Namespace) -> None:
        if args.archive and args.unarchive or args.test_card is not None and args.no_test_card:
            raise UsageError("--archive and --unarchive, --test-card and --no-test-card go apart")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        if args.file:
            body = read_json(args.file)
            body = {"changes": body} if isinstance(body, list) else body
            if args.reason is not None:
                body = {**body, "reason": args.reason}
        else:
            body = {"changes": [change_of(args)], "reason": args.reason or ""}
        return answer(ctx, Canvases.of(ctx).edit(args.slug, body), edit_view)


class CanvasUndo(HubCommand):
    name = "undo"
    help = "take back a claim's current rev"
    arguments = (
        SLUG,
        CLAIM,
        PROJECT,
        Arg("--rev", type=int, metavar="REV", help="the rev to take back; the claim's current when left out"),
        REASON,
    )
    output = returns_object(*EDIT_KEYS, schema="CanvasEditResult")

    def run(self, ctx: HubContext) -> Result:
        canvases, args = Canvases.of(ctx), ctx.args
        rev = args.rev
        if rev is None:
            rev = canvases.claim(args.slug, args.claim)["rev"]
        body = {"revs": [{"claim": args.claim, "rev": rev}], "reason": args.reason or ""}
        return answer(ctx, canvases.undo(args.slug, body), edit_view)
