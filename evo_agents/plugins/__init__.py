"""Plugins of evo-agents: the code the hub and the worker share to read, trust and place a plugin.

- ``manifest``: ``evo-plugin.json``, read against ``harness/schemas/plugin.schema.json`` (the seam plugin-v1);
- ``bundle``: a plugin directory as a deterministic tar.gz, and the checks every bundle passes;
- ``signing``: the signature beside a bundle (Sigstore keyless from the plugin repo's CI, or Ed25519 for hubs that
  cannot reach Sigstore) and the publishers an operator trusts;
- ``compat``: whether a plugin runs on a release, by its ``engines`` range and ``plugin_api``;
- ``core``: the core plugins installed as Python packages, from the entry point group ``evo_agents.plugins``.

A plugin is core (Python in the hub's and the worker's processes, installed into the image or the machine) or
isolated (its backend in a process of its own, its pages in a sandboxed iframe, installed on a hub at run time from
the hub's store of signed bundles). This package imports neither the hub, the worker, the knowledge graph nor the
harness loader, which import it (import-linter contract plugins-standalone). Standard library only at import;
checking a signature imports ``cryptography`` or ``sigstore`` then.
"""
