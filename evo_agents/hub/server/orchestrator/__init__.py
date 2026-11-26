"""The Orchestrator of an agent system: the threads the hub opens in a system's ops chat when a run fails, stalls,
spends its budget or asks for help, and what the Orchestrator does in them (answer, reassign, ask the owner).
``service.threads`` describes the design.

The package has four layers, which import-linter keeps in order (contract ``hub-orchestrator-layers``): ``routes``
(the worker protocol's routes of the Orchestrator's actions and of an escalation), then ``service`` (the threads and
the actions), ``repository`` (every SQL statement, on ``evo_agents.hub.tables``) and ``models`` (the Pydantic bodies
and the values the layers pass).
"""
