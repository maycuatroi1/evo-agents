"""The core plugin browser: the agent pack every run with a browser gets (plan agent-browser, criterion a8).

A run dispatched with browser (``runs.browser``) has a Chromium of its own on its worker, whose DevTools endpoint its
agent reads in EVO_BROWSER_CDP. The pack ``browser/browser`` gives the agent the way to drive it and the rule to follow
at a login page:

- ``.mcp.json``: the Playwright MCP server, pinned (``PLAYWRIGHT_MCP``) and attached to that Chromium with
  ``--cdp-endpoint``; ``${EVO_BROWSER_CDP}`` in its args is the run's endpoint, which the worker sets as it hands the
  server to the runtime (``evo_agents.worker.packs.Pack.servers``);
- ``INSTRUCTIONS.md``: on a login page the agent runs ``evo-agents worker browser login``, which asks the person who
  dispatched the run to sign in through the live view; it types no password and solves no captcha.

The worker builds the pack from this package for every run with browser, whatever pack the run's profile attaches
(``evo_agents.worker.packs.browser_ticket``), so a profile need not name it; a profile that does attach it gets it once.
The hub lists the plugin with the other core plugins and takes no part in handing the pack out.
"""

PLAYWRIGHT_MCP = (
    "@playwright/mcp@0.0.83"  # the version the pack pins; .mcp.json spells it too (a test keeps them equal)
)
PACK = "browser"

MANIFEST = {
    "id": "browser",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Browser",
    "description": "The agent pack of a run with a browser: Playwright on the run's Chromium, and the rule to ask a "
    "person to sign in.",
    "engines": {"evo-agents": ">=0.16"},
    "plugin_api": 1,
    "permissions": [],
    "contributes": {
        "agent_packs": [
            {
                "name": PACK,
                "title": "Browser",
                "description": "Drives the run's own Chromium with Playwright, and asks a person to sign in at a "
                "login page.",
            }
        ]
    },
}
