"use client";

import { useQuery } from "@tanstack/react-query";
import { Play, X } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure } from "@/components/admin/notice";
import { useStepStatusText } from "@/components/plans/status";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { KNOWN_RUNTIMES } from "@/components/workers/model";
import { workersQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { planQuery } from "@/lib/plan-queries";
import { parsePlan } from "@/lib/plans";
import { projectQuery, whoamiQuery } from "@/lib/queries";

import { AccountField } from "./account-field";
import { accountField, BY_RULES } from "./account-model";
import { Choice, ModelField, Outlook, RuntimeHint, Section, type Target, WorkerPicker } from "./dispatch-fields";
import { useDispatchPlanRun } from "./hooks";
import { dispatchOutlook, dispatchWorkers, modelSuggestions, planRunScope, readModel } from "./model";
import { Blockers, blockersOf, ScopeSummary } from "./plan-run-scope";
import {
  DEFAULT_PLAN_TIMEOUT,
  MODES,
  PLAN_TIMEOUT_CHOICES,
  type PlanTimeout,
  readyStepsQuery,
  type RequestedRuntime,
  type Run,
  type RunMode,
  RUNTIMES,
} from "./queries";

/**
 * The switch between one session for every step and a new session for each step (plan-run-sessions.tsx). Its code
 * loads when the dialog opens, with the dialog's data. The plan pages load the dialog with the page, and the kit's
 * switch imported here regrouped the chunks of every page, each 0.2 kB over its First Load JS budget
 * (web/bundle-budget.json).
 */
const SessionsField = dynamic(() => import("./plan-run-sessions").then((module) => module.SessionsField), {
  ssr: false,
  loading: () => <Skeleton className="h-24 w-full" data-testid="plan-run-sessions-loading" />,
});

type Props = {
  project: string;
  planId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the plan run the hub queued, for the page to say so once the dialog closes. */
  onDispatched: (run: Run) => void;
  /** Where focus goes once the dialog has closed, for a dialog opened without a trigger (the command palette). */
  onCloseAutoFocus?: (event: Event) => void;
};

/**
 * Run plan: one run, on one of the visitor's workers, that does every step of the plan not done yet, in depends_on
 * order, in one session or, switched on, in a new session for each step (docs/workers.md, A plan run on the machine).
 * The dialog says what the run will do (its steps, repos, checkpoints, and where the agent may push and merge), takes
 * the runtime, model, mode, sessions, worker and timeout, and says in its footer which worker could take it now. The
 * content mounts each time the dialog opens.
 */
export function PlanRunDialog({ project, planId, open, onOpenChange, onDispatched, onCloseAutoFocus }: Props) {
  const write = useDispatchPlanRun(project);
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
        onCloseAutoFocus={onCloseAutoFocus}
        data-testid="plan-run-dialog"
      >
        <PlanRunForm project={project} planId={planId} write={write} onClose={() => onOpenChange(false)} onDispatched={onDispatched} />
      </DialogContent>
    </Dialog>
  );
}

type Write = ReturnType<typeof useDispatchPlanRun>;

function PlanRunForm({
  project,
  planId,
  write,
  onClose,
  onDispatched,
}: {
  project: string;
  planId: string;
  write: Write;
  onClose: () => void;
  onDispatched: (run: Run) => void;
}) {
  const t = useTranslations("runs.planRun");
  const tDispatch = useTranslations("runs.dispatch");
  const statusText = useStepStatusText();
  const ids = useId();
  const failure = useWriteFailure();
  const plan = useQuery(planQuery(browserApi, project, planId));
  const ready = useQuery({ ...readyStepsQuery(browserApi, project, planId), refetchInterval: 10_000 });
  const me = useQuery(whoamiQuery(browserApi));
  const workersState = useQuery(workersQuery(browserApi));
  const projectState = useQuery(projectQuery(browserApi, project));

  const [runtime, setRuntime] = useState<RequestedRuntime>("any");
  const [modelText, setModelText] = useState("");
  const [mode, setMode] = useState<RunMode>("headless");
  const [target, setTarget] = useState<Target>("auto");
  const [pinnedChoice, setPinnedChoice] = useState<number | null>(null);
  const [timeout, setTimeoutHours] = useState<PlanTimeout>(DEFAULT_PLAN_TIMEOUT);
  const [perStep, setPerStep] = useState(false);
  const [account, setAccount] = useState(BY_RULES);

  const view = plan.data ? parsePlan(plan.data.body, plan.data.plan_id) : null;
  const defaults = Object.fromEntries((projectState.data?.repos ?? []).map((repo) => [repo.name, repo.default_branch ?? null]));
  const scope = view && ready.data ? planRunScope(ready.data.steps, view, defaults) : null;
  const blockers = scope && ready.data ? blockersOf(scope, ready.data.steps, ready.data.plan_run ?? null) : [];
  const mine = me.data && workersState.data ? dispatchWorkers(workersState.data, me.data.login, project) : [];
  const pinned = target === "pin" ? (pinnedChoice ?? mine[0]?.id ?? null) : null;
  const model = readModel(modelText, runtime);
  const suggestions = modelSuggestions(mine, runtime);
  const runnable = scope !== null && blockers.length === 0;
  const repos = scope?.repos.map((repo) => repo.repo) ?? [];
  const outlook = dispatchOutlook(runnable ? 1 : 0, mine, { project, runtime, repos, sessionPerStep: perStep }, pinned);
  const pending = write.isPending;
  const loading = plan.isPending || ready.isPending;
  const loadFailed = !loading && (plan.isError || ready.isError) && scope === null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending || !runnable || model.problem !== null) return;
    let run: Run;
    try {
      const each = perStep ? { session_per_step: true as const } : {}; // sent only when on: an older hub ignores it
      run = await write.mutateAsync({ plan_id: planId, worker_id: pinned, runtime, model: model.model, mode, timeout_h: timeout, ...each, ...accountField(account) });
    } catch {
      return; // shown above the buttons, from the mutation's state
    }
    onDispatched(run);
    onClose();
  };

  const error = write.isError ? failure(write.error, { 403: t("errors.forbidden"), 404: t("errors.notFound"), 409: t("errors.conflict"), 422: t("errors.invalid") }) : null;
  const apiDetail = write.isError && isApiError(write.error) && [404, 409, 422].includes(write.error.info.status) ? t("errors.hubSays", { message: write.error.info.message }) : null;
  const planTitle = view?.title ?? planId;

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex min-h-0 flex-1 flex-col" aria-busy={pending || undefined}>
      <div className="flex items-start gap-3 border-b px-4 pt-4 pb-3 sm:px-5">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <DialogTitle className="flex items-center gap-2 text-lg font-semibold">
            <Play className="size-5 text-muted-foreground" aria-hidden="true" />
            {t("title")}
          </DialogTitle>
          <DialogDescription className="text-pretty">
            {t.rich("description", {
              plan: planTitle,
              sessions: perStep ? "step" : "one",
              name: (chunks) => <span className="font-medium text-foreground [overflow-wrap:anywhere]">{chunks}</span>,
            })}
          </DialogDescription>
        </div>
        <DialogClose asChild>
          <Button type="button" variant="ghost" size="icon-lg" className="-mt-1 -mr-1 shrink-0" aria-label={tDispatch("close")} disabled={pending}>
            <X aria-hidden="true" />
          </Button>
        </DialogClose>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-4 py-4 sm:px-5" data-testid="plan-run-body">
        {loading ? (
          <div className="flex flex-col gap-2" role="status" aria-label={t("loading")}>
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : loadFailed ? (
          <p className="text-sm text-danger" role="alert" data-testid="plan-run-load-failed">
            {t("loadFailed")}
          </p>
        ) : scope ? (
          <>
            <Blockers project={project} blockers={blockers} />
            <ScopeSummary scope={scope} statusText={statusText} />
          </>
        ) : null}

        <Section legend={tDispatch("runtime")} testId="dispatch-runtime">
          <div className="grid gap-2 sm:grid-cols-2">
            {(["any", ...RUNTIMES] as const).map((value) => (
              <Choice
                key={value}
                type="radio"
                name="runtime"
                value={value}
                checked={runtime === value}
                onChange={() => setRuntime(value)}
                title={value === "any" ? tDispatch("runtimeAny") : KNOWN_RUNTIMES[value]}
                hint={<RuntimeHint runtime={value} workers={mine} loaded={workersState.isSuccess} />}
              />
            ))}
          </div>
        </Section>

        <ModelField value={modelText} onChange={setModelText} runtime={runtime} suggestions={suggestions} problem={model.problem} disabled={pending} />

        <Section legend={tDispatch("mode")} testId="dispatch-mode">
          <div className="grid gap-2 sm:grid-cols-2">
            {MODES.map((value) => (
              <Choice
                key={value}
                type="radio"
                name="mode"
                value={value}
                checked={mode === value}
                onChange={() => setMode(value)}
                title={tDispatch(`modes.${value}`)}
                hint={tDispatch(`modes.${value}Hint`)}
              />
            ))}
          </div>
        </Section>

        <SessionsField
          on={perStep}
          onChange={setPerStep}
          disabled={pending}
          workers={mine}
          loaded={workersState.isSuccess}
        />

        <WorkerPicker
          project={project}
          workers={mine}
          loading={workersState.isPending || me.isPending}
          target={target}
          pinned={pinned}
          onTarget={setTarget}
          onPinned={setPinnedChoice}
          subject="plan"
        />

        <AccountField project={project} value={account} onChange={setAccount} disabled={pending} />

        <div className="flex flex-col gap-1.5">
          <label htmlFor={`${ids}-timeout`} className="text-sm font-medium">
            {t("timeout")}
          </label>
          <NativeSelect
            id={`${ids}-timeout`}
            value={String(timeout)}
            onChange={(event) => setTimeoutHours(Number(event.target.value) as PlanTimeout)}
            className="w-full sm:w-56 md:[&_select]:h-9"
            aria-describedby={`${ids}-timeout-hint`}
            data-testid="plan-run-timeout"
          >
            {PLAN_TIMEOUT_CHOICES.map((hours) => (
              <NativeSelectOption key={hours} value={String(hours)}>
                {t("timeoutValue", { hours })}
              </NativeSelectOption>
            ))}
          </NativeSelect>
          <p id={`${ids}-timeout-hint`} className="text-xs text-pretty text-muted-foreground">
            {t("timeoutHint")}
          </p>
        </div>

        {error ? <InlineError text={error.text} detail={apiDetail ?? error.detail} requestId={error.requestId} /> : null}
      </div>

      <div className="flex flex-col gap-3 border-t bg-muted/50 px-4 py-3 sm:flex-row sm:items-center sm:px-5">
        <Outlook outlook={outlook} project={project} subject="plan" />
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
              {tDispatch("cancel")}
            </Button>
          </DialogClose>
          <Button type="submit" size="lg" disabled={!runnable || model.problem !== null} busy={pending} data-testid="plan-run-submit">
            <Play aria-hidden="true" />
            {pending ? t("submitting") : t("submit")}
          </Button>
        </div>
      </div>
    </form>
  );
}
