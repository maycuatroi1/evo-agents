"""A project's agent systems (``evo_agents.hub.system_spec`` holds their spec): the named, versioned specs of agents
that work together, their default and ops settings, the traces of their runs and what the Curator changes in them.
``docs/agents.md`` (Agent systems) is the design.

The package has four layers, which import-linter keeps in order (contract ``hub-systems-layers``): ``routes`` (the
HTTP routes of a system, of its ops settings and of its traces), then ``service`` (who may do what, the traces a run
opens and closes, the Curator's lens on a system, the seed of a project's default system and the messages sent to a
system), ``repository`` (every SQL statement, on ``evo_agents.hub.tables``) and ``models`` (the Pydantic bodies, the
values the layers pass and the texts of the refusals).
"""
