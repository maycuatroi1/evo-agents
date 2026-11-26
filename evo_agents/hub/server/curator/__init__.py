"""The night shift on the hub: a project's charter, its schedules, pausing them, the job that queues their runs, and
the threads in which the Curator's proposals are answered. ``evo_agents.hub.curator`` holds the model: the window,
the night, the caps and how a run's cost is read.

GET /v1/projects/{p}/curator/charter (reader) shows the project's charter at its newest revision, or at ``?revision``;
404 when it has none. The checks of the Judge (``judge.hidden_checks``) are shown to the project's admins alone, and as
null to anyone else. GET .../curator/charter/revisions lists every revision, newest first: none is ever deleted.

PUT .../curator/charter (admin of the project) writes a new revision: the time zone of its window, or of its pacing,
must be one Postgres knows; a charter has one of the two, and with pacing ``night_budget_usd`` is left out. The
charter names no worker and no pool: the night shift runs on the pool of the project's default agent
(``evo_agents.hub.server.night_duty``). The project's schedule (one per kind, here ``night_shift``) follows it: its
owner, the member it dispatches as, is that pool's owner. A body equal to the newest revision's writes nothing and
answers that revision. ``judge.hidden_checks`` null keeps the checks of the newest revision. The keys ``charter show
--json`` prints besides the body (``curator.CHARTER_META``) are ignored, so what was shown can be written back. Each
revision is audited (curator.charter). A member without the admin role gets 403, a worker token 403 from the guard of
/v1, before any of this.

GET /v1/projects/{p}/curator (reader) says where the night shift stands: the charter, whether it is paused, each
schedule with its owner and worker, and the night now (or the last one, outside the window): its runs and its cost,
the run of the schedule that is queued or held, if any. POST .../curator/pause (an admin of the project, or the owner of
one of its schedules) pauses every schedule of the project at once and cancels each run they queued that is still
queued and resumes no parked run; .../curator/resume lets them run again, a night shift the circuit breaker paused
included, and starts its count of jobs gone wrong again. Each is audited (curator.pause,
curator.resume); a second pause keeps the first one's time. GET .../curator says the night shift's ``state`` in a word
(CURATOR_STATES) and how many of the project's proposals wait for an answer; ``curator_overview`` says the same of
every project of the caller for GET /v1/me/overview. GET .../curator/nights (reader) lists the latest nights, newest
first: for each, the runs the schedules queued for it by how they stand, their cost as a night's cost is counted, its
review run with what it wrote, and whether curator.collect counted its figures.

``fire_schedules`` (``service.shift``) is the job ``hub.fire_schedules``, run every minute by
the hub's worker: for each schedule of kind night_shift, under its row's lock (a schedule another run of the job holds
is passed over), it first lets the circuit breaker look at the night (``outcomes.check_circuit``: once the charter's
``circuit_breaker.max_failed_in_a_row`` jobs of the night in a row failed of the agent's cause or were reverted,
every schedule of the project is paused with the reason in ``pause_reason`` and no member in ``paused_by``, and their
owner gets the notice curator_paused), then it cancels its queued runs when it is paused, and outside its window
those of the night (``shift_hours``), and otherwise queues at most one run (``gate_of`` says when it may): the
night's review run first, when the night has none yet and the worker runs review runs
(``evo_agents.hub.server.collect``), else the night's
history run once its review run was queued (``evo_agents.hub.server.history``), these two inside the window alone,
else the judge run of a change of the Curator that waits for one, else the Builder of a change the Curator planned
(``evo_agents.hub.server.changes``), else the replay run of a change that waits for one
(``evo_agents.hub.server.replays``), else, inside the window, a plan run of the charter's night_plans, as
``evo_agents.hub.curator`` says, audited as curator.dispatch with the owner as actor and no token, else, when no plan
of night_plans has work, the probe run of a probe and a model the week has not measured, within the charter's
probes.budget_usd (``evo_agents.hub.server.probe_runs``). GET .../curator also names the last review run of the
project, with how many findings and proposals it wrote, and its last morning brief (``evo_agents.hub.server.brief``).
A night's cost is that of its runs (their ``usage``), each agent session once at its largest total, as Claude Code
reports a session's running total. The charter also names what the Curator's runs get of the owner's secrets
(``git_secret``, ``env_secrets``): no other secret is leased to them.

The package has four layers, which import-linter keeps in order (contract ``hub-curator-layers``): ``routes`` (the
HTTP routes of the charter and the night shift, ``charter``, and of the agent of a proposal's thread, ``threads``),
then ``service`` (who may do what and what a route answers: ``charter``, ``status``, ``shift``, the job, and
``threads``), ``repository`` (every SQL statement, on ``evo_agents.hub.tables``) and ``models`` (the Pydantic bodies,
the charter's settings, the audit actions and the limits). Other modules import ``service`` and ``models``, never
``routes``.
"""
