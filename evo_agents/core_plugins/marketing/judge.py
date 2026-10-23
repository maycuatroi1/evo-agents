"""The Judge of the posting gate: an agent in a fresh context that scores a post on four axes from 0 to 5 and has not seen
who proposed it or why (``docs/marketing-plugin.md``).

The axes are truth (every claim is backed by a source), brand (stays inside what the project says of itself), voice (no
spam, no hype) and privacy (no personal data of a third person). The post passes when every axis is 4 or more; any axis
at 3 or less withdraws it, and exactly one axis at 3 with the rest at 4 or 5 also tells the admins in the Inbox, since
that is a post close to the line. The agent answers as one JSON object; an answer that does not read as one fails the
post (a Judge that cannot be read is no Judge).

``ask`` puts the question to the project's ``marketing-judge`` agent type through the hub's question dispatch, with the
text and the links of the post only; ``verdict_of`` reads the answer once the run finished. A test (or a project that
wants another Judge) gives ``flow.Runtime`` a ``judge`` that answers at once.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server import core_plugin_api as hub

AGENT_TYPE = "marketing-judge"
AXES = ("truth", "brand", "voice", "privacy")
PASS_AT = 4
ASKER = "marketing"
CLEARED = hub.Clearance("internal")  # the answer stays in the hub: it goes to no channel above the project's label


@dataclass(frozen=True)
class Verdict:
    ok: bool
    scores: dict
    detail: str
    near_line: bool = False  # exactly one axis at 3: the Inbox is told

    def as_dict(self) -> dict:
        return {"ok": self.ok, "scores": self.scores, "detail": self.detail, "near_line": self.near_line}


def parse(text: str) -> Verdict:
    """The verdict an answer holds: the first JSON object with the four axes as numbers from 0 to 5."""
    scores = None
    for match in re.finditer(r"\{.*?\}", text or "", re.DOTALL):
        try:
            found = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(found, dict) and all(isinstance(found.get(axis), (int, float)) for axis in AXES):
            scores = {axis: found[axis] for axis in AXES}
            break
    if scores is None or any(not 0 <= value <= 5 for value in scores.values()):
        return Verdict(False, scores or {}, "the Judge's answer is no score of the four axes from 0 to 5")
    low = sorted(axis for axis, value in scores.items() if value < PASS_AT)
    if low:
        return Verdict(False, scores, f"the Judge scored {', '.join(low)} below {PASS_AT}", near_line=_near(scores))
    return Verdict(True, scores, "the Judge passed it")


def _near(scores: dict) -> bool:
    return sorted(scores.values())[0] == 3 and sorted(scores.values())[1] >= PASS_AT


def verdict_of(axes: dict) -> Verdict:
    """The verdict of axes a fake or another Judge gave directly."""
    return parse(json.dumps(axes))


def question(body: str, links: list[str], channel: str) -> str:
    """What the Judge is asked: the post and its links, nothing of who proposed it."""
    shown = "\n".join(links) or "(none)"
    return (
        f"Score this draft for the channel {channel} on four axes from 0 to 5: truth (every claim has a source), "
        "brand (stays inside what the project says of itself), voice (no spam, no hype) and privacy (no personal data "
        'of a third person). Answer with one JSON object only, like {"truth": 5, "brand": 5, "voice": 4, "privacy": 5}.\n\n'
        f"Draft:\n{body}\n\nLinks:\n{shown}"
    )


async def ask(conn: AsyncConnection, project: str, post_id: int, body: str, links: list[str], channel: str) -> int:
    """Ask the project's judge agent about the post; the id of the question. The same post asks once."""
    made = await hub.ask_agent_type(
        conn,
        hub.plugin(__name__),
        project,
        AGENT_TYPE,
        question(body, links, channel),
        asker=ASKER,
        ref=f"post:{post_id}",
        cleared=CLEARED,
    )
    return made.question_id


async def answered(conn: AsyncConnection, question_id: int) -> Verdict | None:
    """The verdict of a finished question; None while it is pending; a failed verdict when the run failed."""
    answer = await hub.read_agent_answer(conn, hub.plugin(__name__), question_id)
    state = answer.state
    if state == "done":
        return parse(answer.text or "")
    if state in ("failed", "cancelled", "refused"):
        return Verdict(False, {}, f"the Judge did not answer ({state})")
    return None
