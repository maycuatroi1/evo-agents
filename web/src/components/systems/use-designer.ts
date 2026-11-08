"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";

import { agentsQuery } from "@/lib/agent-queries";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import {
  type History,
  historyOf,
  onlyRenamed,
  push,
  redo,
  renamed,
  sameSpec,
  type Spec,
  specOf,
  type SystemOut,
  type SystemProblem,
  undo,
  withNode,
} from "@/lib/systems";
import { putSystem, REVISION_CONFLICT, systemKeys, systemQuery, validateSystem } from "@/lib/systems-queries";

/** How long the draft rests before the hub checks it. */
const CHECK_AFTER_MS = 400;

export type Selection = { node: string } | { edge: number } | null;
export type SaveState = { status: "idle" | "saving" } | { status: "failed"; error: unknown };

function isProblem(value: unknown): value is SystemProblem {
  return typeof value === "object" && value !== null && "level" in value && "message" in value;
}

/**
 * The state of the designer: the draft with what it can undo, the revision it was opened on (and saves against), the
 * problems the hub finds in it, checked a moment after each change, and the save. A save that meets a revision written
 * meanwhile keeps the draft and says so (`conflict`): Load latest drops the draft for the hub's system, Keep mine saves
 * it over that revision on the next Save. The system read again while the draft has no change replaces it.
 */
export function useDesigner(project: string, system: SystemOut) {
  const queryClient = useQueryClient();
  const [base, setBase] = useState(() => ({ revision: system.revision, spec: specOf(system.spec) }));
  const [history, setHistory] = useState<History>(() => historyOf(specOf(system.spec)));
  const [selection, setSelection] = useState<Selection>(null);
  const [problems, setProblems] = useState<SystemProblem[]>(system.problems);
  const [checking, setChecking] = useState(false);
  const [save, setSave] = useState<SaveState>({ status: "idle" });
  const [conflict, setConflict] = useState<{ revision: number | null } | null>(null);
  const spec = history.present;
  const dirty = !sameSpec(spec, base.spec);
  const agents = useQuery(agentsQuery(browserApi, project));

  /** The newest version of each profile of the project, against which a pinned node is outdated. */
  const latest = useMemo(
    () => new Map((agents.data ?? []).filter((agent) => agent.deleted_at === null).map((agent) => [agent.handle, agent.version])),
    [agents.data],
  );

  // The hub's system, read again (after a save elsewhere, or Load latest), replaces a draft with no change. A rename
  // from the page's title (nothing else changed) moves a draft with changes onto the new revision, with the new name.
  // Adjusted while rendering, as React advises for state that follows a prop, rather than in an effect.
  const [seen, setSeen] = useState(system.revision);
  if (system.revision !== seen) {
    setSeen(system.revision);
    const next = specOf(system.spec);
    if (!dirty) {
      setBase({ revision: system.revision, spec: next });
      setHistory(historyOf(next));
      setProblems(system.problems);
    } else if (onlyRenamed(base.spec, next)) {
      setBase({ revision: system.revision, spec: next });
      setHistory((current) => renamed(current, next.name));
    }
  }

  // The hub checks the draft a moment after it last changed; a later change cancels the check under way.
  useEffect(() => {
    const control = new AbortController();
    const timer = window.setTimeout(() => {
      setChecking(true);
      validateSystem(browserApi(), project, spec, system.slug, control.signal)
        .then((checked) => setProblems(checked.problems))
        .catch(() => undefined) // the last problems stay; the save says what the hub refuses
        .finally(() => {
          if (!control.signal.aborted) setChecking(false);
        });
    }, CHECK_AFTER_MS);
    return () => {
      window.clearTimeout(timer);
      control.abort();
    };
  }, [project, system.slug, spec]);

  const change = useCallback((next: Spec | ((current: Spec) => Spec)) => {
    setSave({ status: "idle" });
    setHistory((current) => push(current, typeof next === "function" ? next(current.present) : next));
  }, []);

  const write = useCallback(
    async (next: Spec, revision: number): Promise<boolean> => {
      setSave({ status: "saving" });
      try {
        const written = await putSystem(browserApi(), project, system.slug, next, revision);
        const saved = specOf(written.spec);
        setSeen(written.revision);
        setBase({ revision: written.revision, spec: saved });
        // The hub's spec is the draft as it is now: a step it keeps for undo would be the same.
        setHistory((current) => (sameSpec(current.present, saved) ? current : push(current, saved)));
        setProblems(written.problems);
        setConflict(null);
        setSave({ status: "idle" });
        queryClient.setQueryData(systemKeys.one(project, system.slug), written);
        void queryClient.invalidateQueries({ queryKey: systemKeys.list(project) });
        return true;
      } catch (error) {
        if (isApiError(error) && error.status === 409 && error.info.code === REVISION_CONFLICT) {
          setConflict({ revision: null });
          void queryClient.fetchQuery({ ...systemQuery(browserApi, project, system.slug), staleTime: 0 }).then(
            (held) => setConflict({ revision: held.revision }),
            () => undefined,
          );
        }
        if (isApiError(error) && error.status === 422 && error.info.detail?.every(isProblem)) setProblems(error.info.detail);
        setSave({ status: "failed", error });
        return false;
      }
    },
    [project, system.slug, queryClient],
  );

  /** Write the draft as the next revision, against the revision the designer opened on. */
  const saveDraft = useCallback(() => write(history.present, base.revision), [write, history.present, base.revision]);

  /**
   * Pin `version` of node `id`'s profile: an upgrade, or the version a save of its config just made. A draft with no
   * other change is saved at once as the next revision; otherwise the pin waits in the draft for Save.
   */
  const pin = useCallback(
    (id: string, version: number): Promise<boolean> => {
      const clean = !dirty && conflict === null;
      const next = withNode(history.present, id, { version });
      change(next);
      return clean ? write(next, base.revision) : Promise.resolve(false);
    },
    [dirty, conflict, history.present, base.revision, change, write],
  );

  /** Drop the draft for the system as the hub holds it now. */
  const loadLatest = useCallback(async () => {
    const held = await queryClient.fetchQuery({ ...systemQuery(browserApi, project, system.slug), staleTime: 0 });
    const next = specOf(held.spec);
    setSeen(held.revision);
    setBase({ revision: held.revision, spec: next });
    setHistory(historyOf(next));
    setProblems(held.problems);
    setSelection(null);
    setConflict(null);
    setSave({ status: "idle" });
  }, [queryClient, project, system.slug]);

  /** Keep the draft and save it over the revision written meanwhile on the next Save. */
  const keepMine = useCallback(() => {
    if (conflict?.revision == null) return;
    const revision = conflict.revision;
    const held = queryClient.getQueryData(systemQuery(browserApi, project, system.slug).queryKey);
    setSeen(revision);
    setBase({ revision, spec: held ? specOf(held.spec) : base.spec });
    setConflict(null);
    setSave({ status: "idle" });
  }, [conflict, queryClient, project, system.slug, base.spec]);

  return {
    spec,
    revision: base.revision,
    dirty,
    canUndo: history.past.length > 0,
    canRedo: history.future.length > 0,
    undo: () => setHistory(undo),
    redo: () => setHistory(redo),
    change,
    selection,
    select: setSelection,
    problems,
    checking,
    save,
    saveDraft,
    pin,
    conflict,
    loadLatest,
    keepMine,
    latest,
    agents: agents.data ?? [],
  };
}

export type Designer = ReturnType<typeof useDesigner>;
