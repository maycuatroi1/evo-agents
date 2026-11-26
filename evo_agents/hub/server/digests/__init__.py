"""Session digests: what the Stop hook of the evo-hub plugin pushes about each Claude Code session of a project's
directory. ``evo_agents.hub.digest`` builds a digest from the session's transcript, and ``evo_agents.hub.redact``
cleans it on the machine before it is sent.

PUT /v1/projects/{project}/digests/{session_id} writes the digest of one session under the write rule of the project:
the writer role, and a sink of kind hub that clears the project's default label, which the digest carries. A push of a
later turn of the session replaces it; only the member who pushed the session first may (403 for anyone else). The
body is checked against the limits of ``evo_agents.hub.digest`` and stored as the model reads it: a field the hub does
not know is dropped, so a newer client works with an older hub. A request is at most MAX_REQUEST bytes, read before it
is parsed. A push writes no audit row, since one comes after every turn of a session; the log line names the project
and the session, never what the digest says.

GET /v1/projects/{project}/digests lists the digests the caller may read, the latest pushed first, filtered by
``since`` (pushed at or after) and ``login`` (who pushed them), with how many match; GET .../digests/{session_id} is
one of them with the digest itself. Reading needs a grant on the project and goes through the read rule with the
digest's label, through the sink X-Evo-Sink names, else the project's hub sink, as runs and plans are read. A digest one
may not read answers as one that does not exist. A list examines at most SCAN_ROWS digests.

``prune_digests`` (the job hub.prune_digests, daily) deletes the digests not pushed again for DIGEST_DAYS days.

The summary push of Today (``service.today``), the job hub.today_digest, sends each member who is away a push of
their Today once a local day.

The package has four layers, which import-linter keeps in order (contract ``hub-digests-layers``): ``routes`` (the
HTTP routes), then ``service`` (who may push and read a digest, and the summary push of Today), ``repository`` (every
SQL statement, on ``evo_agents.hub.tables``) and ``models`` (the Pydantic bodies, the limits of a request and the
texts of the refusals).
"""
