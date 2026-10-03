#!/bin/sh
# A tiny shop project for the impact-before-edit case; the kg_* tools in mocks/ describe the same project.
set -e
mkdir -p app docs
: > app/__init__.py
printf 'def compute_total(items):\n    return sum(item["price"] * item["qty"] for item in items)\n' > app/pricing.py
printf 'from app.pricing import compute_total\n\n\nclass Checkout:\n    def finish(self, cart):\n        return {"total": compute_total(cart), "status": "paid"}\n' > app/checkout.py
printf 'from app import pricing\n\n\ndef monthly_report(orders):\n    return [pricing.compute_total(order) for order in orders]\n' > app/reports.py
printf '# Pricing\n\n`compute_total` in app/pricing.py adds price times quantity over the items of an order.\n' > docs/pricing.md
