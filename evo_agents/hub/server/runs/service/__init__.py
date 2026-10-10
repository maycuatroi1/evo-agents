"""The run API's service: public functions that check what a caller may do, query and move runs with SQLAlchemy Core
on ``evo_agents.hub.tables``, and return the models of ``evo_agents.hub.server.runs.models``. The routes call them,
and so do the modules around the run API (decisions, notifications, credentials, the Curator's night shift)."""
