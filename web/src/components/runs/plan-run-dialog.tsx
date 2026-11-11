"use client";

import { useQuery } from "@tanstack/react-query";
import { Info, Play, X } from "lucide-react";
import dynamic from "next/dynamic";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure } from "@/components/feedback/notice";
import { useStepStatusText } from "@/components/plans/status";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { workersQuery } from "@/components/workers/queries";
import { agentModels, agentProblems, agentWorkers, profileField } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { planQuery } from "@/lib/plan-queries";
import { parsePlan } from "@/lib/plans";
import { projectQuery, whoamiQuery } from "@/lib/queries";
import { modelsFor, runnableModelsQuery } from "@/lib/runnable-models";

import { AccountField, useAccountChoice } from "./account-field";
import { accountField } from "./account-model";
import { AgentBlocked, AgentField, useAgentChoice } from "./agent-field";
import { AsMeField } from "./as-me-field";
import { FieldLabel } from "./field-label";
import { useDispatchPlanRun } from "./hooks";
import {
  dispatchWorkers,
  modelSuggestions,
  planRunScope,
  readModel,
  runtimeCount,
  type RuntimeName,
} from "./model";
import { ModelField } from "./model-field";
import { choiceBody, fieldsOf, type PlanRunOutcome, planRunChoiceQuery, type PlanTarget } from "./plan-run-choice";
import { Blockers, blockersOf, ScopeSummary, SYSTEM_BLOCKERS } from "./plan-run-scope";
import { useDispatchPlanInSystem, useSavePlanRunChoice, useSystemOptions, WhereField } from "./plan-run-where";
import {
  PLAN_TIMEOUT_CHOICES,
  type PlanTimeout,
  readyStepsQuery,
  type RequestedRuntime,
  type Run,
  type RunMode,
  RUNTIMES,
} from "./queries";
import { ModeField, RuntimeField } from "./runtime-field";

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
  /** Called with the plan run the hub queued, or the plan handed to a system, for the page to say so once the dialog closes. */
  onDispatched: (outcome: PlanRunOutcome) => void;
  /** Where focus goes once the dialog has closed, for a dialog opened without a trigger (the command palette). */
  onCloseAutoFocus?: (event: Event) => void;
};

/**
 * Run plan: one run, on a node of the agent's own pool, that does every step of the plan not done yet, in depends_on
 * order, in one session or, switched on, in a new session for each step (docs/workers.md, A plan run on the machine).
 * The dialog says what the run will do (its steps, repos, checkpoints, and where the agent may push and merge) and takes
 * the agent, runtime, model, mode, account, timeout and sessions, and no worker. Where the project has an agent system
 * with an entry agent, "Where it runs" offers to hand the plan to that system instead (plan-run-where.tsx), and the
 * fields only a plan run takes are hidden. The dialog opens on the visitor's remembered choice (plan-run-choice.ts),
 * says which fields fell back to their default, and remembers the choice again after a Run the hub took. It fills the account with the one it
 * finds for the run, which the visitor may change; what a field means shows from its info icon, and below a
 * field only what is wrong with the choice. The content mounts each time the dialog opens.
 */
export function PlanRunDialog({ project, planId, open, onOpenChange, onDispatched, onCloseAutoFocus }: Props) {
  const write = useDispatchPlanRun(project);
  const handOver = useDispatchPlanInSystem(project);
  const pending = write.isPending || handOver.isPending;
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
        <PlanRunForm
          project={project}
          planId={planId}
          write={write}
          handOver={handOver}
          onClose={() => onOpenChange(false)}
          onDispatched={onDispatched}
        />
      </DialogContent>
    </Dialog>
  );
}

/** The fields of a remembered choice, as the line that names those fallen back to their default calls them. */
const FIELD_NAMES: readonly string[] = ["target", "system", "agent", "runtime", "model", "mode", "timeout_h", "session_per_step"];
type FieldName = "target" | "system" | "agent" | "runtime" | "model" | "mode" | "timeout_h" | "session_per_step";

type Write = ReturnType<typeof useDispatchPlanRun>;
type HandOver = ReturnType<typeof useDispatchPlanInSystem>;

function PlanRunForm({
  project,
  planId,
  write,
  handOver,
  onClose,
  onDispatched,
}: {
  project: string;
  planId: string;
  write: Write;
  handOver: HandOver;
  onClose: () => void;
  onDispatched: (outcome: PlanRunOutcome) => void;
}) {
  const t = useTranslations("runs.planRun");
  const tDispatch = useTranslations("runs.dispatch");
  const statusText = useStepStatusText();
  const format = useFormatter();
  const ids = useId();
  const failure = useWriteFailure();
  const plan = useQuery(planQuery(browserApi, project, planId));
  const ready = useQuery({ ...readyStepsQuery(browserApi, project, planId), refetchInterval: 10_000 });
  const me = useQuery(whoamiQuery(browserApi));
  const workersState = useQuery(workersQuery(browserApi));
  const projectState = useQuery(projectQuery(browserApi, project));
  const gateway = useQuery(runnableModelsQuery(browserApi, project)).data ?? [];
  const choice = useQuery(planRunChoiceQuery(browserApi, project));
  const systems = useSystemOptions(project);
  const save = useSavePlanRunChoice(project);

  const [target, setTarget] = useState<PlanTarget>("worker");
  const [system, setSystem] = useState<string | null>(null);
  const [runtime, setRuntime] = useState<RequestedRuntime>("any");
  const [modelText, setModelText] = useState("");
  const [mode, setMode] = useState<RunMode>("headless");
  const [timeout, setTimeoutHours] = useState<PlanTimeout>(fieldsOf(undefined).timeout);
  const [perStep, setPerStep] = useState(false);
  const [asMe, setAsMe] = useState(false);
  const agents = useAgentChoice(project);
  // The remembered choice fills the form once, as soon as the hub answers (an older hub's 404 keeps the defaults); the
  // fields show only then, so nothing the visitor picks is overwritten.
  const [prefilled, setPrefilled] = useState(false);
  if (!prefilled && !choice.isPending) {
    setPrefilled(true);
    const start = fieldsOf(choice.data);
    setTarget(start.target);
    setSystem(start.system);
    setRuntime(start.runtime);
    setModelText(start.model);
    setMode(start.mode);
    setTimeoutHours(start.timeout);
    setPerStep(start.perStep);
    if (start.agent) agents.choose(start.agent);
  }
  const settled = prefilled && !systems.loading;
  const defaulted = (choice.data?.defaulted ?? []).map((field) => (FIELD_NAMES.includes(field) ? t(`fields.${field as FieldName}`) : field));
  const chosen = target === "system" ? (systems.options.find((option) => option.slug === system) ?? null) : null;

  const view = plan.data ? parsePlan(plan.data.body, plan.data.plan_id) : null;
  const defaults = Object.fromEntries((projectState.data?.repos ?? []).map((repo) => [repo.name, repo.default_branch ?? null]));
  const scope = view && ready.data ? planRunScope(ready.data.steps, view, defaults) : null;
  const allBlockers = scope && ready.data ? blockersOf(scope, ready.data.steps, ready.data.plan_run ?? null, planId, ready.data.trace ?? null) : [];
  const blockers = chosen ? allBlockers.filter((blocker) => SYSTEM_BLOCKERS.includes(blocker.kind)) : allBlockers;
  const own = me.data && workersState.data ? dispatchWorkers(workersState.data, me.data.login, project) : [];
  const mine = agentWorkers(own, agents.agent);
  const [account, setAccount] = useAccountChoice(project, runtime, null);
  const models = Object.fromEntries(RUNTIMES.map((name) => [name, agentModels(agents.agent, modelSuggestions(mine, name))])) as Record<RuntimeName, string[]>;
  const reach = workersState.isSuccess && mine.length > 0 ? Object.fromEntries((["any", ...RUNTIMES] as const).map((name) => [name, runtimeCount(mine, name)])) : null;
  const model = readModel(modelText, runtime, gateway.map((item) => item.model));
  const problems = agentProblems(agents.agent, { runtime, model: model.model, workers: own });
  const runnable = settled && scope !== null && blockers.length === 0 && (chosen !== null || (problems.length === 0 && model.problem === null));
  const pending = write.isPending || handOver.isPending;
  const loading = plan.isPending || ready.isPending;
  const loadFailed = !loading && (plan.isError || ready.isError) && scope === null;

  // A model goes with its runtime: one the next runtime does not list is cleared, as is any model for any runtime,
  // but a provider's model the next runtime runs through the gateway.
  const changeRuntime = (next: RequestedRuntime) => {
    setRuntime(next);
    const kept = modelsFor(gateway, next).some((item) => item.model === modelText.trim());
    if (!kept && (next === "any" || !models[next].includes(modelText.trim()))) setModelText("");
  };

  const remember = () =>
    void save(choiceBody({ target: chosen ? "system" : "worker", system, agent: agents.agent?.handle ?? null, runtime, model: modelText, mode, timeout, perStep }, model.problem === null ? model.model : null));

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending || !runnable) return;
    if (chosen) {
      try {
        const handed = await handOver.mutateAsync({ planId, system: chosen.slug });
        remember();
        onDispatched({ ...handed, project });
        onClose();
      } catch {
        // shown above the buttons, from the mutation's state
      }
      return;
    }
    let run: Run;
    try {
      const each = perStep ? { session_per_step: true as const } : {}; // sent only when on: an older hub ignores it
      run = await write.mutateAsync({
        plan_id: planId,
        runtime,
        model: model.model,
        mode,
        timeout_h: timeout,
        ...each,
        ...accountField(account),
        ...profileField(agents.agent),
        as_me: asMe,
      });
    } catch {
      return; // shown above the buttons, from the mutation's state
    }
    remember();
    onDispatched(run);
    onClose();
  };

  const failed = chosen ? handOver : write;
  const error = failed.isError ? failure(failed.error, { 403: t("errors.forbidden"), 404: t("errors.notFound"), 409: t(chosen ? "errors.systemConflict" : "errors.conflict"), 422: t("errors.invalid") }) : null;
  const apiDetail = failed.isError && isApiError(failed.error) && [404, 409, 422].includes(failed.error.info.status) ? t("errors.hubSays", { message: failed.error.info.message }) : null;
  const where = (next: PlanTarget, slug: string | null) => {
    setTarget(next);
    if (slug) setSystem(slug);
  };

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex min-h-0 flex-1 flex-col" aria-busy={pending || undefined}>
      <div className="flex items-start gap-3 border-b px-4 pt-4 pb-3 sm:px-5">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <DialogTitle className="flex items-center gap-2 text-lg font-semibold">
            <Play className="size-5 text-muted-foreground" aria-hidden="true" />
            {t("title")}
          </DialogTitle>
          <DialogDescription className="truncate font-mono text-xs" title={view?.title ?? planId} data-testid="plan-run-plan">
            {view?.title ?? planId}
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

        {!settled ? (
          <div className="flex flex-col gap-2" role="status" aria-label={t("loadingChoice")} data-testid="plan-run-choice-loading">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-28 w-full" />
          </div>
        ) : (
          <>
            {defaulted.length > 0 ? (
              <p className="flex items-start gap-2 text-xs text-pretty text-muted-foreground" role="note" data-testid="plan-run-defaulted">
                <Info className="mt-px size-3.5 shrink-0" aria-hidden="true" />
                <span>{t("defaulted", { fields: format.list(defaulted, { type: "conjunction" }) })}</span>
              </p>
            ) : null}
            <WhereField
              options={systems.options}
              target={chosen ? "system" : "worker"}
              system={chosen?.slug ?? null}
              pending={ready.data ? ready.data.steps.filter((step) => step.status === "pending").length : null}
              onChange={where}
              disabled={pending}
            />
            {chosen ? null : (
              <>
                <AgentField choice={agents} problems={problems} disabled={pending} />
                <RuntimeField value={runtime} onChange={changeRuntime} reach={reach} />
                <div className="grid gap-x-4 gap-y-5 sm:grid-cols-2">
                  <ModelField
                    value={modelText}
                    onChange={setModelText}
                    runtime={runtime}
                    models={models}
                    gateway={gateway}
                    onRuntime={changeRuntime}
                    problem={model.problem}
                    disabled={pending}
                  />
                  <ModeField value={mode} onChange={setMode} />
                  <AccountField project={project} value={account} onChange={setAccount} disabled={pending} />
                  <div className="flex min-w-0 flex-col gap-1.5">
                    <FieldLabel htmlFor={`${ids}-timeout`} label={t("timeout")} info={t("timeoutHint")} testId="plan-run-timeout-info" />
                    <NativeSelect
                      id={`${ids}-timeout`}
                      value={String(timeout)}
                      onChange={(event) => setTimeoutHours(Number(event.target.value) as PlanTimeout)}
                      disabled={pending}
                      className="w-full md:[&_select]:h-9"
                      data-testid="plan-run-timeout"
                    >
                      {PLAN_TIMEOUT_CHOICES.map((hours) => (
                        <NativeSelectOption key={hours} value={String(hours)}>
                          {t("timeoutValue", { hours })}
                        </NativeSelectOption>
                      ))}
                    </NativeSelect>
                  </div>
                  <SessionsField on={perStep} onChange={setPerStep} disabled={pending} workers={mine} loaded={workersState.isSuccess} />
                </div>

                <AsMeField checked={asMe} onChange={setAsMe} disabled={pending} />
              </>
            )}
          </>
        )}

        {error ? <InlineError text={error.text} detail={apiDetail ?? error.detail} requestId={error.requestId} /> : null}
      </div>

      <div className="flex flex-col gap-3 border-t bg-muted/50 px-4 py-3 sm:flex-row sm:items-center sm:justify-end sm:px-5">
        {!chosen && agents.agent && problems.length ? <AgentBlocked agent={agents.agent} /> : null}
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
          <Button type="submit" size="lg" disabled={!runnable} busy={pending} data-testid="plan-run-submit">
            <Play aria-hidden="true" />
            {pending ? t("submitting") : t("submit")}
          </Button>
        </div>
      </div>
    </form>
  );
}
