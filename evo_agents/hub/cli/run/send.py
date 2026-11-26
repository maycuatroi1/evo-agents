"""``evo-agents hub run send RUN [TEXT] [--attach FILE]...``, for ``evo_agents.hub.cli.run``: a message for a run's
agent, which its worker hands over (POST /v1/projects/{p}/runs/{id}/messages); to an author run, the owner's reply in
its chat. ``--attach`` uploads each file first (``evo_agents.hub.attach_cli``), and the message names them; with one,
TEXT may be left out. ``-`` as TEXT reads it from stdin. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import argparse
import sys

from evo_agents.clikit import Arg, Result, Text, UsageError, returns_object
from evo_agents.hub import attach_cli
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext
from evo_agents.hub.cli.run.options import RUN_ID
from evo_agents.hub.cli.run.resources import Runs

MESSAGE_KEYS = ("id", "run_id", "seq", "text", "sent_by", "created_at", "delivered_at", "attachments")


def message_body(args: argparse.Namespace) -> dict:
    """The message ``TEXT`` gives, read from stdin for ``-``; UsageError for none without a file attached."""
    text = sys.stdin.read() if args.text == "-" else args.text or ""
    if not text.strip() and not args.attach:
        raise UsageError("give the message as TEXT (- reads it from stdin), or attach a file with --attach FILE")
    return {"text": text}


def sent_text(message: dict, run_id: int) -> str:
    """What ``send`` says of the message the hub took for run ``run_id``: sent, or queued in a run resuming it."""
    sent = attach_cli.describe(message.get("attachments"))
    files_said = f" with {sent}" if sent else ""
    if message["run_id"] != run_id:
        return (
            f"Run #{run_id} was parked: the reply queued run #{message['run_id']}, which resumes it in its session "
            f"on the same worker, and its agent gets message #{message['id']}{files_said} (event {message['seq']})."
        )
    return (
        f"Sent message #{message['id']}{files_said} to run #{message['run_id']} (event {message['seq']}); "
        "its worker hands it to the agent."
    )


class RunSend(HubCommand):
    name = "send"
    help = (
        "leave a message for a run's agent, which its worker hands over; to an author run, your reply in its "
        "chat (owner only)"
    )
    arguments = (
        RUN_ID,
        PROJECT,
        Arg(
            "text",
            metavar="TEXT",
            nargs="?",
            help="at most 8 KiB; - reads it from stdin; may be left out with --attach",
        ),
        Arg("--attach", action="append", metavar="FILE", help=attach_cli.ATTACH_HELP),
    )
    output = returns_object(*MESSAGE_KEYS, schema="Message")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = message_body(args)
        try:
            files = attach_cli.checked(args.attach or [])
        except (ValueError, OSError) as exc:
            raise UsageError(str(exc)) from None
        runs = Runs.of(ctx)
        if files:
            body["attachment_ids"] = attach_cli.upload(runs.hub, runs.path(args.run), files)
        message = runs.send(args.run, body)
        return Result(message, Text(sent_text(message, args.run)))
