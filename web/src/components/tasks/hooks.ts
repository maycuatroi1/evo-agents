"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { whoamiQuery } from "@/lib/queries";

import { editTask, type Task, type TaskDetail, type TaskEdit, tasksKeys, type TasksSettings } from "./api";
import { patchTask, type Viewer } from "./model";

/** The visitor as the Tasks pages need them: their login, for their own tasks, and their role on the project. */
export function useTasksViewer(project: string): Viewer | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  if (!data) return null;
  const role = data.grants.find((grant) => grant.project === project)?.role ?? null;
  return { login: data.login, role: role === "reader" || role === "writer" || role === "admin" ? role : null };
}

export function signInAgainOn401(error: unknown): void {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}

/** Reload what a write may have changed: every list of tasks, the sprints (their counts) and the task, when named. */
export function useTasksRefresh(project: string) {
  const queryClient = useQueryClient();
  return useCallback(
    async (ref?: string | null) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: tasksKeys.lists(project) }),
        queryClient.invalidateQueries({ queryKey: tasksKeys.sprints(project) }),
        ref ? queryClient.invalidateQueries({ queryKey: tasksKeys.task(project, ref) }) : Promise.resolve(),
      ]);
    },
    [queryClient, project],
  );
}

/**
 * Change a task from its own page, where the form that asked says a refusal next to itself: the hub is asked, the
 * task's page shows its answer, and the lists reload. Throws what the hub refused.
 */
export function useTaskSave(project: string) {
  const queryClient = useQueryClient();
  const refresh = useTasksRefresh(project);
  return useCallback(
    async (ref: string, change: TaskEdit): Promise<TaskDetail> => {
      try {
        const saved = await editTask(browserApi(), project, ref, change);
        queryClient.setQueryData(tasksKeys.task(project, saved.ref), saved);
        return saved;
      } finally {
        await refresh(ref);
      }
    },
    [project, queryClient, refresh],
  );
}

/**
 * Change a task from a list or the board: every list shows the change at once, the hub is asked, then the lists and
 * the task reload. A refusal puts the lists back as they were and says why in a toast; success says what changed when
 * `done` gives the words. Resolves with the task as the hub keeps it, or null when it refused.
 */
export function useTaskEdit(project: string, settings: Pick<TasksSettings, "statuses"> | null) {
  const t = useTranslations("tasks.edit");
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const refresh = useTasksRefresh(project);
  return useCallback(
    async (task: Pick<Task, "ref" | "number">, change: TaskEdit, done?: string): Promise<TaskDetail | null> => {
      const lists = tasksKeys.lists(project);
      await queryClient.cancelQueries({ queryKey: lists });
      const before = queryClient.getQueriesData<Task[]>({ queryKey: lists });
      if (settings) {
        queryClient.setQueriesData<Task[]>({ queryKey: lists }, (rows) =>
          rows?.map((row) => (row.number === task.number ? patchTask(row, change, settings) : row)),
        );
      }
      try {
        const saved = await editTask(browserApi(), project, task.ref, change);
        queryClient.setQueryData(tasksKeys.task(project, saved.ref), saved);
        if (done) notify({ tone: "success", text: done });
        return saved;
      } catch (error) {
        for (const [key, rows] of before) queryClient.setQueryData(key, rows);
        signInAgainOn401(error);
        notifyFailure(t("failed", { ref: task.ref }), failure(error));
        return null;
      } finally {
        await refresh(task.ref);
      }
    },
    [failure, project, queryClient, refresh, settings, t],
  );
}
