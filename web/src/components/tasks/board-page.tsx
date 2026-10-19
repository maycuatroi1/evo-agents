"use client";

import { useQuery } from "@tanstack/react-query";
import { Kanban, ListChecks, Plus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { ToolbarField, ToolbarFilters } from "@/components/data/filter-sheet";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, NoResults } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { ROLES, type Role, type Sprint, sprintsQuery, type TaskList, tasksPeopleQuery, tasksQuery, type TasksSettings, tasksSettingsQuery } from "./api";
import { SprintBadge, useSprintDays } from "./badges";
import { Board } from "./board";
import { ReaderNote, TasksTabs } from "./chrome";
import { useTaskEdit, useTasksViewer } from "./hooks";
import { backlogHref, boardHref, newTaskHref } from "./links";
import {
  type BoardAsk,
  boardColumns,
  boardList,
  type BoardFilter,
  canWrite,
  filtering,
  filterTasks,
  linkedCodes,
  NO_FILTER,
  orderedSprints,
  type Viewer,
} from "./model";

const NO_SPRINT = "__none__"; // the sprint picker's value for the tasks of no sprint

function BoardSkeleton() {
  return (
    <div className="grid auto-cols-[85%] grid-flow-col gap-3 overflow-hidden md:auto-cols-[minmax(13.5rem,1fr)]" role="status">
      {[0, 1, 2, 3, 4].map((column) => (
        <div key={column} className="flex flex-col gap-2 rounded-md border bg-muted/30 p-2.5">
          <Skeleton className="h-5 w-24" />
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
        </div>
      ))}
    </div>
  );
}

function SprintPicker({ project, sprints, list }: { project: string; sprints: readonly Sprint[]; list: TaskList }) {
  const t = useTranslations("tasks.board");
  const router = useRouter();
  const id = useId();
  const value = list.kind === "sprint" ? list.name : NO_SPRINT;
  const known = list.kind !== "sprint" || sprints.some((sprint) => sprint.name === list.name);
  return (
    <div className="flex min-w-0 items-center gap-2">
      <Label htmlFor={id} className="shrink-0 text-[13px] font-normal text-muted-foreground">
        {t("sprint")}
      </Label>
      <NativeSelect
        id={id}
        value={value}
        onChange={(event) => {
          const next = event.target.value;
          router.push(boardHref(project, next === NO_SPRINT ? { backlog: true } : { sprint: next }));
        }}
        className="min-w-0 flex-1 md:flex-none"
        data-testid="board-sprint"
      >
        {known ? null : <NativeSelectOption value={value}>{value}</NativeSelectOption>}
        {orderedSprints(sprints).map((sprint) => (
          <NativeSelectOption key={sprint.name} value={sprint.name}>
            {sprint.state === "active" ? t("activeSprint", { sprint: sprint.name }) : sprint.name}
          </NativeSelectOption>
        ))}
        <NativeSelectOption value={NO_SPRINT}>{t("noSprint")}</NativeSelectOption>
      </NativeSelect>
    </div>
  );
}

function BoardBody({
  project,
  settings,
  sprints,
  list,
  viewer,
}: {
  project: string;
  settings: TasksSettings;
  sprints: readonly Sprint[];
  list: TaskList;
  viewer: Viewer | null;
}) {
  const t = useTranslations("tasks.board");
  const tRole = useTranslations("tasks.role");
  const writer = canWrite(viewer);
  const mineId = useId();
  const [filter, setFilter] = useState<BoardFilter>(NO_FILTER);
  const state = useHubQuery(tasksQuery(browserApi, project, list));
  const people = useQuery({ ...tasksPeopleQuery(browserApi, project), enabled: writer });
  const edit = useTaskEdit(project, settings);
  const set = (patch: Partial<BoardFilter>) => setFilter((held) => ({ ...held, ...patch }));
  const sprintName = list.kind === "sprint" ? list.name : null;
  return (
    <QueryView state={state} loading={<BoardSkeleton />}>
      {(tasks) => {
        const codes = settings.srs ? linkedCodes(tasks) : [];
        const shown = filterTasks(tasks, filter);
        const { columns, cancelled } = boardColumns(settings.statuses, shown);
        const active = [filter.role, filter.code, filter.mine].filter((value) => value !== null).length;
        const inUse = [
          ...(filter.role ? [{ label: t("role"), value: tRole(filter.role) }] : []),
          ...(filter.code ? [{ label: t("code"), value: filter.code }] : []),
          ...(filter.mine ? [{ label: t("mineFilter"), value: filter.mine }] : []),
        ];
        return (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-3 md:flex-row md:flex-wrap md:items-center" role="search" aria-label={t("toolbar")}>
              <SprintPicker project={project} sprints={sprints} list={list} />
              <ToolbarFilters active={active} summary={t("shown", { shown: shown.length, total: tasks.length })} onClear={() => setFilter(NO_FILTER)}>
                <ToolbarField label={t("role")}>
                  {(id) => (
                    <NativeSelect id={id} value={filter.role ?? ""} onChange={(event) => set({ role: (event.target.value || null) as Role | null })} data-testid="board-role">
                      <NativeSelectOption value="">{t("anyRole")}</NativeSelectOption>
                      {ROLES.map((role) => (
                        <NativeSelectOption key={role} value={role}>
                          {t("hasRole", { role: tRole(role) })}
                        </NativeSelectOption>
                      ))}
                    </NativeSelect>
                  )}
                </ToolbarField>
                {codes.length ? (
                  <ToolbarField label={t("code")}>
                    {(id) => (
                      <NativeSelect id={id} value={filter.code ?? ""} onChange={(event) => set({ code: event.target.value || null })} data-testid="board-code">
                        <NativeSelectOption value="">{t("anyCode")}</NativeSelectOption>
                        {codes.map((code) => (
                          <NativeSelectOption key={code} value={code}>
                            {code}
                          </NativeSelectOption>
                        ))}
                      </NativeSelect>
                    )}
                  </ToolbarField>
                ) : null}
                {viewer ? (
                  <div className="flex min-h-8 items-center gap-2 max-md:min-h-11">
                    <Switch id={mineId} checked={filter.mine !== null} onCheckedChange={(on) => set({ mine: on ? viewer.login : null })} data-testid="board-mine" />
                    <Label htmlFor={mineId} className="text-[13px] font-normal">
                      {t("mine")}
                    </Label>
                  </div>
                ) : null}
              </ToolbarFilters>
              {writer ? <p className="text-xs text-fg-subtle max-md:hidden md:ml-auto">{t("dragHint")}</p> : null}
            </div>
            <p className="sr-only" aria-live="polite">
              {filtering(filter) ? t("shown", { shown: shown.length, total: tasks.length }) : ""}
            </p>
            {tasks.length === 0 ? (
              <EmptyState
                icon={Kanban}
                title={sprintName ? t("emptySprintTitle", { sprint: sprintName }) : t("emptyTitle")}
                description={writer ? t("emptyWriter") : t("emptyReader")}
                testId="tasks-board-empty"
              >
                {writer ? (
                  <Button asChild>
                    <Link href={newTaskHref(project, { sprint: sprintName })}>
                      <Plus aria-hidden="true" />
                      {t("newTask")}
                    </Link>
                  </Button>
                ) : null}
                <Button asChild variant="secondary">
                  <Link href={backlogHref(project)}>
                    <ListChecks aria-hidden="true" />
                    {t("openBacklog")}
                  </Link>
                </Button>
              </EmptyState>
            ) : shown.length === 0 ? (
              <NoResults title={t("noResults")} filters={inUse} onClear={() => setFilter(NO_FILTER)} testId="tasks-board-no-results" />
            ) : (
              <>
                <Board columns={columns} project={project} settings={settings} people={people.data ?? []} writer={writer} edit={edit} />
                {cancelled ? (
                  <p className="text-xs text-fg-subtle" data-testid="board-cancelled">
                    {t("cancelledOff", { count: cancelled })}{" "}
                    <Link href={backlogHref(project)} className="text-brand underline underline-offset-4 hover:text-brand-hover">
                      {t("openBacklog")}
                    </Link>
                  </p>
                ) : null}
              </>
            )}
          </div>
        );
      }}
    </QueryView>
  );
}

/**
 * The board of a project that enabled the core plugin tasks (/p/{project}/x/tasks/board; web/DESIGN.md, Plugins): the
 * tasks of one sprint, the active one unless the URL names another or asks for the tasks of no sprint, in a column per
 * status. Filters keep a role, an SRS code or the visitor's own tasks. A writer moves a card to another status by
 * dragging it or from its menus, assigns it from its Assignee menu, and adds a task; a reader reads.
 */
export function BoardPage({ project, title, ask, initialError }: { project: string; title: string; ask: BoardAsk; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("tasks.board");
  const viewer = useTasksViewer(project);
  const writer = canWrite(viewer);
  const settings = useHubQuery(tasksSettingsQuery(browserApi, project), initialError);
  const sprints = useHubQuery(sprintsQuery(browserApi, project));
  const known = sprints.status === "success" ? sprints.data : undefined;
  const list = boardList(ask, known);
  const sprint = list?.kind === "sprint" ? (known?.find((one) => one.name === list.name) ?? null) : null;
  const days = useSprintDays();
  const sub = sprint
    ? [days(sprint), sprint.goal ? t("goal", { goal: sprint.goal }) : null].filter(Boolean).join(". ")
    : list?.kind === "backlog"
      ? t("noSprintText")
      : null;
  return (
    <div className="flex flex-col gap-6" data-testid="tasks-board-page">
      <div className="flex flex-col gap-4">
        <PageHeader
          title={list?.kind === "sprint" ? list.name : title}
          status={sprint ? <SprintBadge state={sprint.state} size="lg" /> : null}
          sub={sub}
          actions={
            writer ? (
              <Button asChild>
                <Link href={newTaskHref(project, { sprint: list?.kind === "sprint" ? list.name : null })} data-testid="tasks-new">
                  <Plus aria-hidden="true" />
                  {t("newTask")}
                </Link>
              </Button>
            ) : null
          }
          testId="tasks-board-head"
        />
        <TasksTabs project={project} current="board" writer={writer} />
      </div>
      {viewer && !writer ? <ReaderNote project={project}>{t("readerText")}</ReaderNote> : null}
      <QueryView state={settings} loading={<BoardSkeleton />}>
        {(config) => (
          <QueryView state={sprints} loading={<BoardSkeleton />}>
            {(all) => (list ? <BoardBody project={project} settings={config} sprints={all} list={list} viewer={viewer} /> : null)}
          </QueryView>
        )}
      </QueryView>
    </div>
  );
}
