"""The Loop of a project (plan loop-engine): deterministic code of the hub that turns each change of state of a project
into work items with ancestors and a key, and moves them on.

- ``model``: the words of contract loop/1 and the narrow interface of an engine (``Engine``);
- ``rules2``: the engine of this release, ``rules/2`` (plan problem-oriented-hub), which goes from the problem a goal
  answers; ``rules``: the engine before it, ``rules/1``, the table of rules of the report's section Loop, on which
  rules/2 builds;
- ``view``: what a tick reads of a project for the engine;
- ``tick``: the job ``hub.loop_tick``, which writes what the engine returns;
- ``admit``: the last step of a tick, which checks each queued work item against the breaker, the caps of concurrency,
  the rate of hand-outs and the worker before a run is handed out for it (criterion a5);
- ``caps``: what a run the Loop hands out may use: the turns and minutes of ``loop.caps`` for an author or a plan run,
  the agent type's daily cap for a task run;
- ``dispatch`` and ``texts`` (criterion a6): the hand-out of each kind of work item through a run kind the hub has, and
  what its agent is told; ``author_mode``: the end of an author run of the Loop once its plan is on the hub;
  ``runs_of``: which work item a run was handed out for;
- ``stall`` and ``breaker`` (criterion a7): triage at the second failure, a decision of a person at the third, and the
  breaker that stops the Loop of a project making too many work items in an hour; ``routes``: its state and the reset
  of its breaker;
- ``state``: the Loop turned on and off with the revision of the policy that sets ``loop.enabled``;
- ``board``, ``board_gates``, ``board_chain`` and ``board_items`` (criterion a8): the project's decision board, the
  three gates and what the Loop's chain waits on a person for; ``notices``: the month's review of the goals, the Loop's
  part of the morning brief, and its brief of its own for a project whose Curator sends none.

The Loop's rows are tables loop_states, loop_items and loop_item_events (``evo_agents.hub.tables_loop``); its keys are
the section ``loop`` of the autonomy policy (``evo_agents.hub.autonomy``). This package imports no core plugin: it
reads claims, use cases and tasks through the hub's tables. Nothing is imported here, so the policy's writes may
import ``state`` alone.
"""
