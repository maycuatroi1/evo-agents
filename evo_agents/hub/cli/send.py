"""``evo-agents hub send FILE... | --queued``: the command over ``evo_agents.hub.local_send``, which sends the outputs
of local sessions to their runs on the hub. The evo-hub plugin's commands attach it after its hooks
(``evo_agents.hub.cli.hook.HOOK``).

It is a class of ``evo_agents.clikit``: ``--queued`` is what the hooks start in the background, and prints nothing,
with ``--json`` or without, so :class:`Send` runs it before the command's skeleton.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Result, Stderr, Text, Views, returns_object
from evo_agents.hub import local_outputs, local_sync
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.client import HubError
from evo_agents.hub.local_send import SEND_KEYS, files_to_send, send_files, send_json, send_queued, session_of


class Send(HubCommand):
    name = "send"
    help = (
        "send files to the hub as outputs of this Claude Code session's local run, as SendUserFile does in "
        "Claude Desktop; --queued sends what the evo-hub hooks queued, as they do in the background"
    )
    arguments = (
        Arg("files", nargs="*", metavar="FILE", help="files to send, at most 200 MiB each"),
        Arg("--caption", help="what the files show, for the people who read the run"),
        Arg(
            "--session",
            metavar="ID",
            help="the Claude Code session (default: CLAUDE_CODE_SESSION_ID, else the newest here)",
        ),
        Arg("--queued", action="store_true", help="send what the evo-hub hooks queued; they run it"),
        Arg(
            "--wait",
            type=int,
            default=0,
            choices=range(0, 61),
            metavar="SECONDS",
            help="with --queued, wait first: 0 to 60",
        ),
    )
    output = returns_object(*SEND_KEYS)

    def __call__(self, args: argparse.Namespace) -> int:
        if not args.queued:
            return super().__call__(args)
        try:
            return send_queued(args.wait)
        except HubError as exc:
            return self.failed(exc) or 0

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        if not args.files:
            raise HubError("name the files to send, or pass --queued")
        if not local_sync.enabled():
            raise HubError(
                "local sessions are not synced here: EVO_HUB_LOCAL_SYNC turns them off, and the agent of a worker run "
                "sends its files with `evo-agents worker output`"
            )
        hub = ctx.hub
        files = files_to_send(args.files, local_outputs.caption_of(args.caption))
        session_id, rec = session_of(hub, args.session)
        sent, waiting, report = send_files(hub, session_id, files)
        data = send_json(rec["project"], rec["run_id"], session_id, sent, waiting)
        lines = [
            f"sent {output['name']} as output #{output['id']} of local run #{rec['run_id']} ({rec['project']})"
            for output in sent
        ]
        if waiting:
            why = f": {report.stopped}" if report.stopped else ""
            lines.append(
                f"queued {', '.join(waiting)} for local run #{rec['run_id']}{why}; the evo-hub hooks send it later"
            )
        dropped = Stderr(Text(*(f"not sent: {item}" for item in report.dropped)))
        return Result(data, Views(Text(*lines), dropped), 1 if report.dropped else 0)


SEND = Send()
