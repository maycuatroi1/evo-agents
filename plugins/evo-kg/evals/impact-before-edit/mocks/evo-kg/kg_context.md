---
type: fixed
---
context for compute_total: 4 nodes, 3 edges (build 7, connected through the seeds)
label of every node: internal,U
[app:app/pricing.py::compute_total] Symbol (parsed) line=1
[app:app/checkout.py::Checkout.finish] Symbol (parsed) line=5
[app:app/reports.py::monthly_report] Symbol (parsed) line=4
[app:app/pricing.py] File (parsed)
edges:
  [app:app/checkout.py::Checkout.finish] -calls-> [app:app/pricing.py::compute_total] (resolved)
  [app:app/reports.py::monthly_report] -calls-> [app:app/pricing.py::compute_total] (resolved)
  [app:app/pricing.py] -defines-> [app:app/pricing.py::compute_total] (parsed)
