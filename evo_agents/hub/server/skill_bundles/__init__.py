"""Skills on the hub, the code behind the routes of ``evo_agents.hub.server.skills``, which describes them.

The package has four layers, which import-linter keeps in order (contract ``hub-skill-bundles-layers``): ``routes``
(empty: the routes stay in ``evo_agents.hub.server.skills``), then ``service`` (who reads and publishes a skill, what
the blob store is asked, what a route answers: ``skills``), ``repository`` (every SQL statement on skills and
skill_versions, and what a skill reads of grants, blobs and workers) and ``models`` (the bodies, the place of a skill,
the audiences and the limits). Other modules import ``service`` and ``models``, or the names
``evo_agents.hub.server.skills`` re-exports.
"""
