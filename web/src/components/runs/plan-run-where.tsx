"use client";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Network, Server, TriangleAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback } from "react";

import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { planKeys } from "@/lib/plan-queries";
import { type Spec, specOf } from "@/lib/systems";
import { systemKeys, systemQuery, systemsQuery } from "@/lib/systems-queries";

import { Choice, Section } from "./dispatch-fields";
import {
  dispatchPlanToSystem,
  overLimit,
  planRunChoiceKey,
  type PlanRunChoiceIn,
  savePlanRunChoice,
  type PlanTarget,
  type SystemOption,
  systemOptions,
} from "./plan-run-choice";
import { runKeys } from "./queries";

/**
 * The plan handed to an agent system from the browser, as `useDispatchPlanRun`: either way the project's runs, the plan
 * (its readiness says the trace that holds it) and the system's traces are read again.
 */
export function useDispatchPlanInSystem(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ planId, system }: { planId: string; system: string }) => dispatchPlanToSystem(browserApi(), project, planId, system),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: (_data, _error, { planId, system }) =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: runKeys.all(project) }),
        queryClient.invalidateQueries({ queryKey: planKeys.one(project, planId) }),
        queryClient.invalidateQueries({ queryKey: systemKeys.traces(project, system) }),
      ]),
  });
}

/**
 * Remember the visitor's Run plan choice in the project, after a Run the hub took. A failure is not shown: the run is
 * queued either way, and the dialog only opens on the hub's defaults the next time.
 */
export function useSavePlanRunChoice(project: string) {
  const queryClient = useQueryClient();
  return useCallback(
    (body: PlanRunChoiceIn) =>
      savePlanRunChoice(browserApi(), project, body).then(
        (saved) => queryClient.setQueryData(planRunChoiceKey(project), saved),
        () => queryClient.invalidateQueries({ queryKey: planRunChoiceKey(project) }),
      ),
    [project, queryClient],
  );
}

/**
 * The systems Run plan may hand the plan to: the project's systems with an entry agent, with the purpose and limits of
 * each once its spec is read. `loading` holds until the list is read (or could not be: then only a worker is offered).
 */
export function useSystemOptions(project: string): { options: SystemOption[]; loading: boolean } {
  const list = useQuery(systemsQuery(browserApi, project));
  const entries = (list.data ?? []).filter((system) => system.entry_agent);
  const specs = useQueries({ queries: entries.map((system) => ({ ...systemQuery(browserApi, project, system.slug), staleTime: 15_000 })) });
  const found = new Map<string, Spec>();
  for (const { data } of specs) if (data) found.set(data.slug, specOf(data.spec));
  return { options: systemOptions(list.data ?? [], found), loading: list.isPending };
}

/**
 * "Where it runs" in Run plan: on a worker, as a plan run with the fields below, or handed to one of `options`, each
 * with its purpose, entry agent and the limits of its trace, and a warning when the plan's pending steps may need more
 * runs than one trace may take. Shown only when the project has such a system.
 */
export function WhereField({
  options,
  target,
  system,
  pending,
  onChange,
  disabled = false,
}: {
  options: readonly SystemOption[];
  target: PlanTarget;
  system: string | null;
  /** The plan's pending steps, for the warning; null until read. */
  pending: number | null;
  onChange: (target: PlanTarget, system: string | null) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("runs.planRun.where");
  const format = useFormatter();
  if (options.length === 0) return null;
  return (
    <Section legend={t("legend")} info={t("info")} testId="plan-run-where">
      <div className="flex flex-col gap-2">
        <Choice
          type="radio"
          name="plan-run-where"
          value="worker"
          checked={target === "worker"}
          onChange={() => onChange("worker", null)}
          disabled={disabled}
          title={
            <span className="inline-flex items-center gap-1.5 font-medium">
              <Server className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              {t("worker")}
            </span>
          }
          hint={t("workerHint")}
          testId="plan-run-where-worker"
        />
        {options.map((option) => {
          const over = pending === null ? null : overLimit(pending, option.limits);
          return (
            <Choice
              key={option.slug}
              type="radio"
              name="plan-run-where"
              value={option.slug}
              checked={target === "system" && system === option.slug}
              onChange={() => onChange("system", option.slug)}
              disabled={disabled}
              title={
                <span className="inline-flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5">
                  <Network className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                  <span className="font-medium">{option.name}</span>
                  {option.name !== option.slug ? <span className="font-mono text-xs text-muted-foreground">{option.slug}</span> : null}
                </span>
              }
              hint={
                <span className="flex flex-col gap-1">
                  {option.purpose ? <span className="line-clamp-2 text-pretty">{option.purpose}</span> : null}
                  <span className="flex flex-wrap gap-x-3 gap-y-0.5">
                    <span>{t.rich("entry", { handle: option.entry, name: (chunks) => <span className="font-mono text-foreground">{chunks}</span> })}</span>
                    {option.limits ? (
                      <span className="tabular-nums">
                        {t("limits", { runs: option.limits.trace_runs, usd: format.number(option.limits.trace_usd, { style: "currency", currency: "USD" }) })}
                      </span>
                    ) : null}
                  </span>
                  {over ? (
                    <span className="flex items-start gap-1.5 text-attention" data-testid="plan-run-where-over">
                      <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden="true" />
                      <span className="text-pretty">{t("over", { steps: pending ?? 0, runs: over.runs, limit: over.limit })}</span>
                    </span>
                  ) : null}
                </span>
              }
              testId={`plan-run-where-system-${option.slug}`}
            />
          );
        })}
      </div>
    </Section>
  );
}
