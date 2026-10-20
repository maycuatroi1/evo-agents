"""The core plugins evo-agents ships: Python packages of plugins written against the hub's extension points, each
declared in the entry point group ``evo_agents.plugins`` of the evo-ak distribution, so every install of evo-ak, the
hub's image among them, has them.

The hub reaches a core plugin through its manifest only (``evo_agents.plugins.core``, ``evo_agents.hub.plugin_calls``):
no package of evo_agents imports this one (import-linter contract core-plugins-by-entry-point). A core plugin's
package holds its manifest in ``__init__`` and imports nothing heavy there, since every process that reads the entry
points imports it; its contributions' Python lives in modules the hub imports when it uses them.

- ``builder``, ``srs`` and ``tasks``: @builder, which changes the hub's settings through chat once an admin approves,
  BA documents to a template, and the backlog (``docs/plugins.md``, sections Builder and SRS and tasks).
- ``canvas``: a living Lean Canvas per product, whose claims carry a status, a test card, evidence and every rev
  (plan living-lean-canvas).

telegram and chats, the Telegram channel and the history of group chats in the knowledge graph, left for the
distribution evo-agents-core-plugins (repository maycuatroi1/evo-agents-core-plugins), which deploy/hub/core-plugins.txt
pins into the hub's image.
"""
