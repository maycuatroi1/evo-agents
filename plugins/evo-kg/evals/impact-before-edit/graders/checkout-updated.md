---
type: regex
pattern: '^(?![\s\S]*compute_total)[\s\S]*order_total\('
match: contains
target:
  source: file
  path: app/checkout.py
---
