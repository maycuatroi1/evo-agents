"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Switch } from "@/components/ui/switch";

import { Field, FormDialog, LoginInput } from "./fields";
import { emptySeatForm, seatAddBody, seatEditBody, type SeatField, type SeatForm, seatFormOf, seatProblems } from "./form-model";
import { useOrgSubmit } from "./hooks";
import type { Rights } from "./model";
import { HANDLE, type OrgSeat, type OrgUnit, type SeatRole } from "./queries";

/** What the seat dialog does: add a seat to `unit` (for `login` when it comes from the Unplaced list), or edit `seat`. */
export type SeatTarget = { unit: string; seat?: OrgSeat; login?: string };

type Props = {
  target: SeatTarget | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  units: readonly OrgUnit[];
  rights: Rights;
  logins: readonly string[];
};

export function SeatDialog(props: Props) {
  if (!props.target) return null;
  const key = props.target.seat ? `seat-${props.target.seat.id}` : `new-${props.target.unit}-${props.target.login ?? ""}`;
  return <SeatDialogBody key={key} {...props} target={props.target} />;
}

function SeatDialogBody({ target, open, onOpenChange, units, rights, logins }: Props & { target: SeatTarget }) {
  const t = useTranslations("org.seatDialog");
  const tRole = useTranslations("org.roles");
  const ids = useId();
  const editing = target.seat;
  const [form, setForm] = useState<SeatForm>(() => (editing ? seatFormOf(editing) : emptySeatForm(target.unit, rights.roles(target.unit).includes("member") ? "member" : (rights.roles(target.unit)[0] ?? "member"), target.login)));
  const [checked, setChecked] = useState(false);
  const { run, pending, error } = useOrgSubmit();
  const problems = seatProblems(form, !editing);
  const shown = (field: SeatField) => (checked ? problems[field] : undefined);
  const set = (patch: Partial<SeatForm>) => setForm((current) => ({ ...current, ...patch }));
  const agent = form.who === "agent";
  const roles: SeatRole[] = rights.roles(form.unit).filter((role) => role !== "agent");
  const placeable = units.filter((unit) => !unit.archived_at && rights.roles(unit.slug).length > 0);
  const canAgent = rights.roles(form.unit).includes("agent");
  const unitName = units.find((unit) => unit.slug === form.unit)?.name ?? form.unit;
  const who = editing ? (editing.login ?? `${editing.agent} (${editing.project})`) : "";

  const submit = async () => {
    setChecked(true);
    if (Object.keys(problems).length > 0) return;
    const ok = editing
      ? await run({ kind: "editSeat", id: editing.id, body: seatEditBody(editing, form) }, t("saved", { who }))
      : await run({ kind: "addSeat", body: seatAddBody(form) }, t("added", { who: agent ? form.agent : form.login, unit: unitName }));
    if (ok) onOpenChange(false);
  };

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? t("editTitle", { who }) : t("addTitle")}
      description={editing ? t("editDescription", { unit: unitName }) : t("addDescription")}
      submitLabel={editing ? t("save") : t("add")}
      pending={pending}
      error={error}
      onSubmit={() => void submit()}
      testId="org-seat-dialog"
    >
      {editing ? null : (
        <Field id={`${ids}-unit`} label={t("unit")}>
          {(control) => (
            <NativeSelect {...control} value={form.unit} onChange={(event) => set({ unit: event.target.value })} className="w-full" data-testid="org-seat-unit">
              {placeable.map((unit) => (
                <NativeSelectOption key={unit.slug} value={unit.slug}>
                  {unit.name}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          )}
        </Field>
      )}
      {!editing && canAgent ? (
        <Field id={`${ids}-who`} label={t("who")}>
          {(control) => (
            <NativeSelect {...control} value={form.who} onChange={(event) => set({ who: event.target.value as SeatForm["who"] })} className="w-full" data-testid="org-seat-who">
              <NativeSelectOption value="person">{t("person")}</NativeSelectOption>
              <NativeSelectOption value="agent">{t("agent")}</NativeSelectOption>
            </NativeSelect>
          )}
        </Field>
      ) : null}
      {!editing && !agent ? (
        <Field id={`${ids}-login`} label={t("login")} problem={shown("login")}>
          {(control) => <LoginInput {...control} value={form.login} onChange={(login) => set({ login })} logins={logins} aria-required testId="org-seat-login" />}
        </Field>
      ) : null}
      {!editing && agent ? (
        <div className="grid min-w-0 gap-4 sm:grid-cols-2">
          <Field id={`${ids}-agent`} label={t("agentHandle")} problem={shown("agent")}>
            {(control) => <Handle {...control} value={form.agent} onChange={(value) => set({ agent: value })} testId="org-seat-agent" />}
          </Field>
          <Field id={`${ids}-project`} label={t("project")} problem={shown("project")}>
            {(control) => <Handle {...control} value={form.project} onChange={(value) => set({ project: value })} testId="org-seat-project" />}
          </Field>
        </div>
      ) : null}
      {agent ? null : (
        <Field id={`${ids}-role`} label={t("role")}>
          {(control) => (
            <NativeSelect {...control} value={form.role} onChange={(event) => set({ role: event.target.value as SeatRole })} className="w-full" data-testid="org-seat-role">
              {(roles.includes(form.role) ? roles : [form.role, ...roles]).map((role) => (
                <NativeSelectOption key={role} value={role}>
                  {tRole(role)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          )}
        </Field>
      )}
      <Field id={`${ids}-reports`} label={t("reportsTo")} hint={form.role === "intern" ? t("reportsToIntern") : t("reportsToHint")} problem={shown("reportsTo")}>
        {(control) => <LoginInput {...control} value={form.reportsTo} onChange={(reportsTo) => set({ reportsTo })} logins={logins} testId="org-seat-reports-to" />}
      </Field>
      {agent || form.role === "intern" || form.sponsor ? (
        <Field id={`${ids}-sponsor`} label={t("sponsor")} hint={t("sponsorHint")} problem={shown("sponsor")}>
          {(control) => <LoginInput {...control} value={form.sponsor} onChange={(sponsor) => set({ sponsor })} logins={logins} testId="org-seat-sponsor" />}
        </Field>
      ) : null}
      <div className="grid min-w-0 gap-4 sm:grid-cols-2">
        <Field id={`${ids}-starts`} label={t("startsOn")} hint={editing ? undefined : t("startsOnHint")}>
          {(control) => <Input {...control} type="date" value={form.startsOn} onChange={(event) => set({ startsOn: event.target.value })} className="h-9" data-testid="org-seat-starts-on" />}
        </Field>
        <Field id={`${ids}-ends`} label={t("endsOn")} problem={shown("endsOn")}>
          {(control) => <Input {...control} type="date" value={form.endsOn} onChange={(event) => set({ endsOn: event.target.value })} className="h-9" data-testid="org-seat-ends-on" />}
        </Field>
      </div>
      {agent || editing?.primary ? null : (
        <div className="flex items-start gap-3">
          <Switch id={`${ids}-primary`} checked={form.primary} onCheckedChange={(primary) => set({ primary })} className="mt-0.5" data-testid="org-seat-primary" />
          <div className="flex min-w-0 flex-col gap-1">
            <Label htmlFor={`${ids}-primary`}>{t("primary")}</Label>
            <p className="text-xs text-pretty text-muted-foreground">{t("primaryHint")}</p>
          </div>
        </div>
      )}
    </FormDialog>
  );
}

function Handle({ value, onChange, testId, ...control }: { id: string; value: string; onChange: (value: string) => void; testId: string; "aria-describedby"?: string; "aria-invalid"?: true }) {
  return (
    <Input
      {...control}
      value={value}
      onChange={(event) => onChange(event.target.value.trim().toLowerCase())}
      pattern={HANDLE.source}
      autoComplete="off"
      autoCapitalize="none"
      spellCheck={false}
      maxLength={100}
      aria-required="true"
      className="h-9 font-mono"
      data-testid={testId}
    />
  );
}
