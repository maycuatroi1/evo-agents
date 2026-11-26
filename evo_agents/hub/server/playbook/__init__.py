"""The playbook of the Curator on the hub (``evo_agents.hub.playbook`` holds the model): its entries in
curator_playbook, and every delta in curator_playbook_deltas, never changed or deleted. ``service`` says what the
routes do and how the playbook is kept.

The package has four layers, which import-linter keeps in order (contract ``hub-playbook-layers``): ``routes`` (the
worker's delta route and the project's read route), then ``service`` (who may write what, the prompt's lines and the
maintenance), ``repository`` (every SQL statement, on ``evo_agents.hub.tables``) and ``models`` (the Pydantic bodies
and the refusals). Other modules import ``service`` and ``models``, never ``routes``.
"""
