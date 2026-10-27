# The hub feature srs: BA documents on the hub

The plugin **srs** keeps a project's BA documents in the hub's database: an SRS written to a template, or a free
specification, with versions that members approve, and with a code on every use case, business rule, screen and
requirement that stays the same from version to version. Its code is `evo_agents/hub/server/srs`, a feature of the hub,
so every hub has it. It does nothing in a project until an admin of the project enables it there. Its agent type `ba`
adds @ba, an agent that drafts the documents for the project's writers and reviews every version that enters review,
comparing it with the other documents and with the knowledge graph; a conflict it can show a source for is a finding,
and an open finding of level `decide` holds approval until members settle it in a settle chat (sections Findings, Settle
chats and @ba). The plugin tasks links tasks to its items (`docs/tasks-plugin.md`), and an approved version keeps those
tasks in step with its use cases (section Tasks when a version is approved).

A feature of the hub since migration 0147, a core plugin before it, it keeps the plugin's names: the routes under
`/v1/x/srs/`, its MCP tools, its config (`hub plugin config srs`), and `hub plugin enable srs`, which turns the
feature on in the project's layer of its features. A hub admin turns it on or off in the administration, Features,
for the hub or for one project. `hub plugin list` and /admin/plugins do not list it (`docs/plugins.md`, Core plugins).

## Enabling it

```sh
evo-agents hub plugin enable srs --project evo-agents
evo-agents hub plugin config srs --project evo-agents --set 'roles={"ba": ["alice"], "qa": ["bao"], "dev": ["chi"]}'
evo-agents hub plugin config srs --project evo-agents \
  --set 'approval_policy={"srs": {"required": ["ba", "qa"], "optional": ["dev"]}}'
evo-agents hub plugin config srs --project evo-agents --set self_approval=true   # authors may approve their versions
evo-agents hub plugin config srs --project evo-agents \
  --set 'required_use_case_fields=["intent", "context", "expectation"]'          # use cases fill these before review
evo-agents hub plugin config srs --project evo-agents --effective                # each field, the layer that set it
```

It asks for no permission. Its config, in the project's layer (`docs/hub.md`, Plugins in a project):

| Field | What it holds | When left out |
| --- | --- | --- |
| `template` | the template documents are read and written with | `srs-basic`, the only template of this release |
| `code_patterns` | per kind of coded item (`use_case`, `business_rule`, `screen`, `nfr`, `cbr`), a regular expression that replaces the template's | the template's patterns (section Markdown) |
| `roles` | per role of a review (`ba`, `qa`, `dev`), the members who hold it, by login | nobody holds a role |
| `approval_policy` | per kind of document (`srs`, `spec`), the roles whose approval a version needs (`required`) and those that may approve too (`optional`) | one approval is enough (section Approval) |
| `required_use_case_fields` | the fields every use case fills, besides its actor, before its version is submitted: `intent`, `context`, `expectation`, `objective`, `trigger`, `pre_condition`, `post_condition`, `status` or `id` | `[]`: the actor alone |
| `self_approval` | `true` lets the author of a version review it, approve it or ask for changes, as any writer of the project (section Approval) | `false`: another member reviews it |

The headings a template reads stay the template's; a client changes only the patterns of its codes. Only an admin of the
project writes the config, with the commands above or on the plugin's page of the project on the web
(`/p/{project}/plugins/srs`); `--unset self_approval` turns self-approval off again, back to its default. Turning the
plugin off deletes nothing: its tables and rows stay for when it is on again.

## The model

- **A document** has a slug, unique in the project, a title, a kind (`srs` or `spec`), its template and a label, the
  project's unless an admin gives it another when the document is made. Every row of a document carries that label,
  and a member reads the document only when the sink of their session clears it. An admin can archive a document
  (`archived_at`, section Archiving a document).
- **A version** is one save of a document, never changed after. A document's versions form one straight line, each
  built on the one before it: `draft`, `in_review`, `approved`, and `superseded` once a newer version is approved; or
  `withdrawn`, when its author or an admin takes it out of review for good (section Approval). `superseded` and
  `withdrawn` are last states. Each version keeps its author, its summary, its review round and, for a version @ba
  drafted, the run that drafted it (`drafted_by_run`).
- **An item** is a part of a version: a section, a use case, a business rule, a screen, a non-functional requirement
  (`nfr`), a common business rule (`cbr`), a glossary, the approval page or the revision history. The hub gives each
  item a key and keeps it across versions, so an item renamed, moved or renumbered is the same item. A use case keeps
  its fields (ID, Status, Objective, Intent, Actor, Context, Trigger, Pre-condition, Post-condition, Expectation, and
  any other row of its table of items) and the steps of its activity flow; a business rule the steps it applies to.
- **A use case review** (`srs_use_case_reviews`) is one approval, or one request for changes, of a writer on one use
  case of a version in review, with the role it counts for and a note. Rows are kept; the ones that count are worked
  out when read (section Approval, Reviewing a use case).
- **A code** (`UC ORD-3`, `BR ORD3-2`, `SCR-4`, `NFR-SEC-01`, `CBR 2`) names a coded item. A code and its aliases are
  unique in the project across every document, compared with whitespace runs as one space and case folded, so
  `UC ORD-1` and `uc  ord-1` are one code. A code stays when its item is renamed or moved, no save changes it, and
  once its item is dropped it is never given again. A screen without a code gets the next `SCR-<n>` of the project
  when it is saved.
- **Links** between the items of a version: `uses_screen` (a use case to a screen), `refines` and `relates`. They are
  set by a save of changes (the web, the API or @ba); Markdown does not carry them.
- **Reviews** approve a version in review or ask for changes, per round, each counting for a role.
- **Comments** are threads on a document, a version or an item. A comment @ba wrote names its run.
- **Findings** are the conflicts @ba found in a document, each with the sources that show it, a level and a status
  (section Findings).

Migration 0046 adds the nine tables, each with the plugin's prefix: `srs_documents`, `srs_versions`, `srs_texts` (each
Markdown body once, by SHA-256), `srs_items`, `srs_codes`, `srs_item_versions`, `srs_refs`, `srs_reviews` and
`srs_comments`. Going back to 0045 drops them. Migration 0064 lets `srs_versions.state` be `withdrawn`, which carries
`decided_at`, the moment of the withdrawal; going back to 0063 makes every withdrawn version a draft again, without
`decided_at`. Migration 0069 adds `srs_findings` and `srs_versions.review_run_id`, the task run of @ba's review of the
version's current round; 0070 adds `srs_settle_chats`, the settle chat of a version, and `srs_findings.commit_authors`;
0071 adds `srs_approval_tasks`, what the approval of a version did to the project's tasks. Going back past each drops
what it added.

## Who does what

The routes answer a member with a grant on a project that enabled the plugin, and 404 in a project that did not.

| Who | May |
| --- | --- |
| a reader | read the documents, their versions, items, comparisons, exports, comments and findings (`srs findings`), archived documents included; settle a finding in its settle chat |
| a writer | import Markdown, save a version, submit a draft, review a version (section Approval: one they wrote only with `self_approval` on), withdraw a version of their own in review, comment and close a thread |
| an admin | what a writer may, plus drop a coded item, retire items by their codes (`retire`), withdraw any version in review (`withdraw`), waive an open finding (`srs waive`), archive a document (`archive`) and open it again (`unarchive`), and give a new document a label of its own |
| the agent of a run | what the tools of @ba allow (section @ba): record, resolve, waive and reopen findings with `srs__finding`; never approve, return or withdraw a version, waive a finding through the route, retire items, archive or unarchive a document |

Reads go through the sink `X-Evo-Sink` names, else the project's hub sink. A refusal is the hub's error body, with the
problems that refused it in `detail`; nothing is written.

## Saving a version

A save sends changes made on a version (`base_version`) and the hub writes them as the document's next version, built
on its latest. A change names an item by its key and gives its new title, body, steps or links, drops it (`retire`),
or adds a new item. When an item changed in the latest version since `base_version`, the save is a 409 that holds both
texts of the item, and the member merges them by hand. Each save is checked:

- **Errors that refuse the save**: a code that does not match its kind's pattern, a code twice in the document, a
  code another item of the project holds or held, a code changed, a coded item dropped by a member who is not an
  admin.
- **Errors that block submitting it**: a business rule outside every use case, a use case without an actor
  (`uc_no_actor`) or without another field the config requires (`uc_field_missing`), a link to an item the version
  does not have, a `uses_screen` link to an item that is no screen.
- **Warnings**, shown only: a business rule whose code does not match its use case's, a business rule naming a step its
  use case does not have, and what reading the Markdown noted (a heading that skips a level, one the template does not
  name, coded rows kept as text).

### Retiring items by code

An admin drops coded items by their codes, without a key and without importing the whole Markdown again:

```sh
evo-agents hub srs retire orders "UC ORD-1" --project evo-agents --dry-run   # the items it would drop, nothing written
evo-agents hub srs retire orders "UC ORD-1" "UC RET-1" --project evo-agents --summary "Out of this release"
```

Each code or alias names one item of the latest version, compared as codes are compared (`uc  ord-1` is `UC ORD-1`).
The item leaves the next version with every item under it: retiring `UC ORD-1` drops `BR ORD1-1` and `BR ORD1-2` too,
and naming one of them as well adds nothing. The next version is a draft built on the latest by the same save as any
other, so every check of a save holds and a code retired is never given again; its summary is `Retired UC ORD-1`
unless `--summary` gives one. It goes through submit and review as any version. The command prints the new version's
number and each item dropped; `--dry-run` prints the items only and writes nothing.

A code that no item of the latest version holds, one retired before included, refuses the whole request: a 422 that
lists each such code as a `code_unknown` problem saying why (dropped in version N, a code of another document, or held
by no item), and nothing is written. A writer who is not an admin, and the agent of a run, get 403.

## Approval

A writer submits a draft that has no error; it goes to `in_review` and its round goes up by one. A writer then
approves it or asks for changes, with these rules:

- Nobody who wrote a version since the one approved last, the version under review included, reviews it. Admins are
  no exception. With the config's `self_approval` on, this rule alone goes: the author reviews their version as any
  writer does, so a project without an `approval_policy` approves it with the author's own approval. The review page
  says self-approval is on for the project. Every rule below holds either way.
- The agent of a run never approves or returns a version: the route refuses any token scoped to a run.
- One review per member per round.
- An approval counts for a role the reviewer holds in `roles`: the one they name (`--role`), else the first role the
  policy requires that they hold and no approval of the round filled, else the first role they hold.
- The version is approved once every role the `approval_policy` of the document's kind requires approved it in this
  round. Without a policy, or one that requires no role, one approval is enough. Optional roles approve without being
  waited for.
- A request for changes sends the version back to `draft`. Submitted again, it is a new round.
- An approval goes through the gate first (section Findings): no open finding of level `decide` in the document, and
  no review of @ba of the version's current round still queued or running. Otherwise it is a 409 that names each
  finding, with its chat link, and the command that waives one. A request for changes is never held.
- A version answers its `review_run`: the id and state of @ba's review of its current round, null when none was
  handed. The submit route answers it too, queued, once the hub handed one.
- Approving a version supersedes every older one but a withdrawn one and, when the project has a knowledge config on
  the hub, queues a build of its knowledge graph. It emits the event `srs.version.approved` in the same transaction;
  when the project enables the plugin tasks, the plugin keeps the project's tasks in step once the hub delivers that
  event (section Tasks when a version is approved).

**Reviewing a use case.** A version in review has *use cases to review*: those it adds, those it changes (the use case
itself, or a business rule under it added, changed or dropped) and those it drops, compared with the version approved
last, every use case when none was approved yet. A use case that only moved needs no review; nothing else of the
document is reviewed on its own. A writer approves one or asks for changes on one, under the rules above (writer, not a
run's token, version in review, not an author since the last approval unless `self_approval`, a role from `roles`):

```sh
curl -X POST "$HUB/v1/x/srs/evo-agents/documents/orders/versions/2/items/UC%20ORD-1/reviews" \
  -H "Authorization: Bearer $TOKEN" -d '{"decision": "request_changes", "body": "Name the gateway"}'
```

- `ref` is a code, alias or key. A ref that is not a use case to review of that version is a 422 that lists them, and a
  request for changes without a note is a 422. An approval goes through the findings gate; a request is never held.
- A later review of the same person on the same use case replaces the earlier one when counted; the rows stay.
- A use case is `changes_requested` while one counted review asks for changes, else `approved` when its counted
  approvals and the version-wide approvals of the round satisfy the policy (a version-wide approval of role R counts
  for every use case, for R), else `pending`.
- The version is approved, with the event `srs.version.approved` and the knowledge build, by the review that makes
  every use case to review `approved`. Submit never approves. A version with no use case to review is approved by
  version-wide reviews alone.
- A request for changes on a use case keeps the version in review and blocks every approval, version-wide ones too
  (those are still recorded). A version-wide request for changes still returns the version to `draft`.
- A review carries to a later version while the use case reads the same there (title, fields, body and the business
  rules under it; not its position), it was recorded on a version after the one approved last, and its reviewer may
  still review. A version answers `use_cases` (`code`, `key`, `title`, `change`, `state` and the counted `reviews`, each
  with the version it was recorded on) while it is a draft or in review, and as the version approved last (read against
  the version approved before it, as its event says); the review route answers the same.
- The Loop does not hand a use case again when only one use case is change-requested: it is handed use cases through
  the event `srs.version.approved` (section Tasks when a version is approved), which a use case review emits only when
  it approves the whole version, so a request for changes on one use case hands nothing.

**Withdrawing a version.** The author of a version in review, while they hold writer, or an admin of the project,
takes it out of review for good:

```sh
evo-agents hub srs withdraw orders 2 --project evo-agents --comment "The payment gateway is not chosen yet"
```

The version becomes `withdrawn`, a last state: it is never submitted, reviewed or approved again, and approving a
later version leaves it withdrawn. `--comment` keeps the reason as a comment on that version, in the same
transaction. Another writer and the agent of a run get 403, and a version that is not in review 409. To change a
withdrawn version, save a new one: the next version is built on the latest, withdrawn or not.

**On the web.** A version's review page (`/p/{project}/x/srs/documents/{slug}/versions/{number}`) has a Findings card,
which readers see too: the document's open findings of level `decide` first, with their count, then the other open
ones, then the settled ones. Each shows its level, kind and status, its summary, the item it is about and its sources,
a code linking to its item's page (in another document when the reader sees that document); an open one links its
settle chat, a settled one says who settled it (a member, the knowledge graph, or @ba's waiver), when, how, and links
the message that settled it. While a finding of level `decide` is open, or @ba's review of the round is queued or
running, the form's Approve is locked with the reason (the settle chat, or the run and its state) and Request changes
stays open; while the review runs, the page reads the document and its findings again every 5 seconds until it ends.
The hub stays the gate: an approval that still meets the 409 shows its refusal, and the page reads the findings
again. Nothing on the web settles or waives a finding: settling goes through the settle chat, waiving through
`evo-agents hub srs waive`.

**On the web, reviewing use cases.** While a version is in review, its review page has a card, Use cases to review, with
one row for each use case the version needs reviewed: its code linking to its page (a dropped use case opens at the
version approved last), title, what the version does to it (added, changed, removed), where it stands (pending,
approved, changes requested) and the reviews that count, each with its reviewer, role, note and, for a review kept from
an earlier version because the use case reads the same, `from vM`. The card says that v{N} is approved once every use
case has the approvals the policy asks for, together with its changes outside use cases, and, while one use case has a
request for changes, that the version cannot be approved. A writer who may review the version (the rules of the section
above, but a review the person already gave on the version or on that use case does not stop another: it replaces the
last) gets Approve and Request changes on each row; Request changes opens a note field and needs the note. Approve is
locked with the same reason as the version's form (open findings of level `decide`, @ba's review of the round running),
and the 409 of the hub shows as a refusal under the row. A person who holds several roles picks which one their use case
reviews count for. After each review the page reads the version again. The form that reviews the whole version stays as
it is. The page of a use case, in a version that is a draft or in review, says where the use case stands in that
version and links to the review page.

## Findings

A finding is one conflict @ba found in a document (`srs_findings`): the version it was found on, the item it is about
(none for the whole document), its kind (`duplicate`, `contradiction`, `kg_conflict`, `overlap`), its level (`info`,
`advisory`, `decide`), a summary, the sources that show it, and its status: `open`, `resolved` or `waived`. Each row
carries the document's label and names the run that wrote it.

**No source, no finding.** A finding names at least one source, and the hub looks each up before it writes anything:
a code an item of the project holds (`UC ORD-2`, in any document), the id of a node of the project's knowledge graph
on the hub (`plan:rollout/step:2`), or a path a File or Document node carries (`lumen-docs:docs/billing.md`, or the path
alone), the graph read as the agent of the run reads it. A source the hub cannot find refuses the whole call, and
nothing is written. For a file or a document, the finding also keeps the emails of the authors of its three newest
commits (`commit_authors`), as the graph's Commit nodes hold them: whom its settle chat invites. No route returns them.

**Settling.** Only the agent of a run of @ba writes findings, with `srs__finding`: it records one, resolves or waives
an open one, and reopens one settled at most 24 hours ago (later, the tool refuses: a new finding is recorded). A
resolution says how it was settled and names the member who settled it, with the link of the chat message, or, settled
from the graph, the sources that settled it, a later version of the document among them. An admin of the project
waives an open finding with a reason, kept as its resolution:

```sh
evo-agents hub srs findings orders --project evo-agents                 # every finding, oldest first
evo-agents hub srs findings orders --project evo-agents --status open --json
evo-agents hub srs waive orders 12 --project evo-agents --reason "Billing keeps its own limit on purpose"
```

A writer who is not an admin, and the agent of a run, get 403; a finding that is not open, or an archived document,
409.

**The approval gate.** A version is not approved while its document has an open finding of level `decide`, whatever
version it was found on, so a finding reopened after an approval holds the next one; nor while @ba's review of the
version's current round (`srs_versions.review_run_id`) is queued or running. A review that failed, was cancelled or
was lost holds nothing, nor does a round no review was handed for (the type off, or past its cap). Findings of level
`info` and `advisory` hold nothing, and neither do comments. The 409 names each finding that holds it (`#12
(contradiction, UC ORD-2, chat /chats/7?message=31)`) and the waive command; `hub srs approve` prints those lines. A
request for changes is never held.

## Settle chats

When @ba's review of a version ends, whatever it ended as, and its document has findings of level `decide` still open,
the hub opens a chat for that version, a settle chat (`kind` settle, `docs/hub.md`, Group chats), with no model asked.
It does so through the plugin's events contribution `reviews`, which takes the event `task.result` of @ba's task runs,
once the run's end is committed. A version has one settle chat (`srs_settle_chats`): a second delivery finds the
first. No chat opens for a version that left review, an archived document, a hub whose feature chats is off, or while
the agent type ba is off.

The hub invites, each with why: the author of the version; the members of the roles `ba`, `qa` and `dev` of the
plugin's config; the people behind the findings' sources (the author of the latest version that holds a code a source
names, the member who wrote a plan a source names, the authors of the three newest commits of a file a source names,
their emails turned into logins through the members' emails, `evo-agents hub email`); and the assignees of the tasks
linked to the use case a finding is about. Someone with no account on the hub, no grant on the project, or a grant
that does not clear the chat's label is named in the first message with why, and not added.

The first message lists the findings and ends with a mention of @ba. Every message of a person in the chat wakes @ba
without an @, a reader's too, on a worker of the type's `run_as`. Anyone in the chat settles a finding: @ba sums the
conflicts up, writes a line `Đã chốt: ...` with what was chosen, who chose it and when, records the resolution, the
member and the link of the message on the finding, and proposes the next version, written as the author of the version
the chat is for, so whoever settled keeps the right to approve it. While a finding is open, its chat link is the first
message of its chat; once settled, the message that settled it.

## Tasks when a version is approved

An approval emits the event `srs.version.approved` in its transaction (`evo_agents/hub/server/srs/events.py`): the
document's slug and id, the version's number and id, the version approved before it (null the first time), the
approver's login and id, and the codes of the items the version added, changed and dropped since that one, by kind.
Plugins the project granted `srs:read` get it, at least once, and the hub keeps it in `hub_events`, where the Loop
reads it (`docs/plugins.md`, section Extension points). The plugin srs takes it itself, with no grant, through its
events contribution `approvals`; the job `hub.deliver_plugin_events` delivers it within a minute or so of the
approval, and the approval's route does nothing more to tasks.

A review also emits the event `srs.use_case.approved` (plan srs-backlog-loop), one for each use case it just made
approved: the document's slug and id, the version's number and id, the use case's code and key, the code (or else the
title) of its direct parent item, its `claim` field when it has one, and the approver's login. A use case of a version is
emitted once, whatever reviews follow: a review of one use case emits its own, and a version approved emits those no
event told of yet, before `srs.version.approved`. Plugins with `srs:read` get it as they get the version's; the hub keeps
it in `hub_events`, where the Loop reads it to know which use cases are approved ahead of their version
(`docs/loop.md`, SRS là nguồn).

When the project enables the plugin tasks, whether its Loop is on or off, that handler compares the version with the
version approved before it (none the first time) and keeps the project's tasks in step with its use cases, in the
delivery's transaction (`evo_agents/hub/server/srs/auto_tasks.py`):

- **A new use case** gets one frame task: `UC ORD-1: Place an order`, linked to the use case, of type `story` when the
  project has it (its first type otherwise), in the first status, carrying the document's label, made by the approver.
  Its `external_key`, `srs:<slug>:<version>:<code>`, is how a second call finds it, so no call makes it twice.
- **A use case that changed**, itself or a business rule under it, gets a comment of the approver on each open task
  linked to it (a status whose group is not `done` or `cancelled`), with the link of the comparison of the two
  versions.
- **A use case the version dropped** moves each open task linked to it to the first status of group `cancelled`; the
  task's history names the hub and the reason. A project whose statuses have no such group keeps the task, which gets
  a comment saying so.

Then the hub hands @ba a task run, `@ba splits the tasks of orders v3`, that lists every open frame task of the
document with no child task yet. @ba reads each use case and makes its child tasks straight away with `tasks__create`,
each with the frame task as its `parent` and the use case linked, for the member who approved the version: the hub
keeps that member for the run (`srs_approval_tasks`), and the tool refuses a child whose title the frame task has
already (`docs/tasks-plugin.md`). No run is handed while the agent type ba is off or past its cap, or when no frame
task waits; the approval goes through all the same.

Each frame task made and each task cancelled emits `tasks.status` with the cause `srs_approval` and the document and
version as its source (`docs/tasks-plugin.md`).

Everything goes through one function, which takes the document, the version approved, the version approved before and
the approver: the event's payload. It is idempotent by document and version: `srs_approval_tasks` holds one row a
version, claimed first, so a delivery again makes no task, comment or run. A project that enabled the plugin tasks
only after the version was approved gets nothing from that version, as when the approval made the tasks itself.

## Archiving a document

An admin of the project archives a document that is no longer in use, and opens it again; nothing is deleted:

```sh
evo-agents hub srs archive orders --project evo-agents
evo-agents hub srs list --project evo-agents --all      # the archived documents too, with when each was archived
evo-agents hub srs unarchive orders --project evo-agents
```

An archived document leaves `srs list` (`GET documents` without `all=true`), the documents `srs__read` lists, the hits
of `srs__search` and the knowledge graph; archiving and unarchiving queue a build of the graph when the project has a
knowledge config. Reading it by its slug still works: `show`, its versions, items, comparisons, exports and comments.
It takes no write until it is unarchived: saving or importing a version, a dry run of an import, submit, review,
withdraw, retire, comments and their resolve, and `srs__propose` and `srs__comment` answer 409 with the command that
unarchives it. Its codes stay held in the project, so an import or a save in another document that uses one is
refused as before, and its slug stays taken. Archiving an archived document, or unarchiving one that is not, is a
409; a writer, a reader and the agent of a run get 403.

## Markdown: import and export

An import reads Markdown whose headings follow the template, as a new document or as the next version of one, after a
preview: every item with its kind, code and title, the count of each kind, the problems, and, for a document that
exists, the items the import adds, changes and drops. A dry run stops at the preview, and so does an import with an
error that refuses a save (422 with the problems). A new document takes the slug given, else one made of its title,
and the title given, else its first bold line. The file is at most 4 MiB.

`srs-basic` reads the common headings of an SRS. Every ATX heading (`#` to `######`, never inside a fenced code
block) starts an item, whose body is the text up to the next heading; the text before the first heading is the
document's preamble.

| Heading or row | Item | Code pattern |
| --- | --- | --- |
| `UC ORD-1: Place an order` | use case | `^UC [A-Z]+-\d+$` |
| `BR ORD1-1 – Cart rules`, or `Step (2): BR ORD1-1 – Cart rules` naming the steps it applies to | business rule | `^BR [A-Z]+\d+-\d+$` |
| `Screen: Cart`, its code in `<!-- srs: SCR-3 -->` on the line after it | screen | `^SCR-\d+$` |
| a row of a table whose first cell is `NFR-SEC-01` | non-functional requirement | `^NFR-[A-Z]+-\d+$` |
| a row whose first cell is `CBR 2`, under a heading Common Business Rules | common business rule | `^CBR \d+$` |
| Approval Page, Revision History | approval page, revision history | none |
| a heading with Glossary, Definitions, Terms, Abbreviations or Acronyms | glossary | none |
| any other heading (numbered `2.1.`, Actors, Site Map, Open Items, Pending Points, or one the template does not name) | section | none |

A use case's body gives its fields: the rows of its table of items (`| ID | UC-ORD-001 (ORD-01) |` makes
`UC-ORD-001` and `ORD-01` aliases of `UC ORD-1`), and the steps of its Activity Flow table. The table of items is the
table one of whose rows the template names. Besides the classic rows (ID, Status, Objective or Goal, Actor, Trigger,
Pre-condition, Post-condition), a use case may follow intent, context and expectation: `Intent`, `Context` (or
`Background`) and `Expectation` (or `Expected result`, `Expected outcome`, `Acceptance criteria`), the checks that tell
the use case is done. Every other row of that table is kept under its label (`| Claim | CL-3 |` gives the field
`claim`) and shown after the template's fields; its head row (`| Item | Content |`) is kept only when the template
names it. In a section, the first run
of table rows whose first cell is a code becomes one item per row; the table's head stays the section's body.
`tests/hub/fixtures/srs/demo-shop-srs.md` is a whole example.

An export writes a version back in the same structure with the same codes: the preamble, then each item's heading at
its level, its body, its rows and the text after them. A document imported and exported again gives its source back,
up to trailing whitespace and runs of blank lines. `--no-anchors` leaves out the screens' code comments.

## Commands

`evo-agents hub srs` is the client of the routes below (`evo_agents/hub/cli_srs.py`). `--json` prints what the hub
answered, and a refusal prints the hub's reason and each problem it listed.

```sh
evo-agents hub srs list --project evo-agents                          # the documents, latest and approved versions
evo-agents hub srs import order-srs.md --project evo-agents --dry-run # the preview only
evo-agents hub srs import order-srs.md --project evo-agents --slug orders --title "Order service"
evo-agents hub srs import order-srs.md --project evo-agents --doc orders --summary "Payment retries"
evo-agents hub srs show orders --project evo-agents                   # its versions and the items of the latest
evo-agents hub srs show orders --project evo-agents --item "UC ORD-1" # one item, its fields and children
evo-agents hub srs submit orders 2 --project evo-agents               # a draft to review
evo-agents hub srs approve orders 2 --project evo-agents --role qa
evo-agents hub srs approve orders 2 --project evo-agents --request-changes --comment "UC ORD-2 has no actor"
evo-agents hub srs approve orders 2 --project evo-agents --item "UC ORD-1" --item "UC ORD-2"  # use cases one by one
evo-agents hub srs reviews orders 2 --project evo-agents              # the use cases to review and their state
evo-agents hub srs withdraw orders 2 --project evo-agents --comment "Not ready" # out of review for good
evo-agents hub srs findings orders --project evo-agents --status open # the conflicts @ba found
evo-agents hub srs waive orders 12 --project evo-agents --reason "Kept on purpose" # an admin, an open finding
evo-agents hub srs retire orders "UC ORD-1" --project evo-agents --dry-run # what retiring would drop
evo-agents hub srs retire orders "UC ORD-1" --project evo-agents      # the next version, a draft, without it
evo-agents hub srs export orders --project evo-agents --version 2 --output orders-v2.md
evo-agents hub srs archive orders --project evo-agents                # out of list, search and the graph
evo-agents hub srs list --project evo-agents --all                    # archived documents too
evo-agents hub srs unarchive orders --project evo-agents
```

## Routes

Under `/v1/x/srs/{project}/`:

| Route | What |
| --- | --- |
| `GET documents` (`?all=true`) | the documents the caller sees, each with its latest and approved versions; the archived ones only with `all` |
| `GET documents/{slug}` (`?version=N`) | a document with every version, the newest first, and one version whole |
| `GET documents/{slug}/versions` | every version with its reviews |
| `GET documents/{slug}/versions/{number}` | a version whole: its items, its problems and its `use_cases` to review |
| `GET documents/{slug}/items/{ref}` (`?version=N`) | an item by code, alias or key, with its children |
| `GET documents/{slug}/diff?from=N&to=M` | what changed between two versions, item by item |
| `GET documents/{slug}/export` (`?version=N&anchors=false`) | a version as Markdown |
| `POST import` | Markdown as a new document or the next version of `document`; `dry_run` stops at the preview |
| `POST documents/{slug}/versions` | changes made on `base_version` as the next version (201), 409 on a conflict |
| `POST documents/{slug}/versions/{number}/submit` | a draft to review |
| `POST documents/{slug}/versions/{number}/reviews` | `{"decision": "approve" \| "request_changes", "role", "body"}` |
| `POST documents/{slug}/versions/{number}/items/{ref}/reviews` | same body: one use case of a version in review; answers the version with its `use_cases` |
| `POST documents/{slug}/versions/{number}/withdraw` | `{"comment"}`, optional: a version in review out of review for good |
| `POST documents/{slug}/retire` | `{"codes", "summary", "dry_run"}`: the next version without those items; `dropped` lists each item |
| `POST documents/{slug}/archive`, `POST documents/{slug}/unarchive` | archive a document, open it again; the document's summary |
| `GET` and `POST documents/{slug}/comments`, `POST .../comments/{id}/resolve` | threads on a document, a version or an item |
| `GET documents/{slug}/findings` (`?status=open`) | the findings of a document, oldest first, each with its sources and how it was settled; a code source names the `document` whose item holds it, when the caller sees that document |
| `POST documents/{slug}/findings/{id}/waive` | `{"reason"}`: an admin waives an open finding |

The web shows them on the plugin's pages, at `/p/{project}/x/srs/documents` in the project's menu (SRS): the documents,
a document's contents with each section read and edited in place, an item's page with the trace the knowledge graph
holds, the versions, the comparison of two versions, the review with the document's findings and the comments; the
import is reached from the documents.

## The knowledge graph

The plugin is a source of the project's knowledge graph (extension point `kg_sources`, `docs/plugins.md`): the version
of each document approved last, never a draft or a version in review, and nothing of an archived document. A document is
a Document node, each heading a Section, a use case a UseCase node (`usecase:UC ORD-1`) and a business rule, an NFR or a
common business rule a Requirement node (`requirement:BR ORD1-2`), with the id a mention of the code anywhere in the
graph gets, so a plan step or a document that names the code reaches the node. A section `defines` its coded items, a
business rule is `child_of` its use case, and a use case `mentions` the section of each screen it uses. A task of the
plugin tasks that links an item `implements` it, so the graph traces a use case to its tasks, their plan steps and the
commits of those steps (`docs/kg.md`).

## @ba

The plugin contributes the agent type `ba`: the profile @ba with the pack `srs/ba` (its instructions and the skills
`srs-writing`, `srs-review`, `srs-settle` and `srs-tasks`). A writer of the project mentions @ba in a group chat to ask
about the documents, draft or change sections, use cases, business rules, screens and requirements, review a version,
or make tasks of items (`mentioned_by: writer`). The hub also hands @ba work of its own: the review of each version
that enters review, the settle chat of a version whose review left findings of level `decide` open, and the split of
the frame tasks of an approved version.

**Enabling it.** An admin of the project enables the type after the plugin (`docs/plugins.md`, Agent types):

```sh
curl -X PUT "$HUB/v1/projects/evo-agents/agent-types/srs/ba" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"run_as": "alice", "workers": ["alice-mini"], "daily_usd": 5}'
```

`run_as` is the member whose workers run @ba, a writer or an admin of the project; `workers` the workers of that
member it may take (none: any the profile allows); `daily_usd` the cap of what its task runs, the reviews and the
splits below, cost in 24 hours. A chat run of @ba has no cost cap, as any chat run. A handle is unique on the hub, so
the profile is @ba in the first project that enables the type and @ba-<project> in the next, unless the admin names
another in `handle`.

**Where its runs go.** The type declares `chat_runs: run_as`, so every chat run of @ba goes to a worker of `run_as`,
whoever mentioned it: a writer without a worker reaches @ba too. The run's row is dispatched by `run_as`, whose worker
claims it and whose credentials it leases, while the person who mentioned @ba stays the origin of the chain, whom its
tools act for. When `run_as` no longer holds writer, or has no worker that can take the run, the hub answers in the
thread why and starts no run. While the type or the plugin is off, a mention of @ba goes to the mentioner's worker,
as for any agent.

**Its tools** are on the hub's /mcp, each listed in a session whose project enabled its plugin:

| Tool | What it does | Who may call it |
| --- | --- | --- |
| `srs__read` | the documents; a version as Markdown with its state and problems; an item and the items under it | anyone with a grant |
| `srs__search` | items of the latest versions by code, alias or words | anyone with a grant |
| `srs__diff` | what changed between two versions, item by item | anyone with a grant |
| `srs__propose` | changes on `base_version` as the document's next version, sent to review at once | the agent of a chat run of @ba, or of a task run of @ba the Loop handed out for a use case |
| `srs__comment` | a comment on a document, a version or an item | the agent of a run of @ba |
| `srs__finding` | record a finding with its sources; resolve, waive or reopen one (section Findings) | the agent of a run of @ba |
| `kg_search`, `kg_context` | nodes of the project's knowledge graph by words, and a node with what it links to | the agent of any run, as the hub's /mcp offers them |
| `tasks__create` | a task linked to the items it names by code, under `parent` when named (plugin tasks, `docs/tasks-plugin.md`) | the agent of a chat run, or a member's own session, for a writer; the split run of @ba, for the approver |

Each tool acts for a person: the agent of a chat run for the person who mentioned the agent, the agent of another run
for the member who dispatched it (the split run's `tasks__create` for the approver). A document is read when that person sees it through the session's sink and, when
the run's worker is another member's, when that member sees it too, since what a tool answers reaches the machine the
agent runs on.

**A proposal is a version members approve.** `srs__propose` checks its changes as a save is checked, writes them as
the next version under the name of the person who mentioned @ba (for a use case the Loop handed out, the owner of
its goal, plan loop-engine), with `drafted_by_run` set, and submits it at once.
That person is its author, so they never approve it: another writer does, unless the project turned `self_approval`
on, when they may approve it as any writer may, and the tool's answer says so. The agent of the run never approves it
either way. Its schema takes no drop and never changes a code.

**@ba reviews each version that enters review.** Every time a version goes to review, in every round, from a
member's submit or from a proposal, the plugin hands @ba its review as a task run on a worker of `run_as`, with no
`runs:dispatch` grant: the image trusts the plugin. The run is kept as the review of the version's round
(`review_run_id`), and approval waits until it ends (section Findings). It reads the version and the document's open
findings, and its task and the skill `srs-review` tell it to:

- check the version against the template and write what is missing or wrong as comments on its items (at most 20,
  each starting `Blocking:` or `Suggestion:`), as `run_as`, with the run named on the comment; comments are advice,
  and approval reads none;
- compare each use case the version changes with every use case of every document of the project, drafts and versions
  in review included (`srs__search`), and with the knowledge graph (`kg_search`, `kg_context`), going through actors,
  systems, repositories, seams, terms and business rules, each searched in Vietnamese and in English;
- record each conflict as a finding with its sources (`srs__finding`); a conflict with no source is no finding;
- settle a finding from the graph first, when a plan's Decision line, an approved SRS item or a newer document answers
  it: resolved with that source and no member, so it needs no chat;
- go over every open finding of the document and resolve those the new version settles, citing the version.

When the type is off, `run_as` no longer holds writer, or the type's runs spent `daily_usd` in the last 24 hours, the
version goes to review without @ba's review, the hub's log says why, and approval waits for no review. The audit log
keeps `task.dispatch`, with `run_as` as its actor.

**@ba settles nothing itself.** In a settle chat it sums up, asks, records what people chose and proposes the next
version (skill `srs-settle`, section Settle chats).

**@ba splits the frame tasks of an approved version.** In the split run (section Tasks when a version is approved)
it reads each use case and makes its child tasks with `tasks__create` (skill `srs-tasks`), and nothing else.

**What @ba never does**: approve, return or withdraw a version, settle a finding nobody settled, waive a finding
through the route, drop or retire an item, change or give again a code, archive or unarchive a document, change the
plugin's config, or change the files of the repositories its run checks out.
