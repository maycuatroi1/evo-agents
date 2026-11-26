"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Ban, CheckCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import { planKeys } from "@/lib/plan-queries";

import { type PlanAction, type PlanFailure, type PlanTarget, writeEach, writePlan } from "./plan-actions";

const MAX_REASON = 2000;

/**
 * Abandon or Close for one plan (the peek panel's menu) or for every plan ticked in the list. Abandon asks for one
 * reason, kept on each plan; Close asks nothing more, since each plan's steps are all done or descoped. The hub is asked
 * plan by plan through its own endpoints; the dialog then says how many it took, and stays open on the plans it
 * refused, each with the hub's words, so they can be tried again. `onFinished` gets the ids still refused.
 */
export function PlanWriteDialog({
  project,
  action,
  plans,
  open,
  onOpenChange,
  onFinished,
}: {
  project: string;
  action: PlanAction;
  plans: readonly PlanTarget[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onFinished?: (failed: string[]) => void;
}) {
  const t = useTranslations(`plans.write.${action}`);
  const tWrite = useTranslations("plans.write");
  const ids = useId();
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState<PlanFailure[]>([]);
  const [missing, setMissing] = useState(false);
  const count = plans.length;
  const one = count === 1 ? plans[0] : null;

  const change = (next: boolean) => {
    if (pending) return;
    if (!next) {
      setFailed([]);
      setMissing(false);
    }
    onOpenChange(next);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending || count === 0) return;
    const why = reason.trim();
    if (action === "abandon" && !why) {
      setMissing(true);
      return;
    }
    setPending(true);
    const api = browserApi();
    const result = await writeEach(plans, (plan) => writePlan(api, project, action, plan, why));
    setPending(false);
    await queryClient.invalidateQueries({ queryKey: planKeys.all(project) });
    if (result.done.length > 0) {
      notify({ tone: "success", text: result.done.length === 1 ? t("doneOne", { plan: result.done[0].name }) : t("done", { count: result.done.length }) });
    }
    onFinished?.(result.failed.map((plan) => plan.plan_id));
    if (result.failed.length === 0) {
      setReason("");
      setFailed([]);
      onOpenChange(false);
    } else setFailed(result.failed);
  };

  const Icon = action === "abandon" ? Ban : CheckCheck;
  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogContent
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-md"
        onEscapeKeyDown={(event) => pending && event.preventDefault()}
        onInteractOutside={(event) => pending && event.preventDefault()}
        data-testid={`plans-${action}-dialog`}
      >
        <form className="flex flex-col gap-4" onSubmit={(event) => void submit(event)} noValidate>
          <DialogHeader>
            <DialogTitle className="text-balance break-words">{one ? t("titleOne", { plan: one.name }) : t("title", { count })}</DialogTitle>
            <DialogDescription className="text-pretty">{t("description", { count })}</DialogDescription>
          </DialogHeader>
          {one ? null : (
            <ul className="flex max-h-40 flex-col gap-1 overflow-y-auto rounded-md border bg-surface-sunken px-3 py-2 text-xs" data-testid="plans-write-targets">
              {plans.map((plan) => (
                <li key={plan.plan_id} className="min-w-0 truncate font-mono" title={plan.name}>
                  {plan.plan_id}
                </li>
              ))}
            </ul>
          )}
          {action === "abandon" ? (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${ids}-reason`}>{tWrite("reason")}</Label>
              <Textarea
                id={`${ids}-reason`}
                value={reason}
                maxLength={MAX_REASON}
                rows={3}
                onChange={(event) => {
                  setReason(event.target.value);
                  setMissing(false);
                }}
                aria-invalid={missing || undefined}
                aria-describedby={missing ? `${ids}-missing` : `${ids}-hint`}
                data-testid="plans-abandon-reason"
              />
              {missing ? (
                <p id={`${ids}-missing`} className="text-xs text-danger" data-testid="plans-abandon-missing">
                  {tWrite("reasonMissing")}
                </p>
              ) : (
                <p id={`${ids}-hint`} className="text-xs text-muted-foreground">
                  {tWrite("reasonHint")}
                </p>
              )}
            </div>
          ) : null}
          {failed.length > 0 ? (
            <div className="flex flex-col gap-2" role="alert" data-testid="plans-write-failed">
              <InlineError text={tWrite("failed", { count: failed.length })} />
              <ul className="flex flex-col gap-1 text-xs">
                {failed.map((plan) => (
                  <li key={plan.plan_id} className="min-w-0 [overflow-wrap:anywhere]" data-plan-id={plan.plan_id}>
                    <span className="font-mono font-medium">{plan.plan_id}</span>
                    <span className="text-muted-foreground">: {plan.message || tWrite("noReason", { status: plan.status })}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" size="lg" onClick={() => change(false)} aria-disabled={pending || undefined} className="aria-disabled:opacity-50">
              {tWrite("cancel")}
            </Button>
            <Button
              type="submit"
              variant={action === "abandon" ? "destructive" : "default"}
              size="lg"
              busy={pending}
              disabled={count === 0}
              data-testid={`plans-${action}-confirm`}
            >
              <Icon aria-hidden="true" />
              {pending ? t("pending") : failed.length > 0 ? tWrite("retry") : t("confirm", { count })}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
