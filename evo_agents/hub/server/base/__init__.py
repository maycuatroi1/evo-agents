"""What the routes of every domain of the hub's API share: a project scope with its role checked (``scope``), a
transaction (``unit_of_work``), the audit of a write (``auditor``), the errors of a domain (``errors``), the base of a
request model (``models``), the store of a record kept as revisions (``revisions``) or of one whose write names the
version it was made on (``versions``), and the table of a feature's MCP tools (``tools``).

A domain of ``evo_agents.hub.server`` laid out in layers (``goals``, ``problems``, ``opportunities``) has a package of
``routes``, ``service``, ``repository`` and ``models``: a route takes a ``Scope`` from its ``ProjectScope``
dependency and passes it to the service, which checks and decides, and reads and writes through the repository,
whose store is a ``RevisionedStore``. See docs/structure.md, section Package boundaries.
"""
