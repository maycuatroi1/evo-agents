"use client";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  FolderKanban,
  ListChecks,
  LoaderCircle,
  type LucideIcon,
  Play,
  Plus,
  RotateCcw,
  Send,
  Server,
  TriangleAlert,
  X,
} from "lucide-react";
import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { type KeyboardEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { useWriteFailure } from "@/components/admin/notice";
import { RunRef } from "@/components/data/identifier";
import { notify, notifyFailure } from "@/components/feedback/toast";
import type { OverviewRun } from "@/components/home/model";
import { useNow } from "@/components/kg/use-now";
import { planHref } from "@/components/plans/links";
import { DispatchDialog } from "@/components/runs/dispatch-dialog";
import { useDispatchedToast, usePlanRunToast } from "@/components/runs/hooks";
import { PlanRunDialog } from "@/components/runs/plan-run-dialog";
import { controlRun, type Run, runHref, runKeys, runsQuery } from "@/components/runs/queries";
import { HOME_NAV, HUB_NAV, PROJECT_NAV, projectHref } from "@/components/shell/nav";
import { useCurrentProject } from "@/components/shell/project-switcher";
import { roleLabelKey } from "@/components/shell/role-badge";
import { STATUS_LOOKS, useStatusText } from "@/components/status/status-badge";
import { Command, CommandGroup, CommandInput, CommandItem, CommandList, CommandShortcut } from "@/components/ui/command";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
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
  canRegister,
  dispatchProjects,
  grantedProjects,
  IDLE_LIMIT,
  IDLE_PLAN_RUNS,
  initialScope,
  matchesWords,
  orderPlans,
  overviewRuns,
  planRunOffers,
  QUERY_LIMIT,
  queryWords,
  rerunCandidate,
  RUN_SEARCH_LIMIT,
  runHaystack,
  runSearchText,
  type Scope,
  scopeCycle,
  scopeProjects,
  SEARCH_DEBOUNCE_MS,
  stepScope,
} from "./model";

/** A dialog the palette opens once it has closed. Each is the one its page opens; the palette adds no form of its own. */
type PaletteDialog =
  | { kind: "dispatch"; project: string }
  | { kind: "planRun"; project: string; planId: string }
  | { kind: "register" };

type RerunTarget = Pick<OverviewRun, "project" | "id" | "plan_id">;

/** What choosing an item does: open a page, open a dialog, or rerun a failed run as Home's Rerun does. */
type Target = { kind: "page"; href: Route } | { kind: "dialog"; dialog: PaletteDialog } | { kind: "rerun"; run: RerunTarget };

/** After the palette has closed: open the dialog chosen, leave focus to the page opened, or give focus back. */
type After = { kind: "dialog"; dialog: PaletteDialog } | { kind: "navigate" } | { kind: "stay" };

type GroupKey = "actions" | "runs" | "plans" | "workers" | "goto";

type Entry = {
  /** cmdk's value: unique in the palette, stable while the item stays. */
  value: string;
  icon: LucideIcon;
  label: ReactNode;
  /** What a query is matched against. */
  text: string;
  meta?: string;
  target: Target;
};

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Gives focus back to what held it when the palette opened. */
  restoreFocus: () => void;
};

/**
 * The kit's CommandPalette: shadcn's Command (cmdk) in a Dialog, opened with Cmd K or Ctrl K or the top bar's field.
 * Actions first (Run plan, Dispatch a step, Rerun the latest failed run, Register worker, only those the visitor's
 * grants allow), then the runs, plans and workers the query finds, then the pages to go to. It looks in the page's
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

  const choose = (target: Target) => {
    if (target.kind === "page") {
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
            } else if (next?.kind !== "navigate") {
              restoreFocus();
            }
          }}
          data-testid="command-palette"
        >
          <DialogTitle className="sr-only">{t("title")}</DialogTitle>
          <DialogDescription className="sr-only">{t("description")}</DialogDescription>
          <PaletteBody onChoose={choose} />
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

/** `value`, once it has stopped changing for `delay` ms. */
function useDebounced<T>(value: T, delay: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return settled;
}

/** A run's title as Home writes it: the plan's for a plan run, the step's for a run of one step. */
function runTitle(run: Pick<Run, "kind" | "title" | "step_key" | "plan_id"> & { plan_title?: string | null }): string {
  if (run.kind === "plan" || run.step_key === null) return run.plan_title ?? run.title ?? run.plan_id;
  return run.title ?? run.step_key ?? run.plan_id;
}

/** The meta at an item's end: the project when the palette looks in every project, then what the item says. */
function metaOf(project: string | null, text: string | null): string {
  return [project, text].filter(Boolean).join(", ");
}

const WORKER_ORDER = { busy: 0, idle: 1, draining: 2, offline: 3, revoked: 4 } as const;

/** The keys with which cmdk moves the selection. */
const MOVE_KEYS = new Set(["ArrowDown", "ArrowUp", "Home", "End"]);

/** The palette's search, groups and footer; mounted each time the palette opens, so it starts empty in the page's project. */
function PaletteBody({ onChoose }: { onChoose: (target: Target) => void }) {
  const t = useTranslations("palette");
  const tNav = useTranslations("nav");
  const tRoles = useTranslations("roles");
  const runStatus = useStatusText("run");
  const workerStatus = useStatusText("worker");
  const format = useFormatter();
  const current = useCurrentProject();
  const input = useRef<HTMLInputElement>(null);

  const { data: me } = useQuery(whoamiQuery(browserApi));
  const grants = useMemo(() => me?.grants ?? [], [me]);
  const projects = useMemo(() => grantedProjects(grants), [grants]);
  const cycle = useMemo(() => scopeCycle(projects), [projects]);
  const [scope, setScope] = useState<Scope>(() => initialScope(current, projects));
  const [query, setQuery] = useState("");
  const search = useDebounced(runSearchText(query), SEARCH_DEBOUNCE_MS);
  // The first item stays selected while results arrive, until the person moves the selection (cmdk alone keeps the
  // item it selected first, which may sit second once a group above it has loaded).
  const [selected, setSelected] = useState("");
  const [moved, setMoved] = useState(false);

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

  const take = (entries: Entry[], cap = limit) => entries.filter((entry) => matchesWords(words, entry.text)).slice(0, cap);

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
  const runPlans: Entry[] = planRunOffers(plansByProject, grants).map((offer) => ({
    value: `action:plan-run:${offer.project}:${offer.planId}`,
    icon: Play,
    label: <span className="truncate">{t("actions.runPlan", { plan: offer.title })}</span>,
    text: `${t("actions.runPlan", { plan: offer.title })} run plan ${offer.planId} ${offer.project}`,
    meta: metaOf(showProject ? offer.project : null, t("actions.stepsLeft", { count: offer.left })),
    target: { kind: "dialog", dialog: { kind: "planRun", project: offer.project, planId: offer.planId } },
  }));
  const dispatches: Entry[] = dispatchProjects(scoped, grants).map((project) => {
    const label = scope === null ? t("actions.dispatchIn", { project }) : t("actions.dispatch");
    return {
      value: `action:dispatch:${project}`,
      icon: Send,
      label: <span className="truncate">{label}</span>,
      text: `${label} dispatch step ${project}`,
      target: { kind: "dialog", dialog: { kind: "dispatch", project } },
    };
  });
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
  const registers: Entry[] = canRegister(grants)
    ? [
        {
          value: "action:register",
          icon: Plus,
          label: <span className="truncate">{t("actions.register")}</span>,
          text: `${t("actions.register")} register worker`,
          target: { kind: "dialog", dialog: { kind: "register" } },
        },
      ]
    : [];
  const actions = [...take(runPlans, typed ? QUERY_LIMIT : IDLE_PLAN_RUNS), ...take(dispatches), ...take(reruns), ...take(registers)];

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

  // Go to: the scope's project pages, the hub's pages, then the projects (while looking in every project, or typing).
  const projectPages: Entry[] =
    scope === null
      ? []
      : PROJECT_NAV.map((item) => {
          const label = t("goto.page", { page: tNav(item.label), project: scope });
          return {
            value: `goto:${scope}:${item.segment || "overview"}`,
            icon: item.icon,
            label: <span className="truncate">{label}</span>,
            text: label,
            target: { kind: "page", href: projectHref(scope, item.segment) },
          };
        });
  const hubPages: Entry[] = [...HOME_NAV, ...HUB_NAV]
    .filter((item) => !item.adminOnly || me?.admin)
    .map((item) => ({
      value: `goto:hub:${item.label}`,
      icon: item.icon,
      label: <span className="truncate">{tNav(item.label)}</span>,
      text: tNav(item.label),
      target: { kind: "page", href: item.href },
    }));
  const projectLinks: Entry[] =
    scope === null || typed
      ? grants
          .filter((grant) => grant.project !== scope)
          .sort((a, b) => a.project.localeCompare(b.project))
          .slice(0, typed ? undefined : IDLE_LIMIT)
          .map((grant) => ({
            value: `goto:project:${grant.project}`,
            icon: FolderKanban,
            label: <span className="truncate">{grant.project}</span>,
            text: grant.project,
            meta: tRoles(roleLabelKey(grant.role)),
            target: { kind: "page", href: projectHref(grant.project) },
          }))
      : [];
  const goto = take([...projectPages, ...hubPages, ...projectLinks], typed ? QUERY_LIMIT : Infinity);

  const groups: { key: GroupKey; entries: Entry[] }[] = [
    { key: "actions", entries: actions },
    { key: "runs", entries: runs },
    { key: "plans", entries: planItems },
    { key: "workers", entries: workerItems },
    { key: "goto", entries: goto },
  ];
  const count = groups.reduce((total, group) => total + group.entries.length, 0);
  const partial = [overview, workers, ...plans, ...(remote ? [found] : [])].some((state) => state.isError);

  const values = groups.flatMap((group) => group.entries.map((entry) => entry.value));
  const value = moved && values.includes(selected) ? selected : (values[0] ?? "");
  const changeQuery = (text: string) => {
    setQuery(text);
    setMoved(false);
  };
  const switchScope = (step: 1 | -1) => {
    setScope((shown) => stepScope(cycle, shown, step));
    setMoved(false);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Tab" || event.altKey || event.ctrlKey || event.metaKey || cycle.length < 2) return;
    // Tab switches the project instead of leaving the field (the dialog's focus trap would wrap it otherwise).
    event.preventDefault();
    event.stopPropagation();
    switchScope(event.shiftKey ? -1 : 1);
  };

  return (
    <Command
      label={t("label")}
      shouldFilter={false}
      loop
      vimBindings={false}
      value={value}
      onValueChange={setSelected}
      onKeyDown={(event) => {
        if (MOVE_KEYS.has(event.key)) setMoved(true);
      }}
      data-testid="palette-command"
    >
      <CommandInput
        ref={input}
        value={query}
        onValueChange={changeQuery}
        onKeyDown={onKeyDown}
        placeholder={t("placeholder")}
        maxLength={200}
        data-testid="palette-input"
      >
        {pending ? <LoaderCircle className="size-4 shrink-0 animate-spin" aria-hidden="true" data-testid="palette-pending" /> : null}
        <DialogClose asChild>
          <button
            type="button"
            aria-label={t("close")}
            title={t("close")}
            className="inline-flex shrink-0 cursor-pointer items-center justify-center rounded-xs text-fg-subtle transition-colors hover:text-foreground max-md:-mr-2 max-md:size-11 max-md:rounded-sm max-md:hover:bg-accent"
            data-testid="palette-close"
          >
            <Kbd className="max-md:hidden">Esc</Kbd>
            <X className="size-5 md:hidden" aria-hidden="true" />
          </button>
        </DialogClose>
      </CommandInput>
      <CommandList
        label={t("list")}
        className="max-h-[min(380px,calc(100dvh-11rem))]"
        onPointerMove={() => setMoved(true)}
        data-testid="palette-list"
      >
        {groups.map((group) =>
          group.entries.length > 0 ? (
            <CommandGroup key={group.key} heading={t(`groups.${group.key}`)} data-testid={`palette-group-${group.key}`}>
              {group.entries.map((entry) => (
                <CommandItem
                  key={entry.value}
                  value={entry.value}
                  onSelect={() => onChoose(entry.target)}
                  data-testid="palette-item"
                  data-item={entry.value}
                >
                  <entry.icon aria-hidden="true" />
                  <span className="flex min-w-0 flex-1 items-center gap-2">{entry.label}</span>
                  {entry.meta ? (
                    <CommandShortcut className="max-w-[45%] max-sm:max-w-[40%]">
                      <span className="truncate">{entry.meta}</span>
                    </CommandShortcut>
                  ) : null}
                  <Kbd className="hidden md:group-data-[selected=true]/command-item:inline-flex" aria-hidden="true">
                    ↵
                  </Kbd>
                </CommandItem>
              ))}
            </CommandGroup>
          ) : null,
        )}
      </CommandList>
      {count === 0 && typed && !pending ? (
        <p className="px-4 pt-2 pb-5 text-center text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-testid="palette-empty">
          {t("noMatch", { query: query.trim(), scope: scopeName })}
          {cycle.length > 1 ? <span className="max-md:hidden"> {t("trySwitch")}</span> : null}
        </p>
      ) : null}
      {partial ? (
        <p className="flex items-center gap-2 border-t px-4 py-2 text-xs text-danger" data-testid="palette-partial">
          <TriangleAlert className="size-3.5 shrink-0" aria-hidden="true" />
          {t("partial")}
        </p>
      ) : null}
      <p className="sr-only" aria-live="polite" data-testid="palette-count">
        {pending ? t("searching") : t("results", { count, scope: scopeName })}
      </p>
      <div className="flex items-center gap-x-4 gap-y-2 border-t px-4 py-2 text-xs text-fg-subtle max-md:py-0">
        <span className="inline-flex items-center gap-1.5 max-md:hidden" aria-hidden="true">
          <Kbd>↑</Kbd>
          <Kbd>↓</Kbd>
          {t("hints.move")}
        </span>
        <span className="inline-flex items-center gap-1.5 max-md:hidden" aria-hidden="true">
          <Kbd>↵</Kbd>
          {t("hints.open")}
        </span>
        {cycle.length > 1 ? (
          <span className="inline-flex items-center gap-1.5 max-md:hidden" aria-hidden="true">
            <Kbd>Tab</Kbd>
            {t("hints.scope")}
          </span>
        ) : null}
        <button
          type="button"
          onClick={() => {
            switchScope(1);
            input.current?.focus();
          }}
          disabled={cycle.length < 2}
          aria-label={t("scope.switch", { scope: scopeName })}
          title={t("scope.switch", { scope: scopeName })}
          className="ml-auto inline-flex h-6 min-w-0 cursor-pointer items-center gap-1.5 rounded-xs px-1.5 font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-default disabled:hover:bg-transparent max-md:-mr-1.5 max-md:h-11"
          data-testid="palette-scope"
          data-scope={scope ?? ""}
        >
          <FolderKanban className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="truncate">{scopeName}</span>
        </button>
      </div>
    </Command>
  );
}
