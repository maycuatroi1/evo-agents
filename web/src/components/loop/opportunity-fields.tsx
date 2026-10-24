"use client";

import { useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { OPPORTUNITY_LIMITS } from "@/lib/opportunity-writes";

import type { OpportunityField, OpportunityForm } from "./opportunity-model";

const FIELD = "flex min-w-0 flex-col gap-1.5";

/** A field's label, its control, and its help or the error that replaces the help once the writer tried to save. */
function Field({ id, label, required = false, help, error, children }: { id: string; label: string; required?: boolean; help?: string; error?: string | null; children: ReactNode }) {
  const t = useTranslations("opportunities.form");
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
 * The fields of an opportunity, for a new one and for an edit: what could be won, who gains, what makes it reachable
 * now, the window, the problems it answers, its rank and owner, and a note for the history. `wrong` names the fields to
 * mark once the writer tried to save.
 */
export function OpportunityFields({
  form,
  onChange,
  wrong,
  isNew,
}: {
  form: OpportunityForm;
  onChange: (form: OpportunityForm) => void;
  wrong: readonly OpportunityField[];
  isNew: boolean;
}) {
  const t = useTranslations("opportunities.form");
  const ids = useId();
  const set = <K extends keyof OpportunityForm>(field: K, value: OpportunityForm[K]) => onChange({ ...form, [field]: value });
  const error = (field: OpportunityField) => (wrong.includes(field) ? t(`errors.${field}`) : null);
  const described = (field: OpportunityField) => (wrong.includes(field) ? `${ids}-${field}-error` : `${ids}-${field}-help`);
  const text = (field: "statement" | "beneficiary" | "leverage", rows: number, limit: number) => (
    <Field id={`${ids}-${field}`} label={t(field)} required help={t(`${field}Help`)} error={error(field)}>
      <Textarea
        id={`${ids}-${field}`}
        value={form[field]}
        onChange={(event) => set(field, event.target.value)}
        rows={rows}
        maxLength={limit}
        aria-required="true"
        aria-invalid={wrong.includes(field) || undefined}
        aria-describedby={described(field)}
        data-testid={`opportunity-${field}-input`}
      />
    </Field>
  );
  return (
    <div className="flex flex-col gap-5" data-testid="opportunity-fields">
      {text("statement", 3, OPPORTUNITY_LIMITS.statement)}
      <div className="grid gap-5 sm:grid-cols-2">
        {text("beneficiary", 2, OPPORTUNITY_LIMITS.text)}
        {text("leverage", 2, OPPORTUNITY_LIMITS.text)}
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field id={`${ids}-windowUntil`} label={t("windowUntil")} help={t("windowUntilHelp")} error={error("windowUntil")}>
          <Input
            id={`${ids}-windowUntil`}
            type="date"
            value={form.windowUntil}
            onChange={(event) => set("windowUntil", event.target.value)}
            aria-invalid={wrong.includes("windowUntil") || undefined}
            aria-describedby={described("windowUntil")}
            data-testid="opportunity-window-until-input"
          />
        </Field>
        <Field id={`${ids}-windowNote`} label={t("windowNote")} help={t("windowNoteHelp")}>
          <Input
            id={`${ids}-windowNote`}
            value={form.windowNote}
            onChange={(event) => set("windowNote", event.target.value)}
            maxLength={OPPORTUNITY_LIMITS.text}
            aria-describedby={`${ids}-windowNote-help`}
            data-testid="opportunity-window-note-input"
          />
        </Field>
      </div>
      <Field id={`${ids}-answers`} label={t("answers")} help={t("answersHelp", { max: OPPORTUNITY_LIMITS.answers })} error={error("answers")}>
        <Input
          id={`${ids}-answers`}
          value={form.answers}
          onChange={(event) => set("answers", event.target.value)}
          autoComplete="off"
          spellCheck={false}
          aria-invalid={wrong.includes("answers") || undefined}
          aria-describedby={described("answers")}
          data-testid="opportunity-answers-input"
        />
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field id={`${ids}-rank`} label={t("rank")} help={isNew ? t("rankNewHelp") : t("rankHelp")} error={error("rank")}>
          <Input
            id={`${ids}-rank`}
            inputMode="numeric"
            value={form.rank}
            onChange={(event) => set("rank", event.target.value)}
            aria-invalid={wrong.includes("rank") || undefined}
            aria-describedby={described("rank")}
            data-testid="opportunity-rank-input"
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
            aria-describedby={described("owner")}
            data-testid="opportunity-owner-input"
          />
        </Field>
      </div>
      <Field id={`${ids}-note`} label={t("note")} help={t("noteHelp")}>
        <Input
          id={`${ids}-note`}
          value={form.note}
          onChange={(event) => set("note", event.target.value)}
          maxLength={OPPORTUNITY_LIMITS.note}
          aria-describedby={`${ids}-note-help`}
          data-testid="opportunity-note-input"
        />
      </Field>
    </div>
  );
}
