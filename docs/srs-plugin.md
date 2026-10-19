# The core plugin srs: BA documents on the hub

The plugin **srs** keeps a project's BA documents in the hub's database: an SRS written to a template, or a free
specification, with versions that members approve, and with a code on every use case, business rule, screen and
requirement that stays the same from version to version. Its code is `evo_agents/core_plugins/srs`; evo-ak ships it as
a core plugin, so every hub has it (`docs/plugins.md`, Core plugins). It does nothing in a project until an admin of
the project enables it there. Its agent type `ba` adds @ba, an agent that drafts and reviews the documents for the
project's writers (section @ba). The plugin tasks links tasks to its items (`docs/tasks-plugin.md`).

## Enabling it

```sh
evo-agents hub plugin enable srs --project evo-agents
evo-agents hub plugin config srs --project evo-agents --set 'roles={"ba": ["alice"], "qa": ["bao"], "dev": ["chi"]}'
evo-agents hub plugin config srs --project evo-agents \
  --set 'approval_policy={"srs": {"required": ["ba", "qa"], "optional": ["dev"]}}'
```

It asks for no permission. Its config, in the project's layer (`docs/hub.md`, Plugins in a project):

| Field | What it holds | When left out |
| --- | --- | --- |
| `template` | the template documents are read and written with | `srs-basic`, the only template of this release |
| `code_patterns` | per kind of coded item (`use_case`, `business_rule`, `screen`, `nfr`, `cbr`), a regular expression that replaces the template's | the template's patterns (section Markdown) |
| `roles` | per role of a review (`ba`, `qa`, `dev`), the members who hold it, by login | nobody holds a role |
| `approval_policy` | per kind of document (`srs`, `spec`), the roles whose approval a version needs (`required`) and those that may approve too (`optional`) | one approval is enough (section Approval) |

The headings a template reads stay the template's; a client changes only the patterns of its codes. Turning the
plugin off deletes nothing: its tables and rows stay for when it is on again.

## The model

- **A document** has a slug, unique in the project, a title, a kind (`srs` or `spec`), its template and a label, the
  project's unless an admin gives it another when the document is made. Every row of a document carries that label,
  and a member reads the document only when the sink of their session clears it.
- **A version** is one save of a document, never changed after. A document's versions form one straight line, each
  built on the one before it: `draft`, `in_review`, `approved`, and `superseded` once a newer version is approved.
  Each version keeps its author, its summary, its review round and, for a version @ba drafted, the run that drafted it
  (`drafted_by_run`).
- **An item** is a part of a version: a section, a use case, a business rule, a screen, a non-functional requirement
  (`nfr`), a common business rule (`cbr`), a glossary, the approval page or the revision history. The hub gives each
  item a key and keeps it across versions, so an item renamed, moved or renumbered is the same item. A use case keeps
  its fields (ID, Status, Objective, Actor, Trigger, Pre-condition, Post-condition) and the steps of its activity flow;
  a business rule the steps it applies to.
- **A code** (`UC ORD-3`, `BR ORD3-2`, `SCR-4`, `NFR-SEC-01`, `CBR 2`) names a coded item. A code and its aliases are
  unique in the project across every document, compared with whitespace runs as one space and case folded, so
  `UC ORD-1` and `uc  ord-1` are one code. A code stays when its item is renamed or moved, no save changes it, and
  once its item is dropped it is never given again. A screen without a code gets the next `SCR-<n>` of the project
  when it is saved.
- **Links** between the items of a version: `uses_screen` (a use case to a screen), `refines` and `relates`. They are
  set by a save of changes (the web, the API or @ba); Markdown does not carry them.
- **Reviews** approve a version in review or ask for changes, per round, each counting for a role.
- **Comments** are threads on a document, a version or an item. A comment @ba wrote names its run.

Migration 0046 adds the nine tables, each with the plugin's prefix: `srs_documents`, `srs_versions`, `srs_texts` (each
Markdown body once, by SHA-256), `srs_items`, `srs_codes`, `srs_item_versions`, `srs_refs`, `srs_reviews` and
`srs_comments`. Going back to 0045 drops them.

## Who does what

The routes answer a member with a grant on a project that enabled the plugin, and 404 in a project that did not.

| Who | May |
| --- | --- |
| a reader | read the documents, their versions, items, comparisons, exports and comments |
| a writer | import Markdown, save a version, submit a draft, review a version (section Approval), comment and close a thread |
| an admin | what a writer may, plus drop a coded item and give a new document a label of its own |

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
- **Errors that block submitting it**: a business rule outside every use case, a use case without an actor, a link
  to an item the version does not have, a `uses_screen` link to an item that is no screen.
- **Warnings**, shown only: a business rule whose code does not match its use case's, a business rule naming a step its
  use case does not have, and what reading the Markdown noted (a heading that skips a level, one the template does not
  name, coded rows kept as text).

## Approval

A writer submits a draft that has no error; it goes to `in_review` and its round goes up by one. A writer then
approves it or asks for changes, with these rules:

- Nobody who wrote a version since the one approved last, the version under review included, reviews it. Admins are
  no exception.
- The agent of a run never approves or returns a version: the route refuses any token scoped to a run.
- One review per member per round.
- An approval counts for a role the reviewer holds in `roles`: the one they name (`--role`), else the first role the
  policy requires that they hold and no approval of the round filled, else the first role they hold.
- The version is approved once every role the `approval_policy` of the document's kind requires approved it in this
  round. Without a policy, or one that requires no role, one approval is enough. Optional roles approve without being
  waited for.
- A request for changes sends the version back to `draft`. Submitted again, it is a new round.
- Approving a version supersedes every older one and, when the project has a knowledge config on the hub, queues a
  build of its knowledge graph.

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
`UC-ORD-001` and `ORD-01` aliases of `UC ORD-1`), and the steps of its Activity Flow table. In a section, the first run
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
evo-agents hub srs export orders --project evo-agents --version 2 --output orders-v2.md
```

## Routes

Under `/v1/x/srs/{project}/`:

| Route | What |
| --- | --- |
| `GET documents` | the documents the caller sees, each with its latest and approved versions |
| `GET documents/{slug}` (`?version=N`) | a document with every version, the newest first, and one version whole |
| `GET documents/{slug}/versions` | every version with its reviews |
| `GET documents/{slug}/versions/{number}` | a version whole: its items and its problems |
| `GET documents/{slug}/items/{ref}` (`?version=N`) | an item by code, alias or key, with its children |
| `GET documents/{slug}/diff?from=N&to=M` | what changed between two versions, item by item |
| `GET documents/{slug}/export` (`?version=N&anchors=false`) | a version as Markdown |
| `POST import` | Markdown as a new document or the next version of `document`; `dry_run` stops at the preview |
| `POST documents/{slug}/versions` | changes made on `base_version` as the next version (201), 409 on a conflict |
| `POST documents/{slug}/versions/{number}/submit` | a draft to review |
| `POST documents/{slug}/versions/{number}/reviews` | `{"decision": "approve" \| "request_changes", "role", "body"}` |
| `GET` and `POST documents/{slug}/comments`, `POST .../comments/{id}/resolve` | threads on a document, a version or an item |

The web shows them on the plugin's pages, at `/p/{project}/x/srs/documents` in the project's menu (SRS): the documents,
a document's contents with each section read and edited in place, an item's page with the trace the knowledge graph
holds, the versions, the comparison of two versions, the review and the comments; the import is reached from the
documents.

## The knowledge graph

The plugin is a source of the project's knowledge graph (extension point `kg_sources`, `docs/plugins.md`): the version
of each document approved last, never a draft or a version in review. A document is a Document node, each heading a
Section, a use case a UseCase node (`usecase:UC ORD-1`) and a business rule, an NFR or a common business rule a
Requirement node (`requirement:BR ORD1-2`), with the id a mention of the code anywhere in the graph gets, so a plan
step or a document that names the code reaches the node. A section `defines` its coded items, a business rule is
`child_of` its use case, and a use case `mentions` the section of each screen it uses. A task of the plugin tasks that
links an item `implements` it, so the graph traces a use case to its tasks, their plan steps and the commits of those
steps (`docs/kg.md`).

## @ba

The plugin contributes the agent type `ba`: the profile @ba with the pack `srs/ba` (its instructions and the skills
`srs-writing` and `srs-review`). A writer of the project mentions @ba in a group chat to ask about the documents,
draft or change sections, use cases, business rules, screens and requirements, review a version, or make tasks of
items (`mentioned_by: writer`).

**Enabling it.** An admin of the project enables the type after the plugin (`docs/plugins.md`, Agent types):

```sh
curl -X PUT "$HUB/v1/projects/evo-agents/agent-types/srs/ba" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"run_as": "alice", "workers": ["alice-mini"], "daily_usd": 5}'
```

`run_as` is the member whose workers run @ba, a writer or an admin of the project; `workers` the workers of that
member it may take (none: any the profile allows); `daily_usd` the cap of what its task runs, the reviews below, cost
in 24 hours. A chat run of @ba spends the chat's budget, as any chat run does.

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
| `srs__propose` | changes on `base_version` as the document's next version, sent to review at once | the agent of a chat run of @ba |
| `srs__comment` | a comment on a document, a version or an item | the agent of a run of @ba |
| `tasks__create` | a task linked to the items it names by code (plugin tasks, `docs/tasks-plugin.md`) | the agent of a chat run, or a member's own session, for a writer |

Each tool acts for a person: the agent of a chat run for the person who mentioned the agent, the agent of another run
for the member who dispatched it. A document is read when that person sees it through the session's sink and, when
the run's worker is another member's, when that member sees it too, since what a tool answers reaches the machine the
agent runs on.

**A proposal is a version members approve.** `srs__propose` checks its changes as a save is checked, writes them as
the next version under the name of the person who mentioned @ba, with `drafted_by_run` set, and submits it at once.
That person is its author, so they never approve it: another writer does. Its schema takes no drop and never changes a
code.

**@ba reviews each version that enters review.** The first time a version goes to review (round 1, from a member's
submit or from a proposal), the plugin hands @ba its review as a task run on a worker of `run_as`, with no
`runs:dispatch` grant: the image trusts the plugin. The run reads the version, checks it against the template, looks
for contradictions between use cases, with the glossary and with the NFRs, and writes each finding as a comment on its
item (at most 20, each starting `Blocking:` or `Suggestion:`), as `run_as`, with the run named on the comment. The
comments are advice: approval waits for none and reads none. A version sent back and submitted again is not reviewed
again. When the type is off, `run_as` no longer holds writer, or the type's runs spent `daily_usd` in the last 24
hours, the version goes to review without @ba's review and the hub's log says why. The audit log keeps
`task.dispatch`, with `run_as` as its actor.

**What @ba never does**: approve or return a version, drop an item, change or give again a code, archive a document,
change the plugin's config, or change the files of the repositories its run checks out.
