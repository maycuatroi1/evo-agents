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
"""

SERVER = "evo_agents.core_plugins.crm.server"
JOB = "evo_agents.core_plugins.crm.job"
KG = "evo_agents.core_plugins.crm.kg"
INTAKE_JOB = "intake"
INTAKE_CRON = "*/15 * * * *"  # every 15 minutes, UTC

CONFIG = {
    "type": "object",
    "properties": {
        "repo": {
            "title": "GitHub repo",
            "description": "The repo whose issues, pull requests and discussions the contacts are read from, as "
            "owner/name; left unset, only the hub's own invites and grants make contacts.",
            "type": "string",
            "pattern": "^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$",
        }
    },
}

TABLES = (
    ("crm_contacts", "The contacts of a project: login, profile link, source, stage and times"),
    ("crm_touchpoints", "What a contact did: invites, grants, issues, pull requests, discussions and notes"),
    ("crm_stage_history", "Each stage a contact reached and when"),
    ("crm_cursors", "How far each source was read"),
    ("crm_deletions", "The contacts deleted on request: their number and count of touchpoints, no personal data"),
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
