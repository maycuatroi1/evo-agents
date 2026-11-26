"use client";

import { useCallback, useDeferredValue, useMemo } from "react";

import type { TraceReveal } from "./agent-trace";
import type { RunEvent } from "./queries";
import type { SessionTab } from "./run-log";
import { useAgentProfile } from "./run-page-state";
import { runTasks, taskOf } from "./run-tasks";

/**
 * The run page's subagents (run-tasks.ts), read once from its events for the Running now panel, the trace's
 * delegations and a subagent's profile; the profile open (`?agent=`, run-page-state.ts), when it names a subagent of the
 * run; and the two things a row of theirs does: open a subagent's profile on the session's Trace tab, or show the call
 * that started a command in the main agent's trace. `showSession` must be stable (useShowSession).
 */
export function useRunAgents(events: readonly RunEvent[], showSession: (tab: SessionTab) => void, setReveal: (reveal: TraceReveal) => void) {
  const deferred = useDeferredValue(events);
  const tasks = useMemo(() => runTasks(deferred), [deferred]);
  const profile = useAgentProfile();
  const found = taskOf(tasks, profile.agent);
  const viewed = found?.kind === "agent" ? found : null;
  const { open, close, agent } = profile;
  const openAgent = useCallback(
    (id: string) => {
      showSession("trace");
      open(id);
    },
    [showSession, open],
  );
  const showCall = useCallback(
    (call: string) => {
      if (agent) close();
      setReveal({ call, seq: null, nonce: Date.now() });
      showSession("trace");
    },
    [agent, close, setReveal, showSession],
  );
  return { tasks, profile, viewed, openAgent, showCall };
}
