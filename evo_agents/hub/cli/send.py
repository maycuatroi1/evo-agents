"""``evo-agents hub send FILE... | --queued``: the command over ``evo_agents.hub.local_send``, which sends the outputs
of local sessions to their runs on the hub. The evo-hub plugin's commands register it after its hooks
(``evo_agents.hub.hooks.register_hooks``).
"""

from __future__ import annotations

import sys

from evo_agents.clikit.json_output import json_option, returns_object
from evo_agents.hub import local_outputs, local_sync
from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.client import HubError
from evo_agents.hub.local_send import SEND_KEYS, files_to_send, send_files, send_json, send_queued, session_of


@client_command
def cmd_send(args) -> int:
    if args.queued:
        return send_queued(args.wait)
    if not args.files:
        raise HubError("name the files to send, or pass --queued")
    if not local_sync.enabled():
        raise HubError(
            "local sessions are not synced here: EVO_HUB_LOCAL_SYNC turns them off, and the agent of a worker run "
            "sends its files with `evo-agents worker output`"
        )
    hub, _ = signed_in()
    files = files_to_send(args.files, local_outputs.caption_of(args.caption))
    session_id, rec = session_of(hub, args.session)
    sent, waiting, report = send_files(hub, session_id, files)
    if args.json:
        print_json(send_json(rec["project"], rec["run_id"], session_id, sent, waiting))
        return 1 if report.dropped else 0
    for output in sent:
        print(f"sent {output['name']} as output #{output['id']} of local run #{rec['run_id']} ({rec['project']})")
    if waiting:
        why = f": {report.stopped}" if report.stopped else ""
        print(f"queued {', '.join(waiting)} for local run #{rec['run_id']}{why}; the evo-hub hooks send it later")
    for dropped in report.dropped:
        print(f"not sent: {dropped}", file=sys.stderr)
    return 1 if report.dropped else 0


def register_send(hsub) -> None:
    send = hsub.add_parser(
        "send",
        help="send files to the hub as outputs of this Claude Code session's local run, as SendUserFile does in "
        "Claude Desktop; --queued sends what the evo-hub hooks queued, as they do in the background",
    )
    send.add_argument("files", nargs="*", metavar="FILE", help="files to send, at most 200 MiB each")
    send.add_argument("--caption", help="what the files show, for the people who read the run")
    send.add_argument(
        "--session",
        metavar="ID",
        help="the Claude Code session (default: CLAUDE_CODE_SESSION_ID, else the newest here)",
    )
    send.add_argument("--queued", action="store_true", help="send what the evo-hub hooks queued; they run it")
    send.add_argument(
        "--wait",
        type=int,
        default=0,
        choices=range(0, 61),
        metavar="SECONDS",
        help="with --queued, wait first: 0 to 60",
    )
    json_option(send, returns_object(*SEND_KEYS))
    send.set_defaults(func=cmd_send)
