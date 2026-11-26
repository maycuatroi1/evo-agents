"""The goals of a project, kept as revisions (plan loop-engine, criterion a2; the model in ``evo_agents.hub.goals``).
Tables goals and goal_revisions, schema 0091.

- GET /v1/projects/{project}/goals: the project's goals as they are now, by rank, for a member with a grant on the
  project; ``?status=`` keeps those of one status. GET .../goals/{ref}: one goal, by its code (G-<number>) or its
  alias, at its newest revision or at ``?revision=``. GET .../goals/{ref}/revisions: its revisions, newest first, each
  with the fields whose value it changed.
- PUT .../goals/{ref}: the goal's next revision, for an admin of the project, made when the project has no such goal
  yet: a new goal takes the next code (``G-<number>``), never an alias. The body is the goal whole; ``rank`` and
  ``owner`` left out keep their value, and a new goal goes after the project's other goals and is owned by its writer.
  A body equal to the newest revision writes nothing (``changed`` false). ``if_revision`` other than the revision now
  is 409, and 0 creates the goal only. POST .../goals/{ref}/close: the next revision, closed with a reason. A revision
  that changes the statement or the metric makes the open proposals of the Loop's brainstorms of the goal stale
  (``brainstorms.stale_for_goal``): a member no longer judges claims brainstormed for a goal that is no more.
- The agent of a run never writes a goal: @builder proposes one (proposal kind ``goal``,
  ``evo_agents.core_plugins.builder.proposals``), which an admin approves, and the hub writes it through ``write``.

A metric names a key of kind number of the project's registry of signals; a canvas, one of the project's canvases;
a problem (plan problem-oriented-hub, criterion a2), one of the project's problems that is not dropped
(``evo_agents.hub.server.problems``), or an opportunity (plan opportunity-intake) that is not dropped, never both; the
owner, a member with the writer role or above; an SRS document of the project that is not archived (``srs_document``, its
slug) with a heading of it (``srs_heading``) to limit to, which the metric ``srs.backlog.open`` of the hub's own counts the
backlog of (plan srs-backlog-loop; no key of the registry of signals is needed for it). An archived key or canvas, or
a dropped problem, a revision names already may stay. Writes
of one project's goals wait for each other (a transaction-level advisory lock). Each revision adds an audit row
``goal.set`` naming the goal, the revision and the fields whose value it changed, never a value.

The goals of the Curator's charter (``{id, what}``). The Curator reads the project's goals here (``curator_goals``, for
the lens ``product_goals`` of the review run). A goal of the project's newest charter that no goal keeps as its alias
yet becomes a goal when the goals are read (``adopt_charter_goals``; a write does not, so the code a writer read as
the next one stays free): no metric, its id as the alias, its text as the statement, the charter's order as the rank
after the project's other goals, the charter's writer as owner and writer, and an audit row with no actor. Once a goal
of the charter is a goal, ``evo-agents hub goal set`` changes it; the charter's revisions keep their text as they are.

The package has four layers, which lint-imports keeps in this order (contract hub-goals-layers in pyproject.toml):
``routes`` passes a ``Scope`` (``evo_agents.hub.server.base.scope``) to ``service``, which checks and decides, and reads
and writes through ``repository`` (``GoalStore``, a ``RevisionedStore``); ``models`` holds the request and response
models. Other modules import ``service`` and ``models``, never ``routes``.
"""
