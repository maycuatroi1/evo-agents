"""The Curator's probes (``evo_agents.hub.probes`` holds their shape): a probe measures whether a merged change's rule
still keeps an agent from the mistake it was born of, by running a task with the rule in place (arm on) and with the
change's diff taken off (arm off). ``docs/hub.md`` (Probes) is the design.

The package has four layers, which import-linter keeps in order (contract ``hub-probes-layers``): ``routes`` and
``mcp`` (the HTTP routes and the curator_probe_report tool of the hub's /mcp), then ``service`` (who may do what, the
probe runs the night shift queues, the weekly report and what it asks of a review run), ``repository`` (every SQL
statement, on ``evo_agents.hub.tables``) and ``models`` (the Pydantic bodies, the values the layers pass and the texts
of the report).
"""
