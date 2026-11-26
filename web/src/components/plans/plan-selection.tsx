"use client";

import { Ban, CheckCheck, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { canCloseAll, planName } from "@/lib/plan-list";
import type { PlanSummary } from "@/lib/plans";

import type { PlanAction } from "./plan-actions";
import { PlanWriteDialog } from "./plan-write-dialog";

/**
 * The bar under the list while plans are ticked (web/DESIGN.md, Plans): how many, Abandon (one reason for all of them),
 * Close when every one is open with all its steps done or descoped, and Clear. It sticks to the foot of the screen,
 * as wide as the list. `onKeep` gets the ids a write left refused, so they stay ticked; the others are let go.
 */
export function PlanSelectionBar({
  project,
  plans,
  onKeep,
  canWrite,
}: {
  project: string;
  plans: readonly PlanSummary[];
  onKeep: (ids: string[]) => void;
  canWrite: boolean;
}) {
  const t = useTranslations("plans.selection");
  const [action, setAction] = useState<PlanAction | null>(null);
  const [open, setOpen] = useState(false);
  if (plans.length === 0 && !open) return null;
  const targets = plans.map((plan) => ({ plan_id: plan.plan_id, name: planName(plan), revision: plan.revision }));
  const closable = canCloseAll(plans);
  const start = (next: PlanAction) => {
    setAction(next);
    setOpen(true);
  };
  return (
    <>
      {plans.length > 0 ? (
        <div
          role="region"
          aria-label={t("label")}
          className="sticky bottom-3 z-10 mt-3 flex flex-wrap items-center gap-2 rounded-lg border bg-popover px-3 py-2 shadow-dialog"
          data-testid="plans-selection-bar"
        >
          <p className="mr-auto text-sm font-medium tabular-nums" aria-live="polite" data-testid="plans-selection-count">
            {t("count", { count: plans.length })}
          </p>
          {canWrite ? (
            <>
              {closable ? (
                <Button type="button" size="sm" variant="outline" onClick={() => start("close")} data-testid="plans-bulk-close">
                  <CheckCheck aria-hidden="true" />
                  {t("close")}
                </Button>
              ) : null}
              <Button type="button" size="sm" variant="outline" className="text-danger" onClick={() => start("abandon")} data-testid="plans-bulk-abandon">
                <Ban aria-hidden="true" />
                {t("abandon")}
              </Button>
            </>
          ) : null}
          <Button type="button" size="sm" variant="ghost" onClick={() => onKeep([])} data-testid="plans-bulk-clear">
            <X aria-hidden="true" />
            {t("clear")}
          </Button>
        </div>
      ) : null}
      {action ? (
        <PlanWriteDialog project={project} action={action} plans={targets} open={open} onOpenChange={setOpen} onFinished={onKeep} />
      ) : null}
    </>
  );
}
