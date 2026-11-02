"""The core plugin guard: keeps the private names of a hub's projects out of the repos they publish (``docs/guard.md``).

A project that enables the plugin lists its private names (its own name, hosts, domains, groups, repos: ``terms``)
and the repos it publishes (``targets``); its admins keep both on the hub, with the routes under /v1/x/guard/<project>/
and the commands ``evo-agents hub guard``. The hub hands every worker and member the union over the projects that
enable it, each term hashed (``evo_agents.hub.guard``, ``evo_agents.private_names``), and three guards read it:

- the worker, for every run: its git hooks refuse a commit message or a push to a guarded repo that holds a name, a
  ``gh`` before the real one on the agent's PATH refuses a pull request, issue, comment or release whose text holds
  one, and the daemon's own pushes are checked the same way (``evo_agents.worker.guard``);
- a member's Claude Code session, through the evo-hub plugin's PreToolUse hook on Bash (``hub hook pre-tool-use``);
- the hub, which takes a name out of the title and body of a pull request it opens on a guarded repo.

The hub imports this package for the manifest only; ``server`` holds the routes.
"""

import json
from pathlib import Path

SERVER = "evo_agents.core_plugins.guard.server"
CLI = json.loads(Path(__file__).with_name("cli.json").read_text(encoding="utf-8"))  # evo-agents hub guard ...

MANIFEST = {
    "id": "guard",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Guard",
    "description": "Keeps the private names of the hub's projects out of the repos they publish: commits, pushes, "
    "pull requests, issues and releases.",
    "engines": {"evo-agents": ">=0.54"},
    "plugin_api": 1,
    "permissions": [],
    "contributes": {
        "routes": [
            {
                "name": "lists",
                "title": "The private names and published repos of a project",
                "python": f"{SERVER}:router",
            },
            {"name": "policy", "title": "The hub's policy, hashed", "prefix": "", "python": f"{SERVER}:policy_router"},
        ],
        "cli": CLI,
        "tables": [
            {"name": "guard_terms", "title": "The private names of each project"},
            {"name": "guard_targets", "title": "The repos each project publishes"},
        ],
    },
}
