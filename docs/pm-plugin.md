# The core plugin pm: the daily report of a project

The plugin **pm** answers, for a project that enabled it, what the team did on the last working day and what needs a
project manager today: the use cases and tasks that reached done, where the sprint stands against what it held when it
opened, the tasks that are late, in review, stale or blocked, what to escalate and how loaded each member is. The report writes
nothing: it reads the hub's tables for the plugins tasks (`docs/tasks-plugin.md`) and srs (`docs/srs-plugin.md`), the
plans, runs and decisions. Its code is `evo_agents/core_plugins/pm`; evo-ak ships it as a core plugin
(`docs/plugins.md`, Core plugins). It does nothing in a project until an admin of the project enables it there, and it
needs the plugin tasks enabled too (without it the report is a 409 saying so).

## Enabling it

```sh
evo-agents hub plugin enable pm --project evo-agents        # asks for notifications:send and chats:write
evo-agents hub plugin config pm --project evo-agents --set pm=alice --set timezone=Asia/Ho_Chi_Minh
```

It asks for `notifications:send` and `chats:write` so that the morning job can reach a member and a group chat; the
report itself uses neither. Its config, in the project's layer (`docs/hub.md`, Plugins in a project):

| Field | What it holds | When left out |
| --- | --- | --- |
| `pm` | the login of the project manager, named in `escalate_to` | none |
| `timezone` | the IANA time zone whose days the report counts | `Asia/Ho_Chi_Minh` |
| `working_days` | the ISO weekdays that count, 1 (Monday) to 7 (Sunday) | `[1, 2, 3, 4, 5]` |
| `stale_days` | working days without activity after which a task being worked on is stale | 2 |
| `escalate_after_days` | working days a blocker is open after which it is to escalate | 1 |
| `at_risk_days` | a task still to do whose due date is this many working days away or less is at risk | 2 |
| `wip_limit` | tasks a member may have in progress before they are overloaded | 2 |
| `digest_chat` | the id of the group chat for the daily digest (optional) | none |

A time zone that is not an IANA name, or working days outside 1 to 7, make the report answer 409 with the reason until
an admin fixes the config. Holidays are not known.

## The report

`GET /v1/x/pm/{project}/daily?date=YYYY-MM-DD` (today in the project's time zone when `date` is left out) answers:

| Key | What it holds |
| --- | --- |
| `project`, `date` | the project and the day of the report |
| `previous_working_day` | the last working day before `date`: a Monday's is the Friday |
| `done_use_cases` | use cases whose task moved to done from the start of `previous_working_day` to the start of `date` (so a Monday's holds the weekend), and still is. A use case's task is its frame task, else the task opened last that is not cancelled; `outcome` is the task's `outcome_md` when it is not empty (docs/tasks-plugin.md), else the last section `## Kết quả` or `## Outcome` of its description, else null |
| `done_tasks` | the tasks that moved to done in that window and are done still |
| `sprint` | the active sprint: `scope` and `done_percent` of the tasks it held **when it opened** (rebuilt from the tasks' history, so tasks added or removed since count in `added` and `removed` only), `working_days` and `working_days_gone`, and the progress of each use case it works on |
| `late` | tasks past `due_on` and not finished (`overdue`, `days_late`), and tasks still to do due within `at_risk_days` (`at_risk`, `days_left`, today's included) |
| `review` | tasks in a status of group review, with `age_days` since they entered it and their QA |
| `stale` | tasks in progress whose latest activity is more than `stale_days` working days old, with its `source` |
| `blocked` | the open blockers of unfinished tasks, set by hand or open decisions of runs that belong to the task (`docs/tasks-plugin.md`, Blockers), with `age_days` |
| `escalate` | the blockers older than `escalate_after_days`, with `escalate_to` (the config's `pm`) |
| `load` | for each member with the writer role or more: `doing`, `wip_limit`, `overloaded` (more tasks doing than the limit), `empty` (none doing), `review_as_qa` and `late` |
| `hidden` | how many entries each list left out because the reader may not see them |

Every count of days is in working days of the project's calendar: a review entered on Friday is 1 day old on Monday, and
a task due on Friday is 1 day late on Monday. The activity that keeps a task from being stale is any row of its
history, a comment, a run linked to it (directly or through a plan step it links), or a change of the status of a plan
step it links, whichever is latest; with none, its creation.

### Who sees what

A reader reads. Tasks carry the label of the project (or the one an admin set) and are read as the routes of tasks read
them, through the sink the header `X-Evo-Sink` names, else the project's hub sink. A task the caller may not see appears
in no list and names nothing, and adds to the counts only (`hidden`, a member's `doing`). The route answers 404 for a
project that did not enable pm and for a member without a grant, and 409 when tasks is not enabled or the config
cannot be used.

## Tools and command line

The hub's `/mcp` lists, in a session whose project enabled pm, `pm__daily` (optional `date`) and `pm__member` (`login`,
optional `date`): the first is the report above for the caller, the second one member's day (the tasks they do and
review, those late, the blockers on their tasks and those that wait on them). They act for the member whose session
calls them, or who mentioned the agent of a chat run.

```sh
evo-agents hub pm daily --project evo-agents                 # today
evo-agents hub pm daily --project evo-agents --date 2026-10-19 --json
```

The command speaks HTTP only (`evo_agents/hub/cli_pm.py`).

## The morning job

The job `daily` runs at 01:00 UTC on weekdays (`0 1 * * 1-5`, `hub.plugin_jobs`). For each project that enables pm it takes
today's date in the project's `timezone`; on a day outside `working_days`, or when the pm or tasks config cannot be used,
it does nothing. On a working day:

- **The digest.** The report of the day, computed as the route computes it, goes as the notice `pm_daily` to the PM (the
  config's `pm`; the project's admins when it is empty or names a member with no grant) when the project granted the
  plugin `notifications:send`, and as a message of the hub in the group chat `digest_chat` when it granted `chats:write`
  and that chat still exists with an agent of the project (otherwise the job logs and skips it).
- **The escalations.** Each blocker in `escalate` sends the notice `pm_escalation` to the PM and to whom it waits on: the
  member, the assignees of the task it waits on, or the owner of the run whose decision is open. A blocker that waits on
  a text goes to the PM alone.
- **What they say.** Counts, task codes and a link to `/p/{project}/x/pm`; never a title, a reason or a description,
  because Telegram forwards a notice and a chat's label may be lower than a task's. A notice counts what its recipient may
  see, the report computed with their grant; a recipient who may not see a blocker's task gets no escalation for it. The
  chat message counts what the project's top grant sees.
- **Once.** The table `pm_sends` (migration 0086) holds one row per project, day and thing sent: the digest, and each
  blocker (`blocker:<id>` or `decision:<id>`). A row is claimed in the transaction that sends, so running the job again or
  twice the same day sends nothing more, and a failed pass claims nothing.

The notice kinds `pm_daily` and `pm_escalation` are in `runs.NOTICE_KINDS` and the check of `notifications.notice_kind`
(`docs/notifications.md`).

## Web

The manifest has one native page, `daily`, in the project's menu (`/p/{project}/x/pm/daily`; `/p/{project}/x/pm`, the
link of the digest, redirects to it). It shows the eight sections of one day (`?date=YYYY-MM-DD`, today in the project's
timezone by default): done, sprint, late, review, stale, blocked, escalate and load. Each task links to its page in tasks;
each blocker names who it waits on and its age. A 409 (tasks not enabled, or a config of pm the hub refuses) shows the
hub's reason with links to the plugins' pages. Code: `web/src/components/pm`, `web/src/app/(app)/p/[project]/x/pm`.

## Code

`workdays` counts days, `settings` reads the config of pm and the statuses of tasks, `reads` reads the facts from the
tables (SQLAlchemy only), `report`, `sprint_view` and `outcome` compute from them with no I/O (tests give a fixed date),
`service` joins the two for `server` (the route) and `agent` (the tools), `models` is the API and `job` is the morning job. Import-linter keeps it
in its own package (contract pm-plugin-own-package): it imports neither tasks nor srs, and reads their tables through
`evo_agents.hub.tables`.
