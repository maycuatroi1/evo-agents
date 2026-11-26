"""``hub srs submit|approve|queue|reviews|withdraw``: the review of a version, or of its use cases one by one."""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Result, Text, UsageError, View, Views, returns_array, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.features import answer
from evo_agents.hub.cli.srs.keys import (
    NUMBER,
    OPTIONAL_NUMBER,
    PROJECT,
    QUEUE_KEYS,
    ROLES,
    SLUG,
    SUBMITTED_KEYS,
    USE_CASE_VERSION_KEYS,
    VERSION_KEYS,
)
from evo_agents.hub.cli.srs.resources import Documents, Web
from evo_agents.hub.cli.srs.views import LinkOf, case_name, queue_views, use_cases_view, version_view
from evo_agents.hub.client import HubError


def latest_number(documents: Documents, slug: str) -> int:
    found = documents.latest(slug)
    if not found.get("latest_version"):
        raise HubError(f"{slug} has no version yet")
    return found["latest_version"]


def submitted_view(slug: str, found: dict, web: LinkOf) -> Text:
    case = found["use_case"]
    run = found.get("use_case_review_run")
    ba = f"; @ba reviews it in run #{run['id']}" if run else ""
    return Text(
        f"Sent {case_name(case)} of {slug} to review on its own, from version {found['number']}{ba}.",
        f"  review: {web(case.get('link') or '')}",
    )


class Submit(HubCommand):
    name = "submit"
    help = "send a draft to review, or one use case with --item"
    arguments = (
        SLUG,
        OPTIONAL_NUMBER,
        PROJECT,
        Arg("--item", metavar="CODE", help="send this use case alone: the revision it has in the latest version"),
    )
    output = returns_object(
        *USE_CASE_VERSION_KEYS,
        schema="SrsVersionCases",
        variants=(("--item", returns_object(*SUBMITTED_KEYS, schema="SrsUseCaseSubmitted")),),
    )

    def check(self, args: argparse.Namespace) -> None:
        if args.item and args.number is not None:
            raise UsageError("--item sends a use case of the latest version: leave N out")
        if not args.item and args.number is None:
            raise UsageError("submit needs N, the version to send, or --item CODE, a use case")

    def run(self, ctx: HubContext) -> Result:
        documents, args, web = Documents.of(ctx), ctx.args, Web(ctx)
        if args.item:
            found = documents.submit_use_case(args.slug, args.item)
            return answer(ctx, found, lambda done: submitted_view(args.slug, done, web))
        found = documents.submit(args.slug, args.number)
        return answer(ctx, found, lambda done: Views(version_view(args.slug, done), use_cases_view(done, web)))


class Approve(HubCommand):
    name = "approve"
    help = "approve a version in review, or send it back with --request-changes"
    arguments = (
        SLUG,
        OPTIONAL_NUMBER,
        PROJECT,
        Arg("--request-changes", action="store_true", help="send it back to draft instead"),
        Arg("--comment", metavar="TEXT", help="what the review says; needed with --request-changes"),
        Arg("--role", choices=ROLES, help="the role the review counts for (one you hold)"),
        Arg(
            "--item",
            action="append",
            metavar="CODE",
            help="review this use case only, by code, alias or key (repeatable)",
        ),
    )
    output = returns_object(
        *VERSION_KEYS,
        schema="SrsVersion",
        variants=(("--item", returns_object(*USE_CASE_VERSION_KEYS, schema="SrsVersionCases")),),
    )

    def check(self, args: argparse.Namespace) -> None:
        if args.request_changes and not args.comment:
            raise UsageError("--request-changes needs --comment, the changes the author is to make")
        if args.number is None and not args.item:
            raise UsageError("approve needs N, the version, unless --item names use cases of the latest")

    def run(self, ctx: HubContext) -> Result:
        documents, args = Documents.of(ctx), ctx.args
        body = {"decision": "request_changes" if args.request_changes else "approve", "body": args.comment or ""}
        if args.role:
            body["role"] = args.role
        number = args.number if args.number is not None else latest_number(documents, args.slug)
        found = {} if args.item else documents.review(args.slug, number, body)
        for ref in args.item or ():  # one by one, in order; the first the hub refuses stops the rest
            found = documents.review_item(args.slug, number, ref, body)
        return answer(ctx, found, lambda done: self.view(args.slug, done, bool(args.item), Web(ctx)))

    @staticmethod
    def view(slug: str, found: dict, cases: bool, web: LinkOf) -> View:
        return Views(version_view(slug, found), use_cases_view(found, web)) if cases else version_view(slug, found)


class Queue(HubCommand):
    name = "queue"
    help = "what waits for review in the project, use case by use case, with links"
    arguments = (PROJECT,)
    output = returns_array(*QUEUE_KEYS, schema="SrsQueuedVersion")

    def run(self, ctx: HubContext) -> Result:
        project, web = ctx.args.project, Web(ctx)
        return answer(
            ctx,
            Documents.of(ctx).queue(),
            lambda found: Views(*queue_views(found, web)) if found else Text(f"Nothing waits for review in {project}."),
        )


class Reviews(HubCommand):
    name = "reviews"
    help = "a version's state and the use cases it needs reviewed"
    arguments = (SLUG, OPTIONAL_NUMBER, PROJECT)
    output = returns_object(*USE_CASE_VERSION_KEYS, schema="SrsVersionCases")

    def run(self, ctx: HubContext) -> Result:
        documents, args = Documents.of(ctx), ctx.args
        number = args.number if args.number is not None else latest_number(documents, args.slug)
        found = documents.version(args.slug, number)
        if args.json:
            return Result({key: found[key] for key in USE_CASE_VERSION_KEYS})
        return Result(found, Views(version_view(args.slug, found), use_cases_view(found, Web(ctx))))


class Withdraw(HubCommand):
    name = "withdraw"
    help = "withdraw a version in review, for good (its author or an admin)"
    arguments = (SLUG, NUMBER, PROJECT, Arg("--comment", metavar="TEXT", help="why, kept as a comment on the version"))
    output = returns_object(*VERSION_KEYS, schema="SrsVersion")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"comment": args.comment} if args.comment else {}
        found = Documents.of(ctx).withdraw(args.slug, args.number, body)
        return answer(ctx, found, lambda done: version_view(args.slug, done))
