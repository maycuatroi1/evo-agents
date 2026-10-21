"use client";

import dynamic from "next/dynamic";

import { Skeleton } from "@/components/ui/skeleton";

import type { StepReportMessageProps } from "./trace-report";

function ReportSkeleton() {
  return <Skeleton className="h-24 w-full rounded-md" aria-busy="true" data-testid="trace-report-loading" />;
}

/** The chunk failed to load (offline, a deploy replaced it): the message as Markdown, as it read before the card. */
function ReportLoadError({ text, message }: StepReportMessageProps) {
  return message(text);
}

/**
 * An agent's message that holds a step's report, loaded on demand (`next/dynamic`): most messages of a trace hold
 * none, so the parser (step-report.ts) and the card stay out of the run page's first load (web/bundle-budget.json).
 * step-report-start.ts tells which messages hold one.
 */
export const StepReportMessage = dynamic<StepReportMessageProps>(
  () => import("./trace-report").then((module) => module.StepReportMessage).catch(() => ReportLoadError),
  { loading: ReportSkeleton },
);
