"use client";

import dynamic from "next/dynamic";
import { useMemo } from "react";

import { type PeekProps, PlansList } from "@/components/plans/plans-list";
import { useCanDispatch } from "@/components/runs/hooks";
import type { ApiErrorInfo } from "@/lib/api/errors";

/** The peek panel loads the first time a row opens it, or the address names a plan: the list does not carry it. */
const PlanPeek = dynamic(() => import("./plan-peek").then((module) => module.PlanPeek), { ssr: false });

function Peek(props: PeekProps) {
  return <PlanPeek {...props} />;
}

/**
 * The plans list with what it borrows from the runs feature: the peek panel (Run plan, the plan's recent runs) and
 * whether the member may write. Feature folders do not import one another, so the route joins them here.
 */
export function PlansRoute({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const canWrite = useCanDispatch(project);
  const slots = useMemo(() => ({ Peek, canWrite }), [canWrite]);
  return <PlansList project={project} initialError={initialError} slots={slots} />;
}
