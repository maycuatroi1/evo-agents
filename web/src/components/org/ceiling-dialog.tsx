"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Textarea } from "@/components/ui/textarea";

import { Field, FormDialog, LevelSelect } from "./fields";
import { useOrgSubmit } from "./hooks";
import type { Rights } from "./model";
import { type Level, MAX_NOTE_CHARS, type OrgSeat, type OrgUnit } from "./queries";

/** Whose ceiling the dialog sets: a unit's, or one seat's. */
export type CeilingTarget = { unit: OrgUnit; seat?: undefined } | { seat: OrgSeat; unit?: undefined };

type Props = { target: CeilingTarget | null; open: boolean; onOpenChange: (open: boolean) => void; rights: Rights };

/** Writes the next ceiling of a unit or a seat: a level up to the viewer's own, and why. */
export function CeilingDialog(props: Props) {
  if (!props.target) return null;
  const key = props.target.seat ? `seat-${props.target.seat.id}` : `unit-${props.target.unit?.slug}`;
  return <CeilingDialogBody key={key} {...props} target={props.target} />;
}

function CeilingDialogBody({ target, open, onOpenChange, rights }: Props & { target: CeilingTarget }) {
  const t = useTranslations("org.ceilingDialog");
  const tLevel = useTranslations("org.levels");
  const ids = useId();
  const current = (target.seat ?? target.unit).ceiling;
  const [level, setLevel] = useState<Level | "">(current && rights.levels.includes(current) ? current : "");
  const [note, setNote] = useState("");
  const [checked, setChecked] = useState(false);
  const { run, pending, error } = useOrgSubmit();
  const name = target.seat ? (target.seat.login ?? target.seat.agent ?? "") : target.unit.name;
  const missing = checked && !level ? "level" : undefined;

  const submit = async () => {
    setChecked(true);
    if (!level || note.length > MAX_NOTE_CHARS) return;
    const body = { level, ...(target.seat ? { seat: target.seat.id } : { unit: target.unit.slug }), ...(note.trim() ? { note: note.trim() } : {}) };
    if (await run({ kind: "setCeiling", body }, t("saved", { name, level: tLevel(level) }))) onOpenChange(false);
  };

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("title", { name })}
      description={current ? t("current", { level: tLevel(current) }) : t("noCurrent")}
      submitLabel={t("save")}
      pending={pending}
      error={error}
      onSubmit={() => void submit()}
      testId="org-ceiling-dialog"
    >
      <Field id={`${ids}-level`} label={t("level")} hint={t("levelHint")} problem={missing}>
        {(control) => <LevelSelect {...control} value={level} levels={rights.levels} onChange={setLevel} none={t("choose")} />}
      </Field>
      <Field id={`${ids}-note`} label={t("note")} hint={t("noteHint")}>
        {(control) => <Textarea {...control} value={note} onChange={(event) => setNote(event.target.value)} maxLength={MAX_NOTE_CHARS} rows={3} data-testid="org-ceiling-note" />}
      </Field>
    </FormDialog>
  );
}
