"""The core plugin crm: the people who found the project themselves, as contacts with a stage (criteria a13, a21 and
a22 of plan autonomous-oss-growth; ``docs/crm-plugin.md``).

A contact is a GitHub login with its public profile link, the source it was first seen from, its stage and the
touchpoints that put it there. It comes from the invite-request issue form, an invite link made for or accepted by
its login, a grant on the project, and an issue, pull request or discussion of its own on the project's repo by someone
who is no member of it. A person who only replies is none. Stored: login, link, source, stage, times and touchpoints;
never an email address, never anything read from another service.

The stages lead, invited, activated and retained are computed from what the hub holds (``rules``, ``staging``), kept
with their history, and only move forward. The job ``intake`` (``job``) reads the sources (``intake``, ``github``),
computes the stages and pushes the stage counts to the project's Signal as ``crm.lead``, ``crm.invited``,
``crm.activated`` and ``crm.retained`` (``push``). The source of the project's knowledge graph (``kg``) gives the login,
the source and the stage of each contact and nothing else.

The routes under ``/v1/x/crm/<project>`` (``server``, ``models``): a reader lists and shows contacts, a writer adds a
note, an admin deletes a contact on request with all its touchpoints, and the audit then holds no personal data;
``evo-agents hub crm list|show|delete`` is their client. A project that does not enable the plugin writes nothing and
answers 404 on its routes. The package imports no other core plugin (import-linter contract ``crm-plugin-own-package``).

On the Loop (criterion a16; ``loop``, ``tools``) the agent type @community answers an invite request in its issue and,
when the Policy allows (at most ``invite_links_per_week`` links in 7 days, a GitHub account at least
``invite_min_github_age_days`` old), makes a join link bound to the requester's login with ``crm__invite``; it gives an
outsider's new issue, pull request or discussion its first reply through the posting gate of the plugin marketing.
"""

SERVER = "evo_agents.core_plugins.crm.server"
JOB = "evo_agents.core_plugins.crm.job"
KG = "evo_agents.core_plugins.crm.kg"
INTAKE_JOB = "intake"
INTAKE_CRON = "*/15 * * * *"  # every 15 minutes, UTC
COMMUNITY = "community"  # tools.COMMUNITY
LOOP = "evo_agents.core_plugins.crm.loop"
TOOLS = "evo_agents.core_plugins.crm.tools"

COMMUNITY_INSTRUCTIONS = """# @community

You answer the people who come to an open-source project on GitHub by themselves. You are polite and short, and you
promise nothing the project has not decided.

## Tools

- crm__invite: a join link of the project for the GitHub login that asked in an invite-request issue. It refuses
  when the Policy does not allow one; say the reason it gives.
- marketing__propose: your reply in an issue, a pull request or a discussion, with kind reply, reply_to the ref you
  were given and the project's github account. Every reply passes the posting gate.

## Rules

- Write in English, plainly: thank them in one line, then say what happens next. No emoji, no filler closing line.
- Answer only from the project's docs, releases and changelog, with the link.
- Do not promise a fix, a date, a merge or access. Do not close, label or assign anything.
- Never reply from a personal account, and never message a person outside the thread they opened.
- You do not approve a baseline, and you do not change a Goal, the Policy or a channel's mode.
"""

INVITE_SCHEMA = {
    "type": "object",
    "properties": {
        "login": {"type": "string", "description": "the GitHub login that asked for the invite"},
        "ref": {"type": "string", "description": "the invite request, issue#<n>"},
    },
    "required": ["login", "ref"],
}

CONFIG = {
    "type": "object",
    "properties": {
        "repo": {
            "title": "GitHub repo",
            "description": "The repo whose issues, pull requests and discussions the contacts are read from, as "
            "owner/name; left unset, only the hub's own invites and grants make contacts.",
            "type": "string",
            "pattern": "^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$",
        },
        "invite_links_per_week": {
            "title": "Invite links a week",
            "description": "How many join links @community makes for invite requests in 7 days at most.",
            "type": "integer",
            "minimum": 0,
            "default": 3,
        },
        "invite_min_github_age_days": {
            "title": "Minimum GitHub account age (days)",
            "description": "@community makes a join link only for a GitHub account at least this old.",
            "type": "integer",
            "minimum": 0,
            "default": 30,
        },
    },
}

TABLES = (
    ("crm_contacts", "The contacts of a project: login, profile link, source, stage and times"),
    ("crm_touchpoints", "What a contact did: invites, grants, issues, pull requests, discussions and notes"),
    ("crm_stage_history", "Each stage a contact reached and when"),
    ("crm_cursors", "How far each source was read"),
    ("crm_deletions", "The contacts deleted on request: their number and count of touchpoints, no personal data"),
    ("crm_invites_sent", "The join links @community made for invite requests, once per request"),
)

MANIFEST = {
    "id": "crm",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "CRM",
    "description": "The people who found the project themselves: contacts with a stage, from GitHub and the hub.",
    "engines": {"evo-agents": ">=0.21"},
    "plugin_api": 1,
    "permissions": [],
    "config": CONFIG,
    "contributes": {
        "routes": [{"name": "crm", "title": "The contacts of a project", "python": f"{SERVER}:router"}],
        "jobs": [
            {
                "name": INTAKE_JOB,
                "title": "Find contacts, compute their stages, push the counts",
                "description": "Reads the invites and grants of the project and the issues, pull requests and "
                "discussions of its repo, computes the stage of each contact and pushes the stage counts to Signal.",
                "python": f"{JOB}:run",
                "cron": INTAKE_CRON,
            }
        ],
        "agent_types": [
            {
                "name": COMMUNITY,
                "title": "Community",
                "description": "Answers invite requests, makes a join link when the Policy allows, and gives an "
                "outsider's new issue, pull request or discussion its first reply.",
                "mentioned_by": "admin",
                "profile": {
                    "handle": COMMUNITY,
                    "name": "Community",
                    "description": "Answers the people who come to the project on GitHub.",
                    "instructions": COMMUNITY_INSTRUCTIONS,
                },
            }
        ],
        "mcp_tools": [
            {
                "name": "invite",
                "title": "Make a join link for an invite request",
                "description": "A one-time join link bound to the requester's GitHub login, when the Policy allows: "
                "at most invite_links_per_week in 7 days, an account at least invite_min_github_age_days old. Only "
                "a run of @community calls it.",
                "python": f"{TOOLS}:invite",
                "input_schema": INVITE_SCHEMA,
            }
        ],
        "loop_rules": [
            {
                "name": "growth",
                "python": f"{LOOP}:growth",
                "on": ["plugin.crm.touch"],
                "emits": ["touch"],
                "schedule": False,
                "kinds": [
                    {"kind": "growth_invite", "run": "task", "agent_type": COMMUNITY},
                    {"kind": "growth_draft", "run": "task", "agent_type": COMMUNITY},
                ],
            }
        ],
        "kg_sources": [
            {
                "name": "contacts",
                "title": "The contacts of a project: login, source and stage",
                "python": f"{KG}:kg_source",
            }
        ],
        "tables": [{"name": name, "title": title} for name, title in TABLES],
    },
}

__all__ = ["MANIFEST"]
