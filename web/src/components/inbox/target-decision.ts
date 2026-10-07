"use client";

import { useQuery } from "@tanstack/react-query";
import { type RefObject, useEffect } from "react";

import { browserApi } from "@/lib/api/browser";

import { useInboxViewer } from "./hooks";
import { decisionQuery, locateDecisionQuery } from "./queries";

/** The decision a sheet shows: its id, and its project when the caller knows it (Home, a notification on the page). */
export type DecisionTarget = {
  id: number;
  project: string | null;
  /** When its run parks, if the caller holds it already (the Home's overview). */
  parksAt?: string | null;
};

/**
 * The decision a sheet or the phone's screen shows: read from its project, or, for a link that names only the decision,
 * asked of each project the visitor holds a grant on at once. The question's heading takes focus once it shows, unless
 * the visitor has already moved on inside the sheet. `notFound` when no project of the visitor holds it.
 */
export function useTargetDecision(
  target: DecisionTarget,
  headingRef: RefObject<HTMLHeadingElement | null>,
  contentRef: RefObject<HTMLDivElement | null>,
) {
  const viewer = useInboxViewer();
  const located = useQuery({
    ...locateDecisionQuery(browserApi, target.id, viewer.projects),
    enabled: target.project === null && viewer.login !== null,
  });
  const project = target.project ?? located.data?.project ?? null;
  const decision = useQuery({
    ...decisionQuery(browserApi, project ?? "-", target.id),
    enabled: project !== null,
    initialData: located.data && located.data.project === project ? located.data.decision : undefined,
  });
  const loaded = decision.data !== undefined;
  useEffect(() => {
    if (!loaded) return;
    const active = document.activeElement;
    if (active === null || active === document.body || active === contentRef.current) headingRef.current?.focus();
  }, [loaded, headingRef, contentRef]);

  const notFound =
    (target.project === null && located.isSuccess && located.data === null) ||
    (decision.isError && decision.error.info.status === 404) ||
    (target.project === null && viewer.login !== null && viewer.projects.length === 0);
  const error = decision.isError ? decision.error : located.isError ? located.error : null;
  const retry = () => void (decision.isError ? decision.refetch() : located.refetch());
  return { project, decision, notFound, error, retry };
}
