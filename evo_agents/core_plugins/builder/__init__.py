"""The core plugin builder: the agent type @builder, which changes the hub's settings and a client's plugins through
group chat, never without an admin's approval (criterion a12 of plan hub-agent-plugins; ``docs/plugins.md``,
Builder).

An admin of a project that enabled the plugin enables its agent type, which creates the profile @builder there with
the pack ``builder/builder``: its instructions and two skills, one for writing evo-agents plugins (the manifest,
plugin/1, the iframe bridge of a page, signing, the CI that publishes a version to the hub) and one for changing the
branding, the features, the config of a plugin and an agent profile. The type takes work from the project's admins
alone (``mentioned_by: admin``): a writer who mentions @builder gets the hub's refusal in the thread.

Two routes for a change:

- A declarative one (the branding of the hub, a layer of features, a layer of a plugin's config in the project, an
  agent profile, the autonomy policy, a problem or a goal of the project) is a proposal: the agent calls the tool
  ``builder__propose`` of the hub's /mcp, which checks the change, keeps it pending in builder_proposals, shows it in
  the thread and tells the members who may approve it (a writer of the project for a problem, an admin otherwise).
  They approve or reject it in the thread (``approve 12``, ``reject 12``), from the Inbox, or with the routes under
  /v1/x/builder/<project>/proposals; approved, the hub applies it as the setting's next version, which
  ``rollback 12`` makes a copy of the one before (``proposals``). The Curator's review run on a
  night of the lens product proposes a change of the autonomy policy through the same tool, with no thread: an admin
  decides it from the Inbox or the routes (plan loop-engine, criterion a9).
- A change of code is the chat run itself: the agent works on the client's plugin repo the chat run checks out, pushes
  its branch and the hub opens the draft pull request; the repo's CI (the skill's ``ci.yml``) tests, packs, signs and
  publishes the version to the hub, where it waits as available until a hub admin upgrades to it.

The hub imports this package for the manifest only; ``server`` holds the tool, the event and the routes, and
``proposals`` what they share.
"""

SERVER = "evo_agents.core_plugins.builder.server"
KINDS = (
    "branding",
    "features",
    "plugin_config",
    "agent",
    "policy",
    "problem",
    "goal",
)  # tables_settings.PROPOSAL_KINDS
# The vocabulary of a line of evidence, copies of evo_agents.hub.problems (this package is read for its manifest alone;
# tests.hub.test_builder keeps them equal).
EVIDENCE_SOURCE_KINDS = ["hub_chat", "messenger", "chat_fpt", "email", "file", "harness", "other"]
EVIDENCE_SIGNALS = [
    "complaint",
    "workaround",
    "repeated_request",
    "blocker",
    "commitment",
    "proposed_solution",
    "contradiction",
    "decision_without_reason",
]
EVIDENCE_LABELS = ["public", "internal"]
MAX_SUMMARY_CHARS = 500
MAX_FIELDS = 50  # fields one proposal sets or unsets

INSTRUCTIONS = (
    "You are @builder, the agent that changes this hub and the client's plugins for the project's admins. You change "
    "nothing yourself: a change of a setting is a proposal (the tool builder__propose) that an admin approves, and a "
    "change of code is a branch of the plugin's repo whose draft pull request the hub opens. Follow the instructions "
    "of your pack."
)

PROPOSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "summary"],
    "properties": {
        "kind": {
            "enum": list(KINDS),
            "description": "branding: the hub's name and colours; features: a layer of features (target.layer); "
            "plugin_config: a layer of a plugin's config in the project (target.plugin, target.agent); agent: an "
            "agent profile of the project (target.agent); policy: the project's autonomy policy, each key by its "
            "dotted path such as merge.hold_until or repos.<repo>.mode (no target); problem: a problem of the project "
            "(target.problem, its code; none for a new problem), its fields statement, who, when, workaround, metric "
            "(key, aggregate, window_days, tenant; no target), rank, owner, status (open, solved, dropped) and "
            "close_reason, which a writer approves; goal: a goal of the project (target.goal, its code; none for a "
            "new goal), its fields statement, metric, deadline, canvas, problem (a problem's code), rank, owner, "
            "status and close_reason",
        },
        "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_SUMMARY_CHARS,
            "description": "what the change does and why, for the admin who approves it",
        },
        "target": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "layer": {"enum": ["hub", "project"], "description": "features: the hub's own layer or the project's"},
                "plugin": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,62}$", "description": "plugin_config"},
                "agent": {
                    "type": "string",
                    "pattern": "^[a-z0-9][a-z0-9-]{0,99}$",
                    "description": "agent: the profile's handle; plugin_config: the agent layer of that profile",
                },
                "goal": {
                    "type": "string",
                    "pattern": "^(?:G-[1-9][0-9]{0,8}|[a-z0-9][a-z0-9-]{0,63})$",
                    "description": "goal: the goal's code (G-<number>) or alias; left out for a new goal",
                },
                "problem": {
                    "type": "string",
                    "pattern": "^P-[1-9][0-9]{0,8}$",
                    "description": "problem: the problem's code (P-<number>); left out for a new problem",
                },
            },
        },
        "set": {"type": "object", "description": "field -> new value"},
        "evidence": {
            "type": "array",
            "maxItems": 20,
            "description": "problem only: lines of sources that show the problem hurts, made with the problem when a "
            "writer approves; with target.problem and no set or unset, they are added to that problem",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_kind", "source_uri", "excerpt", "signal", "label"],
                "properties": {
                    "source_kind": {"enum": EVIDENCE_SOURCE_KINDS},
                    "source_uri": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "locator": {"type": "string", "minLength": 1, "maxLength": 500},
                    "speaker": {"type": "string", "minLength": 1, "maxLength": 200},
                    "excerpt": {"type": "string", "minLength": 1, "maxLength": 300},
                    "signal": {"enum": EVIDENCE_SIGNALS},
                    "label": {"enum": EVIDENCE_LABELS},
                },
            },
        },
        "unset": {"type": "array", "items": {"type": "string"}, "description": "fields the layer stops setting"},
    },
}

MANIFEST = {
    "id": "builder",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Builder",
    "description": "@builder: changes the hub's settings and the client's plugins through chat, once an admin approves.",
    "engines": {"evo-agents": ">=0.16"},
    "plugin_api": 1,
    "permissions": ["chats:read"],
    "contributes": {
        "agent_packs": [
            {
                "name": "builder",
                "title": "Builder",
                "description": "Writing evo-agents plugins, and proposing changes of the hub's settings.",
            }
        ],
        "agent_types": [
            {
                "name": "builder",
                "title": "Builder",
                "description": "Proposes changes of settings for an admin to approve, and changes plugins by pull request.",
                "mentioned_by": "admin",
                "packs": ["builder"],
                "profile": {
                    "handle": "builder",
                    "name": "Builder",
                    "description": "Changes the hub's settings and the client's plugins, once an admin approves.",
                    "instructions": INSTRUCTIONS,
                },
            }
        ],
        "mcp_tools": [
            {
                "name": "propose",
                "title": "Propose a change of a setting",
                "description": "Propose a change of the hub's branding, a layer of features, a layer of a plugin's "
                "config, an agent profile, the autonomy policy, a problem or a goal of the project, which an admin "
                "(a writer, for a problem) approves before the hub applies it. Only the agent of a chat run of @builder may call it, and the Curator's "
                "review run on a night of the lens product for a change of the autonomy policy; it applies nothing.",
                "python": f"{SERVER}:propose",
                "input_schema": PROPOSE_SCHEMA,
            }
        ],
        "events": [
            {
                "name": "answers",
                "title": "An admin's answer to a proposal in its thread",
                "on": ["chat.message"],
                "python": f"{SERVER}:on_message",
            }
        ],
        "routes": [{"name": "proposals", "title": "The proposals of @builder", "python": f"{SERVER}:router"}],
        "tables": [{"name": "builder_proposals", "title": "The changes @builder proposed, and what became of them"}],
    },
}
