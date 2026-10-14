"use client";

import { CircleCheck, CircleDashed, Info } from "lucide-react";
import { useTranslations } from "next-intl";

import { type StepStatus, useStatusText } from "@/components/status/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

import { Choice, Section } from "./dispatch-fields";
import { type PlanRunHold, planRunReason, splitSteps } from "./dispatch-steps";
import { PlanRunHoldNote } from "./plan-run-hold";
import type { StepReadiness } from "./queries";

/**
 * The Dispatch dialog's steps: the pending steps of the plan in plan order, the ready ones to pick, the others with
 * the reason they cannot be picked yet, and the done, in progress and blocked ones folded away.
 */
export function StepPicker({
  project,
  state,
  planRun,
  steps,
  selected,
  onToggle,
  onPickAll,
}: {
  project: string;
  state: { isPending: boolean; isError: boolean };
  planRun: PlanRunHold | null;
  steps: StepReadiness[];
  selected: string[];
  onToggle: (key: string, on: boolean) => void;
  onPickAll: (keys: string[]) => void;
}) {
  const t = useTranslations("runs.dispatch");
  const { open, settled } = splitSteps(steps);
  const readyKeys = steps.filter((step) => step.ready).map((step) => step.key);
  const allPicked = readyKeys.length > 0 && readyKeys.every((key) => selected.includes(key));

  return (
    <Section legend={t("steps")} hint={t("stepsHint")} testId="dispatch-steps">
      {state.isPending && steps.length === 0 ? (
        <div className="flex flex-col gap-2" aria-hidden="true">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </div>
      ) : state.isError && steps.length === 0 ? (
        <p className="text-sm text-danger" role="alert">
          {t("stepsFailed")}
        </p>
      ) : (
        <>
          {planRun ? <PlanRunHoldNote project={project} hold={planRun} testId="dispatch-plan-run" /> : null}
          {readyKeys.length > 1 ? (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs text-muted-foreground" aria-live="polite" data-testid="dispatch-picked">
                {t("picked", { count: selected.length, ready: readyKeys.length })}
              </p>
              <Button type="button" variant="outline" size="sm" onClick={() => onPickAll(allPicked ? [] : readyKeys)}>
                {allPicked ? t("pickNone") : t("pickAll", { count: readyKeys.length })}
              </Button>
            </div>
          ) : null}
          {open.length === 0 ? (
            <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="dispatch-no-open">
              {t("noOpen")}
            </p>
          ) : (
            <ul className="flex flex-col gap-2" data-testid="dispatch-open-steps">
              {open.map((step) => (
                <li key={step.key}>
                  <StepChoice step={step} planRun={planRun} checked={selected.includes(step.key)} onToggle={onToggle} />
                </li>
              ))}
            </ul>
          )}
          {open.length > 0 && readyKeys.length === 0 ? (
            <p className="flex items-start gap-2 text-sm text-muted-foreground" data-testid="dispatch-none-ready">
              <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              {t("noReady")}
            </p>
          ) : null}
          {settled.length > 0 ? (
            <details className="group rounded-md border" data-testid="dispatch-settled">
              <summary className="flex min-h-10 cursor-pointer max-md:min-h-11 items-center gap-2 rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-muted/50">
                {t("settled", { count: settled.length })}
              </summary>
              <ul className="flex flex-col gap-2 border-t p-2">
                {settled.map((step) => (
                  <li key={step.key}>
                    <StepChoice step={step} planRun={planRun} checked={false} onToggle={onToggle} />
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </>
      )}
    </Section>
  );
}

/**
 * Why a step cannot be dispatched now, in the visitor's language where the hub's answer says enough. `planRun` is the
 * plan's active plan run, when ready-steps names one: a step it holds says so.
 */
export function useNotReadyReason() {
  const t = useTranslations("runs.dispatch.reason");
  const tStatus = useStatusText("step");
  const tState = useStatusText("run");
  return (step: StepReadiness, planRun: PlanRunHold | null = null): string => {
    if (step.ready) return "";
    if (step.active_run) {
      return t("activeRun", { id: step.active_run.id, state: tState(step.active_run.state), login: step.active_run.dispatched_by });
    }
    if (step.status && step.status !== "pending") {
      const known = ["in_progress", "blocked", "done"].includes(step.status);
      return t("status", { status: known ? tStatus(step.status as StepStatus) : step.status });
    }
    if (planRun && step.reason === planRunReason(planRun)) {
      return t("planRun", { id: planRun.run.id, state: tState(planRun.run.state), login: planRun.run.dispatched_by });
    }
    const reason = step.reason ?? "";
    return reason ? reason.charAt(0).toUpperCase() + reason.slice(1) : t("unknown");
  };
}

function StepChoice({
  step,
  planRun,
  checked,
  onToggle,
}: {
  step: StepReadiness;
  planRun: PlanRunHold | null;
  checked: boolean;
  onToggle: (key: string, on: boolean) => void;
}) {
  const t = useTranslations("runs.dispatch");
  const reason = useNotReadyReason();
  return (
    <Choice
      type="checkbox"
      name="steps"
      value={step.key}
      checked={checked}
      disabled={!step.ready}
      onChange={(on) => onToggle(step.key, on)}
      testId={`dispatch-step-${step.key}`}
      title={
        <>
          <span className="mr-1.5 font-mono text-xs text-muted-foreground tabular-nums">{step.key}</span>
          {step.title ?? t("untitled")}
        </>
      }
      hint={
        step.ready ? (
          step.repo ? (
            t("repo", { repo: step.repo })
          ) : null
        ) : (
          <span data-testid="dispatch-step-reason">{reason(step, planRun)}</span>
        )
      }
      badge={
        step.ready ? (
          <Badge variant="success" className="shrink-0">
            <CircleCheck aria-hidden="true" />
            {t("ready")}
          </Badge>
        ) : (
          <Badge variant="outline" className="shrink-0 text-muted-foreground">
            <CircleDashed aria-hidden="true" />
            {t("notReady")}
          </Badge>
        )
      }
    />
  );
}
