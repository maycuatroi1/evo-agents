"use client";

import { useQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useState } from "react";

import { Segmented } from "@/components/data/segmented";
import { InlineError } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { Agent, AgentEdit, Axis } from "@/lib/agents";
import { MAX_DESCRIPTION_CHARS, MAX_INSTRUCTIONS_CHARS, MAX_NAME_CHARS, MAX_STARTER_CHARS, MAX_STARTERS } from "@/lib/agent-rules";
import { browserApi } from "@/lib/api/browser";

import { AxisField } from "./axis-field";
import { Field } from "./form-parts";
import { useAgentFailure, useEditAgent } from "./hooks";
import { type AgentDraft, draftOf, draftProblems, editBody, type FieldProblem, hasProblems } from "./model";
import { agentOptionsQuery } from "./options";

/** The axes an agent's config in a system shows: what it may run on, use and load. The others stay in Edit agent. */
const CONFIG_AXES: readonly Axis[] = ["model", "skill", "tool"];
type RunsOn = Agent["runs_on"];

/** The PATCH of the config: the form's changes (`editBody`) and where its runs run; null when nothing changed. */
export function configBody(draft: AgentDraft, runsOn: RunsOn, agent: Agent): AgentEdit | null {
  const body = editBody(draft, agent) ?? {};
  if (runsOn !== agent.runs_on) body.runs_on = runsOn;
  return Object.keys(body).length ? { ...body, if_version: agent.version } : null;
}

type Props = {
  project: string;
  agent: Agent;
  /** Whether the visitor may change the agent: an admin of the project. Otherwise the fields only read. */
  canChange: boolean;
  /** Called with the agent as the hub saved it, at its new version. */
  onSaved: (agent: Agent) => void;
};

/**
 * An agent's config in a side panel (the designer of an agent system): its name, description, instructions, where
 * its runs run, the models, skills and tools it may use, and the starters the chat offers. Saving writes the agent's
 * next version against the version it opened on, as Edit agent does; the caller then pins that version. Mounted again
 * for each agent and version (its `key`), so it starts from the agent as the hub holds it.
 */
export function AgentConfigPanel({ project, agent, canChange, onSaved }: Props) {
  const t = useTranslations("agents.form");
  const tc = useTranslations("agents.config");
  const tp = useTranslations("agents.form.problems");
  const format = useFormatter();
  const failure = useAgentFailure();
  const options = useQuery(agentOptionsQuery(browserApi, project));
  const edit = useEditAgent(project, agent.handle);
  const [draft, setDraft] = useState<AgentDraft>(() => draftOf(agent));
  const [runsOn, setRunsOn] = useState<RunsOn>(agent.runs_on);
  const [tried, setTried] = useState(false);
  const problems = draftProblems(draft, { creating: false, projects: [] });
  const shown = tried ? problems : { axes: {} };
  const changes = configBody(draft, runsOn, agent);
  const disabled = !canChange || edit.isPending;
  const set = (patch: Partial<AgentDraft>) => {
    edit.reset();
    setDraft((current) => ({ ...current, ...patch }));
  };
  const fieldText = (problem: FieldProblem | undefined, max: number) => (problem ? tp(problem, { max }) : null);
  const count = (text: string, max: number) => t("chars", { count: format.number([...text].length), max: format.number(max) });

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (edit.isPending || !changes) return;
    setTried(true);
    if (hasProblems(problems)) return;
    try {
      onSaved(await edit.mutateAsync(changes));
    } catch {
      // Shown above the button, from the mutation's state.
    }
  };
  const error = edit.isError ? failure(edit.error, { 409: t("errors.moved", { handle: agent.handle }) }) : null;

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex flex-col gap-4" aria-busy={edit.isPending || undefined} data-testid="agent-config">
      <Field label={t("name")} error={fieldText(shown.name, MAX_NAME_CHARS)} testId="agent-config-name">
        {({ id, describedBy, invalid }) => (
          <Input id={id} value={draft.name} onChange={(e) => set({ name: e.target.value })} disabled={disabled} aria-invalid={invalid || undefined} aria-describedby={describedBy || undefined} data-testid="agent-config-name-input" />
        )}
      </Field>
      <Field label={t("description")} optional={t("optional")} error={fieldText(shown.description, MAX_DESCRIPTION_CHARS)} testId="agent-config-description">
        {({ id, describedBy, invalid }) => (
          <Textarea id={id} rows={2} value={draft.description} onChange={(e) => set({ description: e.target.value })} disabled={disabled} aria-invalid={invalid || undefined} aria-describedby={describedBy || undefined} className="max-h-32" data-testid="agent-config-description-input" />
        )}
      </Field>
      {agent.is_default ? null : (
        <Field
          label={t("instructions")}
          optional={t("optional")}
          hint={count(draft.instructions, MAX_INSTRUCTIONS_CHARS)}
          error={fieldText(shown.instructions, MAX_INSTRUCTIONS_CHARS)}
          testId="agent-config-instructions"
        >
          {({ id, describedBy, invalid }) => (
            <Textarea id={id} rows={6} value={draft.instructions} onChange={(e) => set({ instructions: e.target.value })} disabled={disabled} aria-invalid={invalid || undefined} aria-describedby={describedBy || undefined} className="max-h-[40dvh] font-mono text-[13px]" data-testid="agent-config-instructions-input" />
          )}
        </Field>
      )}
      <div className="flex flex-col gap-1.5" data-testid="agent-config-runs-on">
        <span className="text-sm font-medium">{tc("runsOn")}</span>
        {canChange ? (
          <Segmented<RunsOn>
            label={tc("runsOn")}
            value={runsOn}
            onChange={(value) => {
              edit.reset();
              setRunsOn(value);
            }}
            options={[
              { value: "worker", label: tc("worker"), testId: "agent-config-runs-on-worker" },
              { value: "hub", label: tc("hub"), testId: "agent-config-runs-on-hub" },
            ]}
          />
        ) : (
          <span className="text-sm">{runsOn === "hub" ? tc("hub") : tc("worker")}</span>
        )}
        <p className="text-xs text-pretty text-muted-foreground">{tc("runsOnHint")}</p>
      </div>
      {agent.is_default
        ? null
        : CONFIG_AXES.map((axis) => (
            <AxisField
              key={axis}
              axis={axis}
              value={draft.permissions[axis]}
              onChange={(rule) => set({ permissions: { ...draft.permissions, [axis]: rule } })}
              options={options.data?.byAxis[axis] ?? []}
              loading={options.isPending}
              failed={options.isError || (options.data?.failed.includes(axis) ?? false)}
              problem={shown.axes[axis]}
              disabled={disabled}
            />
          ))}
      <Field
        label={t("starters")}
        optional={t("optional")}
        hint={t("startersHint", { max: format.number(MAX_STARTERS), chars: format.number(MAX_STARTER_CHARS) })}
        error={shown.starters ? t(`startersProblems.${shown.starters}`, { max: format.number(shown.starters === "long" ? MAX_STARTER_CHARS : MAX_STARTERS) }) : null}
        testId="agent-config-starters"
      >
        {({ id, describedBy, invalid }) => (
          <Textarea id={id} rows={3} value={draft.starters} onChange={(e) => set({ starters: e.target.value })} disabled={disabled} aria-invalid={invalid || undefined} aria-describedby={describedBy || undefined} className="max-h-32" data-testid="agent-config-starters-input" />
        )}
      </Field>
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      {canChange ? (
        <div className="flex items-center justify-end gap-3">
          {changes ? null : (
            <p className="mr-auto text-xs text-muted-foreground" aria-live="polite">
              {t("unchanged")}
            </p>
          )}
          <Button type="submit" busy={edit.isPending} disabled={!changes} data-testid="agent-config-save">
            {edit.isPending ? t("saving") : tc("save")}
          </Button>
        </div>
      ) : null}
      {canChange ? <p className="text-xs text-pretty text-muted-foreground">{tc("saveHint", { version: agent.version })}</p> : null}
    </form>
  );
}
