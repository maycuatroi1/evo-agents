"""The intakes of a project (plan problem-intake, criterion a2; the model in ``evo_agents.hub.intakes``). Table
intakes, schema 0138.

An intake is one reading of vague context (a chat, a mail thread, a folder) by the session of a member: the member
starts it with the kinds and labels of the sources the session will read, never their content, and puts what the
session made of them once, as an ``evo-intake/1`` document (``evo_agents/harness/schemas/intake.schema.json``).

- POST /v1/projects/{project}/intakes: a new intake I-<n>, open, for a writer of the project. GET .../intakes (``?status=``)
  and GET .../intakes/{ref}: for a member with a grant on the project.
- PUT .../intakes/{ref}: the result, once (409 when the intake is done). The document is checked against the schema;
  every refusal is a 422 that names each reason and writes nothing:
  - a candidate with one independent source (a source is a kind and an address) that nobody confirmed. A candidate is
    confirmed by ``confirmed_by``, a member of the project, or by a confirmed line of the problem it matches; for a
    candidate that matches a problem, the problem's lines count with its own;
  - a line labelled personal, an excerpt over 300 characters, a line the problem holds already, a P-<n> the project
    lacks, a date that does not exist.
- Each candidate becomes a pending proposal of @builder, kind ``problem``, without a chat: a new problem takes the next
  free code (the codes of one put follow each other), a candidate that matches a problem proposes its lines as
  evidence of it. A writer approves them as any proposal of @builder; the agent of a run never puts a result. With no
  thread to answer in, a writer finds them on the Problems and Opportunities pages of the web, or with ``evo-agents
  hub intake proposal list`` (both read /v1/x/builder/<project>/proposals/waiting), and answers each there.
- ``evo-intake/2`` adds ``opportunities``: candidate opportunities, checked and proposed by ``opportunity_candidates``
  (a pending proposal of @builder, kind ``opportunity``, for each); a document of ``evo-intake/1`` is accepted as
  before.
- Each promise becomes a task of the plugin tasks when the project enabled it: due on the date the session inferred,
  assigned to the promiser when they hold a grant on the project and to the member who asked for the intake otherwise,
  its description saying who promised, from which source and whether the session saw evidence that it was delivered.
  Without the plugin the promises stay in the intake, and the answer says so (``tasks_skipped``).

Writes of one project's intakes, problems and proposals wait for each other (the lock of ``problems.write``). The hub
layer cannot import a core plugin, so the proposal rows and the tasks are made here through the tables, as the plugin
srs does for its frame tasks.

The package has five layers, which lint-imports keeps in this order (contract hub-intake-layers in pyproject.toml):
``routes`` passes a ``Scope`` (``evo_agents.hub.server.base.scope``, the scopes of the problems) to ``service``, which
checks a result and makes its proposals and tasks; ``opportunity_candidates`` checks and proposes the candidate
opportunities; ``repository`` reads and writes the tables; ``models`` holds the request and response models. Other
modules import ``service`` and ``models``, never ``routes``.
"""
