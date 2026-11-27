"""The work card of a trace of an agent system (``docs/hub.md``, The work card; ``work_status`` derives it).

When a trace's work chat opens (``work_chats.open_work``), the hub posts one message of its own in the root thread, the
thread of the trace's first message: ``system_traces.card_message_id`` names it, and every read of it carries the card
as its ``work`` part (``chats.service.views.messages_view``). A move that changes what the card shows (a run of the trace moving,
``chats.service.runs.chat_run_moved``; a decision of one asked or answered, ``decisions``; the entry agent's ``work set``) writes
a ``message_changed`` event for it in the root chat, whose streams then read it again: a chat event, woken through
``evo_agents.hub.wake`` as every chat event is, so it needs no event bus. ``system_traces.card_state`` keeps the key of
what the card showed (``work_status.card_state``), so a move that changes nothing on it writes no event. A change of the
card, and every ``work set``, also moves the trace (``system_traces.touch``), so the trace stream of the system's
Observe sends it with its new title, status and wait.

- POST /v1/worker/runs/{id}/work (``evo-agents worker work set``, the hub runtime's ``work_set``): the coordinator (the
  system's entry agent) sets the title, goal and done-when of the work of its run's trace, and records its outcome
  (met or not_met against the done-when) with what is still to do. The worker token that holds the run; 409 for a run
  in no trace with a work chat, 403 for an agent that is not the coordinator. An outcome given clears a member's mark
  of handled, and what was left when ``left`` is not given with it.
- POST /v1/chats/{id}/work/handled (Mark as handled): a member of the work chat who holds the writer role on the
  trace's project marks the work handled, so a recorded not_met no longer reads so; the card names who and when. 404
  to anyone who does not read the chat and for a chat that is no work chat, 403 to a member who only reads (a reader
  of the project, or no grant on it), audited as ``work.handled``.
- GET /v1/chats/{id}/work: the card of the trace whose work chat that is, to a member who reads it (404 to anyone
  else, and for a chat that is no work chat; 403 to a member who no longer clears its label).

The package goes from its routes to its service to its repository to its models (contract hub-work-cards-layers):
``routes`` hands the request's parts and the app's engine to ``service``, which opens the transaction (``UnitOfWork``)
and decides; ``repository`` (``CardStore``) reads and writes system_traces; ``models`` holds the body of ``work set``
and the refusals. Other modules import the names below, never the routes.
"""

from evo_agents.hub.server.work_cards.models import FIELDS as FIELDS
from evo_agents.hub.server.work_cards.models import WorkSetIn as WorkSetIn
from evo_agents.hub.server.work_cards.service import decision_moved as decision_moved
from evo_agents.hub.server.work_cards.service import post_card as post_card
from evo_agents.hub.server.work_cards.service import refresh as refresh
from evo_agents.hub.server.work_cards.service import run_moved as run_moved
from evo_agents.hub.server.work_cards.service import set_work as set_work
