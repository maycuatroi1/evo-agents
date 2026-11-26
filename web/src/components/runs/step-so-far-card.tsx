"use client";

import { useQuery } from "@tanstack/react-query";
import { Check, CircleAlert, ListChecks } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useMemo } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { browserApi } from "@/lib/api/browser";
import { agentQuery } from "@/lib/agent-queries";
import { cn } from "@/lib/utils";

import { isActiveState, type Run, type RunEvent } from "./queries";
import { usePlanProgress } from "./run-plan-steps";
import type { RunTask } from "./run-tasks";
import { doneWhenOf } from "./step-criteria";
import { type SoFarItem, type SoFarSource, stepSoFar } from "./step-so-far";

const NEUTRAL = "#475569";
const COLUMNS = ["done", "now", "next"] as const;

/** The face of whoever does an item of Now: the run's agent for the main agent, a letter on a neutral colour for a subagent. */
function Doer({ run, item }: { run: Run; item: SoFarItem }) {
  const profile = item.agent?.id ? null : run.profile;
  const found = useQuery({ ...agentQuery(browserApi, run.project, profile ?? ""), enabled: Boolean(profile) });
  if (!item.agent) return null;
  const name = item.agent.name ?? found.data?.name ?? run.profile ?? "Agent";
  const agent = item.agent.id ? { name, handle: name, color: NEUTRAL, icon_url: null } : (found.data ?? { name, handle: run.profile ?? "main", color: NEUTRAL, icon_url: null });
  return <AgentAvatar agent={agent} size="sm" />;
}

function Item({ run, item, onOpenAgent }: { run: Run; item: SoFarItem; onOpenAgent: (id: string) => void }) {
  const t = useTranslations("runs.detail.soFar");
  const body = (
    <>
      {item.agent ? <Doer run={run} item={item} /> : null}
      <span className="min-w-0 flex-1">
        <span className="line-clamp-3 block text-[15px] leading-6 text-pretty [overflow-wrap:anywhere]">{item.text}</span>
        <span className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
          {item.failed ? <CircleAlert className="size-3.5 text-danger" aria-hidden="true" /> : null}
          {item.failed ? <span className="text-danger">{t("failed")}</span> : null}
          <span data-testid="so-far-source" data-source={item.source}>
            {t(`source.${item.source}`)}
          </span>
        </span>
      </span>
    </>
  );
  const id = item.agent?.id ?? null;
  return (
    <li className="flex min-w-0" data-testid="so-far-item" data-source={item.source}>
      {id ? (
        <button
          type="button"
          onClick={() => onOpenAgent(id)}
          aria-label={t("openAgent", { name: item.agent?.name ?? "" })}
          className="flex min-h-11 w-full min-w-0 cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 text-left hover:bg-accent focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
        >
          {body}
        </button>
      ) : (
        <div className="flex min-w-0 flex-1 items-start gap-2 px-2 py-1.5">{body}</div>
      )}
    </li>
  );
}

/**
 * "Step 2 so far", under the run's head: three columns, Done, Now and Next, for the step the run works on. Built from
 * the main agent's latest checklist, the subagents' results and the step's `done_when`, in that order, with no model
 * call; each item says which, and the line under the title names the sources and when they last changed. With none of
 * them it says so rather than make something up. The columns stack on a phone.
 */
export function StepSoFar({ run, events, tasks, onOpenAgent }: { run: Run; events: readonly RunEvent[]; tasks: readonly RunTask[]; onOpenAgent: (id: string) => void }) {
  const t = useTranslations("runs.detail.soFar");
  const format = useFormatter();
  const ids = useId();
  const { current, view } = usePlanProgress(run);
  const active = isActiveState(run.state);
  const key = run.step_key ?? current?.key ?? null;
  const step = (key !== null ? view?.steps.find((candidate) => candidate.key === key) : null) ?? null;
  const doneWhen = useMemo(() => (step ? doneWhenOf(step) : []), [step]);
  const so = useMemo(() => stepSoFar({ events, tasks, active, doneWhen }), [events, tasks, active, doneWhen]);
  if (key === null) return null;
  const sources = so.sources.map((source: SoFarSource) => t(`from.${source}`));
  const line = so.empty
    ? null
    : t("line", { sources: format.list(sources, { type: "conjunction" }), time: so.at ? format.dateTime(new Date(so.at), { hour: "2-digit", minute: "2-digit" }) : "" });
  return (
    <section className="flex min-w-0 flex-col rounded-md border bg-card shadow-raised" aria-labelledby={`${ids}-title`} data-testid="step-so-far">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 border-b px-4 py-3">
        <h2 id={`${ids}-title`} className="flex items-center gap-2 text-[15px] leading-[22px] font-semibold">
          <ListChecks className="size-4 text-muted-foreground" aria-hidden="true" />
          {t("title", { step: key })}
        </h2>
        {line ? (
          <p className="text-xs text-muted-foreground" data-testid="so-far-line">
            {line}
          </p>
        ) : null}
      </div>
      {so.empty ? (
        <p className="px-4 py-3 text-sm text-muted-foreground" data-testid="so-far-empty">
          {t("empty")}
        </p>
      ) : (
        <div className="grid min-w-0 divide-y md:grid-cols-3 md:divide-x md:divide-y-0">
          {COLUMNS.map((column) => (
            <div key={column} className="flex min-w-0 flex-col gap-1 px-2 py-3" data-testid={`so-far-${column}`}>
              <h3 className="flex items-center gap-1.5 px-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                {column === "done" ? <Check className="size-3.5" aria-hidden="true" /> : null}
                {t(`column.${column}`)}
                <span className={cn("font-normal tabular-nums")}>{so[column].length}</span>
              </h3>
              {so[column].length === 0 ? (
                <p className="px-2 py-1.5 text-sm text-muted-foreground">{t("none")}</p>
              ) : (
                <ul className="flex flex-col">
                  {so[column].map((item) => (
                    <Item key={item.key} run={run} item={item} onOpenAgent={onOpenAgent} />
                  ))}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
