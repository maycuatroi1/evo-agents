"""``evo-agents worker curator answer``: what the agent of a proposal's thread does on its own for the owner
(``evo_agents.hub.server.curator_threads``; the skill answering-curator-proposals of the evo-hub plugin says how it
decides).

- ``answer PROPOSAL accept|reject|defer --note TEXT [--days N]``: answer the proposal of the thread for the owner, an
  admin of the project, as their answer on the web does: accepting one of a tier the charter plans makes the Curator's
  plan. ``--days`` goes with defer (7 by default).

It reads the run from EVO_RUN_ID like ``worker chat`` (``chat_cli``) and is for a chat run only. The hub refuses what is
outside the frame (another proposal than the thread's, an owner who is no admin, a proposal answered already), with the
reason, and the command prints it on stderr and exits 1. A success is one line, and the hub posts the ``Decision:`` line
in the thread. TEXT ``-`` reads stdin. No ``--json``: this module may not import the hub's contract.
"""

from __future__ import annotations

import sys

from evo_agents.worker.chat_cli import agent_run, call, chat_command

ACTIONS = ("accept", "reject", "defer")  # review.ANSWERS, which the hub checks


def _text(value: str) -> str:
    return sys.stdin.read().strip() if value == "-" else value


@chat_command
def cmd_answer(args) -> int:
    run = agent_run("curator answer")
    body = {"proposal_id": args.proposal, "action": args.action, "note": _text(args.note)}
    if args.days is not None:
        body["defer_days"] = args.days
    answer = call(run, "curator/answer", body)
    proposal = answer["proposal"]
    change = proposal.get("change") or {}
    made = f"; change #{change['id']} is {change['state']}" if change else ""
    print(
        f"Proposal #{proposal['id']} is {proposal['state']}{made}; the Decision line is message #{answer['message_id']}."
    )
    return 0


def register_curator(wsub) -> None:
    """``evo-agents worker curator`` and its commands, under the parser ``wsub`` of ``evo-agents worker``."""
    curator = wsub.add_parser(
        "curator", help="inside a proposal's thread: answer the Curator's proposal for its owner, inside the frame"
    )
    csub = curator.add_subparsers(dest="curator_command", required=True)
    answer = csub.add_parser("answer", help="accept, reject or defer the proposal of the thread for its owner")
    answer.add_argument("proposal", type=int, metavar="PROPOSAL", help="the proposal's id, the thread's")
    answer.add_argument("action", choices=ACTIONS, help="the answer")
    answer.add_argument("--note", required=True, metavar="TEXT", help="why, in one line (- for stdin)")
    answer.add_argument("--days", type=int, metavar="N", help="with defer: how many days, 1 to 90 (7 by default)")
    answer.set_defaults(func=cmd_answer)
