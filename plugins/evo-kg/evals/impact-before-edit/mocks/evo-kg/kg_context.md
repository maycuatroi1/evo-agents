---
type: fixed
---
context for compute_total: 4 nodes, 3 edges (build 7, connected through the seeds)
[symbol:app:app/pricing.py::compute_total] Symbol 'compute_total' (parsed, internal,U) path=app/pricing.py line=1
[symbol:app:app/checkout.py::Checkout.finish] Symbol 'Checkout.finish' (parsed, internal,U) path=app/checkout.py line=5
[symbol:app:app/reports.py::monthly_report] Symbol 'monthly_report' (parsed, internal,U) path=app/reports.py line=4
[app:file:app/pricing.py] File 'app/pricing.py' (parsed, internal,U) path=app/pricing.py
edges:
  [symbol:app:app/checkout.py::Checkout.finish] -calls-> [symbol:app:app/pricing.py::compute_total] (resolved)
  [symbol:app:app/reports.py::monthly_report] -calls-> [symbol:app:app/pricing.py::compute_total] (resolved)
  [app:file:app/pricing.py] -defines-> [symbol:app:app/pricing.py::compute_total] (parsed)
