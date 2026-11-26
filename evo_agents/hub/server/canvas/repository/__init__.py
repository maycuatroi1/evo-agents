"""The repository layer of the plugin canvas: every SQL statement on its tables and on the hub's tables it reads,
with SQLAlchemy Core on ``evo_agents.hub.tables``. Its functions take a connection the caller's transaction holds and
answer the rows of ``models``; the base_rev write of a claim goes through ``claims.ClaimStore``, a
``VersionedStore``. It checks no role and decides nothing: that is the service's."""
