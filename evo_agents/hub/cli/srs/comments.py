"""``hub srs comments list|add|reply|resolve|reopen``: threads of comments on a document, one on a passage of an
item with ``--item`` and ``--quote``; ``@ba`` in a comment asks @ba to answer in its thread."""

from __future__ import annotations

from evo_agents.clikit import Arg, Group, Result, Text, returns_array, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.features import answer
from evo_agents.hub.cli.srs.keys import COMMENT_KEYS, PROJECT, SLUG
from evo_agents.hub.cli.srs.resources import Documents
from evo_agents.hub.cli.srs.views import threads_view

COMMENT_OUTPUT = returns_object(*COMMENT_KEYS, schema="SrsComment")
COMMENT = Arg("comment", type=int, metavar="COMMENT", help="the id of a comment of the thread")
BODY = Arg("body", metavar="TEXT")


def written_view(slug: str, found: dict) -> Text:
    root = found["parent_id"] or found["id"]
    asked = f"; @ba answers it in run #{found['ask_run_id']}" if found.get("ask_run_id") else ""
    return Text(f"Comment {found['id']} in thread {root} of {slug}{asked}.")


def settled_view(slug: str, found: dict) -> Text:
    root = found["parent_id"] or found["id"]
    return Text(f"Thread {root} of {slug} is {'resolved' if found['resolved_by'] else 'open'}.")


class CommentList(HubCommand):
    name = "list"
    help = "the threads of a document, of one item, open or resolved"
    arguments = (
        SLUG,
        PROJECT,
        Arg("--item", metavar="CODE", help="the threads of this item and the items under it"),
        Arg("--status", choices=("open", "resolved"), help="only the threads open or resolved"),
        Arg("--version", type=int, metavar="N", help="read the passages in this version (default: latest)"),
    )
    output = returns_array(*COMMENT_KEYS, schema="SrsComment")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        query = {"item": args.item, "status": args.status, "version": args.version}
        found = Documents.of(ctx).comments(args.slug, query)
        return answer(ctx, found, lambda comments: threads_view(args.slug, comments or []))


class CommentAdd(HubCommand):
    name = "add"
    help = "a new thread; @ba in the text asks @ba to answer in it"
    arguments = (
        SLUG,
        BODY,
        PROJECT,
        Arg("--item", metavar="CODE", help="the item it is on"),
        Arg("--quote", metavar="TEXT", help="the passage of the item it is on (needs --item)"),
        Arg("--field", metavar="NAME", help="the part of the item the passage is in"),
        Arg("--version", type=int, metavar="N", help="the version it is on"),
    )
    output = COMMENT_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body: dict = {"body": args.body}
        body |= {
            name: getattr(args, name)
            for name in ("item", "version", "quote", "field")
            if getattr(args, name) is not None
        }
        found = Documents.of(ctx).comment(args.slug, body)
        return answer(ctx, found, lambda done: written_view(args.slug, done))


class CommentReply(HubCommand):
    name = "reply"
    help = "answer a thread, by the id of any comment of it"
    arguments = (SLUG, COMMENT, BODY, PROJECT)
    output = COMMENT_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Documents.of(ctx).comment(args.slug, {"body": args.body, "parent_id": args.comment})
        return answer(ctx, found, lambda done: written_view(args.slug, done))


class CommentResolve(HubCommand):
    name = "resolve"
    help = "resolve a thread (a member, never an agent)"
    arguments = (SLUG, COMMENT, PROJECT)
    output = COMMENT_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Documents.of(ctx).settle_thread(args.slug, args.comment, "resolve")
        return answer(ctx, found, lambda done: settled_view(args.slug, done))


class CommentReopen(HubCommand):
    name = "reopen"
    help = "open a resolved thread again"
    arguments = (SLUG, COMMENT, PROJECT)
    output = COMMENT_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Documents.of(ctx).settle_thread(args.slug, args.comment, "reopen")
        return answer(ctx, found, lambda done: settled_view(args.slug, done))


COMMENTS = Group(
    "comments",
    "threads of comments: list, add, reply, resolve, reopen",
    [CommentList(), CommentAdd(), CommentReply(), CommentResolve(), CommentReopen()],
)
