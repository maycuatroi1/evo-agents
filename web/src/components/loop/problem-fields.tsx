"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import type { ProblemAggregate } from "@/lib/problem-queries";
import { DEFAULT_WINDOW_DAYS, PROBLEM_AGGREGATES, PROBLEM_LIMITS } from "@/lib/problem-writes";
import { signalKeysQuery } from "@/lib/signal-queries";

import type { ProblemField, ProblemForm } from "./problem-model";

const FIELD = "flex min-w-0 flex-col gap-1.5";

/** A field's label, its control, and its help or the error that replaces the help once the writer tried to save. */
function Field({
  id,
  label,
  required = false,
  help,
  error,
  children,
}: {
  id: string;
  label: string;
  required?: boolean;
  help?: string;
  error?: string | null;
  children: ReactNode;
}) {
  const t = useTranslations("problems.form");
  return (
    <div className={FIELD}>
      <Label htmlFor={id}>
        {label}
        {required ? <span className="font-normal text-fg-subtle">{t("required")}</span> : null}
      </Label>
      {children}
      {error ? (
        <p id={`${id}-error`} className="text-xs font-medium text-danger">
          {error}
        </p>
      ) : help ? (
        <p id={`${id}-help`} className="text-xs text-fg-subtle">
          {help}
        </p>
      ) : null}
    </div>
  );
}

/**
 * The fields of a problem, for a new one and for an edit: what hurts, who, when and how they cope today (each said),
 * then an optional metric (a key of kind number of the project's signals, summed up over a window, of one tenant),
 * its rank and owner, and a note for the history. `wrong` names the fields to mark once the writer tried to save.
 */
export function ProblemFields({
  project,
  form,
  onChange,
  wrong,
  isNew,
}: {
  project: string;
  form: ProblemForm;
  onChange: (form: ProblemForm) => void;
  wrong: readonly ProblemField[];
  isNew: boolean;
}) {
  const t = useTranslations("problems.form");
  const ids = useId();
  const keys = useQuery(signalKeysQuery(browserApi, project));
  const numbers = (keys.data ?? []).filter((key) => key.kind === "number").map((key) => key.name);
  const names = [...new Set([...numbers, ...(form.metricKey ? [form.metricKey] : [])])].sort();
  const set = <K extends keyof ProblemForm>(field: K, value: ProblemForm[K]) => onChange({ ...form, [field]: value });
  const error = (field: ProblemField) => (wrong.includes(field) ? t(`errors.${field}`) : null);
  const described = (field: ProblemField, help: boolean) => (wrong.includes(field) ? `${ids}-${field}-error` : help ? `${ids}-${field}-help` : undefined);
  const text = (field: "statement" | "who" | "when" | "workaround", rows: number, limit: number) => (
    <Field id={`${ids}-${field}`} label={t(field)} required help={t(`${field}Help`)} error={error(field)}>
      <Textarea
        id={`${ids}-${field}`}
        value={form[field]}
        onChange={(event) => set(field, event.target.value)}
        rows={rows}
        maxLength={limit}
        aria-required="true"
        aria-invalid={wrong.includes(field) || undefined}
        aria-describedby={described(field, true)}
        data-testid={`problem-${field}-input`}
      />
    </Field>
  );
  return (
    <div className="flex flex-col gap-5" data-testid="problem-fields">
      {text("statement", 3, PROBLEM_LIMITS.statement)}
      <div className="grid gap-5 sm:grid-cols-2">
        {text("who", 2, PROBLEM_LIMITS.text)}
        {text("when", 2, PROBLEM_LIMITS.text)}
      </div>
      {text("workaround", 2, PROBLEM_LIMITS.text)}
      <fieldset className="flex min-w-0 flex-col gap-4">
        <legend className="mb-1 text-[13px] font-semibold text-foreground">{t("metric")}</legend>
        <p className="-mt-1 text-xs text-fg-subtle">{t("metricHelp")}</p>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field id={`${ids}-key`} label={t("key")} help={names.length === 0 ? t("noKeys") : undefined}>
            <NativeSelect
              id={`${ids}-key`}
              value={form.metricKey}
              onChange={(event) => set("metricKey", event.target.value)}
              className="w-full"
              aria-describedby={names.length === 0 ? `${ids}-key-help` : undefined}
              data-testid="problem-key-input"
            >
              <NativeSelectOption value="">{t("noMetric")}</NativeSelectOption>
              {names.map((name) => (
                <NativeSelectOption key={name} value={name}>
                  {name}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </Field>
          <Field id={`${ids}-aggregate`} label={t("aggregate")}>
            <NativeSelect
              id={`${ids}-aggregate`}
              value={form.aggregate}
              onChange={(event) => set("aggregate", event.target.value as ProblemAggregate)}
              disabled={!form.metricKey}
              className="w-full"
              data-testid="problem-aggregate-input"
            >
              {PROBLEM_AGGREGATES.map((aggregate) => (
                <NativeSelectOption key={aggregate} value={aggregate}>
                  {t(`aggregates.${aggregate}`)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </Field>
          <Field id={`${ids}-windowDays`} label={t("windowDays")} help={t("windowDaysHelp", { days: DEFAULT_WINDOW_DAYS, max: PROBLEM_LIMITS.windowDays })} error={error("windowDays")}>
            <Input
              id={`${ids}-windowDays`}
              inputMode="numeric"
              value={form.windowDays}
              onChange={(event) => set("windowDays", event.target.value)}
              disabled={!form.metricKey}
              placeholder={String(DEFAULT_WINDOW_DAYS)}
              aria-invalid={wrong.includes("windowDays") || undefined}
              aria-describedby={described("windowDays", true)}
              data-testid="problem-window-input"
            />
          </Field>
          <Field id={`${ids}-tenant`} label={t("tenant")} help={t("tenantHelp")} error={error("tenant")}>
            <Input
              id={`${ids}-tenant`}
              value={form.tenant}
              onChange={(event) => set("tenant", event.target.value)}
              disabled={!form.metricKey}
              maxLength={PROBLEM_LIMITS.tenant}
              aria-invalid={wrong.includes("tenant") || undefined}
              aria-describedby={described("tenant", true)}
              data-testid="problem-tenant-input"
            />
          </Field>
        </div>
      </fieldset>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field id={`${ids}-rank`} label={t("rank")} help={isNew ? t("rankNewHelp") : t("rankHelp")} error={error("rank")}>
          <Input
            id={`${ids}-rank`}
            inputMode="numeric"
            value={form.rank}
            onChange={(event) => set("rank", event.target.value)}
            aria-invalid={wrong.includes("rank") || undefined}
            aria-describedby={described("rank", true)}
            data-testid="problem-rank-input"
          />
        </Field>
        <Field id={`${ids}-owner`} label={t("owner")} help={isNew ? t("ownerNewHelp") : t("ownerHelp")} error={error("owner")}>
          <Input
            id={`${ids}-owner`}
            value={form.owner}
            onChange={(event) => set("owner", event.target.value)}
            autoComplete="off"
            spellCheck={false}
            aria-invalid={wrong.includes("owner") || undefined}
            aria-describedby={described("owner", true)}
            data-testid="problem-owner-input"
          />
        </Field>
      </div>
      <Field id={`${ids}-note`} label={t("note")} help={t("noteHelp")}>
        <Input
          id={`${ids}-note`}
          value={form.note}
          onChange={(event) => set("note", event.target.value)}
          maxLength={PROBLEM_LIMITS.note}
          aria-describedby={`${ids}-note-help`}
          data-testid="problem-note-input"
        />
      </Field>
    </div>
  );
}
