"""The problems of a project, kept as revisions (plan problem-oriented-hub, criterion a1; the model in
``evo_agents.hub.problems``). Tables problems and problem_revisions, schema 0134.

- GET /v1/projects/{project}/problems: the project's problems as they are now, by rank, for a member with a grant on
  the project; ``?status=`` keeps those of one status. GET .../problems/{ref}: one problem, by its code (P-<number>),
  at its newest revision or at ``?revision=``, with the goals whose newest revision answers it, the claims of the
  project's canvases that link it (plan problem-oriented-hub, criterion a3) and the plans whose body names it
  (criterion a4), each the member sees through the project's hub sink. GET
  .../problems/{ref}/revisions: its revisions, newest first, each with the fields whose value it changed.
- PUT .../problems/{ref}: the problem's next revision, for a writer of the project, made when the project has no such
  problem yet: a new problem takes the next code. The body is the problem whole; ``rank`` and ``owner`` left out keep
  their value, and a new problem goes after the project's open problems and is owned by its writer. A body equal to
  the newest revision writes nothing (``changed`` false). ``if_revision`` other than the revision now is 409, and 0
  creates the problem only. POST .../problems/{ref}/close: the next revision, solved or dropped with a reason.
- The agent of a run never writes a problem: @builder proposes one (proposal kind ``problem``,
  ``evo_agents.core_plugins.builder.proposals``), which a writer approves, and the hub writes it through ``write``.

A member without a grant on the project gets 404, as for every route of a project. A metric names a key of kind number
of the project's registry of signals, and has no target; the owner is a member with the writer role or above. An
archived key a revision names already may stay. Writes of one project's problems wait for each other (a
transaction-level advisory lock). Each revision adds an audit row ``problem.set`` naming the problem, the revision and
the fields whose value it changed, never a value.

The package has four layers, which lint-imports keeps in this order (contract hub-problems-layers in pyproject.toml):
``routes`` passes a ``Scope`` (``evo_agents.hub.server.base.scope``) to ``service``, which checks and decides, and reads
and writes through ``repository`` (``ProblemStore``, a ``RevisionedStore``); ``models`` holds the request and response
models. Other modules import ``service`` and ``models``, never ``routes``.
"""
