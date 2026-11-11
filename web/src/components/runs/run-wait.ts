"use client";

import { useQuery } from "@tanstack/react-query";

import { browserApi } from "@/lib/api/browser";
import { isWait, onlyWaits, waitSubject, type WaitSubject } from "@/lib/waits";

import { type Decision, openDecisionsQuery } from "./decision-queries";
import { LIVE_REFRESH_MS, type Run } from "./queries";

/**
 * The wait a waiting or parked run is on, when every open decision of it is a wait (web/DESIGN.md, The wait panel): the
 * run waits on something else, not a person, so its header and its plan's banner name what it waits for in a neutral
 * pill and ask nobody for an answer. Null while the run works, while it waits for a person's answer too, and while its
 * decisions are read. The query is the run's open decisions, the one its side column reads at the same rate.
 */
export function useRunWait(run: Pick<Run, "id" | "project" | "state">): { decision: Decision; subject: WaitSubject } | null {
  const waiting = run.state === "waiting" || run.state === "parked";
  const { data } = useQuery({ ...openDecisionsQuery(browserApi, run.project, run.id), enabled: waiting, refetchInterval: LIVE_REFRESH_MS });
  const open = data?.decisions ?? [];
  if (!waiting || !onlyWaits(open)) return null;
  const decision = open.find(isWait);
  return decision ? { decision, subject: waitSubject(decision.waiting_on, decision.wake_at ?? null) } : null;
}
