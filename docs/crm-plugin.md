# The core plugin crm: the people who found the project themselves

The plugin **crm** keeps, for a project that enabled it, a short list of the people who came to the project on their own
and where each stands: a contact is a GitHub login with its public profile link, the source it was first seen from, its
stage and the touchpoints that put it there. It stores no email address and reads nothing from any other service about
the person. Its code is `evo_agents/core_plugins/crm`; evo-ak ships it as a core plugin (`docs/plugins.md`, Core
plugins). It does nothing in a project until an admin of the project enables it there; until then it writes no row and
its routes answer 404.

## Enabling it

```sh
evo-agents hub plugin enable crm --project evo-agents                       # asks for no permission
evo-agents hub plugin config crm --project evo-agents --set repo=acme/widgets
```

| Field | What it holds | When left out |
| --- | --- | --- |
| `repo` | `owner/name` of the repo whose issues, pull requests and discussions make contacts | only the hub's invites and grants count |

Discussions are read through GitHub's GraphQL API, which takes no anonymous call: set `GITHUB_TOKEN` in the hub's
environment (a token that reads public repos is enough) or the plugin reads issues and pull requests only. A read that
fails is logged and changes nothing; the next pass reads the same span again.

## Model

Five tables, from migration 0124 (`evo_agents/hub/tables_crm.py`):

| Table | What it holds |
| --- | --- |
| `crm_contacts` | one row for each login (lower case) in a project: profile link, first source, stage, `first_seen_at`, `stage_at`, label |
| `crm_touchpoints` | what the contact did: kind, a reference that makes it one row (`issue#12`, `invite:3`), a link, its time, and for a note its text and writer |
| `crm_stage_history` | each stage a contact reached, with the time it was reached |
| `crm_cursors` | how far the intake of each source (`hub`, `github`) has read a project |
| `crm_deletions` | one row for each contact deleted on request: its number, how many touchpoints went, who deleted it and when; no login, no link, no text |

A contact carries the label of its project's default when it is made, and reads follow it like the rest of the hub: a
caller whose grant or sink is below a contact's label does not see it and does not count it.

## Sources

| Source | Who becomes a contact |
| --- | --- |
| `invite_request` | the author of an issue whose title starts with `Invite:` (the form `.github/ISSUE_TEMPLATE/invite.yml`) |
| `invite_link` | the login an invite link was made for (`for_login`), or the login that accepted a join link |
| `grant` | a login with a grant on the project whose role is not admin |
| `issue`, `pull_request`, `discussion` | the author of one on the project's repo who is no member of it |

Not contacts: the admins of the project (the team), a GitHub owner, member or collaborator of the repo, a bot, and a
person who only replies: comments are never read. The title and the body of an issue stay on GitHub; the plugin keeps
the number, the link and the time.

The job `intake` runs every 15 minutes. GitHub's `since` includes the item at the cursor, so the plugin skips what the
cursor already passed; an item that arrives in the same second as the cursor and after it is read at its next update
only [Inference].

## Stages

The stage is computed from hub data (`evo_agents/core_plugins/crm/rules.py`, `staging.py`), the same facts giving the
same answer, and only moves forward; each stage a contact reached is kept in `crm_stage_history` with its time.

| Stage | When |
| --- | --- |
| `lead` | seen: it has a touchpoint |
| `invited` | an invite link names its login, or it holds a grant |
| `activated` | its login, as a user of the hub, dispatched a run in the project (the first run's time) |
| `retained` | it dispatched a run from day 21 up to day 28 after that first run |

A stage whose own fact is missing but a later one's is present takes the later time, so the times never run backwards.
A note a writer adds is a touchpoint but never moves a stage.

## Signal

The job pushes the number of contacts now in each stage to the project's Signal as `crm.lead`, `crm.invited`,
`crm.activated` and `crm.retained`, from the source `crm`. The hub takes a signal of a key an admin declared, and the
plugin declares none:

```sh
for stage in lead invited activated retained; do
  evo-agents hub signal keys add crm.$stage --project evo-agents --kind number --unit contacts
done
```

Until then the pushes are counted as rejected in the job's answer and nothing else happens. The numbers are written as
the ingest key `crm` of the project, which the plugin makes once with a token nobody sees; an admin who revokes it stops
the pushes. A number is sent once a day for each value it takes. A signal carries counts and no login.

## Reading and writing

| Who | What |
| --- | --- |
| reader | `GET /v1/x/crm/{project}/contacts?stage=&source=` and `GET /v1/x/crm/{project}/contacts/{login}` |
| writer | `POST /v1/x/crm/{project}/contacts/{login}/notes` with `{"note": "..."}` (1 to 1000 characters) |
| admin | `DELETE /v1/x/crm/{project}/contacts/{login}` |

```sh
evo-agents hub crm list --project evo-agents [--stage lead] [--source issue] [--json]
evo-agents hub crm show dung --project evo-agents [--json]
evo-agents hub crm delete dung --project evo-agents --yes
```

The knowledge graph of the project gets one document of kind `doc` for each contact (source `contacts`): its login,
its source and its stage, with the label of the row. No touchpoint, note, time or link reaches the graph.

## @community and the Loop

The plugin contributes the agent type `@community`, the tool `crm__invite` and the Loop rule `growth` (plan
autonomous-oss-growth, criterion a16). When the job `intake` stores a new GitHub touchpoint from the last 7 days, it
records the event `plugin.crm.touch` with the contact's number and no login. The rule turns that event into work:

- An invite request (an issue whose title starts with `invite:`) makes a `growth_invite` task: answer in the issue and,
  when `crm__invite` makes a link, post it there.
- An outsider's new issue, pull request or discussion makes a `growth_draft` task: the first reply, through the gate of
  the plugin marketing (`marketing__propose`, kind reply).

`crm__invite` makes a join link only for a run of `@community`, and only if all of these hold:

- the login asked with that request;
- no link was made for the request before;
- the project made fewer than `invite_links_per_week` links in the last 7 days (3 by default);
- the GitHub account is at least `invite_min_github_age_days` old (30 by default).

The link gives the reader role at the lowest level of the project's ladder and lasts 7 days. Only that login can accept
it. It is made in the name of the member the type runs as, with the audit row `invite.create`. Apart from a hub admin,
`@community` is the only maker of invite links.

## Privacy

[Unverified] The plugin is built for the Vietnamese Personal Data Protection Law, Luật 91/2025/QH15; this page is not
legal advice and cannot verify how the law applies to a given hub, so the operator decides that.

What it does, as the code does it:

- It keeps the least it can: a public GitHub login, the public profile link, the source, the stage, times and the links
  of public issues, pull requests and discussions. No email address, no enrichment from other services, no text of an
  issue.
- A contact can be removed on request, whole: see below.
- The audit and `crm_deletions` hold the number of the contact and no login, so a deletion leaves nothing that names the
  person.
- A person who only replies on GitHub is never stored.

## How to delete a contact

An admin of the project, on the request of the person:

```sh
evo-agents hub crm delete dung --project evo-agents --yes
```

This deletes the contact, all its touchpoints (notes included) and its stage history in one transaction, and adds a row
to `crm_deletions` and to the audit (`crm.delete`, target `contact:<number>`), neither of which holds the login. The
cursors of the intake stay, so what GitHub still shows of the person does not make the contact again; if the person
opens a new issue or pull request after that, it counts as a new contact. [Inference] Signals already pushed hold
counts only and are not changed. A login that the hub's own grants or invites still name (an invite made for the login,
a grant) is not removed by this: revoke that on the hub too.
