"""The history of a project for the Curator. The night's history run on the hub (``service.runs``): queueing it,
its claim, the batches its worker reads and what it reports, the clusters of the facets, and reading them.
``evo_agents.hub.history`` holds the model and ``evo_agents.hub.excerpts`` what the model reads of a transcript.

The items are the facets of session_facets (``evo_agents.hub.facet_job``) of a session whose own transcript the hub
holds, and of a run that ended (not a local run, whose session's transcript is the item, and not a history run), that
the schedule's owner may read: a session by its label through the project's hub sink, a run when it is of a plan the
owner may read or a review run. An item waits while it has no facet of the model of LLM_VERSION written from its
content as it is now (``llm_fingerprint`` is its facet's fingerprint), unless its last attempt failed and its
``llm_retry_at`` has not come.

The night shift (``curator_shift._fire``) queues, right after the night's review run and before anything else, the night's
history run (``queue_history``): one a night, once the night's review run was queued, while an item waits or a group of
facets has no name yet, with a cap of its own (``history.budget_usd`` of the charter, under the next run's caps; none
at 0), on the reviewer's runtime with the charter's ``history.model``, else the cheapest model the worker on duty
reports for it (``history.model_for``). It is a run of the Curator on no plan and no repo, pinned to the worker on duty,
as the schedule's owner, audited as curator.history; its claim hands the daemon the run's own key, whose SHA-256 alone
the hub keeps in history_runs (``history_claim``).

POST /v1/worker/runs/{id}/history/next, with that key (``history.HISTORY_KEY_HEADER``), hands the worker the next
batch: up to ``history.BATCH_ITEMS`` waiting items, the latest first, each with its excerpt (``excerpts``: a session's
own transcript read from the blob store, a run's log or its events, cut around its errors and corrections) and the
fingerprint it was read at; an item whose content cannot be read now waits as a failed attempt does. With ``clusters``
the hub groups the facets again first (``regroup``) and adds the groups that have no name yet, with some of their
facets, and the groups named before. POST .../history/report takes the facets the model wrote and the names it gave:
a facet of content that changed since it was handed out is left (``stale``), an item the model gave none waits
``history.RETRY_HOURS`` (a week after ``history.MAX_ATTEMPTS`` attempts), and a group keeps the first name it got.

The clusters (``regroup``) are counted again each night when curator.collect counts the night's figures, from the
facets of the model the owner may read: per group, its sessions and runs, its members, its latest sessions; the
night's figures hold, under ``clusters``, those of ``history.MIN_CLUSTER_SESSIONS`` sessions or more, groups the model
joined counted as one, and under ``facets.llm`` how far the facets of the model are (``coverage``).

GET /v1/projects/{p}/curator/history (reader) says where the project's history run stands: its cap and model, how far
the facets of the model are, the latest history runs and what each wrote; GET .../curator/history/clusters (reader) the
clusters of the night's figures. Both answer only a caller whose sink clears the project's default label, as the
night's figures do.

Reading a project's whole history (``service.search``): its sessions and runs with the numbers of their facets, the
words of its transcripts, run logs and group chats, and the lines of a transcript around one. What the Curator's
Reviewer reads in two tiers, the numbers first and the words where they point, through the hub's MCP tools (``mcp``),
and what a member reads with ``evo-agents hub curator history``.

GET /v1/projects/{p}/curator/history/sessions lists the sessions and runs of the project's facets
(``evo_agents.hub.facet_job``), the latest first, filtered by time (``since``, ``until``: UTC days), member, runtime,
kind, outcome (what the cheap model wrote of a session or a run, ``history.OUTCOMES``, or a run's state) and cluster
(the key of a cluster of the history run, the groups the model joined to it counted in it), each with
the numbers of its facet (``facets.summary``) and what the model wrote of it. It examines at most SCAN_ROWS facets.

GET .../curator/history/search finds the words of a query (``websearch_to_tsquery`` of the configuration ``simple``:
words, "a phrase", -excluded) in the rows of history_lines (``evo_agents.hub.history_index``) and in the messages of the
group chats the caller is in, best first (``ts_rank``), each with a few words around the match (``ts_headline``) and the
evidence that names it: ``transcript:ID[:FILE]:LINE`` or ``run:ID:SEQ``. Each source answers at most SCAN_HITS hits
before they are merged.

GET /v1/projects/{p}/transcripts/{session_id}/lines reads the lines of one transcript file around a line, from the
blob store, each as its entries: what the person wrote, what the agent said, each tool call and its result.

Each needs a grant on the project and reads what the caller may read, as the hub's other routes read it: a
transcript and a facet of a session by their label through the sink X-Evo-Sink names (else the project's hub sink); a
run as ``runs.service.views.readable_run`` reads it (a local run its member's alone, a run on no plan through the
project's default label, a run of a plan the caller may read); a chat's message when the caller is in the chat now,
clears its label now (``chat_access``) and cleared the message's (``LabelReader``), and the message names the project
with a part of its label visible through that sink.

The agent of a run (the caller of the hub's MCP tools with a ``RunScope``) reads less than its owner, since what it
reads lands in its run's events and in what it writes (a finding, a chat's reply), which readers who are not the
owner read: no replay run, and a group chat or a local run only in a review run, whose Reviewer reads the chats and
the local sessions of the charter's owner (``views.agent_reads``, ``views.agent_reads_chats``). Any other agent
searches no group chat (AGENT_SOURCES; asking for one is 403). The Reviewer's chat hits go through the sink as the
owner's do; what it reads of a chat can reach its run's trace and its findings, a debt kept so it reads the whole
history.

The package has five layers, which lint-imports keeps in this order (contract hub-history-layers in pyproject.toml):
``routes`` (the routes, a worker's with the run's own key) and ``mcp`` (the answers of the hub's MCP tools) hand the
request to ``service`` (``readers``, ``runs``, ``search``), which checks and decides, and reads and writes through
``repository``; ``models`` holds the request and response models and their limits. Other modules import ``service``,
``mcp`` and ``models``, never ``routes``.
"""
