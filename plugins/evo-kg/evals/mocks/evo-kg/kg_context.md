---
type: fixed
---
context for KB-01: 4 nodes, 3 edges (build 4, connected through the seeds)
label of every node: internal,U
[usecase:KB-01] UseCase (parsed)
[app:app/search.py::search] Symbol (parsed) line=5
[plan:demo/step:2] PlanStep 'Wire the server' (parsed) status=pending number=2
[app:app/search.py] File (parsed)
edges:
  [app:app/search.py::search] -implements-> [usecase:KB-01] (declared)
  [plan:demo/step:2] -mentions-> [usecase:KB-01] (parsed)
  [app:app/search.py] -defines-> [app:app/search.py::search] (parsed)
