"""The decisions writers take on a project's knowledge graph, and the proposals agents make, for the hub's web pages,
``evo-agents hub kg decisions`` and the MCP tool ``kg_propose`` (``evo_agents.hub.kg_decisions``).

- GET /v1/kg/{project}/decisions?state=: the decisions the member can see, newest first, each with its history (who,
  when, why, the build), its state (proposed, active, rejected, undone, or orphan for an active one whose subject the
  latest build no longer holds), the project's decision revision and whether the member may decide.
- POST /v1/kg/{project}/decisions {kind, src, rel, dst, reason, base_rev}: a writer deprecates a node (src) or an edge
  (src, rel, dst), merges two node ids (src, dst) or links two nodes (src, rel, dst, rel one the ontology knows). The
  decision is active at once and a build is queued.
- POST /v1/kg/{project}/decisions/{number}/{accept|reject|undo} {reason, base_rev}: a writer accepts or rejects a
  proposal, or undoes an active decision (an orphan too).

Each write names the decision revision of the project it read (``base_rev``, 0 before the first decision); when a
decision changed since, the write answers 409 with the revision now, and nothing changes. Writing needs the writer
role (403 for a reader, 404 for a caller without a grant, as every read of the graph). A decision names only nodes and
edges the writer sees in the latest build; a decision that names anything a member cannot see is not listed to them.
An agent's ``kg_propose`` writes the same decision as a proposal, which changes nothing until a writer accepts it.
A loop applies the fixes of the review queue as decisions of its own (``apply_fixes``, for
``POST /v1/kg/{project}/review/apply``): each step names the loop (``origin``) and its run, and is undone as any other.

The package has four layers, which lint-imports keeps in this order (contract hub-kg-decisions-layers in
pyproject.toml): ``routes`` hands the request to ``service``, which checks the caller's role (``GraphScope``, a
``ProjectScope``), reads the graph and decides, and reads and writes the tables through ``repository``; ``models``
holds the request and response models. The graph is read outside any transaction of the database, so each step of the
service opens its own (``UnitOfWork``). Other modules import ``service`` and ``models``, never ``routes``.
"""
