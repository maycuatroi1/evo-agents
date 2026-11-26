"""The evolutions of the Curator on the hub: an owner starts an evolution and reads it, and the worker of its run
reads its inputs and posts its baseline arms, its candidates and its end (``service.runs`` holds its tasks and its run,
``service.report`` its end, ``evo_agents.hub.evolve`` the model).

A member with the writer role starts an evolution (POST /v1/projects/{project}/curator/evolve) when the charter's
``evolve.enabled``: of target git, harness files of a repo that ``evolve.evolvable_paths`` names; of target
agent_config, an agent that an optimizes edge of an agent system reaches. Its run is queued at once, at any hour, when
the night shift's gate is open to a run a member asks for; otherwise (paused, a spent night, no worker on duty) the
evolution waits, with why, and the night shift queues its run later.

The worker of the run reads and writes with the run's own key alone (``evolve.EVOLVE_KEY_HEADER``), which the claim
handed it: GET /v1/worker/runs/{id}/evolve gives the tasks with their verify, which only the trusted daemon reads, and
never a task of the holdout split; POST .../evolve/arm keeps the result of a baseline arm by ``evolve.arm_key``, read
again by the next evolution instead of run again; POST .../evolve/candidate keeps a candidate, unique by its digest,
and recomputes the Pareto front of the evolution (``evolve.prune_all``, ``evolve.front_freq``); POST .../evolve/report
ends it.

The package has four layers, which import-linter keeps in order (contract ``hub-evolve-layers``): ``routes`` and
``mcp`` (the HTTP routes and the curator_evolve tool of the hub's /mcp), then ``service`` (who may start an evolution
and what its run reads and posts), ``repository`` (every SQL statement, on ``evo_agents.hub.tables``) and ``models``
(the Pydantic bodies, the states of an evolution and its limits).
"""
