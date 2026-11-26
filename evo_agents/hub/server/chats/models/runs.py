"""Chat runs (``chats.service.runs``): what one mention asks, the agent's message a person's answers, what the hub
needs of a chat run, and what a run that goes on in a session copies of the run before."""

from __future__ import annotations

from dataclasses import dataclass

from evo_agents.hub import runs
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.route import Route
from evo_agents.hub.server.profile_store import Profile


@dataclass(frozen=True)
class Answered:
    """The agent's message a person's message answers (``reply_to``): its id, the agent's profile and handle as the
    thread names it, its text and label, and the run that wrote it. The inbox of a run quotes it unless that run's
    session wrote it. A guest's message is one too, with no profile and the guest's name marked (``rules.guest``): it
    wakes no agent, and the inbox quotes it as data."""

    message_id: int
    profile_id: int | None
    author: str
    body: str
    label: list  # as chat_messages.label holds it
    run_id: int | None


@dataclass(frozen=True)
class Hand:
    """What one mention asks: work of ``profile`` for ``dispatcher_id`` (``dispatcher``), the person at the origin of
    the chain, at ``hop``, in the thread of ``root_id`` of ``chat``, woken by message ``message_id`` that ``author``
    wrote, with the credential kind ``via`` (runs.dispatched_via). ``route`` is set by ``hand_work`` for a profile
    whose agent type sends its chat runs under the name of its run_as. ``on_worker`` is the worker a new session goes to
    when that worker can take it: the machine the work is about (a program to install, ``triage``). ``answers`` is the
    agent's message the message answers, when it answers one. ``priority`` is the run's (runs.PRIORITIES): an agent's
    ask sets it, a person's mention has the default. ``asker_run_id`` is the run whose agent's mention this is, when an
    agent's."""

    chat: ChatAccess
    message_id: int
    root_id: int
    profile: Profile
    dispatcher_id: int
    dispatcher: str
    via: str
    hop: int
    author: str
    body: str
    route: Route | None = None
    on_worker: int | None = None
    answers: Answered | None = None
    priority: str = runs.DEFAULT_PRIORITY
    asker_run_id: int | None = None

    @property
    def owner_id(self) -> int:
        """The member the run's row is dispatched by, whose worker takes it."""
        return self.route.user_id if self.route is not None else self.dispatcher_id

    @property
    def owner(self) -> str:
        return self.route.login if self.route is not None else self.dispatcher

    @property
    def run_via(self) -> str:
        """The credential kind of the run's row: the person's, or machine for a run that goes under run_as."""
        # "machine": the dispatched_via of a token (credentials.DispatchedVia), as security.MACHINE names its kind
        return "machine" if self.route is not None else self.via


CONTINUED = ("kind", "project_id", "title", "mode", "approval", "repos", "branch", "model", "chat_id")
CONTINUED += ("profile_id", "profile_version", "session_id", "runtime", "principal_id", "autonomy")
CONTINUED += ("gateway_provider", "priority")
CONTINUED += ("spawned_by_run_id", "spawned_wait_id")  # a run another agent asked: the run that ends answers it

MISSED_MESSAGES = 5  # messages of the person in the thread that woke no agent, which the next run of the session takes


@dataclass(frozen=True)
class ChatRun:
    """What the hub needs of a chat run: its chat, the message that started it, the thread it answers in, its
    profile, its dispatcher (the person at the origin of the chain, whom the agent acts for) and hop, and
    ``dispatched_by``, the member its row is dispatched by: the dispatcher, or the run_as of an agent type that sends
    its chat runs to that member's workers (``chats.service.route``)."""

    run_id: int
    chat_id: int
    message_id: int
    root_id: int
    profile_id: int
    dispatcher_id: int
    dispatcher: str
    via: str
    hop: int
    error: str | None
    dispatched_by: int
