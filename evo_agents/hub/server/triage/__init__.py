"""The triage of a failed run: the thread it opens in the triage chat of its project with no model call, and what the
agent of that thread does on its own behind ``evo-agents worker triage`` (``evo_agents.hub.triage_rules`` is the
frame; ``docs/workers.md``, Failure triage, is the protocol).

The package has four layers, which import-linter keeps in order (contract ``hub-triage-layers``): ``routes`` (the
triage routes of the worker protocol under /v1/worker), then ``service`` (``threads``, the threads of failed and
stale runs and the hub's threads of a project; ``actions``, what the agent of a thread does), ``repository`` (every
SQL statement, on ``evo_agents.hub.tables``) and ``models`` (the Pydantic bodies, the values the layers pass and the
texts of a thread's first message).
"""
