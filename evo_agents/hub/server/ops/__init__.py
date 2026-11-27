"""The ops of a project's default system (plan hub-steward): the pass of the queue of operations events
(``evo_agents.hub.ops_queue`` describes the queue), tier 0 of the queue, what the hub does by itself with no model,
the ops threads it opens to the system's Orchestrator for a batch that closed with items left, the ops actions of
that Orchestrator in its thread, and what the Inbox shows of the batches that hold a notification.

The package has four layers, which import-linter keeps in order (contract ``hub-ops-layers``): ``routes`` (the ops
actions of the worker protocol under /v1/worker), then ``service`` (``queue``, the pass; ``rules``, tier 0;
``threads``, the ops threads, the cap, the breaker and the digest; ``actions``, the Orchestrator's actions),
``repository`` (every SQL statement, on ``evo_agents.hub.tables``) and ``models`` (the Pydantic bodies, the values
the layers pass and the texts of a batch's summary).
"""
