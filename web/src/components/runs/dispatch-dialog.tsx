"use client";

import { useQuery } from "@tanstack/react-query";
import { Send, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { KNOWN_RUNTIMES } from "@/components/workers/model";
import { workersQuery } from "@/components/workers/queries";
import { agentProblems, agentWorkers, profileField } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { plansQuery } from "@/lib/plan-queries";
import { whoamiQuery } from "@/lib/queries";

import { AccountField } from "./account-field";
import { accountField, BY_RULES } from "./account-model";
import { AgentBlocked, AgentField, useAgentChoice } from "./agent-field";
import { Choice, Outlook, RuntimeHint, Section, type Target, WorkerPicker } from "./dispatch-fields";
import { StepPicker } from "./dispatch-step-picker";
import { readySelection, selectedRepos } from "./dispatch-steps";
import { useDispatch } from "./hooks";
import { dispatchOutlook, dispatchWorkers } from "./model";
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
  TIMEOUT_CHOICES,
} from "./queries";

type Props = {
  project: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the runs the hub queued, for the page to say so once the dialog closes. */
  onDispatched: (runs: Run[]) => void;
  /** Where focus goes once the dialog has closed, for a dialog opened without a trigger (the command palette). */
  onCloseAutoFocus?: (event: Event) => void;
  /** The plan, and the step of it, to start from: the step page opens the dialog on its own step. */
  plan?: string;
  step?: string;
};

/**
 * Dispatching runs: the ready steps of an active plan, each one queued as a run that a worker of the visitor claims.
 * Steps that are not ready are listed with the reason and cannot be picked. The footer says which of the visitor's
 * workers could take the runs now. The content mounts each time the dialog opens, so it starts from the defaults.
 */
export function DispatchDialog({ project, open, onOpenChange, onDispatched, plan, step, onCloseAutoFocus }: Props) {
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
        onCloseAutoFocus={onCloseAutoFocus}
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
  const [account, setAccount] = useState(BY_RULES);
  const agents = useAgentChoice(project);

  // The workers of the visitor's for the project, then those the agent lets claim the runs.
  const own = me.data && workersState.data ? dispatchWorkers(workersState.data, me.data.login, project) : [];
  const mine = agentWorkers(own, agents.agent);
  const problems = agentProblems(agents.agent, { runtime, workers: own });
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
    if (pending || plan === null || selected.length === 0 || tooMany || problems.length > 0) return;
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
        ...accountField(account),
        ...profileField(agents.agent),
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
  const apiDetail = write.isError && isApiError(write.error) && [404, 409, 422].includes(write.error.info.status) ? t("errors.hubSays", { message: write.error.info.message }) : null;

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
                className="w-full md:[&_select]:h-9 [&_select]:font-mono"
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
              className="w-full md:[&_select]:h-9"
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

        <AgentField choice={agents} problems={problems} disabled={pending} />

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
          registered={own.length}
          loading={workersState.isPending || me.isPending}
          target={target}
          pinned={pinned}
          onTarget={setTarget}
          onPinned={setPinnedChoice}
        />

        <AccountField project={project} value={account} onChange={setAccount} disabled={pending} />

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
        {error ? <InlineError text={error.text} detail={apiDetail ?? error.detail} requestId={error.requestId} /> : null}
      </div>

      <div className="flex flex-col gap-3 border-t bg-muted/50 px-4 py-3 sm:flex-row sm:items-center sm:px-5">
        {agents.agent && problems.length ? <AgentBlocked agent={agents.agent} /> : <Outlook outlook={outlook} project={project} />}
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
            disabled={selected.length === 0 || tooMany || plan === null || problems.length > 0}
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
