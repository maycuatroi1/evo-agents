"use client";

import { Info } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { useStatusText } from "@/components/status/status-badge";

import type { PlanRunHold } from "./dispatch-steps";
import { runHref } from "./queries";

/** "Plan run #12 (Waiting) holds this plan's steps until it ends.", linked to the run. */
export function PlanRunHoldNote({ project, hold, testId }: { project: string; hold: PlanRunHold; testId?: string }) {
  const t = useTranslations("runs.planRun");
  const tState = useStatusText("run");
  return (
    <p className="flex items-start gap-2 rounded-md border border-dashed px-3 py-2.5 text-sm text-pretty text-muted-foreground" data-testid={testId}>
      <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>
        {t.rich("holds", {
          id: hold.run.id,
          state: tState(hold.run.state),
          login: hold.run.dispatched_by,
          link: (chunks) => (
            <Link href={runHref(project, hold.run.id)} className="font-medium text-brand underline-offset-4 hover:underline">
              {chunks}
            </Link>
          ),
        })}
      </span>
    </p>
  );
}
