---
type: fixed
---
context for KB-01: 4 nodes, 3 edges (build 4, connected through the seeds)
[usecase:KB-01] UseCase 'KB-01' (parsed, internal,U)
[symbol:app:app/search.py::search] Symbol 'search' (parsed, internal,U) path=app/search.py line=5
[plan:demo/step:2] PlanStep 'Wire the server' (parsed, internal,U) status=pending number=2
[app:file:app/search.py] File 'app/search.py' (parsed, internal,U) path=app/search.py
edges:
  [symbol:app:app/search.py::search] -implements-> [usecase:KB-01] (declared)
  [plan:demo/step:2] -mentions-> [usecase:KB-01] (parsed)
  [app:file:app/search.py] -defines-> [symbol:app:app/search.py::search] (parsed)
