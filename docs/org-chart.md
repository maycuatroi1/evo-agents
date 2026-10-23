# The org chart

The hub keeps an org chart of the people and agent profiles that work through it: a tree of units, the seats people
and agents hold in them, and an autonomy ceiling, a level from A0 to A5, that a unit or a seat may carry. A unit below
another never loosens the ceiling of the units above it. Admins of the hub and the leads of units change the chart with
`evo-agents hub org`, `evo-agents hub autonomy ceiling set` and the web's `/org` page; every change has an audit row.

The chart grants nothing on a project. What a person may read or push in a project is still their grant on it
(`docs/hub.md`, Projects and who sees what), and adding an intern to a cohort still takes
`evo-agents hub admin grant <login> <project> --role writer --max-level <level>` of its own. In this release a
ceiling is a level that the hub keeps and shows. It gates nothing, with one exception: a person whose primary seat has
ended is at A0, and the hub stops their runs (Seats that end, below).

`evo_agents/hub/org.py` holds the rules as pure functions on a snapshot of the tables; `evo_agents/hub/tables_org.py`
the tables; `evo_agents/hub/server/org.py` the routes, `org_writes.py` the writes, `org_store.py` the reads and
`org_expiry.py` the seats that end; `evo_agents/hub/cli_org.py` the commands; `web/src/components/org/` the page.

## Units

A unit is the root, a team or a cohort. The tree has exactly one root, of kind `org` with slug `root` and name
Organization. Every other unit has a parent and is a `team` or a `cohort`. A slug is lowercase letters, digits and
dashes, at most 63 characters, unique among all units. Only a cohort has an end date (`ends_on`), its last day: once it
passes, every seat in the cohort ends (Seats that end). A cohort created without a level gets a ceiling of A1; a team
gets none and takes the ceilings of the units above it.

A unit is never deleted. It is archived, and only when no unit below it is still unarchived and no seat in it is live.
The root is never archived. A unit's parent may change, as long as the new parent is not archived and the tree keeps
no cycle. Each change of a unit, or of a seat in it, raises the unit's `revision` by one, and an edit may name the
revision it expects (`--if-revision`), which gives 409 when another change came first.

## Seats

A seat puts one person or one agent profile in a unit, with a role:

| Role | Held by | What it means |
| --- | --- | --- |
| `lead` | a person | leads the unit: changes the chart below it (Who changes what) |
| `member` | a person | works in the unit |
| `intern` | a person | works in the unit under someone: `reports_to` must name a live seat of another person |
| `agent` | an agent profile | the profile's seat; it always has a `sponsor`, the person who answers for it |

Each seat has `starts_on` (today when left out), an optional `ends_on`, and `ended_at` once it ends. `reports_to` names
a person's live seat, never an agent's and never one of the same person. A person holds at most one live seat in a
unit and at most one live primary seat overall; an agent profile holds at most one live seat, which is always
primary.

The primary seat decides a person's ceiling. A new seat of a person is primary when they ask for it (`--primary`), or
by default when their current primary seat is at the root or they hold none; the seat it replaces becomes secondary,
which takes edit rights on that seat unless it lies at the root. A primary seat is never made secondary directly: make
another seat primary instead.

When schema 0099 came in it wrote the root with a ceiling of A5 and a primary seat of role `member` at the root for
every user the hub had. A user made later, at their first sign-in or when a grant names a login that never signed in,
gets that seat in the same transaction. So everyone starts at the root, at A5, and the hub behaves for them as it did
before the chart. A login that signs in after it was granted is merged into the account: its seats move there, the
account keeps its own primary seat, and a second live seat in the same unit is ended.

Like units, seats are never deleted: they end, and the chart keeps them.

## Ceilings and the effective level

A ceiling is one of A0 to A5, with a note saying why. It belongs to a unit or to a seat. Every change writes a new
revision; earlier revisions are kept. A unit or a seat without a ceiling leaves the level to those above it.

The effective level of a person or an agent is the lowest of these ceilings: that of their primary seat, that of the
unit holding the seat, and those of every unit above it up to the root. Its source is the unit (`unit:<slug>`) or the
seat (`seat:<id>`) that sets it; when two set the same level, the source is the one nearest the seat. Three cases
differ:

- a primary seat that has ended gives A0, and the seat itself is the source;
- a person who holds no seat counts as a member of the root;
- a chain without any ceiling gives A5 from `unit:root` (the root always has one from schema 0099 on).

So a team under a unit at A3 is at most A3, whatever its own ceiling says, and a member of that team at A2 is at A2.

### Levels per class

A level is a bundle of modes, one for each of the twelve classes of action K1 to K12 (`deny`, `ask`, `notify`,
`auto`; the table is in [autonomy levels](autonomy.md)). A ceiling may also set the mode of single classes
(`--class K6=deny`, `--class K5=notify:ask_on_risk_paths`), and the environments and workers it allows
(`--environment`, `--worker`; none: no limit). Each class of a seat takes the strictest mode of the ceilings of its
seat and every unit above it, so a unit that caps K6 at ask holds every seat below it at ask on K6 whatever level they
carry. The effective level shown is the lowest level any of them sets; the effective mode of each class, with the
ceiling that set it, is the same computation over these two sources of the seven that bound a run (`docs/autonomy.md`).
The rule that nobody gives more than they have holds per class: a lead whose own K6 is ask cannot give a cohort below
K6 notify.

## Who changes what

Nobody may give more than they have:

- A hub admin (`EVO_HUB_ADMINS`) changes everything, the root and its ceiling included.
- The lead of a unit, someone with a live seat of role `lead` there, creates units below it; adds, edits and ends the
  member and intern seats of that unit and every seat of the units below it; edits and archives the units below it;
  and sets the ceilings of those units and seats.
- A lead never sets a ceiling above their own effective level, never the ceiling of the unit they lead or of a seat
  of their own, and never adds a lead to the unit they lead. A lead of a unit above does that.
- The agent of a run never changes the chart.
- Everyone else gets 403 with the reason, for example "creating a unit below lab needs a hub admin or a lead of lab
  or a unit above it".

A ceiling that a new unit takes from its parent is not checked against the lead's level; a level set explicitly is,
the cohort default A1 included.

Every write runs in one transaction under one lock of the whole chart, so two writes at the same time never break the
rules above: one root, no cycle, one live primary seat per person. Each writes one audit row whose action starts with
`org.`, with the person who made it and the target, old and new values, and the unit's new revision, for example
`seat:12 unit=lab role=member->lead revision=4`. The actions are `org.unit_create`, `org.unit_edit`,
`org.unit_archive`, `org.seat_add`, `org.seat_edit` and `org.seat_end`; a ceiling writes `autonomy.ceiling_set` (see
[autonomy levels](autonomy.md)). The note of a ceiling stays in its revision and is not copied into the audit
row. The web's `/admin/audit` lists them.

## Seats that end

A seat ends in one of three ways: someone ends it (`evo-agents hub org seat end`, which ends it now), its own `ends_on`
passes, or the `ends_on` of a cohort holding it passes. The job `hub.expire_seats` runs hourly, at minute 5. It sets
`ended_at` on each seat past its end date, raises its unit's revision and writes an `org.seat_end` row with no actor,
which means the hub did it. A seat it already ended is left alone, so running it twice writes nothing. The hub counts
a seat as ended from its end date on, even before the job has run.

A person whose primary seat has ended is at A0, and the hub then does three things:

- It takes back the leases still held by every run they dispatched, on every project, as it does when a grant falls
  below writer (`docs/credentials.md`). It revokes their GitHub tokens at GitHub once the transaction commits, and
  writes one `credential.revoke` row per run, with `by=org-expiry` from the job and `by=org-seat-end` from a write.
  This happens at the job and right after any write that ends a seat or moves an end date into the past.
- It answers 403 to a credential request from the worker of such a run.
- It refuses every new run they would start: dispatching a step, a plan run or an author run, a rerun (triage
  included), and a plugin task they would run as. These get 403 with "the primary seat of <login> in unit <slug>
  ended on <day>, which leaves them at level A0: <what> is refused". The day is the earliest end date that passed,
  or the day of `ended_at`. A message that asks an agent for a chat run is stored, the run is refused, and the hub
  replies with the same reason in the thread.

Their grants are not changed: they still read what their grants allow. A person whose primary seat has not ended,
everyone at the root included, sees the hub exactly as before. Local Claude Code sessions and the hub's own night-shift
runs are not checked.

## Commands

Every command signs in like the rest of `evo-agents hub` and only calls the routes below. A refusal (403, 404, 409,
422) prints the hub's reason and exits 1, and a malformed date exits 2.

| Command | What it does |
| --- | --- |
| `evo-agents hub org show [--all] [--json]` | the tree, each unit with its kind, ceiling and effective level, and its seats; archived units and ended seats only with `--all`; `--json` prints `GET /v1/org` whole: `units`, `seats`, `viewer` |
| `evo-agents hub org unit create <slug> --parent <slug> --kind team\|cohort [--name <name>] [--ends-on <date>] [--level A1 --note <text>]` | a team or a cohort below a unit |
| `evo-agents hub org unit edit <slug> [--name <name>] [--parent <slug>] [--ends-on <date> \| --clear-ends-on] [--if-revision <n>]` | rename, move or change a cohort's end date |
| `evo-agents hub org unit archive <slug>` | archive a unit with no unit below it left and no live seat |
| `evo-agents hub org seat add <login> --unit <slug> --role lead\|member\|intern [--reports-to <login>] [--primary] [--starts-on <date>] [--ends-on <date>]` | a seat of a person |
| `evo-agents hub org seat add --agent <handle> --project <project> --unit <slug> --role agent --sponsor <login>` | the seat of an agent profile |
| `evo-agents hub org seat edit <login> [--unit <slug>] [--role member] [--reports-to <login> \| --clear-reports-to] [--sponsor <login>] [--primary] [--starts-on <date>] [--ends-on <date> \| --clear-ends-on]` | change a live seat |
| `evo-agents hub org seat end <login> [--unit <slug>]` | end a seat now |
| `evo-agents hub autonomy ceiling set --unit <slug> --level A3 --note <text> [--class Kn=mode]... [--environment E]... [--worker W]...` | the next ceiling revision of a unit, whole: what it leaves out it drops; `--login <login>` for a person's primary seat, `--seat <id>` for one seat |
| `evo-agents hub stop (--login <login> \| --unit <slug> \| --all) [--reason <text>] [--lift]` | the stop of autonomy S4: cancels the runs of a person, of a unit and the units below it, or of the hub, and holds new ones until `--lift` (`docs/autonomy.md`) |
| `evo-agents hub org worker list [--json]` | the caller's live workers (every live worker for a hub admin), each with `shared`, the units it is narrowed to and its newest doctor report (`doctor_at`, `doctor_high`, `doctor_medium`) |
| `evo-agents hub org worker share <worker> --unit <slug>` | narrow a worker to a unit and every unit below it, turning sharing on; refused (409) while the worker's newest doctor report is missing, older than 48 hours or has a high finding |
| `evo-agents hub org worker unshare <worker> --unit <slug>` | take a unit back from a worker; taking back its last unit turns sharing off |

`hub org worker` names a worker as its name (the caller's own) or as `owner/name`; an unknown or ambiguous name exits 1.
Only the worker's owner or a hub admin narrows it, and an ended or archived unit is refused. What a narrowed worker
takes, and how its doctor report decides, is in `docs/workers.md`, section "Shared workers".

`seat edit` and `seat end` name a seat as `--seat <id>`, as a login, or as `--agent <handle> --project <project>`. With
a login, `--unit <slug>` picks the person's live seat in that unit; without it, their live primary seat is meant. A
login with several live seats that match gives an error listing their ids. Every write takes `--json` to print what the
hub answered.

An intern in a cohort that ends with the term, under their mentor's lead:

```bash
evo-agents hub org unit create interns-2026 --parent lab --kind cohort --ends-on 2026-12-31
evo-agents hub org seat add <login> --unit interns-2026 --role intern --reports-to <login>
evo-agents hub admin grant <login> <project> --role writer --max-level <level>
```

## Routes

| Route | Who | What |
| --- | --- | --- |
| `GET /v1/org` | any member signed in | every unit and seat, live and ended, each with its ceiling, effective level and source, and the viewer: login, admin, the units they lead and their own level |
| `GET /v1/org/runs` | any member signed in | each member's runs at work now, by project, counted only in the projects the viewer holds a grant on |
| `POST /v1/org/units` | admin, lead above | create a unit (201) |
| `PATCH /v1/org/units/{slug}` | admin, lead above | change a unit's name, parent or end date |
| `POST /v1/org/units/{slug}/archive` | admin, lead above | archive a unit |
| `POST /v1/org/seats` | admin, lead (Who changes what) | add a seat (201) |
| `PATCH /v1/org/seats/{seat_id}` | admin, lead | change a live seat, or make it primary |
| `POST /v1/org/seats/{seat_id}/end` | admin, lead | end a seat now |
| `PUT /v1/org/ceilings` | admin, lead | the next ceiling revision of exactly one of `unit`, `login` (their primary seat) or `seat`, with its `classes`, `environments` and `workers` |
| `GET /v1/autonomy/org` | any member signed in | every seat's effective mode of each class with its source, and the stops that hold |
| `GET /v1/org/workers` | any member signed in | the caller's live workers (every live worker for a hub admin), with `shared`, `units` and the newest doctor report |
| `PUT /v1/org/workers/{id}/units/{slug}` | the worker's owner, admin | narrow the worker to a unit, turning sharing on |
| `DELETE /v1/org/workers/{id}/units/{slug}` | the worker's owner, admin | take a unit back; with the last one, sharing turns off |

Writes answer 403 with the reason when the rules refuse, 404 for a unit, seat or login that does not exist, 409 when
the state of the chart is in the way (an archived unit, an ended seat, a slug taken, a second live seat in a unit, a
parent that would make a cycle, a unit to archive that still has units or live seats, a stale `if_revision`), and 422
for a request that is malformed on its own (an intern without `reports_to`, an agent seat without a sponsor, `ends_on`
on a team, an end date before the start).

## The page /org

Org in the main navigation opens `/org`: the unit tree, every unit open at first, with each seat's role, whether it is
primary, reports_to, sponsor, end date (marked when it ends within 7 days), ceiling and effective level with its
source, and the person's runs at work, which link to the runs list of each project. Admins and leads create units, add,
edit and end seats and set ceilings on the page, under the same rules; someone who may not write sees no write button.
A 403 shows the hub's reason. Ended seats and archived units appear with "show all". An admin also sees Unplaced: the
people with a grant and no live seat outside the root.

Each seat also lists how many classes its effective autonomy holds below auto, and opens to the mode of each class.
Someone who may stop a person or a unit (the person themself, a lead above, an admin) gets Stop and Lift there, with a
confirmation that names how many runs the stop cancels; a seat or unit under a stop is marked. Only what is unusual
shows: a stop that holds, a class held below auto.

## Schema

Schema 0099 adds `org_units` (parent, kind, slug, name, `ends_on`, `archived_at`, revision), `org_seats` (unit, user or
agent profile, role, `is_primary`, `reports_to`, sponsor, `starts_on`, `ends_on`, `ended_at`) and `autonomy_ceilings`
(scope `unit` or `seat`, scope id, revision, level, note, author, when written). Constraints in the database keep one
root, one live primary seat per person, one live seat per agent profile, an agent seat of role agent with a sponsor,
and an intern with `reports_to`. The service checks the rest under the chart's lock: no cycle, `reports_to` naming a
live seat of another person. Going back to 0098 drops the three tables, and the chart is lost.

Schema 0107 adds `worker_shares` (worker, unit, who narrowed it and when): the units a shared worker is narrowed to
(`docs/workers.md`, section "Shared workers"). Its downgrade turns sharing off on every narrowed worker before it drops
the table, so no worker goes back to being shared with every writer.
