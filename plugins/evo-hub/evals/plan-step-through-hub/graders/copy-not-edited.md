---
type: regex
pattern: '  - id: 2\n    title: Wire the server\n    repo: app\n    what: Server\.handle in app/server\.py calls search\.\n    depends_on: \[1\]\n    status: pending\n  - id: 3\n'
match: contains
target:
  source: file
  path: plans/active/demo.yaml
---
