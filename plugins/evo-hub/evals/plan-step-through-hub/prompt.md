---
name: plan-step-through-hub
tags: [plans-through-hub]
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill, Edit, Write, mcp__plugin_evo-hub_evo-hub__plan_list, mcp__plugin_evo-hub_evo-hub__plan_show, mcp__plugin_evo-hub_evo-hub__plan_step, mcp__plugin_evo-hub_evo-hub__hub_projects]
---

Step 2 of plan demo, wiring the server, is finished: commit 3f2a9c1 makes Server.handle call search, and `pytest -q` passes with 12 tests. Record that in the plan.
