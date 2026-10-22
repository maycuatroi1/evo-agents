"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";

import { Field, FormDialog, LevelSelect } from "./fields";
import { emptyUnitForm, type UnitField, unitCreateBody, unitEditBody, type UnitForm, unitFormOf, unitProblems } from "./form-model";
import { useOrgSubmit } from "./hooks";
import { type Rights, subtree } from "./model";
import { MAX_NAME_CHARS, MAX_NOTE_CHARS, type OrgUnit } from "./queries";

/** What the unit dialog does: create a unit below `parent`, or edit `unit`. */
export type UnitTarget = { parent: OrgUnit; unit?: undefined } | { unit: OrgUnit; parent?: undefined };

type Props = { target: UnitTarget | null; open: boolean; onOpenChange: (open: boolean) => void; units: readonly OrgUnit[]; rights: Rights };

/** Creates a team or a cohort below a unit, or edits a unit's name, parent and, for a cohort, its end. */
export function UnitDialog({ target, open, onOpenChange, units, rights }: Props) {
  if (!target) return null;
  return <UnitDialogBody key={target.unit?.slug ?? `new-${target.parent?.slug}`} target={target} open={open} onOpenChange={onOpenChange} units={units} rights={rights} />;
}

function UnitDialogBody({ target, open, onOpenChange, units, rights }: Props & { target: UnitTarget }) {
  const t = useTranslations("org.unitDialog");
  const ids = useId();
  const editing = target.unit;
  const [form, setForm] = useState<UnitForm>(() => (editing ? unitFormOf(editing) : emptyUnitForm(target.parent?.slug ?? "")));
  const [checked, setChecked] = useState(false);
  const { run, pending, error } = useOrgSubmit();
  const problems = unitProblems(form, { adding: !editing, taken: units.map((unit) => unit.slug) });
  const shown = (field: UnitField) => (checked ? problems[field] : undefined);
  const set = (patch: Partial<UnitForm>) => setForm((current) => ({ ...current, ...patch }));
  const below = editing ? subtree(units, editing.slug) : new Set<string>();
  const parents = units.filter((unit) => !unit.archived_at && !below.has(unit.slug) && (rights.createUnder(unit) || unit.slug === form.parent));
  const cohort = form.kind === "cohort";

  const submit = async () => {
    setChecked(true);
    if (Object.keys(problems).length > 0) return;
    const ok = editing
      ? await run({ kind: "editUnit", slug: editing.slug, body: unitEditBody(editing, form) }, t("saved", { name: form.name || editing.name }))
      : await run({ kind: "createUnit", body: unitCreateBody(form) }, t("created", { slug: form.slug }));
    if (ok) onOpenChange(false);
  };

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? t("editTitle", { name: editing.name }) : t("createTitle")}
      description={editing ? t("editDescription") : t("createDescription", { parent: target.parent?.name ?? "" })}
      submitLabel={editing ? t("save") : t("create")}
      pending={pending}
      error={error}
      onSubmit={() => void submit()}
      testId="org-unit-dialog"
    >
      {editing ? null : (
        <Field id={`${ids}-slug`} label={t("slug")} hint={t("slugHint")} problem={shown("slug")}>
          {(control) => (
            <Input
              {...control}
              value={form.slug}
              onChange={(event) => set({ slug: event.target.value.trim().toLowerCase() })}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              maxLength={63}
              aria-required="true"
              className="h-9 font-mono"
              data-testid="org-unit-slug"
            />
          )}
        </Field>
      )}
      <Field id={`${ids}-name`} label={t("name")} hint={editing ? undefined : t("nameHint")} problem={shown("name")}>
        {(control) => (
          <Input {...control} value={form.name} onChange={(event) => set({ name: event.target.value })} maxLength={MAX_NAME_CHARS} className="h-9" data-testid="org-unit-name" />
        )}
      </Field>
      {editing ? null : (
        <Field id={`${ids}-kind`} label={t("kind")} hint={cohort ? t("cohortHint") : t("teamHint")}>
          {(control) => (
            <NativeSelect {...control} value={form.kind} onChange={(event) => set({ kind: event.target.value as UnitForm["kind"] })} className="w-full" data-testid="org-unit-kind">
              <NativeSelectOption value="team">{t("team")}</NativeSelectOption>
              <NativeSelectOption value="cohort">{t("cohort")}</NativeSelectOption>
            </NativeSelect>
          )}
        </Field>
      )}
      {editing && editing.parent !== null ? (
        <Field id={`${ids}-parent`} label={t("parent")}>
          {(control) => (
            <NativeSelect {...control} value={form.parent} onChange={(event) => set({ parent: event.target.value })} className="w-full" data-testid="org-unit-parent">
              {parents.map((unit) => (
                <NativeSelectOption key={unit.slug} value={unit.slug}>
                  {unit.name}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          )}
        </Field>
      ) : null}
      {cohort ? (
        <Field id={`${ids}-ends`} label={t("endsOn")} hint={t("endsOnHint")}>
          {(control) => <Input {...control} type="date" value={form.endsOn} onChange={(event) => set({ endsOn: event.target.value })} className="h-9" data-testid="org-unit-ends-on" />}
        </Field>
      ) : null}
      {editing ? null : (
        <>
          <Field id={`${ids}-level`} label={t("level")} hint={cohort ? t("levelCohortHint") : t("levelHint")}>
            {(control) => <LevelSelect {...control} value={form.level} levels={rights.levels} onChange={(level) => set({ level })} none={cohort ? t("levelCohortNone") : t("levelNone")} />}
          </Field>
          {form.level ? (
            <Field id={`${ids}-note`} label={t("note")} problem={shown("note")}>
              {(control) => <Textarea {...control} value={form.note} onChange={(event) => set({ note: event.target.value })} maxLength={MAX_NOTE_CHARS} rows={2} />}
            </Field>
          ) : null}
        </>
      )}
    </FormDialog>
  );
}
