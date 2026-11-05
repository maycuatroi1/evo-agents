"use client";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { ListChecks, RotateCcw, Server } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useMemo, useRef, useState } from "react";

import { useWriteFailure } from "@/components/admin/notice";
import { RunRef } from "@/components/data/identifier";
import { notify, notifyFailure } from "@/components/feedback/toast";
import type { OverviewRun } from "@/components/home/model";
import { useNow } from "@/components/kg/use-now";
import { planHref } from "@/components/plans/links";
import { AuthorRunDialog } from "@/components/runs/author-run-dialog";
import { DispatchDialog } from "@/components/runs/dispatch-dialog";
import { useAuthorRunToast, useDispatchedToast, usePlanRunToast } from "@/components/runs/hooks";
import { PlanRunDialog } from "@/components/runs/plan-run-dialog";
import { controlRun, type Run, runHref, runKeys, runsQuery } from "@/components/runs/queries";
import { useCurrentProject } from "@/components/shell/project-switcher";
import { useOpenShortcuts } from "@/components/shell/shortcuts";
import { STATUS_LOOKS, useStatusText } from "@/components/status/status-badge";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { workerView } from "@/components/workers/model";
import { type Worker, workerHref, workersQuery } from "@/components/workers/queries";
import { RegisterDialog } from "@/components/workers/register-dialog";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { planKeys, plansQuery } from "@/lib/plan-queries";
import type { PlanSummary } from "@/lib/plans";
import { overviewQuery, queryKeys, whoamiQuery } from "@/lib/queries";

import {
  grantedProjects,
  IDLE_LIMIT,
  IDLE_PLAN_RUNS,
  initialScope,
  matchesWords,
  orderPlans,
  overviewRuns,
  QUERY_LIMIT,
  queryWords,
  rerunCandidate,
  RUN_SEARCH_LIMIT,
  runHaystack,
  runSearchText,
  runTitle,
  type Scope,
  scopeCycle,
  scopeProjects,
  SEARCH_DEBOUNCE_MS,
  stepScope,
} from "./model";
import { type After, metaOf, type OpenTarget, type PaletteDialog, useDialogActions, useGotoEntries, useHelpEntries } from "./palette-entries";
import { type Entry as PaletteEntry, type GroupKey, PaletteView } from "./palette-view";
import { useDebounced } from "./use-debounced";

type RerunTarget = Pick<OverviewRun, "project" | "id" | "plan_id">;

/**
 * What choosing an item does: open a page, open a dialog, rerun a failed run as Home's Rerun does, or open the
 * keyboard shortcuts.
 */
type Target = OpenTarget | { kind: "rerun"; run: RerunTarget };

type Entry = PaletteEntry<Target>;

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Gives focus back to what held it when the palette opened. */
  restoreFocus: () => void;
};

/**
 * The kit's CommandPalette: shadcn's Command (cmdk) in a Dialog, opened with Cmd K or Ctrl K or the top bar's field.
 * Actions first (Run plan, Dispatch a step, New plan, Revise with agent once something is typed, Rerun the latest
 * failed run, Register worker, only those the visitor's grants allow), then the runs, plans and workers the query
 * finds, then the pages to go to. It looks in the page's
 * project; Tab moves to the next project of the visitor's grants and to every project. Choosing an item opens a page
 * or the dialog that page opens, after the palette has closed; nothing in it deletes or stops anything. Esc closes it
 * and gives focus back to what held it.
 */
export function CommandPalette({ open, onOpenChange, restoreFocus }: Props) {
  const t = useTranslations("palette");
  const router = useRouter();
  const after = useRef<After | null>(null);
  const [dialog, setDialog] = useState<PaletteDialog | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const rerun = useRerun();
  const dispatched = useDispatchedToast();
  const planRunDispatched = usePlanRunToast();
  const authorDispatched = useAuthorRunToast();
  const openShortcuts = useOpenShortcuts();

  const choose = (target: Target) => {
    if (target.kind === "shortcuts") {
      after.current = { kind: "shortcuts" };
    } else if (target.kind === "page") {
      const here = `${window.location.pathname}${window.location.search}`;
      after.current = target.href === here ? { kind: "stay" } : { kind: "navigate" };
      if (target.href !== here) router.push(target.href);
    } else if (target.kind === "dialog") {
      after.current = { kind: "dialog", dialog: target.dialog };
    } else {
      after.current = { kind: "stay" };
      rerun.mutate(target.run);
    }
    onOpenChange(false);
  };

  // A dialog the palette opened has no trigger to give focus back to; it goes where it was before the palette opened.
  const dialogClosed = (event: Event) => {
    event.preventDefault();
    restoreFocus();
  };

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          showCloseButton={false}
          className="top-4 flex max-w-[calc(100%-2rem)] translate-y-0 flex-col gap-0 overflow-hidden p-0 sm:max-w-160 md:top-[12vh]"
          onCloseAutoFocus={(event) => {
            // Radix gives focus back only to a Dialog trigger, and the palette also opens from the keyboard.
            event.preventDefault();
            const next = after.current;
            after.current = null;
            if (next?.kind === "dialog") {
              setDialog(next.dialog);
              setDialogOpen(true);
            } else if (next?.kind === "shortcuts") {
              restoreFocus();
              openShortcuts?.();
            } else if (next?.kind !== "navigate") {
              restoreFocus();
            }
          }}
          data-testid="command-palette"
        >
          <DialogTitle className="sr-only">{t("title")}</DialogTitle>
          <DialogDescription className="sr-only">{t("description")}</DialogDescription>
          <PaletteBody onChoose={choose} shortcuts={openShortcuts !== null} />
        </DialogContent>
      </Dialog>
      {dialog?.kind === "dispatch" ? (
        <DispatchDialog
          key={`dispatch-${dialog.project}`}
          project={dialog.project}
          open={dialogOpen}
          onOpenChange={setDialogOpen}
          onDispatched={dispatched}
          onCloseAutoFocus={dialogClosed}
        />
      ) : null}
      {dialog?.kind === "planRun" ? (
        <PlanRunDialog
          key={`plan-run-${dialog.project}-${dialog.planId}`}
          project={dialog.project}
          planId={dialog.planId}
          open={dialogOpen}
          onOpenChange={setDialogOpen}
          onDispatched={planRunDispatched}
          onCloseAutoFocus={dialogClosed}
        />
      ) : null}
      {dialog?.kind === "author" ? (
        <AuthorRunDialog
          key={`author-${dialog.project}-${dialog.planId ?? ""}`}
          project={dialog.project}
          planId={dialog.planId}
          open={dialogOpen}
          onOpenChange={setDialogOpen}
          onDispatched={authorDispatched}
          onCloseAutoFocus={dialogClosed}
        />
      ) : null}
      {dialog?.kind === "register" ? (
        <RegisterDialog open={dialogOpen} onOpenChange={setDialogOpen} onJoined={notify} onCloseAutoFocus={dialogClosed} />
      ) : null}
    </>
  );
}

/** Rerun from the palette, as Home's Recent does: a toast with the new run, or why the hub refused. */
function useRerun() {
  const tToast = useTranslations("runs.detail.actions.toast");
  const tErrors = useTranslations("runs.detail.errors");
  const failure = useWriteFailure();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (run: RerunTarget) => controlRun(browserApi(), run.project, run.id, "rerun"),
    onSuccess: (answer, run) =>
      void notify({
        tone: "success",
        text: tToast("rerun", { id: answer.id }),
        description: tToast("rerunText", { of: run.id }),
        link: { label: tToast("openRun"), href: runHref(answer.project, answer.id) },
      }),
    onError: (error, run) => {
      if (isApiError(error) && error.kind === "unauthorized") {
        window.location.assign(LOGIN_PATH);
        return;
      }
      notifyFailure(
        tToast("failed.rerun", { id: run.id }),
        failure(error, { 403: tErrors("forbidden"), 404: tErrors("notFound"), 409: tErrors("conflict") }),
      );
    },
    onSettled: (_answer, _error, run) =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: runKeys.all(run.project) }),
        queryClient.invalidateQueries({ queryKey: planKeys.one(run.project, run.plan_id) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.overview }),
      ]),
  });
}

const WORKER_ORDER = { busy: 0, idle: 1, draining: 2, offline: 3, revoked: 4 } as const;

/**
 * The palette's search, groups and footer; mounted each time the palette opens, so it starts empty in the page's
 * project. `shortcuts`: the shell can open the shortcuts dialog, offered once something typed matches it.
 */
function PaletteBody({ onChoose, shortcuts }: { onChoose: (target: Target) => void; shortcuts: boolean }) {
  const t = useTranslations("palette");
  const runStatus = useStatusText("run");
  const workerStatus = useStatusText("worker");
  const format = useFormatter();
  const current = useCurrentProject();

  const { data: me } = useQuery(whoamiQuery(browserApi));
  const grants = useMemo(() => me?.grants ?? [], [me]);
  const projects = useMemo(() => grantedProjects(grants), [grants]);
  const cycle = useMemo(() => scopeCycle(projects), [projects]);
  const [scope, setScope] = useState<Scope>(() => initialScope(current, projects));
  const [query, setQuery] = useState("");
  const search = useDebounced(runSearchText(query), SEARCH_DEBOUNCE_MS);

  const words = queryWords(query);
  const typed = words.length > 0;
  const limit = typed ? QUERY_LIMIT : IDLE_LIMIT;
  const scoped = scopeProjects(scope, projects);
  const showProject = scope === null;
  const scopeName = scope ?? t("scope.all");
  const overview = useQuery(overviewQuery(browserApi));
  const plans = useQueries({ queries: scoped.map((project) => plansQuery(browserApi, project)) });
  const workers = useQuery({ ...workersQuery(browserApi), refetchInterval: false });
  // A project's runs are found by the hub (`q`: title, step, plan, repo, branch, worker, login, error, or #N), 150 ms
  // after typing stops; a newer query cancels the request of the last one, which consumed the abort signal.
  const remote = scope !== null && typed;
  const found = useQuery({
    ...runsQuery(browserApi, scope ?? "", { q: search, limit: RUN_SEARCH_LIMIT }),
    enabled: remote && search !== "",
    refetchInterval: false,
  });
  const pending = remote && (search !== runSearchText(query) || found.isFetching);

  const plansByProject = scoped.map((project, index) => ({ project, plans: plans[index]?.data ?? [] }));
  const failed = overview.data ? rerunCandidate(overview.data, me?.login ?? null, scope) : null;
  const now = useNow(failed !== null);

  /** "failed 5 minutes ago", "lost just now": when the run Rerun names ended. */
  const endedText = (run: OverviewRun): string | null => {
    if (now === null) return null;
    const ended = new Date(run.finished_at ?? run.queued_at);
    const lost = run.state === "lost";
    if (now - ended.getTime() < 60_000) return t(lost ? "actions.lostJustNow" : "actions.failedJustNow");
    return t(lost ? "actions.lostAgo" : "actions.failedAgo", { ago: format.relativeTime(ended, now) });
  };

  const take = <T,>(entries: PaletteEntry<T>[], cap = limit) => entries.filter((entry) => matchesWords(words, entry.text)).slice(0, cap);

  const runEntry = (run: Run | OverviewRun): Entry => {
    const title = runTitle(run);
    return {
      value: `run:${run.project}:${run.id}`,
      icon: STATUS_LOOKS.run[run.state].icon,
      label: (
        <>
          <RunRef id={run.id} className="text-foreground" />
          <span className="truncate">{title}</span>
        </>
      ),
      text: `${runHaystack(run)} ${runStatus(run.state)}`,
      meta: metaOf(showProject ? run.project : null, runStatus(run.state)),
      target: { kind: "page", href: runHref(run.project, run.id) },
    };
  };

  const planEntry = (plan: PlanSummary & { project: string }): Entry => ({
    value: `plan:${plan.project}:${plan.plan_id}`,
    icon: ListChecks,
    label: (
      <>
        <span className="truncate">{plan.title ?? plan.plan_id}</span>
        {plan.title ? <span className="truncate font-mono text-xs text-fg-subtle max-md:hidden">{plan.plan_id}</span> : null}
      </>
    ),
    text: `${plan.title ?? ""} ${plan.plan_id} ${plan.project}`,
    meta: metaOf(
      showProject ? plan.project : null,
      plan.area === "completed" ? t("plan.completed") : t("plan.progress", { done: plan.steps_done, total: plan.steps_total }),
    ),
    target: { kind: "page", href: planHref(plan.project, plan.plan_id) },
  });

  const workerEntry = (worker: Worker): Entry => {
    const status = workerStatus(workerView(worker));
    return {
      value: `worker:${worker.id}`,
      icon: Server,
      label: <span className="truncate">{worker.name}</span>,
      text: `${worker.name} ${worker.hostname} ${worker.owner} ${status}`,
      meta: status,
      target: { kind: "page", href: workerHref(worker.id) },
    };
  };

  // Actions: only what the grants allow, each opening the dialog its page opens (Rerun reruns, as Home's does).
  const { runPlans, newPlans, revisions, dispatches, registers } = useDialogActions({ plansByProject, grants, scoped, scope });
  const reruns: Entry[] = failed
    ? [
        {
          value: `action:rerun:${failed.project}:${failed.id}`,
          icon: RotateCcw,
          label: (
            <>
              <span className="shrink-0">{t("actions.rerun", { id: failed.id })}</span>
              <span className="truncate text-muted-foreground">{runTitle(failed)}</span>
            </>
          ),
          text: `${t("actions.rerun", { id: failed.id })} rerun ${runHaystack(failed)}`,
          meta: metaOf(showProject ? failed.project : null, endedText(failed)),
          target: { kind: "rerun", run: failed },
        },
      ]
    : [];
  const actions = [
    ...take(runPlans, typed ? QUERY_LIMIT : IDLE_PLAN_RUNS),
    ...take(dispatches),
    ...take(newPlans),
    ...(typed ? take(revisions, QUERY_LIMIT) : []),
    ...take(reruns),
    ...take(registers),
  ];

  // Runs: the hub's search of the project once something is typed; else, and across every project, the overview's.
  const runs = remote
    ? (found.data?.runs ?? []).filter((run) => run.project === scope).map(runEntry)
    : take(overviewRuns(overview.data, scope).map(runEntry));

  const planList = orderPlans(plansByProject.flatMap(({ project, plans: list }) => list.map((plan) => ({ ...plan, project }))));
  const planItems = take(planList.filter((plan) => typed || plan.area === "active").map(planEntry));

  const workerList = (workers.data ?? [])
    .filter((worker) => worker.status !== "revoked" && (scope === null || worker.projects.includes(scope)))
    .sort((a, b) => WORKER_ORDER[workerView(a)] - WORKER_ORDER[workerView(b)] || a.name.localeCompare(b.name));
  const workerItems = take(workerList.map(workerEntry));

  const goto = useGotoEntries({ scope, typed, grants, admin: me?.admin ?? false, take, limit: typed ? QUERY_LIMIT : Infinity });

  const help = useHelpEntries(shortcuts && typed, take);

  const groups: { key: GroupKey; entries: Entry[] }[] = [
    { key: "actions", entries: actions },
    { key: "runs", entries: runs },
    { key: "plans", entries: planItems },
    { key: "workers", entries: workerItems },
    { key: "goto", entries: goto },
    { key: "help", entries: help },
  ];
  const partial = [overview, workers, ...plans, ...(remote ? [found] : [])].some((state) => state.isError);

  return (
    <PaletteView
      query={query}
      onQueryChange={setQuery}
      pending={pending}
      groups={groups}
      typed={typed}
      partial={partial}
      scope={scope}
      scopeName={scopeName}
      scopes={cycle.length}
      onSwitchScope={(step) => setScope((shown) => stepScope(cycle, shown, step))}
      onChoose={onChoose}
    />
  );
}
