"""The Loop of a project (plan loop-engine): deterministic code of the hub that turns each change of state of a project
into work items with ancestors and a key, and moves them on.

- ``model``: the words of contract loop/1 and the narrow interface of an engine (``Engine``);
- ``rules``: the engine of this release, ``rules/1``, the table of rules of the report's section Loop;
- ``view``: what a tick reads of a project for the engine;
- ``tick``: the job ``hub.loop_tick``, which writes what the engine returns;
- ``state``: the Loop turned on and off with the revision of the policy that sets ``loop.enabled``.

The Loop's rows are tables loop_states, loop_items and loop_item_events (``evo_agents.hub.tables_loop``); its keys are
the section ``loop`` of the autonomy policy (``evo_agents.hub.autonomy``). This package imports no core plugin: it
reads claims, use cases and tasks through the hub's tables. Nothing is imported here, so the policy's writes may
import ``state`` alone.
"""
