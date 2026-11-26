"""The opportunities of a project, kept as revisions (plan opportunity-intake, criterion a1; the model in
``evo_agents.hub.opportunities``). Tables opportunities, opportunity_revisions and opportunity_answers, schema 0143.

- GET /v1/projects/{project}/opportunities: the project's opportunities as they are now, by rank, for a member with a
  grant on the project; ``?status=`` keeps those of one status. GET .../opportunities/{ref}: one opportunity, by its
  code (O-<number>), at its newest revision or at ``?revision=``, with the count of its lines of evidence and of the
  independent sources they come from. GET .../opportunities/{ref}/revisions: its revisions, newest first, each with the
  fields whose value it changed.
- PUT .../opportunities/{ref}: the opportunity's next revision, for a writer of the project, made when the project has
  no such opportunity yet: a new one takes the next code. The body is the opportunity whole; ``rank`` and ``owner``
  left out keep their value, and a new opportunity goes after the project's open ones and is owned by its writer. A
  body equal to the newest revision writes nothing (``changed`` false). ``if_revision`` other than the revision now is
  409, and 0 creates the opportunity only. POST .../opportunities/{ref}/close: the next revision, captured, missed or
  dropped with a reason.
- The agent of a run never writes an opportunity: @builder proposes one (proposal kind ``opportunity``,
  ``evo_agents.core_plugins.builder``), which a writer approves, and the hub writes it through ``write``.

``answers`` lists the problems the opportunity answers, at most 20: ``P-3`` is a problem of its own project, and
``<project>/P-3`` one of another project. A code of another project is accepted only while the writer holds a grant on
that project, the problem exists and is not dropped (422 naming the code otherwise); a code the opportunity holds
already may stay. A reader sees the codes of the projects they hold a grant on, and ``hidden_answers`` counts the
rest, without their code or content.

A member without a grant on the project gets 404, as for every route of a project. A metric names a key of kind number
of the project's registry of signals, and has no target; the owner is a member with the writer role or above. Writes of
one project's opportunities wait for each other (a transaction-level advisory lock). Each revision adds an audit row
``opportunity.set`` naming the opportunity, the revision and the fields whose value it changed, never a value.

The package has four layers, which lint-imports keeps in this order (contract hub-opportunities-layers in
pyproject.toml): ``routes`` passes a ``Scope`` (``evo_agents.hub.server.base.scope``) to ``service``, which checks and
decides, and reads and writes through ``repository`` (``OpportunityStore``, a ``RevisionedStore``); ``models`` holds the
request and response models. Other modules import ``service`` and ``models``, never ``routes``.
"""
