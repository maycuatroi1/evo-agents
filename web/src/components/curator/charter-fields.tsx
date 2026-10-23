"use client";

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { cn } from "@/lib/utils";

import type { CharterErrors, CharterField, CharterForm, CharterProblem, RoleForm } from "./charter-model";
import { LIMITS, RUNTIMES } from "./vocab";

/**
 * The parts the charter's form is built of (charter-form.tsx): a field with its label, hint and problem, a group of
 * fields as a card, a role's runtime and model, and the group of the night's own runs beside the review, the replays
 * of the Curator's changes, the history run and the weekly probes.
 */

export const fieldId = (base: string, field: CharterField) => `${base}-${field}`;

/** A field's problem in words. */
export function useProblemText() {
  const t = useTranslations("curator.charter.errors");
  return (problem: CharterProblem) => {
    switch (problem.code) {
      case "money":
        return t("money", { max: problem.max });
      case "moneyOrZero":
        return t("moneyOrZero", { max: problem.max });
      case "whole":
        return t("whole", { min: problem.min, max: problem.max });
      case "goalId":
        return t("goalId", { line: problem.line });
      case "goalWhat":
        return t("goalWhat", { line: problem.line, max: problem.max });
      case "goalRepeated":
        return t("goalRepeated", { id: problem.id });
      case "tooMany":
        return t("tooMany", { max: problem.max });
      case "planId":
        return t("planId", { line: problem.line });
      case "lineTooLong":
        return t("lineTooLong", { line: problem.line, max: problem.max });
      case "control":
        return t("control", { line: problem.line });
      case "model":
        return t("model", { max: problem.max });
      default:
        return t(problem.code);
    }
  };
}

type FieldProps = {
  base: string;
  field: CharterField;
  label: string;
  hint?: ReactNode;
  errors: CharterErrors;
  className?: string;
  children: (props: { id: string; "aria-invalid"?: true; "aria-describedby"?: string }) => ReactNode;
};

/** A label over its control, then its hint or, once the form was sent, its problem in `danger`. */
export function Field({ base, field, label, hint, errors, className, children }: FieldProps) {
  const text = useProblemText();
  const id = fieldId(base, field);
  const problem = errors[field];
  const described = problem || hint ? `${id}-hint` : undefined;
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)} data-field={field}>
      <Label htmlFor={id}>{label}</Label>
      {children({ id, "aria-invalid": problem ? true : undefined, "aria-describedby": described })}
      {problem ? (
        <p id={`${id}-hint`} className="text-xs text-danger" data-testid={`charter-error-${field}`}>
          {text(problem)}
        </p>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-xs text-fg-subtle">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

/**
 * A group of fields as a card: its legend as the card's head. Two fields to a row from md, four from 2xl, where two
 * would stretch a time or a number across half a wide window; a field that spans two keeps half the row.
 */
export function Group({ title, description, children, testId }: { title: string; description?: string; children: ReactNode; testId: string }) {
  return (
    <fieldset className="min-w-0 rounded-md border bg-card shadow-raised" data-testid={testId}>
      <legend className="sr-only">{title}</legend>
      <div className="flex min-h-12 flex-col justify-center gap-0.5 border-b px-4 py-2" aria-hidden="true">
        <span className="text-[15px] leading-[22px] font-semibold">{title}</span>
        {description ? <span className="text-xs text-fg-subtle">{description}</span> : null}
      </div>
      <div className="grid gap-4 px-4 py-4 md:grid-cols-2 2xl:grid-cols-4">{children}</div>
    </fieldset>
  );
}

export function RoleFields({
  base,
  role,
  value,
  onChange,
  errors,
}: {
  base: string;
  role: "reviewer" | "builder" | "judge";
  value: RoleForm;
  onChange: (next: RoleForm) => void;
  errors: CharterErrors;
}) {
  const t = useTranslations("curator.charter");
  const tRuntime = useTranslations("runs.runtime");
  const selectId = `${base}-${role}-runtime`;
  return (
    <div className="grid min-w-0 gap-3 rounded-sm border px-3 py-3 md:col-span-2 md:grid-cols-2">
      <p className="text-[13px] font-medium md:col-span-2">{t(`roles.${role}`)}</p>
      <div className="flex min-w-0 flex-col gap-1.5">
        <Label htmlFor={selectId}>{t("fields.runtime")}</Label>
        <NativeSelect
          id={selectId}
          value={value.runtime}
          onChange={(event) => onChange({ ...value, runtime: event.target.value as RoleForm["runtime"] })}
          className="w-full"
          data-testid={`charter-${role}-runtime`}
        >
          {RUNTIMES.map((runtime) => (
            <NativeSelectOption key={runtime} value={runtime}>
              {tRuntime(runtime)}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </div>
      <Field base={base} field={`${role}Model`} label={t("fields.model")} hint={t("hints.model")} errors={errors}>
        {(props) => (
          <Input
            {...props}
            value={value.model}
            onChange={(event) => onChange({ ...value, model: event.target.value })}
            placeholder={t("placeholders.model")}
            className="font-mono"
            data-testid={`charter-${role}-model`}
          />
        )}
      </Field>
    </div>
  );
}

/**
 * The night's own runs beside the review: how many holdout tasks each change of a skill or an AGENTS.md is replayed
 * on and what a night's replays may cost (none without a cap), what the history run may cost and on which model, and
 * the sessions of each arm of a probe and what a week's probes may cost (none without a cap).
 */
export function NightRunsGroup({
  base,
  form,
  set,
  errors,
}: {
  base: string;
  form: CharterForm;
  set: <K extends keyof CharterForm>(key: K, value: CharterForm[K]) => void;
  errors: CharterErrors;
}) {
  const t = useTranslations("curator.charter");
  return (
    <Group title={t("groups.nightRuns")} description={t("groupHints.nightRuns")} testId="charter-night-runs">
      <Field base={base} field="replayTasks" label={t("fields.replayTasks")} hint={t("hints.replayTasks", LIMITS.replayTasks)} errors={errors}>
        {(props) => <Input {...props} type="number" inputMode="numeric" value={form.replayTasks} onChange={(event) => set("replayTasks", event.target.value)} data-testid="charter-replay-tasks" />}
      </Field>
      <Field base={base} field="replayBudget" label={t("fields.replayBudget")} hint={t("hints.replayBudget")} errors={errors}>
        {(props) => (
          <Input {...props} type="number" inputMode="decimal" min={0} step="0.01" value={form.replayBudget} onChange={(event) => set("replayBudget", event.target.value)} data-testid="charter-replay-budget" />
        )}
      </Field>
      <Field base={base} field="historyBudget" label={t("fields.historyBudget")} hint={t("hints.historyBudget")} errors={errors}>
        {(props) => (
          <Input {...props} type="number" inputMode="decimal" min={0} step="0.01" value={form.historyBudget} onChange={(event) => set("historyBudget", event.target.value)} data-testid="charter-history-budget" />
        )}
      </Field>
      <Field base={base} field="historyModel" label={t("fields.historyModel")} hint={t("hints.historyModel")} errors={errors}>
        {(props) => (
          <Input
            {...props}
            value={form.historyModel}
            onChange={(event) => set("historyModel", event.target.value)}
            placeholder={t("placeholders.historyModel")}
            className="font-mono"
            data-testid="charter-history-model"
          />
        )}
      </Field>
      <Field base={base} field="probeK" label={t("fields.probeK")} hint={t("hints.probeK", LIMITS.probeK)} errors={errors}>
        {(props) => <Input {...props} type="number" inputMode="numeric" value={form.probeK} onChange={(event) => set("probeK", event.target.value)} data-testid="charter-probe-k" />}
      </Field>
      <Field base={base} field="probeBudget" label={t("fields.probeBudget")} hint={t("hints.probeBudget", { max: LIMITS.probeBudgetUsd })} errors={errors}>
        {(props) => (
          <Input {...props} type="number" inputMode="decimal" min={0} step="0.01" value={form.probeBudget} onChange={(event) => set("probeBudget", event.target.value)} data-testid="charter-probe-budget" />
        )}
      </Field>
    </Group>
  );
}
