"""``evo-agents hub run send RUN [TEXT] [--attach FILE]...``, for ``evo_agents.hub.run_cli``: a message for a run's
agent, which its worker hands over (POST /v1/projects/{p}/runs/{id}/messages); to an author run, the owner's reply in
its chat. ``--attach`` uploads each file first (``evo_agents.hub.attach_cli``), and the message names them; with one,
TEXT may be left out. ``-`` as TEXT reads it from stdin. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys

from evo_agents.hub import attach_cli
from evo_agents.hub.cli_client import client_command, print_json, project_path, signed_in
from evo_agents.hub.contract import json_option, returns_object

EXIT_USAGE = 2
MESSAGE_KEYS = ("id", "run_id", "seq", "text", "sent_by", "created_at", "delivered_at", "attachments")


def _body(args) -> dict:
    text = sys.stdin.read() if args.text == "-" else args.text or ""
    if not text.strip() and not args.attach:
        raise ValueError("give the message as TEXT (- reads it from stdin), or attach a file with --attach FILE")
    return {"text": text}


def cmd_send(args, project) -> int:
    try:
        body, files = _body(args), attach_cli.checked(args.attach or [])
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    hub, _ = signed_in()
    run_path = f"{project_path(project(args))}/runs/{args.run}"
    if files:
        body["attachment_ids"] = attach_cli.upload(hub, run_path, files)
    message = hub.call("POST", f"{run_path}/messages", body)
    if args.json:
        print_json(message)
        return 0
    sent = attach_cli.describe(message.get("attachments"))
    files_said = f" with {sent}" if sent else ""
    if message["run_id"] != args.run:
        print(
            f"Run #{args.run} was parked: the reply queued run #{message['run_id']}, which resumes it in its session "
            f"on the same worker, and its agent gets message #{message['id']}{files_said} (event {message['seq']})."
        )
        return 0
    print(
        f"Sent message #{message['id']}{files_said} to run #{message['run_id']} (event {message['seq']}); "
        "its worker hands it to the agent."
    )
    return 0


def register_send(rsub, with_run, project) -> None:
    send = rsub.add_parser(
        "send",
        help="leave a message for a run's agent, which its worker hands over; to an author run, your reply in its "
        "chat (owner only)",
    )
    with_run(send)
    send.add_argument(
        "text", metavar="TEXT", nargs="?", help="at most 8 KiB; - reads it from stdin; may be left out with --attach"
    )
    send.add_argument("--attach", action="append", metavar="FILE", help=attach_cli.ATTACH_HELP)
    json_option(send, returns_object(*MESSAGE_KEYS, schema="Message"))
    send.set_defaults(func=client_command(lambda args: cmd_send(args, project)))
