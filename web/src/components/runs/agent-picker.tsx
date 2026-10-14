"use client";

import { TriangleAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { type Agent, type AgentProblem, allowed, AXES, type Axis } from "@/lib/agents";

// The agent picker of the dispatch forms, loaded when a form opens (agent-field.tsx): the project's agents with the
// face of the one picked, what it limits, and why it refuses what the form asks.

/** What a form knows of the agents (`useAgentChoice` of agent-field.tsx) and the one picked. */
export type AgentChoice = {
  agents: Agent[];
  agent: Agent | null;
  choose: (handle: string) => void;
  loading: boolean;
  failed: boolean;
};

const RUNTIME_NAMES = { "claude-code": true, opencode: true, codex: true } as const;
type RuntimeName = keyof typeof RUNTIME_NAMES;

/** What an allowlist names, as the field says it: runtimes by their names, the rest as written. */
function useItems() {
  const tRuntime = useTranslations("runs.runtime");
  const format = useFormatter();
  return (axis: Axis, items: string[]) =>
    format.list(axis === "runtime" ? items.map((item) => (item in RUNTIME_NAMES ? tRuntime(item as RuntimeName) : item)) : items, { type: "conjunction" });
}

/** The hint under the picker: what the agent is, then each axis it limits and to what. */
function useAgentHint() {
  const t = useTranslations("runs.agent");
  const format = useFormatter();
  const items = useItems();
  return (agent: Agent): string => {
    if (agent.is_default) return t("defaultHint");
    const limits = AXES.flatMap((axis) => {
      const allow = allowed(agent.permissions, axis);
      if (allow === null) return [];
      return [t("limit", { axis: t(`axes.${axis}`), items: allow.length ? items(axis, allow) : t("nothing") })];
    });
    const what = limits.length ? t("limits", { limits: format.list(limits, { type: "conjunction" }) }) : t("noLimits");
    return agent.description.trim() ? `${agent.description.trim()} ${what}` : what;
  };
}

/** Why the agent refuses the dispatch, in words; the hub refuses it the same way (422). */
export function useAgentProblemText() {
  const t = useTranslations("runs.agent.problem");
  const items = useItems();
  const tRuntime = useTranslations("runs.runtime");
  return (agent: Agent, problem: AgentProblem): string => {
    const handle = agent.handle;
    switch (problem.kind) {
      case "deleted":
        return t("deleted", { handle });
      case "runtime":
        if (problem.allow.length === 0) return t("runtimeNone", { handle });
        return t("runtime", {
          handle,
          runtime: problem.runtime in RUNTIME_NAMES ? tRuntime(problem.runtime as RuntimeName) : problem.runtime,
          allow: items("runtime", problem.allow),
        });
      case "model":
        return problem.allow.length ? t("model", { handle, model: problem.model, allow: items("model", problem.allow) }) : t("modelNone", { handle });
      case "workers":
        return problem.allow.length ? t("workers", { handle, allow: items("worker", problem.allow) }) : t("workersNone", { handle });
      case "daemon":
        return t("daemon", { handle });
      case "skill":
        return t("skill", { handle, skill: problem.skill });
    }
  };
}

export function AgentField({ choice, problems, disabled = false }: { choice: AgentChoice; problems: AgentProblem[]; disabled?: boolean }) {
  const t = useTranslations("runs.agent");
  const ids = useId();
  const hint = useAgentHint();
  const problemText = useAgentProblemText();
  const { agents, agent, loading, failed } = choice;
  const described = [`${ids}-hint`, problems.length ? `${ids}-problems` : null].filter(Boolean).join(" ");
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="dispatch-agent">
      <label htmlFor={`${ids}-agent`} className="text-sm font-medium">
        {t("label")}
      </label>
      {loading ? (
        <Skeleton className="h-9 w-full sm:w-80" />
      ) : (
        <div className="flex min-w-0 items-center gap-2.5">
          {agent ? <AgentAvatar agent={agent} /> : null}
          <NativeSelect
            id={`${ids}-agent`}
            value={agent?.handle ?? ""}
            onChange={(event) => choice.choose(event.target.value)}
            disabled={disabled || agents.length === 0}
            aria-invalid={problems.length > 0 || undefined}
            aria-describedby={described}
            className="w-full min-w-0 sm:w-80 md:[&_select]:h-9"
            data-testid="dispatch-agent-select"
          >
            {agents.length === 0 ? <NativeSelectOption value="">{t("defaultOnly")}</NativeSelectOption> : null}
            {agents.map((candidate) => (
              <NativeSelectOption key={candidate.handle} value={candidate.handle}>
                {candidate.is_default
                  ? t("defaultOption", { name: candidate.name, handle: candidate.handle })
                  : t("option", { name: candidate.name, handle: candidate.handle })}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
      )}
      <p id={`${ids}-hint`} className="text-xs text-pretty text-muted-foreground [overflow-wrap:anywhere]" data-testid="dispatch-agent-hint">
        {failed ? t("failed") : agent ? hint(agent) : loading ? t("loading") : t("defaultOnlyHint")}
      </p>
      {agent && problems.length ? (
        <ul id={`${ids}-problems`} className="flex flex-col gap-1" data-testid="dispatch-agent-problems">
          {problems.map((problem) => (
            <li key={problem.kind} className="flex items-start gap-1.5 text-xs font-medium text-pretty text-danger" data-testid="dispatch-agent-problem" data-kind={problem.kind}>
              <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden="true" />
              {problemText(agent, problem)}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
