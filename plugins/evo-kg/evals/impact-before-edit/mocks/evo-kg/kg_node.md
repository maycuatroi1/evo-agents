---
type: fixed
---
[app:app/pricing.py::compute_total] Symbol (parsed, internal,U) line=1
evidence: app:app/pricing.py rev 9c41d07 line 1
in (4):
  <- calls [app:app/checkout.py::Checkout.finish] Symbol (resolved)
  <- calls [app:app/reports.py::monthly_report] Symbol (resolved)
  <- defines [app:app/pricing.py] File (parsed)
  <- mentions [app:docs/pricing.md#pricing] Section 'Pricing' (resolved)
