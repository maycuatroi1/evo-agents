---
type: fixed
---
impact of 1 seed(s) within 2 hop(s) (build 7): 5 dependent(s), 1 document(s) to re-check
label of every node: internal,U
seeds:
  [app:app/pricing.py::compute_total] Symbol (parsed) line=1
coverage of python seeds: calls across files yes; inheritance yes; overrides yes
hop 1:
  [app:app/pricing.py] File (parsed) -defines-> [app:app/pricing.py::compute_total] (parsed)
  [app:app/checkout.py::Checkout.finish] Symbol (parsed) line=5 -calls-> [app:app/pricing.py::compute_total] (resolved)
  [app:app/reports.py::monthly_report] Symbol (parsed) line=4 -calls-> [app:app/pricing.py::compute_total] (resolved)
hop 2:
  [app:app/checkout.py] File (parsed) -imports-> [app:app/pricing.py] (parsed)
  [app:app/reports.py] File (parsed) -defines-> [app:app/reports.py::monthly_report] (parsed)
documents to re-check:
  [app:docs/pricing.md#pricing] Section 'Pricing' (parsed) -mentions-> [app:app/pricing.py] (resolved), [app:app/pricing.py::compute_total] (resolved)
