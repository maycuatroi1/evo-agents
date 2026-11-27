"""Task runs a member hands an agent with no Git: POST and GET /v1/projects/{p}/task-runs (``docs/hub.md``, section
"Member task runs").

POST /v1/projects/{p}/task-runs takes a task, a JSON object as its input and up to MAX_ASSETS external asset URIs,
and queues a run of kind task with no agent type and no repo (runs.repos NULL, member_tasks 0263): a writer of the
project dispatches it, under their own principal, autonomy ceiling and accounts, as any run they dispatch
(``runs.service.dispatch.insert_run``), with the project's default agent profile or the one ``profile`` names; a web
session, an app session (``eva_``) and a token each may. Its worker claims it as a task run (``claim_rules``) and
builds no worktree; the agent reads the task, its input and its assets in its prompt
(``task_runs.build_member_task_prompt``) and writes its result as its run's summary. A profile that runs on the hub
(``runs_on: hub``) has no worker: its task run queues with runtime hub, the model's gateway prefix as a chat turn of
the agent gets it, and the job of the hub runtime deferred in the same transaction (``run_state.notify_queued``), which
takes it as one turn whose last answer is the output (``hub_runtime``).

GET /v1/projects/{p}/task-runs/{task} gives whoever reads the run (``runs.service.views.readable_run``) the task as
its latest attempt has it: its state, its input and assets, and its output, the summary its agent wrote. When it ends,
the project's webhooks that take task.result get it (``task_dispatch.task_run_ended``, ``webhook_events``).

The package goes from its routes to its service to its repository to its models (contract hub-member-tasks-layers):
``routes`` hands the request's parts and the app's engine to ``service``, which opens the transaction (``UnitOfWork``),
checks the caller and dispatches; ``repository`` (``TaskStore``) reads and writes runs and member_tasks; ``models``
holds the bodies and the limits. Other modules import the names below, never the routes.
"""

from evo_agents.hub.server.member_tasks.models import MAX_ASSETS as MAX_ASSETS
from evo_agents.hub.server.member_tasks.models import MAX_INPUT_BYTES as MAX_INPUT_BYTES
