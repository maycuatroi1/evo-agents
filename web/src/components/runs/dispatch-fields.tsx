"use client";

import { CircleCheck, Info, Server, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { useStatusText } from "@/components/status/status-badge";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { KNOWN_RUNTIMES } from "@/components/workers/model";
import { type Worker, WORKERS_HREF } from "@/components/workers/queries";
import { cn } from "@/lib/utils";

import { type DispatchOutlook, type FitProblem, type ModelProblem, runtimeCount, type WorkerFit } from "./model";
import { MAX_MODEL_CHARS, type RequestedRuntime } from "./queries";

/**
 * The parts the Dispatch and Run plan dialogs share: fieldsets of bordered choices, the runtime's reach among the
 * visitor's workers, the worker picker, the model field and the footer's outlook. `subject` says what is dispatched:
 * runs of steps, one plan run, or one author run (New plan and Revise with agent, whose dialog picks its worker itself).
 */
export type DispatchSubject = "runs" | "plan" | "author";
export type Target = "auto" | "pin";

export function Section({ legend, hint, children, testId }: { legend: string; hint?: ReactNode; children: ReactNode; testId?: string }) {
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
export function Choice({
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
        "flex min-h-11 items-start gap-2.5 rounded-md border px-3 py-2 transition-colors",
        disabled ? "cursor-not-allowed border-dashed bg-muted/40" : "cursor-pointer hover:bg-muted/50",
        checked && !disabled && "border-brand/40 bg-surface-selected",
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

/** What a runtime choice runs, and how many of the visitor's workers for the project report it. */
export function RuntimeHint({ runtime, workers, loaded }: { runtime: RequestedRuntime; workers: Worker[]; loaded: boolean }) {
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

export function WorkerPicker({
  project,
  workers,
  loading,
  target,
  pinned,
  onTarget,
  onPinned,
  subject = "runs",
}: {
  project: string;
  workers: Worker[];
  loading: boolean;
  target: Target;
  pinned: number | null;
  onTarget: (target: Target) => void;
  onPinned: (id: number) => void;
  subject?: DispatchSubject;
}) {
  const t = useTranslations("runs.dispatch");
  const tStatus = useStatusText("worker");
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
        hint={t("workerAutoHint", { subject })}
      />
      <Choice
        type="radio"
        name="target"
        value="pin"
        checked={target === "pin"}
        disabled={none}
        onChange={() => onTarget("pin")}
        title={t("workerPin")}
        hint={t("workerPinHint", { subject })}
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
            className="w-full sm:w-80 md:[&_select]:h-9 [&_select]:font-mono"
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
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-dashed px-3 py-2 text-sm text-muted-foreground" data-testid="dispatch-no-workers">
          <Server className="size-4 shrink-0" aria-hidden="true" />
          <span>{t("noWorkers", { project })}</span>
          <Link href={WORKERS_HREF} className="font-medium text-brand underline-offset-4 hover:underline">
            {t("registerWorker")}
          </Link>
        </p>
      ) : null}
    </Section>
  );
}

/**
 * The model a run uses, as its runtime names it: free text, with the models the visitor's workers list for the runtime
 * picked offered as suggestions (a datalist, so any other name can still be typed). Empty leaves the choice to the
 * runtime. The API takes a model with any runtime; this field asks for a runtime first, since a model name means
 * something only to the runtime it was written for.
 */
export function ModelField({
  value,
  onChange,
  runtime,
  suggestions,
  problem,
  disabled = false,
}: {
  value: string;
  onChange: (value: string) => void;
  runtime: RequestedRuntime;
  suggestions: string[];
  problem: ModelProblem | null;
  disabled?: boolean;
}) {
  const t = useTranslations("runs.planRun.model");
  const tRuntime = useTranslations("runs.runtime");
  const ids = useId();
  const listId = `${ids}-models`;
  const hint =
    runtime === "any"
      ? t("hintAny")
      : suggestions.length > 0
        ? t("hintSuggested", { count: suggestions.length, runtime: tRuntime(runtime) })
        : t("hintNone", { runtime: tRuntime(runtime) });
  const message = problem === null ? null : problem === "long" ? t("long", { max: MAX_MODEL_CHARS }) : t(problem);
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="plan-run-model">
      <label htmlFor={`${ids}-input`} className="text-sm font-medium">
        {t("label")} <span className="font-normal text-muted-foreground">{t("optional")}</span>
      </label>
      <Input
        id={`${ids}-input`}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        list={suggestions.length > 0 ? listId : undefined}
        placeholder={t("placeholder")}
        autoComplete="off"
        spellCheck={false}
        disabled={disabled}
        aria-invalid={problem !== null || undefined}
        aria-describedby={`${ids}-hint${message ? ` ${ids}-error` : ""}`}
        className="h-9 font-mono placeholder:font-sans"
        data-testid="plan-run-model-input"
      />
      {suggestions.length > 0 ? (
        <datalist id={listId} data-testid="plan-run-model-suggestions">
          {suggestions.map((model) => (
            <option key={model} value={model} />
          ))}
        </datalist>
      ) : null}
      <p id={`${ids}-hint`} className="text-xs text-pretty text-muted-foreground">
        {hint}
      </p>
      {message ? (
        <p id={`${ids}-error`} className="flex items-start gap-1.5 text-xs font-medium text-danger" data-testid="plan-run-model-error">
          <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          {message}
        </p>
      ) : null}
    </div>
  );
}

export function useProblem() {
  const t = useTranslations("runs.dispatch.problem");
  const format = useFormatter();
  return (problem: FitProblem): string => {
    if (problem.kind === "draining") return t("draining");
    if (problem.kind === "checkout") return t("checkout", { repos: format.list(problem.repos, { type: "conjunction" }) });
    return problem.runtime === "any" ? t("runtimeAny") : t("runtime", { runtime: KNOWN_RUNTIMES[problem.runtime] ?? problem.runtime });
  };
}

const MAX_NAMED = 3;

/** What the dialog's footer says will happen to what is dispatched, from the workers that may take it. */
export function Outlook({ outlook, project, subject = "runs" }: { outlook: DispatchOutlook; project: string; subject?: DispatchSubject }) {
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
      text = t("nothing", { subject });
      tone = "muted";
      break;
    case "noWorker":
      text = t("noWorker", { project, subject });
      break;
    case "now":
      text = t("now", { workers: named(outlook.fits, slot) });
      tone = "ok";
      break;
    case "later":
      text = t("later", { workers: named(outlook.fits, slot), subject });
      break;
    case "none":
      text = t("none", {
        reasons: named(outlook.fits, (fit) => t("reason", { name: fit.worker.name, problem: problem(fit.problem ?? { kind: "draining" }) })),
        subject,
      });
      break;
    case "pinned": {
      const { fit } = outlook;
      const name = fit.worker.name;
      if (fit.fit === "now") {
        text = t("pinnedNow", { name, subject });
        tone = "ok";
      } else if (fit.fit === "busy") text = t("pinnedBusy", { name, subject });
      else if (fit.fit === "offline") text = t("pinnedOffline", { name, subject });
      else text = t("pinnedNo", { name, problem: problem(fit.problem ?? { kind: "draining" }), subject });
      break;
    }
  }
  return (
    <p
      className={cn(
        "flex min-w-0 flex-1 items-start gap-2 text-sm text-pretty",
        tone === "ok" ? "text-success" : tone === "warn" ? "text-attention" : "text-muted-foreground",
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
