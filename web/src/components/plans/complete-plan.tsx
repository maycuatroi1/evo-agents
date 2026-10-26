"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { planKeys } from "@/lib/plan-queries";
import type { PlanSummary } from "@/lib/plans";

/**
 * Mark done, on a row of the active plans for a writer of the project: confirmed first, then the hub moves the plan to
 * the completed area, marking each step that is neither done nor descoped done in the same revision (POST .../complete
 * with mark_steps_done). The confirmation says how many steps that is; a refusal stays in it, in the hub's words.
 */
export function CompletePlanButton({ project, plan }: { project: string; plan: PlanSummary }) {
  const t = useTranslations("plans.complete");
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<WriteFailure | null>(null);
  const title = plan.title ?? plan.plan_id;
  const open = plan.steps_total - plan.steps_done;
  const complete = useMutation({
    mutationFn: () =>
      call(
        browserApi().POST("/v1/projects/{project}/plans/{plan_id}/complete", {
          params: { path: { project, plan_id: plan.plan_id } },
          body: { if_revision: plan.revision, mark_steps_done: true },
        }),
      ),
    onSuccess: () => {
      setConfirming(false);
      notify({ tone: "success", text: t("done", { plan: title }) });
    },
    onError: (cause) => setError(failure(cause, { 409: t("conflict") })),
    onSettled: () => queryClient.invalidateQueries({ queryKey: planKeys.all(project) }),
  });
  return (
    <>
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="shrink-0"
        onClick={() => {
          setError(null);
          setConfirming(true);
        }}
        data-testid="plans-complete"
      >
        <CheckCheck aria-hidden="true" />
        {t("button")}
      </Button>
      <ConfirmAction
        open={confirming}
        onOpenChange={setConfirming}
        title={t("title", { plan: title })}
        description={<p>{open > 0 ? t("descriptionOpen", { count: open }) : t("description")}</p>}
        confirmLabel={t("confirm")}
        pendingLabel={t("pending")}
        cancelLabel={t("cancel")}
        pending={complete.isPending}
        error={error}
        onConfirm={() => complete.mutate()}
        testId="plans-complete-confirm"
      />
    </>
  );
}
