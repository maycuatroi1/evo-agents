"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleDashed, Info, Send, X } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure } from "@/components/admin/notice";
import { type StepStatus, useStatusText } from "@/components/status/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { KNOWN_RUNTIMES } from "@/components/workers/model";
import { workersQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { plansQuery } from "@/lib/plan-queries";
import { whoamiQuery } from "@/lib/queries";

import { Choice, Outlook, RuntimeHint, Section, type Target, WorkerPicker } from "./dispatch-fields";
import { useDispatch } from "./hooks";
import { dispatchOutlook, dispatchWorkers, readySelection, selectedRepos, splitSteps } from "./model";
import {
  type ActiveRun,
  type Approval,
  APPROVALS,
  DEFAULT_TIMEOUT,
  MAX_DISPATCH_STEPS,
  MODES,
  readyStepsQuery,
  type RequestedRuntime,
  type Run,
  runHref,
  type RunMode,
  RUNTIMES,
  type StepReadiness,
  TIMEOUT_CHOICES,
} from "./queries";

type Props = {
  project: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the runs the hub queued, for the page to say so once the dialog closes. */
  onDispatched: (runs: Run[]) => void;
  /** The plan, and the step of it, to start from: the step page opens the dialog on its own step. */
  plan?: string;
  step?: string;
};

/**
 * Dispatching runs: the ready steps of an active plan, each one queued as a run that a worker of the visitor claims.
 * Steps that are not ready are listed with the reason and cannot be picked. The footer says which of the visitor's
 * workers could take the runs now. The content mounts each time the dialog opens, so it starts from the defaults.
 */
export function DispatchDialog({ project, open, onOpenChange, onDispatched, plan, step }: Props) {
  const write = useDispatch(project);
  const pending = write.isPending;
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl"
        onEscapeKeyDown={guard}
        onInteractOutside={guard}
        data-testid="dispatch-dialog"
      >
        <DispatchForm
          project={project}
          write={write}
          presetPlan={plan ?? null}
          presetStep={step ?? null}
          onClose={() => onOpenChange(false)}
          onDispatched={onDispatched}
        />
      </DialogContent>
    </Dialog>
  );
}

type Write = ReturnType<typeof useDispatch>;

function DispatchForm({
  project,
  write,
  presetPlan,
  presetStep,
  onClose,
  onDispatched,
}: {
  project: string;
  write: Write;
  presetPlan: string | null;
  presetStep: string | null;
  onClose: () => void;
  onDispatched: (runs: Run[]) => void;
}) {
  const t = useTranslations("runs.dispatch");
  const ids = useId();
  const failure = useWriteFailure();
  const plans = useQuery(plansQuery(browserApi, project));
  const me = useQuery(whoamiQuery(browserApi));
  const workersState = useQuery(workersQuery(browserApi));

  // The plans a run can come from: the active ones, and the plan the dialog was opened on.
  const choices = (plans.data ?? [])
    .filter((summary) => summary.area === "active" || summary.plan_id === presetPlan)
    .map((summary) => ({ id: summary.plan_id, title: summary.title }));
  const [planChoice, setPlanChoice] = useState<string | null>(presetPlan);
  const plan = planChoice ?? choices[0]?.id ?? null;

  const ready = useQuery({ ...readyStepsQuery(browserApi, project, plan ?? ""), enabled: plan !== null, refetchInterval: 10_000 });
  const steps = ready.data?.steps ?? [];

  // The step the dialog was opened on is picked once it shows as ready, until the person changes the selection.
  const [picked, setPicked] = useState<string[] | null>(null);
  const initial = presetStep !== null && plan === presetPlan ? [presetStep] : [];
  const selected = readySelection(picked ?? initial, steps);

  const [runtime, setRuntime] = useState<RequestedRuntime>("any");
  const [mode, setMode] = useState<RunMode>("headless");
  const [target, setTarget] = useState<Target>("auto");
  const [pinnedChoice, setPinnedChoice] = useState<number | null>(null);
  const [approval, setApproval] = useState<Approval>("review");
  const [timeout, setTimeoutMinutes] = useState<number>(DEFAULT_TIMEOUT);

  const mine = me.data && workersState.data ? dispatchWorkers(workersState.data, me.data.login, project) : [];
  const pinned = target === "pin" ? (pinnedChoice ?? mine[0]?.id ?? null) : null;
  const repos = selectedRepos(selected, steps);
  const outlook = dispatchOutlook(selected.length, mine, { project, runtime, repos }, pinned);
  const tooMany = selected.length > MAX_DISPATCH_STEPS;
  const pending = write.isPending;

  const changePlan = (next: string) => {
    write.reset();
    setPlanChoice(next);
    setPicked([]);
  };
  const toggle = (key: string, on: boolean) => {
    const current = picked ?? initial;
    setPicked(on ? [...current.filter((other) => other !== key), key] : current.filter((other) => other !== key));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending || plan === null || selected.length === 0 || tooMany) return;
    let runs: Run[];
    try {
      runs = await write.mutateAsync({
        plan_id: plan,
        steps: selected,
        runtime,
        mode,
        approval,
        timeout_min: timeout,
        worker_id: pinned,
      });
    } catch {
      return; // shown above the buttons, from the mutation's state
    }
    onDispatched(runs);
    onClose();
  };

  const error = write.isError
    ? failure(write.error, { 403: t("errors.forbidden"), 404: t("errors.notFound"), 409: t("errors.conflict"), 422: t("errors.invalid") })
    : null;

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex min-h-0 flex-1 flex-col" aria-busy={pending || undefined}>
      <div className="flex items-start gap-3 border-b px-4 pt-4 pb-3 sm:px-5">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <DialogTitle className="flex items-center gap-2 text-lg font-semibold">
            <Send className="size-5 text-muted-foreground" aria-hidden="true" />
            {t("title")}
          </DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </div>
        <DialogClose asChild>
          <Button type="button" variant="ghost" size="icon-lg" className="-mt-1 -mr-1 shrink-0" aria-label={t("close")} disabled={pending}>
            <X aria-hidden="true" />
          </Button>
        </DialogClose>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-4 py-4 sm:px-5" data-testid="dispatch-body">
        <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_11rem]">
          <div className="flex min-w-0 flex-col gap-1.5">
            <label htmlFor={`${ids}-plan`} className="text-sm font-medium">
              {t("plan")}
            </label>
            {plans.isPending ? (
              <Skeleton className="h-9 w-full" />
            ) : plans.isError ? (
              <p className="text-sm text-danger" role="alert">
                {t("plansFailed")}
              </p>
            ) : choices.length === 0 ? (
              <p className="rounded-md border border-dashed px-3 py-2 text-sm text-muted-foreground" data-testid="dispatch-no-plans">
                {t("noPlans")}
              </p>
            ) : (
              <NativeSelect
                id={`${ids}-plan`}
                value={plan ?? ""}
                onChange={(event) => changePlan(event.target.value)}
                className="w-full [&_select]:h-9 [&_select]:font-mono"
                data-testid="dispatch-plan"
              >
                {choices.map((choice) => (
                  <NativeSelectOption key={choice.id} value={choice.id}>
                    {choice.title ? `${choice.id}: ${choice.title}` : choice.id}
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            )}
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={`${ids}-timeout`} className="text-sm font-medium">
              {t("timeout")}
            </label>
            <NativeSelect
              id={`${ids}-timeout`}
              value={String(timeout)}
              onChange={(event) => setTimeoutMinutes(Number(event.target.value))}
              className="w-full [&_select]:h-9"
              aria-describedby={`${ids}-timeout-hint`}
              data-testid="dispatch-timeout"
            >
              {TIMEOUT_CHOICES.map((minutes) => (
                <NativeSelectOption key={minutes} value={String(minutes)}>
                  {t("timeoutValue", { minutes })}
                </NativeSelectOption>
              ))}
            </NativeSelect>
            <p id={`${ids}-timeout-hint`} className="text-xs text-muted-foreground">
              {t("timeoutHint")}
            </p>
          </div>
        </div>

        {plan !== null ? (
          <StepPicker
            project={project}
            state={ready}
            planRun={ready.data?.plan_run ? { planId: ready.data.plan_id, run: ready.data.plan_run } : null}
            steps={steps}
            selected={selected}
            onToggle={toggle}
            onPickAll={(keys) => setPicked(keys)}
          />
        ) : null}

        <Section legend={t("runtime")} testId="dispatch-runtime">
          <div className="grid gap-2 sm:grid-cols-2">
            {(["any", ...RUNTIMES] as const).map((value) => (
              <Choice
                key={value}
                type="radio"
                name="runtime"
                value={value}
                checked={runtime === value}
                onChange={() => setRuntime(value)}
                title={value === "any" ? t("runtimeAny") : KNOWN_RUNTIMES[value]}
                hint={<RuntimeHint runtime={value} workers={mine} loaded={workersState.isSuccess} />}
              />
            ))}
          </div>
        </Section>

        <Section legend={t("mode")} testId="dispatch-mode">
          <div className="grid gap-2 sm:grid-cols-2">
            {MODES.map((value) => (
              <Choice
                key={value}
                type="radio"
                name="mode"
                value={value}
                checked={mode === value}
                onChange={() => setMode(value)}
                title={t(`modes.${value}`)}
                hint={t(`modes.${value}Hint`)}
              />
            ))}
          </div>
        </Section>

        <WorkerPicker
          project={project}
          workers={mine}
          loading={workersState.isPending || me.isPending}
          target={target}
          pinned={pinned}
          onTarget={setTarget}
          onPinned={setPinnedChoice}
        />

        <Section legend={t("approval")} testId="dispatch-approval">
          {APPROVALS.map((value) => (
            <Choice
              key={value}
              type="radio"
              name="approval"
              value={value}
              checked={approval === value}
              onChange={() => setApproval(value)}
              title={t(`approvals.${value}`)}
              hint={t(`approvals.${value}Hint`)}
            />
          ))}
        </Section>

        {tooMany ? (
          <p className="text-sm font-medium text-danger" role="alert">
            {t("tooMany", { max: MAX_DISPATCH_STEPS })}
          </p>
        ) : null}
        {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      </div>

      <div className="flex flex-col gap-3 border-t bg-muted/50 px-4 py-3 sm:flex-row sm:items-center sm:px-5">
        <Outlook outlook={outlook} project={project} />
        <div className="flex shrink-0 flex-col-reverse gap-2 sm:flex-row">
          <DialogClose asChild>
            <Button
              type="button"
              variant="outline"
              size="lg"
              aria-disabled={pending || undefined}
              className="aria-disabled:opacity-50"
              onClick={(event) => pending && event.preventDefault()}
            >
              {t("cancel")}
            </Button>
          </DialogClose>
          <Button
            type="submit"
            size="lg"
            disabled={selected.length === 0 || tooMany || plan === null}
            busy={pending}
            data-testid="dispatch-submit"
          >
            <Send aria-hidden="true" />
            {pending ? t("submitting") : t("submit", { count: selected.length })}
          </Button>
        </div>
      </div>
    </form>
  );
}

/** The plan's active plan run, as ready-steps names it: while it is active, no step of the plan is dispatched. */
export type PlanRunHold = { planId: string; run: ActiveRun };

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

function StepPicker({
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
              <summary className="flex min-h-10 cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-muted/50">
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

/** The hub's reason for a step held by the plan's plan run (`runs._plan_busy`), word for word. */
function planRunReason({ planId, run }: PlanRunHold): string {
  return `plan ${planId} has plan run #${run.id}, ${run.state}, dispatched by ${run.dispatched_by}`;
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
