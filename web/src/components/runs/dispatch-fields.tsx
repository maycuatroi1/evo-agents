"use client";

import { CircleCheck, Info, Server } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { InfoTip } from "@/components/feedback/info-tip";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { KNOWN_RUNTIMES } from "@/components/workers/model";
import { WORKERS_HREF } from "@/components/workers/queries";
import { cn } from "@/lib/utils";

import { FieldLabel } from "./field-label";
import type { DispatchOutlook, FitProblem, WorkerFit } from "./model";

/**
 * The parts the dispatch dialogs share (Dispatch, Run plan, and New plan and Revise with agent): fieldsets of bordered
 * choices whose details show from an info icon beside the legend, the worker dropdown and the outlook of a worker.
 * `subject` says what is dispatched: runs of steps, one plan run, or one author run.
 */
export type DispatchSubject = "runs" | "plan" | "author";

/** A fieldset: its legend, with an info icon for `info`, a line below it for `hint`, then its choices. */
export function Section({
  legend,
  hint,
  info,
  children,
  testId,
  className,
}: {
  legend: string;
  hint?: ReactNode;
  info?: ReactNode;
  children: ReactNode;
  /** Of the fieldset; its info icon takes it with `-info`. */
  testId?: string;
  className?: string;
}) {
  const id = useId();
  return (
    <fieldset className={cn("flex min-w-0 flex-col gap-2", className)} aria-describedby={hint ? id : undefined} data-testid={testId}>
      <legend className="mb-1 flex items-center gap-1.5 text-sm font-medium">
        {legend}
        {info ? (
          <InfoTip label={legend} testId={testId ? `${testId}-info` : undefined}>
            {info}
          </InfoTip>
        ) : null}
      </legend>
      {hint ? (
        <p id={id} className="-mt-0.5 mb-0.5 text-xs text-pretty text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {children}
    </fieldset>
  );
}

/**
 * A radio as a compact tile: an icon, the name, and a short note below it for what is wrong with the choice (a runtime
 * none of the workers has). The tiles of a fieldset sit side by side as a segmented control: the radio itself is out of
 * sight but covers its tile, so a click anywhere on the tile lands on it, and the tile picked has a thicker brand
 * border; the fieldset's info icon says what each one does.
 */
export function Tile({
  name,
  value,
  checked,
  onChange,
  icon,
  title,
  note,
  disabled = false,
  testId,
}: {
  name: string;
  value: string;
  checked: boolean;
  onChange: () => void;
  icon?: ReactNode;
  title: ReactNode;
  note?: ReactNode;
  disabled?: boolean;
  testId?: string;
}) {
  const id = useId();
  return (
    <label
      htmlFor={id}
      className={cn(
        "relative flex min-h-11 min-w-0 items-center gap-2 rounded-md border px-3 py-2 transition-colors duration-150 has-[:focus-visible]:ring-3 has-[:focus-visible]:ring-ring/50",
        disabled ? "cursor-not-allowed border-dashed bg-muted/40 text-muted-foreground" : "cursor-pointer hover:bg-muted/50",
        checked && !disabled && "border-brand bg-surface-selected shadow-[inset_0_0_0_1px_var(--color-brand)]",
      )}
      data-testid={testId}
      data-checked={checked || undefined}
    >
      <input
        id={id}
        type="radio"
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={onChange}
        // Transparent over the whole tile rather than sr-only, which the tile's icon and words would cover.
        className={cn("absolute inset-0 m-0 size-full appearance-none opacity-0", disabled ? "cursor-not-allowed" : "cursor-pointer")}
        aria-describedby={note ? `${id}-note` : undefined}
      />
      {icon}
      <span className="flex min-w-0 flex-col">
        <span className="text-sm leading-5 whitespace-nowrap">{title}</span>
        {note ? (
          // Out of the radio's name, which the label around it gives; the radio reads it as its description.
          <span id={`${id}-note`} className="text-xs leading-4 text-attention" aria-hidden="true">
            {note}
          </span>
        ) : null}
      </span>
    </label>
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

/** The worker dropdown's choice of the hub's: the first worker that fits and has a free slot. */
export const ANY_WORKER = "any";
/** What the visitor picked in the worker dropdown: a worker's id, or any worker that fits. */
export type WorkerChoice = number | typeof ANY_WORKER;

/**
 * The worker the runs go to, as a dropdown filled with the worker found for them (`chosenWorker` of model.ts): each of
 * the visitor's workers for the project with whether it can take them now, then any worker that fits, which leaves the
 * choice to the hub. A worker that cannot take them as it is stays listed with the reason but cannot be picked.
 * `registered` is how many of the visitor's workers serve the project before the agent narrowed them: with some but
 * none left, the agent's problem says why, not the note about registering one. `note` goes below the dropdown, for
 * what is wrong with the choice.
 */
export function WorkerField({
  project,
  fits,
  value,
  onChange,
  loading,
  registered,
  subject,
  note,
  disabled = false,
}: {
  project: string;
  fits: WorkerFit[];
  value: number | null;
  onChange: (value: WorkerChoice) => void;
  loading: boolean;
  registered: number;
  subject: DispatchSubject;
  note?: ReactNode;
  disabled?: boolean;
}) {
  const t = useTranslations("runs.dispatch");
  const problem = useProblem();
  const ids = useId();
  const option = (fit: WorkerFit): string => {
    const name = fit.worker.shared ? t("workerShared", { name: fit.worker.name, owner: fit.worker.owner }) : fit.worker.name;
    if (fit.fit === "now") return t("workerFree", { name, count: fit.free });
    if (fit.fit === "busy") return t("workerBusy", { name });
    if (fit.fit === "offline") return t("workerOffline", { name });
    return t("workerNo", { name, problem: problem(fit.problem ?? { kind: "draining" }) });
  };
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="dispatch-worker">
      <FieldLabel htmlFor={`${ids}-worker`} label={t("worker")} info={t("workerInfo", { subject })} testId="dispatch-worker-info" />
      {loading ? (
        <Skeleton className="h-9 w-full" role="status" aria-label={t("workersLoading")} />
      ) : registered === 0 ? (
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-dashed px-3 py-2 text-sm text-muted-foreground" data-testid="dispatch-no-workers">
          <Server className="size-4 shrink-0" aria-hidden="true" />
          <span>{t("noWorkers", { project })}</span>
          <Link href={WORKERS_HREF} className="font-medium text-brand underline-offset-4 hover:underline">
            {t("registerWorker")}
          </Link>
        </p>
      ) : (
        <NativeSelect
          id={`${ids}-worker`}
          value={value === null ? ANY_WORKER : String(value)}
          onChange={(event) => onChange(event.target.value === ANY_WORKER ? ANY_WORKER : Number(event.target.value))}
          disabled={disabled}
          aria-describedby={note ? `${ids}-note` : undefined}
          className="w-full md:[&_select]:h-9"
          data-testid="dispatch-worker-select"
        >
          {fits.map((fit) => (
            <NativeSelectOption key={fit.worker.id} value={String(fit.worker.id)} disabled={fit.fit === "no"}>
              {option(fit)}
            </NativeSelectOption>
          ))}
          <NativeSelectOption value={ANY_WORKER}>{t("workerAny")}</NativeSelectOption>
        </NativeSelect>
      )}
      {note && !loading && registered > 0 ? <div id={`${ids}-note`}>{note}</div> : null}
    </div>
  );
}

export function useProblem() {
  const t = useTranslations("runs.dispatch.problem");
  const format = useFormatter();
  return (problem: FitProblem): string => {
    if (problem.kind === "draining") return t("draining");
    if (problem.kind === "sessionPerStep") return t("sessionPerStep");
    if (problem.kind === "checkout") return t("checkout", { repos: format.list(problem.repos, { type: "conjunction" }) });
    return problem.runtime === "any" ? t("runtimeAny") : t("runtime", { runtime: KNOWN_RUNTIMES[problem.runtime] ?? problem.runtime });
  };
}

const MAX_NAMED = 3;

/** What will happen to what is dispatched, from the workers that may take it: in a footer, or below the worker field. */
export function Outlook({
  outlook,
  project,
  subject = "runs",
  className,
}: {
  outlook: DispatchOutlook;
  project: string;
  subject?: DispatchSubject;
  className?: string;
}) {
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
        className,
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
