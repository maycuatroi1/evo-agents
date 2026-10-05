"""The adapters of the three runtimes, each through the runtime's official SDK or API, never by parsing what its CLI
prints:

- ``claude_code``: Claude Code through ``claude-agent-sdk`` (``ClaudeSDKClient``).
- ``codex``: Codex through ``openai-codex``, which drives ``codex app-server`` over JSON-RPC.
- ``opencode``: opencode through the HTTP API and the event stream of ``opencode serve``, with aiohttp.
- ``common``: what they share: the versions checked, the detection, the launcher that starts a runtime in a session
  of its own, the event queue and the settings a run may carry (model, effort).

The modules import the SDKs only when an agent starts, so the daemon (and ``evo-agents worker status``) loads the
adapters on a core install and reports a runtime whose SDK is missing as unavailable, with the reason.
"""
