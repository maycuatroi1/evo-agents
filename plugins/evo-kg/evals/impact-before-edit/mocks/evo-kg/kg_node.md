---
type: fixed
---
[symbol:app:app/pricing.py::compute_total] Symbol 'compute_total' (parsed, internal,U) path=app/pricing.py line=1
evidence: app:file:app/pricing.py rev 9c41d07 line 1
in (4):
  <- calls [symbol:app:app/checkout.py::Checkout.finish] Symbol 'Checkout.finish' (resolved)
  <- calls [symbol:app:app/reports.py::monthly_report] Symbol 'monthly_report' (resolved)
  <- defines [app:file:app/pricing.py] File 'app/pricing.py' (parsed)
  <- mentions [app:file:docs/pricing.md#pricing] Section 'Pricing' (resolved)
