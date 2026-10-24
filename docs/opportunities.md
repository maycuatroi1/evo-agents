# The opportunities of a project

An opportunity says what a project could win (plan opportunity-intake): who benefits, what makes it reachable now, and
how long the window stays open. Each project on the hub keeps its opportunities as objects of their own, with the code
`O-<number>`, beside its problems (`docs/problems.md`). An opportunity may answer problems, and the evidence that
shows it is there is kept with it. A project with no opportunity works as before.

| Part | Where |
| --- | --- |
| the model: codes, fields, signals | `evo_agents/hub/opportunities.py` |
| the routes and the writes | `evo_agents/hub/server/opportunities.py`, `evo_agents/hub/server/opportunity_evidence.py` |
| the tables | `evo_agents/hub/tables_opportunities.py` (schema 0141) |
| the commands | `evo_agents/hub/cli_opportunities.py` |
| the MCP tools `opportunity_list`, `opportunity_show`, `opportunity_evidence` | `evo_agents/hub/mcp_tools.py`, `evo_agents/hub/server/opportunity_mcp.py` |
| the knowledge graph | `evo_agents/hub/kg_opportunities.py`, `evo_agents/hub/kg_sources.py` |
| @builder's proposals of kind `opportunity` | `evo_agents/core_plugins/builder/opportunity_changes.py` |

## An opportunity

| Field | |
| --- | --- |
| code | `O-<number>`, given in order within the project and never given again: an opportunity is closed, never deleted |
| `statement` | what could be won; required, at most 2000 characters |
| `beneficiary` | who benefits; required |
| `leverage` | what makes it reachable now; required |
| `window_until` | optional: the date after which it is gone (`YYYY-MM-DD`) |
| `window_note` | optional: what the window is |
| `answers` | at most 20 problem codes: `P-3` for a problem of the project, `lumen/P-3` for one of another project |
| `metric` | optional, as a problem's (`docs/problems.md`): a key of the project's registry of signals, summed up over a window, with no target |
| `rank` | its place among the project's opportunities, 1 first; a new one goes after the open ones |
| `owner` | a member with the writer role or above; the writer, for a new opportunity |
| `status` | `open`, `captured`, `missed` or `dropped` |
| `close_reason` | why it was captured, missed or dropped; present exactly when the status is not `open` |
| `note` | what the writer said of this revision, for the history |

The writer sets the status: the hub never works it out from a window or a metric. Every write is the opportunity's
next revision, written whole, and every revision is kept with its writer and time. A write names the revision it read
(`if_revision`); another writer's revision in between makes it a 409, and `if_revision` 0 creates the opportunity only.
A body equal to the newest revision writes nothing (`changed` false). Each revision adds an audit row
`opportunity.set` that names the opportunity, the revision and the fields whose value changed, never a value.

An answer names a problem that exists and is not dropped, else the hub answers 422 with the code. A code of another
project needs a writer grant on that project. A reader of the opportunity who holds no grant on that project sees a
count (`hidden_answers`) and no code of it; a writer who cannot see such an answer keeps it when they write the
opportunity again. A problem that was dropped after the opportunity named it stays named.

## Who reads and writes

Every member with a grant on the project reads its opportunities; a member without one gets 404. A writer of the
project writes, closes and reopens one. The agent of a run never writes one: @builder proposes one (proposal kind
`opportunity`, target `{"opportunity": "O-<number>"}`, none for a new one), and a writer approves or rejects it.
Rolling back an approved proposal that made a new opportunity drops it.

## Evidence

A line of evidence is a problem's (`docs/problems.md`): the kind and address of its source, a place in it, who spoke,
an excerpt of at most 300 characters, the signal it shows, a label (`public` or `internal`; `personal` gives 422), who
added it and who confirmed it. The signals are the eight of a problem and seven more: `wish`, `external_demand`,
`window`, `reusable_asset`, `pull`, `intro` and `idle_capacity`. A problem's evidence keeps its eight. `show` reports
the count of lines and of independent sources, a source being a kind and an address. A proposal of @builder may carry
lines; approving it creates them with the opportunity.

## Commands

```
evo-agents hub opportunity list --project northwind                  # by rank; --status open, captured, missed or dropped
evo-agents hub opportunity show O-1 --project northwind
evo-agents hub opportunity set --statement "Sell the shelf scanner to the depot" --beneficiary "the night shift" \
    --leverage "the depot asked for a demo twice" --window-until 2026-12-01 --answers "P-1,lumen/P-2" --project northwind
evo-agents hub opportunity set O-1 --rank 2 --note "after the fair" --project northwind
evo-agents hub opportunity close O-1 --status captured --reason "the depot signed" --project northwind
evo-agents hub opportunity set O-1 --reopen --project northwind
evo-agents hub opportunity evidence add O-1 --source-kind email --source-uri email://depot/42 \
    --excerpt "Show us the scanner." --signal wish --project northwind
evo-agents hub opportunity evidence list O-1 --json
evo-agents hub opportunity evidence confirm O-1 E-1
```

The routes are under `/v1/projects/{project}/opportunities`. The MCP tools of `evo-agents hub mcp` read the same:
`opportunity_list` (`project`, `status`), `opportunity_show` (`code`, `revision`) and `opportunity_evidence` (`code`).

## The knowledge graph

An opportunity is an `Opportunity` node (`opportunity:O-1`) found by its code or by the words of its text. It
`answers` the Problem nodes of the same project. An answer that names another project's problem lives only in the hub's
table: the graph of a project holds what the project's members read.
