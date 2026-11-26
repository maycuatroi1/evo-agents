"""Memories: the files of Claude Code's auto-memory, so every machine of a person, and the members of a project, see
the same ones.

A memory is one file of a memory directory, stored whole (frontmatter and all) with its type, a label and an integer
revision. Its scope is ``project`` when the directory is the harness root (location ``harness``) or a repo (location:
the repo's name) of a hub project, else ``personal``, where the location is what is left of the directory's slug
(``evo_agents.hub.memory`` maps directories and places both ways).

Who sees a memory: in a project, project and reference memories go to the members the read rule lets through the
sink the caller names, and user and feedback memories to their owner alone, under the same rule; personal memories go
to their owner alone. A memory one cannot see answers exactly as one that does not exist. The sink a read goes through
is the one the caller names; left out, a machine token reads through the Claude Code session's (claude-code@anthropic),
since a pull writes files that sessions read, and a web session through none: the web shows members their own view,
so the label must pass ``ProjectRules.visible_by_grant`` (the grant's max level alone). The agent of a run, on the
hub's /mcp with its worker's token (``Principal.scope``), reads and writes the memories of the run's project alone,
under its owner's grant as the scope caps it (``projects.project_access``): never a personal memory of its owner,
nor one of another project. Every read rule above holds the audience too (``audience``): a memory whose label
names one goes only to a reader whose audience, computed at that read, holds it.

PUT /v1/memories writes a memory by its key: scope, project, location and name, plus the owner for the types only the
owner sees. ``if_revision`` is the revision the writer last saw: none to create (a tombstone counts as nothing), the
current one to change it; anything else is a 409 that carries the current version when the caller may see it. The
same content again changes nothing and writes no audit row, whatever ``if_revision`` says. A write keeps or raises
the memory's audience, never lowers it: ``audience`` is what the writer read, and the hub adds what it showed the agent
of a run (``audience.raised``). PUT /v1/memories/{id}/audience lowers it, for an admin of the memory's project
who reads it, with the audit row ``memory.audience``. DELETE /v1/memories/{id}
leaves a tombstone: deleted, no body, the next revision. Writes into a project follow the write rule (the writer role,
and a label the project's hub sink clears; the current label, else the project's default, when the request names
none); personal memories need a signed-in caller. Every change appends one row to memory_revisions and one audit row
``memory:<id>`` in the same transaction. Neither the audit trail nor the logs carry a name or any content.

GET /v1/memories lists what the caller sees in (updated_at, id) order with an opaque cursor, tombstones included on
request, which is what a sync needs. GET /v1/memories/search ranks the full-text matches of name and body
(configuration simple, websearch syntax). GET /v1/memories/{id} is one memory. GET /v1/memories/{id}/revisions lists
its revisions newest first, and GET .../revisions/{revision} is one with its body; a revision whose type and label the
caller could not read is left out, as the memory itself would be. An agent's memory shows its upkeep
(``models.MemoryState``); POST /v1/memories/{id}/restore brings back one archived (``upkeep``).

Sizes are bounded: a body is at most MAX_BODY bytes of UTF-8, a request at most MAX_REQUEST bytes (read before
parsing), a page at most MAX_LIMIT memories and about PAGE_BYTES of bodies, and one request examines at most
SCAN_ROWS rows the caller cannot see.

The package is laid out in layers, each importing only those below it (contract ``hub-memories-layers``):
``routes`` (the APIRouters, which run no SQL), then ``service`` (the read and write rules: ``core``, the namespaces of
``agents``, the ``audience``, the Curator's ``promotion``, the ``upkeep`` of agents' memories and the daily job
``hub.memory_upkeep``, and the write's model in ``inputs``), then ``repository`` (every statement), then ``models``.
"""
