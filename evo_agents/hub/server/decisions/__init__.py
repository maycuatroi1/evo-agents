"""Decisions: the questions the agent of a plan run or an author run asks its owner, and the owner's answers.
``docs/notifications.md`` (Decisions) is the design, ``runs`` holds the categories, states and limits, and
``run_state`` moves the runs.

POST /v1/worker/runs/{id}/decisions takes a decision from the worker holding a plan run, an author run or a chat run
(404 for any other run: a run of one step, a review run, a judge run): a category of ``runs.DECISION_CATEGORIES`` (a
chat run's is ``triage`` alone, and ``triage`` is no other run's; 422 for any other, and for a question of a plan run
or an author run that asks for a credential's value in words, ``admit_ask``), a question, a context in markdown of at most 16 KiB, 2 to 6 options with unique keys, the key of the option the
agent recommends, and optionally why, whether the act it asks about can be undone and how, and the refs of what it
rests on (``models``, which holds the models, 422 for a ref in no form ``decision_asks`` reads), the step it
is about, which must be a step of the run's plan (422 for an author run that has put no plan yet, which has no steps),
and the outputs of the run it carries (``output_ids``, 422 for one of another run;
``outputs.service.store``), which every view of the decision lists with the decision's URL of their bytes. The decision keeps the plan the run has when its agent asks, none (``plan_id`` null)
for an author run that has put no plan yet. It answers 201 with the decision, whose id the agent waits for, leaves a
``system`` event in the run's log and notifies the run's owner (``notifications.notify``); the notification of a
decision without a plan names the author run and its title instead. A run has at most MAX_OPEN_DECISIONS open at once
(409). A plan run or an author run whose autonomy card leaves merging (K5) or deploying and releasing (K6) to its
agent gets 409 for a question of category merge or deploy, or a scope question asking leave to do it, and so does a
question in the category of any other class its card has auto (``autonomy_card.category_left_to_agent``: run
#1675 asked under access to merge, at A5); the reason quotes the card's rule (``admit_ask``,
``autonomy_card.left_to_agent``); a run without a snapshot asks as before.

POST /v1/projects/{p}/decisions/{id}/answer is for the person it is asked of (the run's owner), one it was escalated
to, or an admin of the project or of the hub (``runs.service.admin_acts``), with a web session or a machine token of
their own: an admin's answer to another member's run writes an event into the run's log naming the admin and the
dispatcher, and tells the dispatcher with a notice; another member gets 403, as does a worker's token (the middleware keeps it under /v1/worker/) and the token
of any run that runs as its owner (``run_access``), and someone without a grant on the project 404. A
machine token also gets 403 when the run is on a worker set to take runs dispatched from the web only, held, pinned or
parked there (``runs.web_only_steering``): its answer would steer that worker's agent, or resume the parked run on it.
A decision that is no longer open gets 409. The answer names an option of the decision, gives text of the owner's own
(at most 4 KiB), or both. Under the plan's dispatch lock (for a decision with a plan) and the run's row lock, the
decision is answered, its notification read, and the answer goes to a run's inbox as a message that names the decision
(``run_inbox.decision_id``), with a ``user_message`` event, so the heartbeat counts it and the worker hands it to the
agent like any message of the owner's:

- a run that is queued or held (``runs.MESSAGE_STATES``, waiting included) gets it in its own inbox;
- a parked plan run is resumed: the hub queues a new plan run pinned to the parked run's worker, with
  ``resume_of_run_id`` naming it, its session, repos, model, timeout and the agent time used so far, at the plan's
  current revision; the parked run is ``done`` with the reason ``resumed as #N``, its decisions still open go to the
  new run, and the answer goes to the new run's inbox;
- a parked author run is resumed so only once the answer leaves none of its decisions open. Until then the answer waits
  in the parked run's inbox, and the run that resumes it (on the last answer, or on a reply in its chat) takes every
  message of that inbox the worker has not handed to an agent, so the agent reads them all in its first turn.

The audit row decision.answer names the decision, its run and the option, never the text. An answer through a
channel of a core plugin (``core_plugin_api.answer_decision``) adds ``via=<kind>`` to it, and the decision keeps that
kind in ``answered_via`` (schema 0259), which every view shows: null for an answer from the web or the CLI. Words with no
option are a free answer, and the web and ``evo-agents hub decision show`` say so with the channel they came through.

A credential request (a decision of category credential that carries ``credential_request``,
``evo_agents.hub.credential_requests``) is asked of the run's owner whoever the run's principal reports to, and takes
the hub's options. Its answer may carry the value of the variable it asks for (``AnswerIn.secret``):
``credential_answers`` keeps it as the owner's secret of kind env for the run's project and lets the run run as them
when the option says so, for the run's owner, before a parked run is resumed, so the run that resumes it is
leased both. An admin answering another member's request gives a value alone: it becomes the admin's own secret, which
a share for that run alone lends it (``credential_answers``), never the dispatcher's. The decision's answer text, and the inbox message, carry the hub's sentence of what was given, by name,
never the value. Before it is asked, the hub looks for the owner's env secret of that variable
(``credential_resolver``): the run leased one, or one the hub may bind under K12 auto, and the hub answers the request
itself, answered by nobody, with a notice to the owner; else the decision's context names the secret, and the option
``found`` binds it when the owner chooses it.

An option the agent marks ``waits`` says not yet (``worker ask --wait-option``): choosing it leaves the decision open
with ``held_at`` set and its notification read, and sends nothing to the agent, so the run goes on waiting, or parked,
for a later answer (``_hold``); words with such an option are 422, since no agent would read them. A decision of
category wait is a wait of the agent's (``run_waits``): the hub answers it when its time or its event comes, and the
person asked may answer it sooner like any other.

A decision of category release is the hub's own, asked of the owner of a release cycle that stopped, on the cycle's
last run (``release_cycles``): its answer names one of its options, which ``release_answer`` does (go on, close the
cycle, or pause the schedule) whatever the state of that run, and goes to no inbox. So is a decision of category loop,
asked of the person a work item of the Loop stands for once it failed a third time, on its last run (``loop.stall``):
``stall.answer_loop`` does the option chosen (run it again, drop it, leave it to a person).

Reading decisions (``service.reads``): GET /v1/projects/{p}/decisions lists the project's decisions, newest first, of
the runs the caller reads, a page at a time, and GET /v1/projects/{p}/decisions/{id} shows one, with the outputs it
carries and where its message stands on each channel of a core plugin (``service.channels``).

The package has four layers, which lint-imports keeps in this order (contract hub-decisions-layers in
pyproject.toml): ``routes`` reads the caller's scope (``service.reads.GRANTED`` lists, ``service.reads.READERS`` reads
and answers) and hands the request to ``service``, which checks and decides: an agent's question (``asks``), an answer
(``answers``), the reads (``reads``), the channels (``channels``) and the run that resumes a parked run (``resume``);
``repository`` holds the SQL, ``models`` the request and response models. Other modules import ``service``,
``repository`` and ``models``, never ``routes``.
"""
