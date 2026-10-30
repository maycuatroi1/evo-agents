"""``evo-agents worker curator answer|merge``: what the agent of a proposal's thread does on its own for the owner
(``evo_agents.hub.server.curator_threads``; the skill answering-curator-proposals of the evo-hub plugin says how it
decides).

- ``answer PROPOSAL accept|reject|defer --note TEXT [--days N]``: answer the proposal of the thread for the owner, an
  admin of the project, as their answer on the web does: accepting one of a tier the charter plans makes the Curator's
  plan. ``--days`` goes with defer (7 by default).
- ``merge CHANGE``: merge, for the owner, the pull request of a change of the thread's proposal that the hub left open,
  at the head its Judge passed, once the agent has read its diff there. The hub merges it only when the Judge passed
  it, its head did not move, every sign of its files is one of a protected path, it is open into the default branch
  and mergeable, K5 lets it and the night shift is not paused.

It reads the run from EVO_RUN_ID like ``worker chat`` (``chat_cli``) and is for a chat run only. The hub refuses what is
outside the frame (another proposal than the thread's, an owner who is no admin, a proposal answered already, a change
the merge does not allow), with the reason, and the command prints it on stderr and exits 1. A success is one line, and
the hub posts the ``Decision:`` line in the thread. TEXT ``-`` reads stdin. No ``--json``: this module may not import the
hub's contract.
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


@chat_command
def cmd_merge(args) -> int:
    run = agent_run("curator merge")
    merged = call(run, "curator/merge", {"change_id": args.change})
    change = merged["change"]
    print(
        f"Change #{change['id']} is {change['state']}: pull request {change['pr_url']} merged as "
        f"{change['merge_sha']}; the Decision line is message #{merged['message_id']}."
    )
    return 0


def register_curator(wsub) -> None:
    """``evo-agents worker curator`` and its commands, under the parser ``wsub`` of ``evo-agents worker``."""
    curator = wsub.add_parser(
        "curator",
        help="inside a proposal's thread: answer the Curator's proposal, or merge its change, for its owner",
    )
    csub = curator.add_subparsers(dest="curator_command", required=True)
    answer = csub.add_parser("answer", help="accept, reject or defer the proposal of the thread for its owner")
    answer.add_argument("proposal", type=int, metavar="PROPOSAL", help="the proposal's id, the thread's")
    answer.add_argument("action", choices=ACTIONS, help="the answer")
    answer.add_argument("--note", required=True, metavar="TEXT", help="why, in one line (- for stdin)")
    answer.add_argument("--days", type=int, metavar="N", help="with defer: how many days, 1 to 90 (7 by default)")
    answer.set_defaults(func=cmd_answer)
    merge = csub.add_parser(
        "merge", help="merge the pull request of the thread's change the hub left open, for its owner, inside the frame"
    )
    merge.add_argument("change", type=int, metavar="CHANGE", help="the change's id, as the thread names it")
    merge.set_defaults(func=cmd_merge)
