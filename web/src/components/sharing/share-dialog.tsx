"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useMemo, useState } from "react";

import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { cn } from "@/lib/utils";

import {
  emptyShareForm,
  GRANTEE_KINDS,
  type GranteeKind,
  LEVELS,
  MAX_BUDGET_USD,
  MAX_RUNNING,
  type ShareField as Field,
  type ShareForm,
  shareBody,
  shareProblems,
  type ShareTarget,
  TERMS_WARNING,
  utcDay,
} from "./model";
import { createShare, shareChartQuery } from "./queries";
import { describedBy, ShareField, TermsWarning } from "./share-fields";

export type ShareDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** What may be shared, each with the projects it is bound to; the first one, or `initial`, is picked. */
  targets: ShareTarget[];
  initial?: string | null;
  /** Reloads what lists the shares once the hub answered, whatever it answered. */
  onChanged: () => Promise<unknown>;
};

const RANGES: Partial<Record<Field, { min: number; max: number }>> = {
  budget: { min: 0.01, max: MAX_BUDGET_USD },
  ceiling: { min: 1, max: 100 },
  maxRunning: { min: 1, max: MAX_RUNNING },
};

/** The units a share may name: live ones of the org chart, and the members who hold a live seat, for the suggestions. */
function useChart(now: number) {
  const chart = useQuery(shareChartQuery(browserApi));
  return useMemo(() => {
    const today = utcDay(now);
    const units = (chart.data?.units ?? [])
      .filter((unit) => unit.archived_at === null && (unit.ends_on === null || unit.ends_on >= today))
      .sort((a, b) => a.name.localeCompare(b.name));
    const logins = [...new Set((chart.data?.seats ?? []).flatMap((seat) => (seat.login && !seat.ended_at ? [seat.login] : [])))].sort();
    return { units, logins, loaded: chart.isSuccess };
  }, [chart.data, chart.isSuccess, now]);
}

/**
 * The form inside the dialog, mounted each time it opens: what to share, with whom (one member, a unit of the org
 * chart and those below it, or every member), on which of its projects, and its limits, each one optional. A personal
 * subscription token shows the hub's warning first, and Share stays off until its owner ticks the box under it.
 */
function ShareFormBody({ targets, initial, onChanged, onDone, pending, setPending }: Omit<ShareDialogProps, "open" | "onOpenChange"> & {
  onDone: () => void;
  pending: boolean;
  setPending: (pending: boolean) => void;
}) {
  const t = useTranslations("sharing.dialog");
  const ids = useId();
  const failureOf = useWriteFailure();
  const [now] = useState(() => Date.now());
  const [form, setForm] = useState<ShareForm>(() => emptyShareForm(targets.find((item) => item.name === initial) ?? targets[0] ?? null));
  const [sent, setSent] = useState(false);
  const [failure, setFailure] = useState<WriteFailure | null>(null);
  // The hub knows best which secret is a subscription: a 409 carrying its warning shows it here too.
  const [warned, setWarned] = useState<string[]>([]);
  const chart = useChart(now);
  const picked = targets.find((item) => item.name === form.secret) ?? null;
  const target = picked ? { ...picked, subscription: picked.subscription || warned.includes(picked.name) } : null;
  const problems = shareProblems(form, target, now);
  const shown = (field: Field) => {
    const problem = sent ? problems[field] : undefined;
    if (!problem || problem === "ack") return null;
    return problem === "range" ? t("problems.range", RANGES[field] ?? { min: 0, max: 0 }) : t(`problems.${problem}`);
  };
  const set = <K extends keyof ShareForm>(key: K, value: ShareForm[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    setFailure(null);
  };
  const pick = (name: string) => {
    setForm(emptyShareForm(targets.find((item) => item.name === name) ?? null));
    setFailure(null);
    setSent(false);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending || !target) return;
    setSent(true);
    if (Object.keys(problems).length > 0) return;
    setPending(true);
    setFailure(null);
    try {
      const share = await createShare(browserApi(), target.name, shareBody(form, target));
      notify({ tone: "success", text: t("done", { name: share.secret }), description: t("doneText") });
      await onChanged();
      setPending(false);
      onDone();
    } catch (error) {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
      const message = isApiError(error) ? error.info.message : "";
      const failed = failureOf(error, { 409: t("conflict") });
      if (isApiError(error) && error.status === 409 && message.includes(TERMS_WARNING)) {
        setWarned((names) => [...names, target.name]);
        setFailure({ ...failed, text: t("termsNeeded"), detail: null });
      } else {
        setFailure(isApiError(error) && error.status === 409 ? { ...failed, detail: message } : failed);
      }
      await onChanged();
      setPending(false);
    }
  };

  const projectsId = `${ids}-projects`;
  const blocked = Boolean(target?.subscription && !form.termsAck);
  return (
    <form className="flex flex-col gap-5" onSubmit={(event) => void submit(event)} noValidate data-testid="share-form">
      <div className="grid gap-4 sm:grid-cols-2">
        <ShareField id={`${ids}-secret`} label={t("credential")} problem={null} testId="share-secret" className="sm:col-span-2">
          <NativeSelect
            id={`${ids}-secret`}
            value={form.secret}
            onChange={(event) => pick(event.target.value)}
            disabled={pending}
            className="w-full md:[&_select]:h-9"
            data-testid="share-secret"
          >
            {targets.map((item) => (
              <NativeSelectOption key={item.name} value={item.name}>
                {item.name}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </ShareField>
        <ShareField id={`${ids}-kind`} label={t("grantee")} problem={null} testId="share-kind">
          <NativeSelect
            id={`${ids}-kind`}
            value={form.granteeKind}
            onChange={(event) => set("granteeKind", event.target.value as GranteeKind)}
            disabled={pending}
            className="w-full md:[&_select]:h-9"
            data-testid="share-kind"
          >
            {GRANTEE_KINDS.map((kind) => (
              <NativeSelectOption key={kind} value={kind}>
                {t(`kind.${kind}`)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </ShareField>
        {form.granteeKind === "user" ? (
          <ShareField id={`${ids}-login`} label={t("login")} hint={t("loginHint")} problem={shown("login")} testId="share-login">
            <Input
              id={`${ids}-login`}
              value={form.login}
              onChange={(event) => set("login", event.target.value)}
              list={`${ids}-logins`}
              autoComplete="off"
              spellCheck={false}
              disabled={pending}
              className="h-9 font-mono"
              aria-invalid={shown("login") ? true : undefined}
              aria-describedby={describedBy(`${ids}-login`, shown("login"))}
              data-testid="share-login"
            />
            <datalist id={`${ids}-logins`}>
              {chart.logins.map((login) => (
                <option key={login} value={login} />
              ))}
            </datalist>
          </ShareField>
        ) : null}
        {form.granteeKind === "unit" ? (
          <ShareField id={`${ids}-unit`} label={t("unit")} hint={t("unitHint")} problem={shown("unit")} testId="share-unit">
            {chart.units.length > 0 || !chart.loaded ? (
              <NativeSelect
                id={`${ids}-unit`}
                value={form.unit}
                onChange={(event) => set("unit", event.target.value)}
                disabled={pending || !chart.loaded}
                className="w-full md:[&_select]:h-9"
                aria-invalid={shown("unit") ? true : undefined}
                aria-describedby={describedBy(`${ids}-unit`, shown("unit"))}
                data-testid="share-unit"
              >
                <NativeSelectOption value="">{chart.loaded ? t("unitPick") : t("loading")}</NativeSelectOption>
                {chart.units.map((unit) => (
                  <NativeSelectOption key={unit.slug} value={unit.slug}>
                    {t("unitOption", { name: unit.name, slug: unit.slug })}
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            ) : (
              <p className="text-sm text-muted-foreground" data-testid="share-no-unit">
                {t("noUnit")}
              </p>
            )}
          </ShareField>
        ) : null}
        {form.granteeKind === "all" ? <p className="self-end text-xs text-pretty text-muted-foreground sm:pb-2">{t("allHint")}</p> : null}
      </div>
      <fieldset className="flex min-w-0 flex-col gap-2" aria-describedby={describedBy(projectsId, shown("projects"))}>
        <legend className="mb-1.5 text-sm font-medium">{t("projects")}</legend>
        <div className="grid gap-2 sm:grid-cols-2" data-testid="share-projects">
          {(target?.projects ?? []).map((name, index) => {
            const checked = form.projects.includes(name);
            return (
              <label
                key={name}
                htmlFor={`${projectsId}-${index}`}
                className={cn(
                  "flex min-h-10 cursor-pointer items-center gap-2.5 rounded-md border px-3 py-2 transition-colors hover:bg-muted/50",
                  checked && "border-brand/40 bg-surface-selected",
                )}
              >
                <input
                  id={`${projectsId}-${index}`}
                  type="checkbox"
                  checked={checked}
                  disabled={pending}
                  onChange={(event) =>
                    set("projects", event.target.checked ? [...form.projects, name].sort() : form.projects.filter((p) => p !== name))
                  }
                  className="size-4 shrink-0 cursor-pointer accent-primary"
                />
                <span className="min-w-0 font-mono text-sm [overflow-wrap:anywhere]">{name}</span>
              </label>
            );
          })}
        </div>
        <p id={`${projectsId}-hint`} className="text-xs text-muted-foreground">
          {t("projectsHint")}
        </p>
        {shown("projects") ? (
          <p id={`${projectsId}-error`} className="text-xs font-medium text-danger" data-testid="share-projects-error">
            {shown("projects")}
          </p>
        ) : null}
      </fieldset>
      <fieldset className="flex min-w-0 flex-col gap-3">
        <legend className="mb-1 text-sm font-medium">{t("limits")}</legend>
        <p className="-mt-1 text-xs text-pretty text-muted-foreground">{t("limitsHint")}</p>
        <div className="grid gap-4 sm:grid-cols-2">
          <ShareField id={`${ids}-level`} label={t("level")} hint={t("levelHint")} problem={null} testId="share-level">
            <NativeSelect
              id={`${ids}-level`}
              value={form.level}
              onChange={(event) => set("level", event.target.value as ShareForm["level"])}
              disabled={pending}
              className="w-full md:[&_select]:h-9"
              aria-describedby={`${ids}-level-hint`}
              data-testid="share-level"
            >
              <NativeSelectOption value="">{t("levelAny")}</NativeSelectOption>
              {LEVELS.map((level) => (
                <NativeSelectOption key={level} value={level}>
                  {level}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </ShareField>
          {(
            [
              ["budget", "decimal"],
              ["ceiling", "numeric"],
              ["maxRunning", "numeric"],
            ] as const
          ).map(([field, mode]) => (
            <ShareField key={field} id={`${ids}-${field}`} label={t(field)} hint={t(`${field}Hint`)} problem={shown(field)} testId={`share-${field}`}>
              <Input
                id={`${ids}-${field}`}
                value={form[field]}
                onChange={(event) => set(field, event.target.value)}
                inputMode={mode}
                autoComplete="off"
                disabled={pending}
                className="h-9 tabular-nums"
                aria-invalid={shown(field) ? true : undefined}
                aria-describedby={describedBy(`${ids}-${field}`, shown(field))}
                data-testid={`share-${field}`}
              />
            </ShareField>
          ))}
          <ShareField id={`${ids}-expires`} label={t("expires")} hint={t("expiresHint")} problem={shown("expires")} testId="share-expires">
            <Input
              id={`${ids}-expires`}
              type="date"
              value={form.expires}
              min={utcDay(now + 86_400_000)}
              onChange={(event) => set("expires", event.target.value)}
              disabled={pending}
              className="h-9 tabular-nums"
              aria-invalid={shown("expires") ? true : undefined}
              aria-describedby={describedBy(`${ids}-expires`, shown("expires"))}
              data-testid="share-expires"
            />
          </ShareField>
        </div>
      </fieldset>
      {target?.subscription ? <TermsWarning id={`${ids}-ack`} checked={form.termsAck} onChange={(on) => set("termsAck", on)} /> : null}
      {failure ? <InlineError text={failure.text} detail={failure.detail} requestId={failure.requestId} /> : null}
      <DialogFooter>
        <DialogClose asChild>
          <Button
            type="button"
            variant="outline"
            size="lg"
            aria-disabled={pending || undefined}
            className="aria-disabled:opacity-50"
            onClick={(event) => pending && event.preventDefault()}
          >
            {t("cancel")}
          </Button>
        </DialogClose>
        <Button type="submit" size="lg" busy={pending} disabled={blocked || !target} data-testid="share-save">
          {pending ? t("saving") : t("save")}
        </Button>
      </DialogFooter>
    </form>
  );
}

/**
 * Share one of the visitor's secrets or accounts (POST /v1/secrets/{name}/shares). The dialog cannot be closed while
 * the write runs; the hub's refusal shows inside it, and a share made is a toast.
 */
export function ShareDialog({ open, onOpenChange, targets, initial = null, onChanged }: ShareDialogProps) {
  const t = useTranslations("sharing.dialog");
  const [pending, setPending] = useState(false);
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-5 overflow-y-auto sm:max-w-xl"
        onEscapeKeyDown={guard}
        onInteractOutside={guard}
        data-testid="share-dialog"
      >
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription className="text-pretty">{t("description")}</DialogDescription>
        </DialogHeader>
        <ShareFormBody
          targets={targets}
          initial={initial}
          onChanged={onChanged}
          onDone={() => onOpenChange(false)}
          pending={pending}
          setPending={setPending}
        />
      </DialogContent>
    </Dialog>
  );
}
