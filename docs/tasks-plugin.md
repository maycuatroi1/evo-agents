# The core plugin tasks: a project's backlog on the hub

The plugin **tasks** keeps a project's tasks in the hub's database: a backlog and sprints, the people of each task, and
links to the use cases and requirements of the plugin srs (`docs/srs-plugin.md`), to plan steps, runs and pull
requests. Its code is `evo_agents/core_plugins/tasks`; evo-ak ships it as a core plugin, so every hub has it
(`docs/plugins.md`, Core plugins). It does nothing in a project until an admin of the project enables it there.

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

- **A task** has a number in the project, shown after the prefix (`T-42`), a type, a title, a Markdown description, a
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

A target the member cannot see (a document or a plan whose label is above the clearance of the session's sink) is not
linked, and an existing link to one shows `hidden` and nothing of it.

Migration 0047 adds the six tables, each with the plugin's prefix: `tasks_sprints`, `tasks_items`, `tasks_people`,
`tasks_links`, `tasks_events` and `tasks_comments`. Going back to 0046 drops them.

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
evo-agents hub task import notion-tasks.json --project evo-agents --dry-run
```

`--assignee` gives the whole list of assignees, and `--role ROLE=LOGIN` the whole list of each role it names (`--role
qa=` empties it). `--use-case`, `--step`, `--run` and `--pull` add links, `--unlink` drops one by the id `show` prints.

## Routes

Under `/v1/x/tasks/{project}/`:

| Route | What |
| --- | --- |
| `GET settings` | the prefix, the statuses with their groups, the types, `on_steps_done`, and whether the project enables srs |
| `GET tasks` (`?status=`, `?sprint=`, `?backlog=true`, `?mine=true`, `?assignee=`, `?limit=`) | the tasks the caller sees that the filters keep, by number |
| `GET tasks/{ref}` | a task whole: its links as they are now, its history and comments |
| `POST tasks` | a new task with its people and links (201) |
| `PATCH tasks/{ref}` | a change of its fields, people and links; `unlink` drops links by id |
| `POST tasks/{ref}/comments` | a comment (201) |
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
`run_as` (`docs/plugins.md`, Agent types), and a member's own session for that member. The agent of any other kind of
run is refused. The person must hold writer on the project when the tool runs. The task is theirs, carries the
project's label, takes the config's first type and status unless the call names them, and links the SRS items it
names by code (`links`) while the project enables srs. The tool's description tells the agent to name assignees only
when the person asked for them.
