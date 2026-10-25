# The hub feature tasks: a project's backlog on the hub

The plugin **tasks** keeps a project's tasks in the hub's database: a backlog and sprints, the people of each task, and
links to the use cases and requirements of the plugin srs (`docs/srs-plugin.md`), to plan steps, runs and pull
requests. Its code is `evo_agents/hub/server/tasks`, a feature of the hub, so every hub has it. It does nothing in a
project until an admin of the project enables it there.

A feature of the hub since migration 0147, a core plugin before it, it keeps the plugin's names: the routes under
`/v1/x/tasks/`, its MCP tools, its config (`hub plugin config tasks`), and `hub plugin enable tasks`, which turns the
feature on in the project's layer of its features. A hub admin turns it on or off in the administration, Features,
for the hub or for one project. `hub plugin list` and /admin/plugins do not list it (`docs/plugins.md`, Core plugins).

## Enabling it

```sh
evo-agents hub plugin enable tasks --project evo-agents        # asks for plans:read
evo-agents hub plugin config tasks --project evo-agents --set prefix=SHOP --set on_steps_done=in_review
```

It asks for `plans:read`: with that grant the hub delivers it the event `plan.step` (section Plan steps). Its config,
in the project's layer (`docs/hub.md`, Plugins in a project):

| Field | What it holds | When left out |
| --- | --- | --- |
| `prefix` | what a task's number is shown after: `T` in `T-42` (an upper-case letter, then at most 9 letters or digits) | `T` |
| `statuses` | the statuses in the order of the board, each a `key`, a `name` and a `group`: `todo`, `doing`, `review`, `done` or `cancelled`. A new task takes the first | Backlog, To do, In progress, In review, Done, Cancelled |
| `types` | the types a task may have; a new task takes the first | `task`, `bug`, `story` |
| `on_steps_done` | the status a task moves to once every plan step it links is done | a task keeps its status and shows its steps' |

A status's group says what it means, whatever the project names it. A config whose statuses or types repeat a key,
or whose `on_steps_done` names no status, makes every route answer 409 until an admin of the project fixes it. A
status taken out of the list stays on the tasks that have it: they show no group, and an edit of one picks a listed
status. Turning the plugin off deletes nothing.

## The model

- **A task** has a number in the project, shown after the prefix (`T-42`), a type, a title, a Markdown description, an
  outcome (`outcome_md`, Markdown, empty until someone states what the task came to; the pm daily report reads it before
  the section `## Kết quả` or `## Outcome` of the description, which it falls back to while the field is empty), a
  status, a priority (`urgent`, `high`, `medium` or `low`), a due date, a sprint, a parent task and a label, the
  project's unless an admin gives it another. A parent is another task of the project, never one below it.
- **Its people**: its assignees, and its BA, QA and Dev, each a list of members with a grant on the project.
- **A sprint** has a name, unique in the project, a goal, its dates and a state: `planned`, `active` or `closed`. One
  sprint of a project is active at a time. Closing one moves its unfinished tasks (those of a listed status whose group
  is not `done` or `cancelled`) to the sprint named, or to no sprint. A task of no sprint is in the backlog.
- **Its links**, each of one kind:
  - `srs_item`: a coded item of the plugin srs (a use case, a business rule, a screen, an NFR, a common business rule),
    named by any code or alias it holds, while the project enables srs. The link keeps the item, so a new code or
    title keeps it, and shows its code and its title in the latest version that has it. A project that turned srs off
    shows no such link.
  - `plan_step`: a step of a plan of the project (`PLAN#STEP`). Its title and status are read from the plan each time
    the task is shown, never copied, with the step's latest runs.
  - `run`: a run of the project, with its kind and state.
  - `pull`: a pull or merge request, by URL.
- **Its history**: every change is a row with the field, the old and the new value, and who made it, or the hub (no
  actor) and why.
- **Its comments.**
- **Frame tasks and child tasks.** When a version of an SRS document is approved, the plugin srs makes a frame task
  for each use case new in it (section Tasks of an approved SRS version), and @ba splits each into child tasks: tasks
  whose parent is the frame task, linked to the same use case. A frame task carries `external_key`
  `srs:<slug>:<version>:<code>` and, in `external_ref`, the document, the version and the use case.

A target the member cannot see (a document or a plan whose label is above the clearance of the session's sink) is not
linked, and an existing link to one shows `hidden` and nothing of it.

Migration 0047 adds the six tables, each with the plugin's prefix: `tasks_sprints`, `tasks_items`, `tasks_people`,
`tasks_links`, `tasks_events` and `tasks_comments`. Going back to 0046 drops them. Migration 0085 adds `tasks_blockers`
and the column `tasks_sprints.activated_at` (see Blockers); going back to 0084 drops both.

## Blockers

A blocker is a flag on a task, never a status: a task keeps its status while it is blocked, and it may have many open
blockers. A manual blocker is a row of `tasks_blockers`: the task, a reason (1 to 1,000 characters), who it waits on,
`since`, `cleared_at`, `cleared_by` and `created_by`. The waiter is in exactly one of three forms: a member with a
grant on the project, another task of the same project (never the task itself), or free text (at most 300
characters). A member without a grant, a task of another project or the task itself is refused with 422.

An open decision of a run linked to the task is a blocker read at read time, never a row: the run is linked by a `run`
link, or because it has a `plan_id` and `step_key` that match a `plan_step` link of the task. The waiter is the member
who dispatched the run (`runs.dispatched_by`), `since` is when the decision was asked, and the entry names the
decision. Answering the decision ends it.

`GET tasks/{ref}` lists all open blockers of both kinds (`blockers`), `GET tasks` gives each task a `blocked` flag, and
`?blocked=true` keeps the blocked ones. Each set and each clear writes a `tasks_events` row under the field `blocker`.
Setting, changing or clearing needs the writer role (readers get 403).

`tasks_sprints.activated_at` records when the sprint turned active; the sprints routes write it when the state becomes
`active`. A sprint that was already active before 0085 has it empty.

## Who does what

The routes answer a member with a grant on a project that enabled the plugin, and 404 in a project that did not.
Readers read the config, the tasks, a task whole, the sprints and the members a task may name. Writers create and
change tasks, set their people, link and unlink them, comment, keep sprints and import. Only an admin gives a task a
label of its own. Reads go through the sink `X-Evo-Sink` names, else the project's hub sink. A refusal is the hub's
error body, with an import's problems in `detail`; nothing is written. Creating, changing or importing tasks queues a
build of the project's knowledge graph when the project has a knowledge config on the hub.

## Plan steps

When a step of a plan turns done, the hub's event `plan.step` writes a row in the history of every task of the project
linked to that step: the hub saw the step go from its old status to done. When the config sets `on_steps_done` and
every plan step a task links is done now, the task moves to that status, unless its status's group is `done` or
`cancelled` already; the row of that move names the last step. The plan is never changed. The hub delivers an event
at least once, with the same delivery id on every try, and a task that has the row of a delivery already gets nothing
more, so a second try writes nothing.

## Tasks of an approved SRS version

When a version of a document of the plugin srs is approved in a project that enables both plugins, srs keeps the
project's tasks in step with the version's use cases once the hub delivers it the event `srs.version.approved`,
comparing the version with the one approved before (`docs/srs-plugin.md`, Tasks when a version is approved):

- a use case new in the version gets a frame task, `UC ORD-1: Place an order`, linked to it, of type `story` when the
  project has it (its first type otherwise), in the first status, made by the approver; its history names the hub
  and why;
- the open tasks linked to a use case that changed, itself or a business rule under it, get a comment of the approver
  with the link of the comparison of the two versions;
- the open tasks linked to a use case the version dropped move to the first status of group `cancelled`, their
  history naming the hub and why; without such a status they keep theirs and get a comment.

A task is open while its status's group is not `done` or `cancelled`. Then @ba gets a task run that splits each open
frame task of the document with no child task yet: it makes the child tasks with `tasks__create` (section The tool
tasks__create), for the approver. An approval of the same version again, such as an event delivered twice, makes no
task, comment or run, and a frame task is found by its `external_key`. srs writes these rows through the hub's tables,
as this plugin reads srs's items: neither imports the other.

## The event tasks.status

Every status a task takes emits the event `tasks.status`, in the transaction that set it
(`evo_agents/hub/task_status.py`, plan loop-engine):

| Cause | When | Who |
| --- | --- | --- |
| `create` | a task made on the web, with the CLI or with `tasks__create` (`from` is null) | the member, or the member an agent works for |
| `edit` | an edit that changes the status; an edit that leaves it emits nothing | the member |
| `import` | each task of an import, once the import has set the parents (`from` is null) | the member who imported |
| `srs_approval` | a frame task srs makes for a new use case (`from` is null), or a task srs cancels because the version dropped its use case | the hub |
| `steps_done` | `on_steps_done` moving a task once every plan step it links is done | the hub |

Its payload, `v: 1`: the task's id, number and ref (`T-42`), `from` and `to` with their groups (`from_group`,
`to_group`), `actor` (`person` or `hub`) with the member's id and login, `cause`, `source` (the SRS document and
version for `srs_approval`, the plan and step for `steps_done`, else null), the parent's id and ref, the SRS items it
links (id and code each) and the plan steps it links (plan and step each). It carries ids, codes and states, never a
title, a description or the text of the task's history. Plugins the project granted `tasks:read` get it, at least
once, and the hub keeps it in `hub_events`, where the Loop reads it (`docs/plugins.md`, section Extension points).

## Commands

`evo-agents hub task` is the client of the routes below (`evo_agents/hub/cli_tasks.py`). `--json` prints what the hub
answered, and a refusal prints the hub's reason and each problem it listed. Sprints have routes and no command.

```sh
evo-agents hub task list --project evo-agents --mine                  # the tasks you stand in a slot of
evo-agents hub task list --project evo-agents --sprint "Sprint 7" --status in_progress
evo-agents hub task list --project evo-agents --backlog
evo-agents hub task show T-42 --project evo-agents                    # links as they are now, history, comments
evo-agents hub task create --project evo-agents --title "Place an order with a card" --type story \
  --use-case "UC ORD-1" --step rollout#2 --assignee alice --role qa=bao
evo-agents hub task edit T-42 --project evo-agents --status in_review --pull https://github.com/acme/shop/pull/88
evo-agents hub task edit T-42 --project evo-agents --role qa= --unlink 7
evo-agents hub task edit T-42 --project evo-agents --outcome -    # the outcome from stdin; --outcome '' clears it
evo-agents hub task import notion-tasks.json --project evo-agents --dry-run
evo-agents hub task block T-42 --project evo-agents --waiting-on alice --reason "needs the API key"
evo-agents hub task unblock T-42 --project evo-agents [--blocker 3]
evo-agents hub task list --project evo-agents --blocked
```

`--assignee` gives the whole list of assignees, and `--role ROLE=LOGIN` the whole list of each role it names (`--role
qa=` empties it). `block --waiting-on` takes a login, a task ref (`T-7`) or text (`--text` forces text). `unblock`
without `--blocker` clears every open manual blocker; decision blockers end when the decision is answered. `--use-case`, `--step`, `--run` and `--pull` add links, `--unlink` drops one by the id `show` prints.

## Routes

Under `/v1/x/tasks/{project}/`:

| Route | What |
| --- | --- |
| `GET settings` | the prefix, the statuses with their groups, the types, `on_steps_done`, and whether the project enables srs |
| `GET tasks` (`?status=`, `?sprint=`, `?backlog=true`, `?mine=true`, `?assignee=`, `?blocked=true`, `?limit=`) | the tasks the caller sees that the filters keep, by number |
| `GET tasks/{ref}` | a task whole: its links as they are now, its history and comments |
| `POST tasks` | a new task with its people and links (201) |
| `PATCH tasks/{ref}` | a change of its fields, people and links; `unlink` drops links by id |
| `POST tasks/{ref}/comments` | a comment (201) |
| `POST tasks/{ref}/blockers`, `PATCH tasks/{ref}/blockers/{id}` | set a blocker (201); change or clear one |
| `GET sprints`, `POST sprints`, `PATCH sprints/{name}` | the sprints; closing one takes `move_to` |
| `GET people` | the members a task may name, by login, with their role |
| `POST import` | `{"data": <evo-tasks/1>, "dry_run": true \| false}` |

The web shows them on the plugin's pages, at `/p/{project}/x/tasks/board` in the project's menu (Tasks): the board of
a sprint by status, the backlog by sprint, the caller's own tasks, a task whole with its links, history and comments,
and the import, reached from the board's tabs.

## Importing: evo-tasks/1

An import reads JSON in the format `evo-tasks/1` once, from a tracker such as Notion or Jira that a script exported.
Keys the format does not name are ignored.

```json
{
  "format": "evo-tasks/1",
  "people": [{"key": "p1", "name": "Alice Nguyen", "github": "alice"}],
  "status_map": {"Doing": "in_progress", "DONE": "done"},
  "sprints": [{"key": "s7", "name": "Sprint 7", "starts_on": "2026-10-13", "ends_on": "2026-10-26", "state": "active"}],
  "tasks": [
    {
      "key": "n-001",
      "external": {"system": "notion", "id": "0000aaaa", "url": "https://example.invalid/task/0000aaaa"},
      "type": "story",
      "title": "Place an order with a card",
      "description_md": "",
      "status": "Doing",
      "priority": "high",
      "due_on": "2026-10-24",
      "done_on": null,
      "sprint": "s7",
      "parent": null,
      "assignees": ["p1"],
      "roles": {"ba": ["p1"], "qa": [], "dev": []},
      "use_cases": ["UC ORD-1"],
      "plan_steps": [{"plan": "rollout", "step": "2"}],
      "pulls": ["https://github.com/acme/shop/pull/88"]
    }
  ]
}
```

| Key | What it holds |
| --- | --- |
| `people` | each person a task names: a `key` of the file, a name and a GitHub login, which is their login on the hub |
| `status_map` | a status of the source to a status key of the project; a status it does not map must be a key already |
| `sprints` | sprints by `key`, each with a name, a goal, dates and a state (`planned` by default); a name the project has already is that sprint |
| `tasks` | at most 5000 tasks: `key`; `external` (the source's system and id, and a URL); `type`; `title`; `description_md`; `status`; `priority`; `due_on`; `done_on`; `sprint` and `parent` by key of the file; `assignees` and `roles` by key of `people`; `use_cases` by code; `plan_steps`; `pulls` |

A dry run reads the file against the project and writes nothing; it lists the problems. **Errors stop the import**: a
status that is no status of the project and that `status_map` does not map, a type the project does not have, a
priority other than the four, a sprint key the file does not have, two sprints that would be active at once, keys or
external ids that repeat. **Warnings leave something out** and the import goes on: a person with no GitHub login, or
one with no grant on the project, whose slots stay empty; a use case code no SRS item holds, kept in the task's
`external_ref`; a plan step that is not there; a parent the file does not have; a sprint the project has already,
which its tasks join.

A task whose `external` (system and id) an import brought in already is skipped, so the same file imported twice adds
nothing the second time. The tasks are numbered in the file's order, and a `done_on` is a row of the task's history.
`tests/hub/fixtures/tasks/demo-shop-tasks.json` is a whole example.

## The knowledge graph

The plugin is a source of the project's knowledge graph (extension point `kg_sources`, `docs/plugins.md`): every task
is a Ticket node (`plugin.tasks:ticket:T-42`) with its number, title, description, label and, as properties, its type,
status and its group, priority, sprint, assignees and due date. A task `implements` each coded item of srs it links to,
while the project enables srs and a version of the item's document was approved, and each plan step it links to
`implements` the task, when the graph has the plan. Runs and pull requests are no nodes of the graph, so their links
are left out.

## The tool tasks__create

The plugin contributes the tool `tasks__create` to the hub's /mcp, listed in a session whose project enabled it. It
makes a task for the member an agent works for, such as @ba of the plugin srs making a task of a use case: the agent of
a chat run acts for the person who mentioned the agent, also when the run went to the worker of its agent type's
`run_as` (`docs/plugins.md`, Agent types), and a member's own session for that member. The task run of @ba that splits
the frame tasks of an approved SRS version acts for the member who approved it, whom the hub keeps for that run
(`srs_approval_tasks`), also in the next attempt of a lost run. The agent of any other task run, @ba's review
included, and of any other kind of run is refused. The person must hold writer on the project when the tool runs. The
task is theirs, carries the project's label, takes the config's first type and status unless the call names them, and
links the SRS items it names by code (`links`) while the project enables srs. The tool's description tells the agent
to name assignees only when the person asked for them.

`parent` puts the task under another task of the project, as `T-12`. A child whose title another child of the same
parent has, case and runs of spaces aside, is refused with 409 naming that child, so a split run tried again makes no
twin. The split run must name a parent, and only a frame task; its child tasks also link the use cases the frame task
links. Its schema:

| Argument | What it holds |
| --- | --- |
| `title` | required, at most 300 characters |
| `description_md` | Markdown |
| `links` | codes of the SRS items it implements |
| `parent` | the task it is part of, as `T-12` |
| `type`, `status`, `priority`, `sprint` | as a task made with the API |
| `assignees`, `ba`, `qa`, `dev` | logins, only when the person named them |
