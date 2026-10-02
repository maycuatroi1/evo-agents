---
name: exact-string
tags: [grep-for-strings]
max_turns: 10
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill, mcp__plugin_evo-kg_evo-kg__kg_search, mcp__plugin_evo-kg_evo-kg__kg_context, mcp__plugin_evo-kg_evo-kg__kg_node, mcp__plugin_evo-kg_evo-kg__kg_status]
---

Find every file in this repository that contains the exact text `return sorted(items)`.
