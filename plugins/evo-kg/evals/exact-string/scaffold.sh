#!/bin/sh
# A tiny project for the evo-kg eval cases; the kg_* tools are mocked to describe the same project.
set -e
mkdir -p app docs plans
printf 'def rank_results(items):\n    return sorted(items)\n\n\ndef search(query):\n    return rank_results([query])\n' > app/search.py
printf 'from app.search import search\n\n\nclass Server:\n    def handle(self, q):\n        return search(q)\n' > app/server.py
printf '# Guide\n\nSearch is implemented for KB-01.\n' > docs/guide.md
printf 'id: demo\nsteps:\n  - id: 1\n    what: ranking\n    status: done\n  - id: 2\n    what: wire the server\n    status: pending\n' > plans/demo.yaml
