---
name: impact-before-edit
tags: [impact-before-edit]
max_turns: 20
timeout_seconds: 400
allowed_tools: [Read, Glob, Grep, Skill, Edit, Write, mcp__plugin_evo-kg_evo-kg__kg_search, mcp__plugin_evo-kg_evo-kg__kg_context, mcp__plugin_evo-kg_evo-kg__kg_node, mcp__plugin_evo-kg_evo-kg__kg_impact, mcp__plugin_evo-kg_evo-kg__kg_status]
---

Rename the function `compute_total` in app/pricing.py to `order_total`, and keep everything that uses it working. Then list the places you changed.
