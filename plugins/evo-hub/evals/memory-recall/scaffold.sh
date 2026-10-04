#!/bin/sh
# A project checkout with a report draft that says nothing about formats; the convention lives only in a project
# memory on the hub, which the mocked memory_search and memory_get return.
set -e
mkdir -p app reports
printf 'def total(items):\n    return sum(item["amount"] for item in items)\n' > app/billing.py
printf '# Report, August\n\nRevenue grew this month. Totals per client are in the table below.\n' > reports/august.md
printf 'name: demo\nworkspace: .\nhub: {project: demo}\nrepos:\n  - {name: app, path: app}\n' > harness.yaml
