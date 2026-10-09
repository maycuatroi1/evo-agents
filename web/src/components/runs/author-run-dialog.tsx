"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleAlert, FilePen, FilePlus, Server, X } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure } from "@/components/admin/notice";
import { useStatusText } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { KNOWN_RUNTIMES } from "@/components/workers/model";
import { type Worker, WORKERS_HREF, workersQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { planQuery } from "@/lib/plan-queries";
import { projectQuery, whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { ModelField, Outlook } from "./dispatch-fields";
import { useDispatchAuthorRun } from "./hooks";
import { authorWorker, dispatchOutlook, dispatchWorkers, harnessRepo, modelSuggestions, readModel, readRequest, workerFitsNow } from "./model";
import {
  AUTHOR_RUNTIME,
  AUTHOR_TIMEOUT_CHOICES,
  type AuthorTimeout,
  DEFAULT_AUTHOR_TIMEOUT,
  MAX_REQUEST_BYTES,
  type Run,
} from "./queries";
import { utf8Bytes } from "./run-model";

type Props = {
  project: string;
  /** The plan the run revises (Revise with agent); null for a new plan (New plan). */
  planId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the author run the hub queued, for the page to say so once the dialog closes. */
  onDispatched: (run: Run) => void;
  /** Where focus goes once the dialog has closed, for a dialog opened without a trigger (the command palette). */
  onCloseAutoFocus?: (event: Event) => void;
};

/** The byte count shows once a request passes this share of the limit. */
const COUNT_FROM = 0.75;

/**
 * New plan and Revise with agent: an author run (docs/workers.md, Author runs), an agent on one of the visitor's own
 * workers that writes a plan of the project from their request, or a new revision of `planId`, asking them in the
 * run's chat when it needs to. The dialog takes the request (at most 16 KiB of UTF-8), the worker (an author run is
 * always pinned), the model and the timeout, as Run plan's does; the runtime is Claude Code, the one an author run
 * takes. Its footer says whether the worker picked can take the run now. The content mounts each time it opens.
 */
export function AuthorRunDialog({ project, planId, open, onOpenChange, onDispatched, onCloseAutoFocus }: Props) {
  const write = useDispatchAuthorRun(project);
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
        data-testid="author-run-dialog"
        data-mode={planId ? "revise" : "new"}
      >
        <AuthorRunForm project={project} planId={planId} write={write} onClose={() => onOpenChange(false)} onDispatched={onDispatched} />
      </DialogContent>
    </Dialog>
  );
}

type Write = ReturnType<typeof useDispatchAuthorRun>;

function AuthorRunForm({
  project,
  planId,
  write,
  onClose,
  onDispatched,
}: {
  project: string;
  planId: string | null;
  write: Write;
  onClose: () => void;
  onDispatched: (run: Run) => void;
}) {
  const t = useTranslations("runs.author.dialog");
  const tDispatch = useTranslations("runs.dispatch");
  const tPlanRun = useTranslations("runs.planRun");
  const tStatus = useStatusText("worker");
  const format = useFormatter();
  const ids = useId();
  const failure = useWriteFailure();
  const plan = useQuery({ ...planQuery(browserApi, project, planId ?? ""), enabled: planId !== null });
  const me = useQuery(whoamiQuery(browserApi));
  const workersState = useQuery(workersQuery(browserApi));
  const projectState = useQuery(projectQuery(browserApi, project));

  const [request, setRequest] = useState("");
  const [problem, setProblem] = useState<"blank" | "long" | null>(null);
  const [modelText, setModelText] = useState("");
  const [workerChoice, setWorkerChoice] = useState<number | null>(null);
  const [timeout, setTimeoutHours] = useState<AuthorTimeout>(DEFAULT_AUTHOR_TIMEOUT);

  const harness = projectState.data ? harnessRepo(projectState.data.harness) : undefined;
  const mine: Worker[] = me.data && workersState.data ? dispatchWorkers(workersState.data, me.data.login, project) : [];
  const fit = { project, runtime: AUTHOR_RUNTIME, repos: harness ? [harness] : [] };
  const workerId = authorWorker(mine, fit, workerChoice);
  const model = readModel(modelText, AUTHOR_RUNTIME);
  const suggestions = modelSuggestions(mine, AUTHOR_RUNTIME);
  const outlook = dispatchOutlook(1, mine, fit, workerId);
  const workersLoading = workersState.isPending || me.isPending;
  const noWorkers = !workersLoading && mine.length === 0;
  const noHarness = projectState.isSuccess && harness === null;
  const bytes = utf8Bytes(request);
  const pending = write.isPending;
  const revise = planId !== null;
  const planTitle = (plan.data?.body as { title?: unknown } | undefined)?.title;
  const planName = typeof planTitle === "string" && planTitle.trim() ? planTitle : (planId ?? "");
  const runnable = workerId !== null && !noHarness && model.problem === null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    const read = readRequest(request);
    setProblem(read.problem);
    if (read.problem !== null || !runnable || workerId === null) return;
    let run: Run;
    try {
      run = await write.mutateAsync({
        request: read.request,
        worker_id: workerId,
        plan_id: planId,
        runtime: AUTHOR_RUNTIME,
        model: model.model,
        timeout_h: timeout,
      });
    } catch {
      return; // shown above the buttons, from the mutation's state
    }
    onDispatched(run);
    onClose();
  };

  const error = write.isError
    ? failure(write.error, { 403: t("errors.forbidden"), 404: t("errors.notFound"), 409: t("errors.conflict"), 422: t("errors.invalid") })
    : null;
  const apiDetail =
    write.isError && isApiError(write.error) && (write.error.info.status === 409 || write.error.info.status === 422)
      ? tPlanRun("errors.hubSays", { message: write.error.info.message })
      : null;
  const requestDescribed = [`${ids}-request-hint`, problem ? `${ids}-request-problem` : null].filter(Boolean).join(" ");

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex min-h-0 flex-1 flex-col" aria-busy={pending || undefined}>
      <div className="flex items-start gap-3 border-b px-4 pt-4 pb-3 sm:px-5">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <DialogTitle className="flex items-center gap-2 text-lg font-semibold">
            {revise ? <FilePen className="size-5 text-muted-foreground" aria-hidden="true" /> : <FilePlus className="size-5 text-muted-foreground" aria-hidden="true" />}
            {revise ? t("reviseTitle") : t("newTitle")}
          </DialogTitle>
          <DialogDescription className="text-pretty">
            {revise
              ? t.rich("reviseDescription", {
                  plan: planName,
                  name: (chunks) => <span className="font-medium text-foreground [overflow-wrap:anywhere]">{chunks}</span>,
                })
              : t.rich("newDescription", {
                  project,
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

      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-4 py-4 sm:px-5" data-testid="author-run-body">
        {noHarness ? (
          <p role="alert" className="flex items-start gap-2 rounded-md border border-attention/30 bg-attention-soft px-3 py-2.5 text-sm text-pretty text-attention" data-testid="author-run-no-harness">
            <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>{t("noHarness", { project })}</span>
          </p>
        ) : null}

        <div className="flex min-w-0 flex-col gap-1.5">
          <label htmlFor={`${ids}-request`} className="text-sm font-medium">
            {revise ? t("requestReviseLabel") : t("requestLabel")}
          </label>
          <textarea
            id={`${ids}-request`}
            value={request}
            rows={6}
            placeholder={revise ? t("requestRevisePlaceholder") : t("requestPlaceholder")}
            onChange={(event) => {
              setRequest(event.target.value);
              setProblem(null);
            }}
            disabled={pending}
            aria-invalid={problem ? true : undefined}
            aria-describedby={requestDescribed}
            className={cn(
              "block max-h-[40dvh] min-h-32 w-full resize-y rounded-sm border border-input bg-card px-3 py-2 text-base leading-6 text-foreground outline-none transition-[border-color,box-shadow] placeholder:text-fg-subtle focus-visible:border-ring focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 md:text-sm md:leading-5",
              problem && "border-danger-solid",
            )}
            data-testid="author-run-request"
          />
          <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
            <p id={`${ids}-request-hint`} className="min-w-0 flex-1 text-xs text-pretty text-muted-foreground">
              {t("requestHint", { max: format.number(MAX_REQUEST_BYTES) })}
            </p>
            {bytes >= MAX_REQUEST_BYTES * COUNT_FROM ? (
              <span className={cn("text-xs text-muted-foreground tabular-nums", bytes > MAX_REQUEST_BYTES && "font-medium text-danger")} data-testid="author-run-bytes">
                {t("bytes", { count: format.number(bytes), max: format.number(MAX_REQUEST_BYTES) })}
              </span>
            ) : null}
          </div>
          {problem ? (
            <p id={`${ids}-request-problem`} className="text-xs font-medium text-danger" data-testid="author-run-request-problem">
              {problem === "blank" ? t("requestBlank") : t("requestLong", { max: format.number(MAX_REQUEST_BYTES) })}
            </p>
          ) : null}
        </div>

        <div className="flex min-w-0 flex-col gap-1" data-testid="author-run-runtime">
          <span className="text-sm font-medium">{tDispatch("runtime")}</span>
          <p className="text-sm">{KNOWN_RUNTIMES[AUTHOR_RUNTIME]}</p>
          <p className="text-xs text-pretty text-muted-foreground">{t("runtimeHint")}</p>
        </div>

        <ModelField value={modelText} onChange={setModelText} runtime={AUTHOR_RUNTIME} suggestions={suggestions} problem={model.problem} disabled={pending} />

        <div className="flex min-w-0 flex-col gap-1.5" data-testid="author-run-worker">
          <label htmlFor={`${ids}-worker`} className="text-sm font-medium">
            {tDispatch("worker")}
          </label>
          {workersLoading ? (
            <Skeleton className="h-9 w-full sm:w-80" role="status" aria-label={t("workersLoading")} />
          ) : noWorkers ? (
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-dashed px-3 py-2 text-sm text-muted-foreground" data-testid="dispatch-no-workers">
              <Server className="size-4 shrink-0" aria-hidden="true" />
              <span>{tDispatch("noWorkers", { project })}</span>
              <Link href={WORKERS_HREF} className="font-medium text-brand underline-offset-4 hover:underline">
                {tDispatch("registerWorker")}
              </Link>
            </p>
          ) : (
            <NativeSelect
              id={`${ids}-worker`}
              value={workerId === null ? "" : String(workerId)}
              onChange={(event) => setWorkerChoice(Number(event.target.value))}
              disabled={pending}
              className="w-full sm:w-80 md:[&_select]:h-9 [&_select]:font-mono"
              aria-describedby={`${ids}-worker-hint`}
              data-testid="author-run-worker-select"
            >
              {mine.map((worker) => (
                <NativeSelectOption key={worker.id} value={String(worker.id)}>
                  {tDispatch("workerOption", {
                    name: worker.name,
                    status: tStatus(worker.status === "online" ? (worker.held_runs > 0 ? "busy" : "idle") : worker.status),
                  })}
                  {workerFitsNow(worker, fit) ? "" : ` ${t("workerCannot")}`}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          )}
          <p id={`${ids}-worker-hint`} className="text-xs text-pretty text-muted-foreground">
            {harness ? t("workerHint", { harness }) : t("workerHintNoHarness")}
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <label htmlFor={`${ids}-timeout`} className="text-sm font-medium">
            {tPlanRun("timeout")}
          </label>
          <NativeSelect
            id={`${ids}-timeout`}
            value={String(timeout)}
            onChange={(event) => setTimeoutHours(Number(event.target.value) as AuthorTimeout)}
            disabled={pending}
            className="w-full sm:w-56 md:[&_select]:h-9"
            aria-describedby={`${ids}-timeout-hint`}
            data-testid="author-run-timeout"
          >
            {AUTHOR_TIMEOUT_CHOICES.map((hours) => (
              <NativeSelectOption key={hours} value={String(hours)}>
                {tPlanRun("timeoutValue", { hours })}
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
        <Outlook outlook={outlook} project={project} subject="author" />
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
          <Button type="submit" size="lg" disabled={!runnable} busy={pending} data-testid="author-run-submit">
            {revise ? <FilePen aria-hidden="true" /> : <FilePlus aria-hidden="true" />}
            {pending ? t("submitting") : t("submit")}
          </Button>
        </div>
      </div>
    </form>
  );
}
