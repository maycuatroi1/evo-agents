---
type: fixed
---
impact of 1 seed(s) within 2 hop(s) (build 7): 5 dependent(s), 1 document(s) to re-check
seeds:
  [symbol:app:app/pricing.py::compute_total] Symbol 'compute_total' (parsed, internal,U) path=app/pricing.py line=1
coverage of python seeds: calls across files yes; inheritance yes; overrides yes
hop 1:
  [app:file:app/pricing.py] File 'app/pricing.py' (parsed, internal,U) path=app/pricing.py -defines-> [symbol:app:app/pricing.py::compute_total] (parsed)
  [symbol:app:app/checkout.py::Checkout.finish] Symbol 'Checkout.finish' (parsed, internal,U) path=app/checkout.py line=5 -calls-> [symbol:app:app/pricing.py::compute_total] (resolved)
  [symbol:app:app/reports.py::monthly_report] Symbol 'monthly_report' (parsed, internal,U) path=app/reports.py line=4 -calls-> [symbol:app:app/pricing.py::compute_total] (resolved)
hop 2:
  [app:file:app/checkout.py] File 'app/checkout.py' (parsed, internal,U) path=app/checkout.py -imports-> [app:file:app/pricing.py] (parsed)
  [app:file:app/reports.py] File 'app/reports.py' (parsed, internal,U) path=app/reports.py -defines-> [symbol:app:app/reports.py::monthly_report] (parsed)
documents to re-check:
  [app:file:docs/pricing.md#pricing] Section 'Pricing' (parsed, internal,U) -mentions-> [app:file:app/pricing.py] (resolved), [symbol:app:app/pricing.py::compute_total] (resolved)
