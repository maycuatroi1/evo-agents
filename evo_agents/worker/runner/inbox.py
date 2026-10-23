"""The owner's messages to a run's agent: the run's inbox on the hub.

The owner's messages reach the agent through ``send`` while it runs; messages the agent no longer takes (its last turn
is over, or a person drives it) wait in the inbox for the next agent, whose prompt carries them, and are marked
delivered once it starts. A message that answers a decision of the run marks the decision answered.

The files a person attached to a message are saved in the agent's directory, under ``.evo-run/inbox/<message id>/``,
before the message goes to the agent, and its text ends with the block that lists them (``evo_agents.worker.
attachments``): their paths, media types and sizes, as data of the sender, never instructions. A file not saved is a
line of that block saying why, and a ``system`` event of the run; the message still goes. A message without files goes
as it always did, its text alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from evo_agents.worker import attachments
from evo_agents.worker.adapter import AgentFinished
from evo_agents.worker.hubapi import Backoff, HubProblem, Unreachable
from evo_agents.worker.runner import transitions
from evo_agents.worker.runner.common import HARD_STOP_TRIES, log, wait_or

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


class Inbox:
    """What of the run's inbox its agents have had (``delivered_upto``), and the decisions those messages answered
    (``answered``)."""

    def __init__(self, run: Run):
        self.run = run
        self.delivered_upto = 0  # the inbox's messages up to this id were handed to the agent
        self.answered: set[int] = set()  # the decisions whose answers the agent was handed
        self.pending_ack: int | None = None  # handed to the agent in the prompt of the next start: ack once it runs
        self.delivering = False
        self.waits = False  # messages wait in the inbox because the agent takes no more input; logged once an agent
        self.texts: dict[int, str] = {}  # the text built for a message with files not handed yet: built once

    async def deliver(self) -> None:
        """Hand the owner's waiting messages to the agent, then mark them delivered on the hub. Messages the agent
        no longer takes (its last turn is over, or a person drives it) wait in the inbox for the next agent."""
        run, agent = self.run, self.run.agent
        if self.delivering or not agent.running or agent.adapter is None:
            return
        self.delivering = True
        adapter = agent.adapter
        try:
            messages = await run.daemon.hub.inbox(run.id)
            while messages and agent.running and adapter is agent.adapter:
                last = None
                try:
                    for message in messages:
                        message_id = message.get("id")
                        if isinstance(message_id, int) and message_id <= self.delivered_upto:
                            last = message_id  # in the prompt the agent started on already
                            continue
                        await agent.login_answered([message])
                        await adapter.send(await self.text_of(message))
                        last = message_id
                        self.handed([message])
                except AgentFinished:
                    if last is not None:
                        await run.daemon.hub.inbox(run.id, ack=int(last))
                    if not self.waits:
                        self.waits = True
                        log.info("messages wait in the inbox: the agent takes no more input", extra={"run_id": run.id})
                    return
                if last is None:
                    break
                messages = await run.daemon.hub.inbox(run.id, ack=int(last))
        except HubProblem as exc:
            log.warning("messages not taken from the inbox", extra={"run_id": run.id, "error": str(exc)})
        except Exception:
            log.exception("messages not handed to the agent", extra={"run_id": run.id})
        finally:
            self.delivering = False

    def handed(self, messages: list[dict]) -> None:
        """Note that these messages of the inbox went to the agent: the decisions they answer are answered."""
        self.delivered_upto, answers = transitions.handed(messages, self.delivered_upto)
        self.texts = {key: text for key, text in self.texts.items() if key > self.delivered_upto}
        self.answered |= answers
        self.run.record["answered"] = sorted(self.answered)

    async def ack(self, ack: int) -> None:
        """Mark the inbox's messages up to ``ack`` delivered, trying a few times."""
        run = self.run
        backoff = Backoff()
        for _ in range(HARD_STOP_TRIES + 1):
            try:
                await run.daemon.hub.inbox(run.id, ack=ack)
                return
            except Unreachable:
                await wait_or(run.daemon.hard_stop, backoff.next())
            except HubProblem as exc:
                log.warning("inbox not acknowledged", extra={"run_id": run.id, "error": str(exc)})
                return

    def ack_pending(self) -> None:
        """The agent that started has the messages its prompt carries: mark them delivered."""
        if self.pending_ack is not None:
            ack, self.pending_ack = self.pending_ack, None
            self.run.spawn(self.ack(ack))

    async def unread(self) -> list[dict]:
        """The inbox's messages the agent has not had yet; none when the hub does not answer."""
        run = self.run
        try:
            messages = await run.daemon.hub.inbox(run.id)
        except HubProblem as exc:
            log.warning("the inbox was not read", extra={"run_id": run.id, "error": str(exc)})
            return []
        return transitions.unread(messages, self.delivered_upto)

    async def take(self, messages: list[dict]) -> str:
        """The text of these messages for the agent's next prompt, with the files each carries saved and listed
        (``text_of``); they are acknowledged once it starts."""
        texts = [await self.text_of(message) for message in messages]
        self.handed(messages)
        self.pending_ack = self.delivered_upto
        return "\n\n".join(texts)

    async def text_of(self, message: dict) -> str:
        """The text of an inbox message for the agent: as the hub gave it, and after it the block that lists the files
        it carries, saved under ``.evo-run/inbox/<its id>/`` (``receive``). Built once for a message: one the agent
        did not take (its turn took no more input) goes to the next prompt as it was, its files not fetched again."""
        text = str(message.get("text") or "")
        files = [entry for entry in message.get("attachments") or [] if isinstance(entry, dict)]
        if not files:
            return text
        message_id = message.get("id")
        if isinstance(message_id, int) and message_id in self.texts:
            return self.texts[message_id]
        built = attachments.with_files(text, await receive(self.run, str(message_id), files))
        if isinstance(message_id, int):
            self.texts[message_id] = built
        return built


async def receive(run: Run, folder: str, files: list[dict]) -> list[attachments.Saved]:
    """The files of a message saved for the run's agent under ``.evo-run/inbox/<folder>/`` of its directory
    (``attachments.save_files``); each one not saved is a ``system`` event of the run saying which and why."""
    root = run.worktree
    if root is None:
        why = "the run has no directory for files yet"
        saved = [
            attachments.Saved(f.get("id"), attachments.shown(f.get("name")), str(f.get("media_type")), 0, error=why)
            for f in files
        ]
    else:
        saved = await attachments.save_files(run.daemon.hub, run.id, root, folder, files)
    fetched = [file for file in saved if file.fetched]
    if fetched:
        log.info("attachments saved", extra={"run_id": run.id, "folder": folder, "files": len(fetched)})
    for file in saved:
        if file.error is not None:
            run.note(
                f"The file {file.name} attached to message {folder} was not saved for the agent: {file.error}. The "
                "agent has the message, with a line saying so.",
                attachment_id=file.attachment_id,
            )
    return saved
