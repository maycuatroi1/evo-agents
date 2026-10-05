"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleDashed, Info, Loader2, Send, Server, X } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useState } from "react";

import { InlineError, useWriteFailure } from "@/components/admin/notice";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { KNOWN_RUNTIMES } from "@/components/workers/model";
import { type Worker, WORKERS_HREF, workersQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { plansQuery } from "@/lib/plan-queries";
import { whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { useDispatch } from "./hooks";
import {
  type DispatchOutlook,
  dispatchOutlook,
  dispatchWorkers,
  type FitProblem,
  readySelection,
  runtimeCount,
  selectedRepos,
  splitSteps,
  type WorkerFit,
} from "./model";
import {
  type Approval,
  APPROVALS,
  DEFAULT_TIMEOUT,
  MAX_DISPATCH_STEPS,
  MODES,
  readyStepsQuery,
  type RequestedRuntime,
  type Run,
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
type Target = "auto" | "pin";

function Section({ legend, hint, children, testId }: { legend: string; hint?: ReactNode; children: ReactNode; testId?: string }) {
  const id = useId();
  return (
    <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={hint ? id : undefined} data-testid={testId}>
      <legend className="mb-1 text-sm font-medium">{legend}</legend>
      {hint ? (
        <p id={id} className="-mt-0.5 mb-0.5 text-xs text-pretty text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {children}
    </fieldset>
  );
}

/** A radio or checkbox as a bordered card: the label, a line below it, and an optional badge. */
function Choice({
  type,
  name,
  value,
  checked,
  disabled = false,
  onChange,
  title,
  hint,
  badge,
  testId,
}: {
  type: "radio" | "checkbox";
  name: string;
  value: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
  title: ReactNode;
  hint?: ReactNode;
  badge?: ReactNode;
  testId?: string;
}) {
  const id = useId();
  return (
    <label
      htmlFor={id}
      className={cn(
        "flex min-h-11 items-start gap-2.5 rounded-lg border px-3 py-2 transition-colors",
        disabled ? "cursor-not-allowed border-dashed bg-muted/40" : "cursor-pointer hover:bg-muted/50",
        checked && !disabled && "border-primary/40 bg-accent",
      )}
      data-testid={testId}
      data-disabled={disabled || undefined}
    >
      <input
        id={id}
        type={type}
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className={cn("mt-0.5 size-4 shrink-0 accent-primary", disabled ? "cursor-not-allowed" : "cursor-pointer")}
        aria-describedby={hint ? `${id}-hint` : undefined}
      />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-sm [overflow-wrap:anywhere]">{title}</span>
        {hint ? (
          <span id={`${id}-hint`} className="text-xs leading-snug text-muted-foreground [overflow-wrap:anywhere]">
            {hint}
          </span>
        ) : null}
      </span>
      {badge}
    </label>
  );
}

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
              <p className="text-sm text-destructive" role="alert">
                {t("plansFailed")}
              </p>
            ) : choices.length === 0 ? (
              <p className="rounded-lg border border-dashed px-3 py-2 text-sm text-muted-foreground" data-testid="dispatch-no-plans">
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
            state={ready}
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
          <p className="text-sm font-medium text-destructive" role="alert">
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
            aria-disabled={pending || undefined}
            data-testid="dispatch-submit"
          >
            {pending ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Send aria-hidden="true" />}
            {pending ? t("submitting") : t("submit", { count: selected.length })}
          </Button>
        </div>
      </div>
    </form>
  );
}

/** What a runtime choice runs, and how many of the visitor's workers for the project report it. */
function RuntimeHint({ runtime, workers, loaded }: { runtime: RequestedRuntime; workers: Worker[]; loaded: boolean }) {
  const t = useTranslations("runs.dispatch");
  const what = runtime === "any" ? t("runtimeAnyHint") : t(`runtimeCommand.${runtime}`);
  if (!loaded || workers.length === 0) return <>{what}</>;
  const count = runtimeCount(workers, runtime);
  const total = workers.length;
  const reach =
    count === 0
      ? t("runtimeWorkers.none")
      : count === total
        ? t("runtimeWorkers.all", { total })
        : t("runtimeWorkers.some", { count, total });
  return (
    <>
      {what}
      <span className="block" data-testid="dispatch-runtime-reach" data-count={count}>
        {reach}
      </span>
    </>
  );
}

function StepPicker({
  state,
  steps,
  selected,
  onToggle,
  onPickAll,
}: {
  state: { isPending: boolean; isError: boolean };
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
        <p className="text-sm text-destructive" role="alert">
          {t("stepsFailed")}
        </p>
      ) : (
        <>
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
            <p className="rounded-lg border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="dispatch-no-open">
              {t("noOpen")}
            </p>
          ) : (
            <ul className="flex flex-col gap-2" data-testid="dispatch-open-steps">
              {open.map((step) => (
                <li key={step.key}>
                  <StepChoice step={step} checked={selected.includes(step.key)} onToggle={onToggle} />
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
            <details className="group rounded-lg border" data-testid="dispatch-settled">
              <summary className="flex min-h-10 cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground hover:bg-muted/50">
                {t("settled", { count: settled.length })}
              </summary>
              <ul className="flex flex-col gap-2 border-t p-2">
                {settled.map((step) => (
                  <li key={step.key}>
                    <StepChoice step={step} checked={false} onToggle={onToggle} />
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

/** Why a step cannot be dispatched now, in the visitor's language where the hub's answer says enough. */
export function useNotReadyReason() {
  const t = useTranslations("runs.dispatch.reason");
  const tStatus = useTranslations("plans.status");
  const tState = useTranslations("runs.state");
  return (step: StepReadiness): string => {
    if (step.ready) return "";
    if (step.active_run) {
      return t("activeRun", { id: step.active_run.id, state: tState(step.active_run.state), login: step.active_run.dispatched_by });
    }
    if (step.status && step.status !== "pending") {
      const known = ["in_progress", "blocked", "done"].includes(step.status);
      return t("status", { status: known ? tStatus(step.status as "done") : step.status });
    }
    const reason = step.reason ?? "";
    return reason ? reason.charAt(0).toUpperCase() + reason.slice(1) : t("unknown");
  };
}

function StepChoice({ step, checked, onToggle }: { step: StepReadiness; checked: boolean; onToggle: (key: string, on: boolean) => void }) {
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
          <span data-testid="dispatch-step-reason">{reason(step)}</span>
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

function WorkerPicker({
  project,
  workers,
  loading,
  target,
  pinned,
  onTarget,
  onPinned,
}: {
  project: string;
  workers: Worker[];
  loading: boolean;
  target: Target;
  pinned: number | null;
  onTarget: (target: Target) => void;
  onPinned: (id: number) => void;
}) {
  const t = useTranslations("runs.dispatch");
  const tStatus = useTranslations("workers.status");
  const ids = useId();
  const none = !loading && workers.length === 0;
  return (
    <Section legend={t("worker")} testId="dispatch-worker">
      <Choice
        type="radio"
        name="target"
        value="auto"
        checked={target === "auto"}
        onChange={() => onTarget("auto")}
        title={t("workerAuto")}
        hint={t("workerAutoHint")}
      />
      <Choice
        type="radio"
        name="target"
        value="pin"
        checked={target === "pin"}
        disabled={none}
        onChange={() => onTarget("pin")}
        title={t("workerPin")}
        hint={t("workerPinHint")}
        testId="dispatch-target-pin"
      />
      {target === "pin" && workers.length > 0 ? (
        <div className="flex flex-col gap-1.5 pl-1">
          <label htmlFor={`${ids}-worker`} className="text-xs font-medium text-muted-foreground">
            {t("workerSelect")}
          </label>
          <NativeSelect
            id={`${ids}-worker`}
            value={pinned === null ? "" : String(pinned)}
            onChange={(event) => onPinned(Number(event.target.value))}
            className="w-full sm:w-80 [&_select]:h-9 [&_select]:font-mono"
            data-testid="dispatch-pinned-worker"
          >
            {workers.map((worker) => (
              <NativeSelectOption key={worker.id} value={String(worker.id)}>
                {t("workerOption", { name: worker.name, status: tStatus(worker.status === "online" ? (worker.held_runs > 0 ? "busy" : "idle") : worker.status) })}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
      ) : null}
      {none ? (
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-dashed px-3 py-2 text-sm text-muted-foreground" data-testid="dispatch-no-workers">
          <Server className="size-4 shrink-0" aria-hidden="true" />
          <span>{t("noWorkers", { project })}</span>
          <Link href={WORKERS_HREF} className="font-medium text-primary underline-offset-4 hover:underline">
            {t("registerWorker")}
          </Link>
        </p>
      ) : null}
    </Section>
  );
}

function useProblem() {
  const t = useTranslations("runs.dispatch.problem");
  return (problem: FitProblem): string => {
    if (problem.kind === "draining") return t("draining");
    if (problem.kind === "checkout") return t("checkout", { repo: problem.repo });
    return problem.runtime === "any" ? t("runtimeAny") : t("runtime", { runtime: KNOWN_RUNTIMES[problem.runtime] ?? problem.runtime });
  };
}

const MAX_NAMED = 3;

function Outlook({ outlook, project }: { outlook: DispatchOutlook; project: string }) {
  const t = useTranslations("runs.dispatch.outlook");
  const format = useFormatter();
  const problem = useProblem();
  const named = (fits: WorkerFit[], describe: (fit: WorkerFit) => string) => {
    const shown = fits.slice(0, MAX_NAMED).map(describe);
    if (fits.length > MAX_NAMED) shown.push(t("more", { count: fits.length - MAX_NAMED }));
    return format.list(shown, { type: "conjunction" });
  };
  const slot = (fit: WorkerFit) =>
    fit.fit === "now"
      ? t("free", { name: fit.worker.name, count: fit.free })
      : fit.fit === "busy"
        ? t("busy", { name: fit.worker.name })
        : t("offline", { name: fit.worker.name });

  let text: string;
  let tone: "muted" | "ok" | "warn" = "warn";
  switch (outlook.kind) {
    case "nothing":
      text = t("nothing");
      tone = "muted";
      break;
    case "noWorker":
      text = t("noWorker", { project });
      break;
    case "now":
      text = t("now", { workers: named(outlook.fits, slot) });
      tone = "ok";
      break;
    case "later":
      text = t("later", { workers: named(outlook.fits, slot) });
      break;
    case "none":
      text = t("none", { reasons: named(outlook.fits, (fit) => t("reason", { name: fit.worker.name, problem: problem(fit.problem ?? { kind: "draining" }) })) });
      break;
    case "pinned": {
      const { fit } = outlook;
      const name = fit.worker.name;
      if (fit.fit === "now") {
        text = t("pinnedNow", { name });
        tone = "ok";
      } else if (fit.fit === "busy") text = t("pinnedBusy", { name });
      else if (fit.fit === "offline") text = t("pinnedOffline", { name });
      else text = t("pinnedNo", { name, problem: problem(fit.problem ?? { kind: "draining" }) });
      break;
    }
  }
  return (
    <p
      className={cn(
        "flex min-w-0 flex-1 items-start gap-2 text-sm text-pretty",
        tone === "ok" ? "text-success-foreground" : tone === "warn" ? "text-warning-foreground" : "text-muted-foreground",
      )}
      role="status"
      aria-live="polite"
      data-testid="dispatch-outlook"
      data-kind={outlook.kind}
    >
      {tone === "ok" ? (
        <CircleCheck className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      ) : (
        <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      )}
      <span>{text}</span>
    </p>
  );
}
