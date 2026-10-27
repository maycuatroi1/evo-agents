"""The API of the hub for core plugins, version 1.7: the one module of ``evo_agents.hub`` a core plugin imports
(``from evo_agents.hub.server import core_plugin_api as hub``), whose ``__all__`` is the whole of it. ``docs/plugins.md``
(The core plugin API) is the contract; ``tests/plugins/golden/core-plugin-api.json`` holds its surface.

A core plugin runs in the hub's processes and holds the hub's connection: the API makes the right code easy to write
and stable from one release to the next, it is no sandbox. Each function that acts for a member takes a ``Member``,
which only this module makes, from a channel of the plugin's bound to a live web session, and goes through the very
code the web route that does the same goes through, with its checks; what the hub refuses is ``Refused``, with the
status and the words of that route. A mistake of the plugin's (a kind of channel it does not contribute, a config too
large, a handle it made itself) is a ValueError.

- The ground: ``require``, ``plugin``, the callers of the plugin's routes (``WebSession``, ``Caller``,
  ``HubAdmin``), ``begin``, ``hub_config``, ``web_url``, ``Context`` and ``transaction`` for its jobs, events, tools
  and sources, ``plugin_environment``, and the class and errors of the extension point channels.
- A member's channel: ``find_member``, ``link_channel``, ``unlink_channel``, ``unlink_by_login``, ``linked_channel``,
  ``link_code``, ``redeem_link_code``, ``direct_chat_member``, ``set_channel_state``, ``delivered_notification``, and
  ``prepare`` with ``restricted`` for what a ``Clearance`` keeps out.
- What a member does from it: ``answer_decision``, ``answer_proposal`` (refused with ``NOT_YOURS`` or
  ``RESTRICTED_ANSWER`` before anything of the decision or proposal is read), ``agents``, ``open_direct_chat``,
  ``send_message``, ``read_message``; the chat functions need the project of each agent to grant the plugin
  ``chats:write`` or ``chats:read``.
- The plugin's own: ``plugin_config`` (its effective config, x-secret fields unsealed), ``granted``, ``audit``, and
  ``chat_history_items`` for a source of the knowledge graph.
- Its agent types: ``ask_agent_type`` asks one a question for a person outside the hub, in a read-only question run,
  and ``read_agent_answer`` reads its ``AgentAnswer``; the plugin gets ``ANSWER_EVENT`` when the run ends.
- Its tables and the hub's data (1.3): ``plugin_metadata``, ``adopt_tables``, ``own_tables`` and ``HUB_KEYS`` for the
  tables a plugin carries with its own chain of migrations (manifest key ``schema``), which it writes with SQLAlchemy
  Core; ``project_access`` and ``has_role`` check a caller as the hub's routes do; the ``read_*`` functions read
  members, runs, grants, plans and steps, decisions, signals, goals, canvas claims, tasks and SRS of a project that
  enables the plugin; ``emit_event``, ``push_signal``, ``create_invite``, ``queue_kg_build`` and ``notify`` (a notice
  of a kind its manifest contributes) write; ``chat_run_of``, ``run_session``, ``reply_in_chat``,
  ``enabled_agent_type`` and ``hand_task`` act in chat runs and hand tasks to its agent types; ``project_configs``
  reads its config in every project that enables it.
- The probes of its own rules (1.4): ``add_probe`` and ``remove_probe`` keep a weekly probe of a rule the plugin
  renders into a repo's harness files, arm on with the files rendered with the rule and arm off without it.

Version 1.1 added ``Caller``, ``linked_channel``, ``unlink_by_login``, ``audit``, ``Clearance.below``, ``NOT_YOURS``
and ``RESTRICTED_ANSWER``, what the core plugin telegram needed to run on this module alone. Version 1.2 added
``ask_agent_type``, ``read_agent_answer``, ``AgentQuestion``, ``AgentAnswer``, ``ANSWER_EVENT``, ``MAX_QUESTION_BYTES``,
``MAX_ASKER_CHARS``, ``MAX_REF_CHARS`` and ``MAX_ANSWER_CHARS``.

Version 1.3 added what a core plugin outside evo-agents needs: its tables and chain of migrations, its framed
pages (manifest key ``ui``), its commands and notice kinds, and the functions of the bullet above, with the values
they return (``ProjectRef``, ``RunRef``, ``SignalPushed``, ``Invite`` and the others of ``__all__``).

Version 1.4 added the keywords ``repo``, ``branch`` and ``minutes`` of ``hand_task`` (a task on a branch of one repo
that is no default branch, pushed back to it, with up to ``MAX_TASK_MINUTES`` of agent time), and ``add_probe``,
``remove_probe`` and ``MAX_PROBE_REF_CHARS`` for the probes of a plugin's own rules.

Version 1.5 added the bridges of group chats to groups of a channel of the plugin, each function named ``bridge_``:
``bridge_code`` (from a web session of a chat admin), ``bridge_link``, ``bridge_find``, ``bridge_read``,
``bridge_unlink``, ``bridge_move``, ``bridge_set_state``, ``bridge_message``, ``bridge_file`` and ``bridge_sent`` for
what goes out to the group, ``bridge_send`` and ``bridge_attach`` for what comes in, with ``Bridge``, ``BridgeMessage``,
``BridgeFile`` and ``BRIDGE_EVENT``, the event a plugin's manifest names to get a bridged chat's new messages, run progress and reactions
(``docs/chat-bridges.md``).

Version 1.6 added the decisions a plugin that sends a project's text to a model takes part in. The plugin declares its
sinks in its manifest (key ``egress``, ``Plugin.egress`` of ``EgressSink``), and each text read returns only what the
project's policy lets that sink read (``Cleared``): ``read_run_failure``, ``read_review_queue``, ``read_corrections`` and
``read_accepted_rules``. The writes act as the plugin: ``classify_run_failure`` (a cause the hub had guessed),
``decide_graph`` (a decision on the graph, active or proposed) and ``dismiss_review_item``; a writer undoes each from
the web and the CLI. The payload of the ``run.state`` event gained ``failure_cause`` and ``failure_cause_inferred`` on a
move to ``failed``.

Version 1.7 gave ``read_review_queue`` the keyword ``rules``: only the items of those review rules
(``evo_agents.kg.review.RULES``) come back and ``limit`` counts after the filter, so a plugin reaches the rules the
hub lists late.

Versions. ``require(major, minor)`` passes when this hub serves exactly that version, and raises UnsupportedApi
naming both otherwise; a plugin calls it in the function its entry point names, so a hub of another version leaves the
plugin out with that reason. A change that only adds to the surface takes the next minor version, one that removes or
changes anything the next major version, minor 0; ``python -m evo_agents.hub.server.core_plugin_api surface`` prints
the surface, ``... version`` the version.
"""

from __future__ import annotations

import sys
from typing import Annotated

from fastapi import Depends

from evo_agents.hub.chat_bridge_events import EVENT as BRIDGE_EVENT
from evo_agents.hub.server.admin import Login
from evo_agents.hub.server.core_plugin_api._access import ROLES, chat_run_of, has_role, project_access, run_session
from evo_agents.hub.server.core_plugin_api._acts import (
    agents,
    answer_decision,
    answer_proposal,
    chat_history_items,
    open_direct_chat,
    read_message,
    send_message,
)
from evo_agents.hub.server.core_plugin_api._agents import MAX_TASK_MINUTES, enabled_agent_type, hand_task, reply_in_chat
from evo_agents.hub.server.core_plugin_api._base import (
    Context,
    audit,
    begin,
    caller,
    granted,
    hub_admin,
    hub_config,
    plugin,
    plugin_config,
    plugin_environment,
    project_configs,
    restricted,
    transaction,
    web_session,
    web_url,
)
from evo_agents.hub.server.core_plugin_api._bridges import (
    bridge_attach,
    bridge_code,
    bridge_file,
    bridge_find,
    bridge_link,
    bridge_message,
    bridge_move,
    bridge_read,
    bridge_send,
    bridge_sent,
    bridge_set_state,
    bridge_unlink,
)
from evo_agents.hub.server.core_plugin_api._channels import (
    delivered_notification,
    direct_chat_member,
    find_member,
    link_channel,
    link_code,
    linked_channel,
    prepare,
    redeem_link_code,
    set_channel_state,
    unlink_by_login,
    unlink_channel,
)
from evo_agents.hub.server.core_plugin_api._decision import (
    AcceptedRule,
    CauseClassified,
    Cleared,
    CorrectionText,
    DecisionTaken,
    DismissedItem,
    ReviewItemText,
    ReviewNodeText,
    RunEventText,
    RunFailure,
    classify_run_failure,
    decide_graph,
    dismiss_review_item,
    read_accepted_rules,
    read_corrections,
    read_review_queue,
    read_run_failure,
)
from evo_agents.hub.server.core_plugin_api._loop import (
    ClaimCardRef,
    GoalMeasure,
    ProjectAgentTypeRef,
    SignalPoint,
    SignalsPushed,
    WorkItemRef,
    admins_notified,
    measure_goals,
    notify_admins,
    push_signals,
    read_agent_types,
    read_claim_cards,
    read_work_items,
    record_audit,
    run_member,
    session_run,
)
from evo_agents.hub.server.core_plugin_api._people import (
    PROJECT_NAME,
    EnabledProject,
    InviteRef,
    LabelReader,
    RunActorRef,
    label_reader,
    match_users,
    read_first_runs,
    read_invites,
    read_project,
    read_run,
)
from evo_agents.hub.server.core_plugin_api._probes import MAX_PROBE_REF_CHARS, add_probe, remove_probe
from evo_agents.hub.server.core_plugin_api._questions import (
    ANSWER_EVENT,
    MAX_ANSWER_CHARS,
    MAX_ASKER_CHARS,
    MAX_QUESTION_BYTES,
    MAX_REF_CHARS,
    AgentAnswer,
    AgentQuestion,
    ask_agent_type,
    read_agent_answer,
)
from evo_agents.hub.server.core_plugin_api._reads import (
    read_claims,
    read_decisions,
    read_goals,
    read_grants,
    read_plan_steps,
    read_plans,
    read_runs,
    read_signals,
    read_sprints,
    read_srs_items,
    read_srs_versions,
    read_task_links,
    read_tasks,
    read_users,
    step_key,
)
from evo_agents.hub.server.core_plugin_api._tables import (
    HUB_KEYS,
    PluginSchemaError,
    adopt_tables,
    own_tables,
    plugin_metadata,
)
from evo_agents.hub.server.core_plugin_api._tasks import (
    PlanStepChangeRef,
    SprintDetailRef,
    TaskActivityRef,
    TaskBlockerRef,
    TaskDecisionRef,
    TaskDetailRef,
    TaskEventRef,
    TaskPersonRef,
    TaskSrsItemRef,
    ToolCaller,
    Visibility,
    enabled_plugin_config,
    post_in_chat,
    read_plan_step_changes,
    read_sprint_details,
    read_task_activity,
    read_task_blockers,
    read_task_decisions,
    read_task_details,
    read_task_events,
    read_task_people,
    read_task_srs_items,
    read_visibility,
    tool_caller,
)
from evo_agents.hub.server.core_plugin_api._types import (
    AgentRef,
    AgentTypeRef,
    Bridge,
    BridgeFile,
    BridgeMessage,
    ChatMessage,
    ChatRunRef,
    ClaimRef,
    Clearance,
    DecisionAnswer,
    DecisionRef,
    DirectChat,
    EgressSink,
    GoalRef,
    GrantRef,
    Invite,
    KgSourceAnswer,
    LinkCode,
    LinkedChannel,
    Member,
    NotACorePlugin,
    NotificationRef,
    PlanRef,
    Plugin,
    PluginConfig,
    ProjectConfig,
    ProjectRef,
    ProposalAnswer,
    Refused,
    RunRef,
    SentMessage,
    Session,
    SignalPushed,
    SignalValue,
    SprintRef,
    SrsItemRef,
    SrsVersionRef,
    StepRef,
    TaskHanded,
    TaskLinkRef,
    TaskRef,
    Unlinked,
    UnsupportedApi,
    UserRef,
)
from evo_agents.hub.server.core_plugin_api._writes import (
    create_invite,
    emit_event,
    notify,
    push_signal,
    queue_kg_build,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.member_channels import NOT_YOURS, RESTRICTED_ANSWER
from evo_agents.hub.server.notifications import (
    SESSION_ENDED,
    Channel,
    ChannelGone,
    Outgoing,
    RetryAfter,
    Undeliverable,
)

API_VERSION: tuple[int, int] = (1, 7)

WebSession = Annotated[Session, Depends(web_session)]
Caller = Annotated[Session, Depends(caller)]
HubAdmin = Annotated[Session, Depends(hub_admin)]


def require(major: int, minor: int) -> None:
    """Raise UnsupportedApi naming both versions unless this hub serves exactly API major.minor."""
    for value in (major, minor):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("require takes the major and the minor version as integers")
    if (major, minor) != API_VERSION:
        caller_module = sys._getframe(1).f_globals.get("__name__", "a core plugin")
        served = ".".join(map(str, API_VERSION))
        raise UnsupportedApi(f"{caller_module} needs core plugin API {major}.{minor}; this hub serves {served}")


__all__ = [
    "ANSWER_EVENT",
    "API_VERSION",
    "AcceptedRule",
    "CauseClassified",
    "Cleared",
    "CorrectionText",
    "DecisionTaken",
    "DismissedItem",
    "ReviewItemText",
    "ReviewNodeText",
    "RunEventText",
    "RunFailure",
    "classify_run_failure",
    "decide_graph",
    "dismiss_review_item",
    "read_accepted_rules",
    "read_corrections",
    "read_review_queue",
    "read_run_failure",
    "BRIDGE_EVENT",
    "AgentAnswer",
    "AgentQuestion",
    "AgentRef",
    "AgentTypeRef",
    "Bridge",
    "BridgeFile",
    "BridgeMessage",
    "Caller",
    "Channel",
    "ChannelGone",
    "ChatMessage",
    "ChatRunRef",
    "ClaimCardRef",
    "ClaimRef",
    "Clearance",
    "Context",
    "DecisionAnswer",
    "DecisionRef",
    "DirectChat",
    "EnabledProject",
    "ErrorBody",
    "GoalMeasure",
    "GoalRef",
    "GrantRef",
    "HUB_KEYS",
    "HubAdmin",
    "Invite",
    "InviteRef",
    "KgSourceAnswer",
    "LabelReader",
    "LinkCode",
    "LinkedChannel",
    "Login",
    "MAX_ANSWER_CHARS",
    "MAX_ASKER_CHARS",
    "MAX_PROBE_REF_CHARS",
    "MAX_QUESTION_BYTES",
    "MAX_REF_CHARS",
    "MAX_TASK_MINUTES",
    "Member",
    "NOT_YOURS",
    "NotACorePlugin",
    "NotificationRef",
    "Outgoing",
    "PROJECT_NAME",
    "PlanRef",
    "PlanStepChangeRef",
    "EgressSink",
    "Plugin",
    "PluginConfig",
    "PluginSchemaError",
    "ProjectAgentTypeRef",
    "ProjectConfig",
    "ProjectRef",
    "ProposalAnswer",
    "RESTRICTED_ANSWER",
    "ROLES",
    "Refused",
    "RetryAfter",
    "RunActorRef",
    "RunRef",
    "SESSION_ENDED",
    "SentMessage",
    "Session",
    "SignalPoint",
    "SignalPushed",
    "SignalValue",
    "SignalsPushed",
    "SprintDetailRef",
    "SprintRef",
    "SrsItemRef",
    "SrsVersionRef",
    "StepRef",
    "TaskActivityRef",
    "TaskBlockerRef",
    "TaskDecisionRef",
    "TaskDetailRef",
    "TaskEventRef",
    "TaskHanded",
    "TaskLinkRef",
    "TaskPersonRef",
    "TaskRef",
    "TaskSrsItemRef",
    "ToolCaller",
    "Undeliverable",
    "Unlinked",
    "UnsupportedApi",
    "UserRef",
    "Visibility",
    "WebSession",
    "WorkItemRef",
    "admins_notified",
    "add_probe",
    "adopt_tables",
    "agents",
    "answer_decision",
    "answer_proposal",
    "ask_agent_type",
    "audit",
    "begin",
    "bridge_attach",
    "bridge_code",
    "bridge_file",
    "bridge_find",
    "bridge_link",
    "bridge_message",
    "bridge_move",
    "bridge_read",
    "bridge_send",
    "bridge_sent",
    "bridge_set_state",
    "bridge_unlink",
    "chat_history_items",
    "chat_run_of",
    "create_invite",
    "delivered_notification",
    "direct_chat_member",
    "emit_event",
    "enabled_agent_type",
    "enabled_plugin_config",
    "find_member",
    "granted",
    "hand_task",
    "has_role",
    "hub_config",
    "label_reader",
    "link_channel",
    "link_code",
    "linked_channel",
    "match_users",
    "measure_goals",
    "notify",
    "notify_admins",
    "open_direct_chat",
    "own_tables",
    "plugin",
    "plugin_config",
    "plugin_environment",
    "plugin_metadata",
    "post_in_chat",
    "prepare",
    "project_access",
    "project_configs",
    "push_signal",
    "push_signals",
    "queue_kg_build",
    "read_agent_answer",
    "read_agent_types",
    "read_claim_cards",
    "read_claims",
    "read_decisions",
    "read_first_runs",
    "read_goals",
    "read_grants",
    "read_invites",
    "read_message",
    "read_plan_step_changes",
    "read_plan_steps",
    "read_plans",
    "read_project",
    "read_run",
    "read_runs",
    "read_signals",
    "read_sprint_details",
    "read_sprints",
    "read_srs_items",
    "read_srs_versions",
    "read_task_activity",
    "read_task_blockers",
    "read_task_decisions",
    "read_task_details",
    "read_task_events",
    "read_task_links",
    "read_task_people",
    "read_task_srs_items",
    "read_tasks",
    "read_users",
    "read_visibility",
    "read_work_items",
    "record_audit",
    "redeem_link_code",
    "remove_probe",
    "reply_in_chat",
    "require",
    "restricted",
    "run_member",
    "run_session",
    "send_message",
    "session_run",
    "set_channel_state",
    "step_key",
    "tool_caller",
    "transaction",
    "unlink_by_login",
    "unlink_channel",
    "web_url",
]
