---
type: fixed
---
# Mirror of evo-agents hub plan demo, revision 3. Do not edit; use evo harness step or evo-agents hub plan.
id: demo
goal: 'Ship search for the demo app: rank results and wire the server to them.'
created_at: '2026-09-28T09:00:00+07:00'
repos:
  - repo: app
    branch: search
    status: in_progress
steps:
  - id: 1
    title: Rank results
    repo: app
    what: rank_results in app/search.py sorts the results.
    status: done
    done_at: '2026-09-30'
    evidence: 'app@9b1e4d2: tests 8 passed'
  - id: 2
    title: Wire the server
    repo: app
    what: Server.handle in app/server.py calls search.
    depends_on: [1]
    status: pending
  - id: 3
    title: Document search
    repo: app
    what: docs/guide.md explains search and ranking.
    depends_on: [2]
    status: pending
hub:
  project: demo
  revision: 3
  digest: sha256:bc8d6bb7ab927edbe8715861d527781b18a382b10ee06032dacf602945ce2338
