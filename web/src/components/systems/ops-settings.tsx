"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Save, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, useWriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { browserApi } from "@/lib/api/browser";
import { OPS_BOUNDS, type OpsField, type OpsForm, opsFormOf, opsProblems, type SystemOps, withOps } from "@/lib/system-ops-queries";
import { type SystemOut, specOf } from "@/lib/systems";
import { putSystem, systemKeys } from "@/lib/systems-queries";
import { cn } from "@/lib/utils";

const FIELDS: { field: OpsField; step: string; unit: "minutes" | "items" | "usd" }[] = [
  { field: "debounce_minutes", step: "1", unit: "minutes" },
  { field: "max_items", step: "1", unit: "items" },
  { field: "ops_daily_usd", step: "0.01", unit: "usd" },
];

/** Save the form as the system's next revision, over the revision the page read; a conflict reads the system again. */
function useSaveOps(project: string, system: SystemOut) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (form: OpsForm) => putSystem(browserApi(), project, system.slug, withOps(specOf(system.spec), form), system.revision),
    onSettled: () => queryClient.invalidateQueries({ queryKey: systemKeys.one(project, system.slug) }),
  });
}

/**
 * The ops of a system as a form (docs/hub.md, Agent systems): on or off, how long a batch gathers, how many items close
 * it, and what the ops threads may spend in 24 hours. An admin saves it as the system's next revision; a reader sees
 * the same values, read-only. Each field says its bounds under it, and a value out of them says so there.
 */
export function OpsSettings({ project, system, ops, canChange }: { project: string; system: SystemOut; ops: SystemOps; canChange: boolean }) {
  const t = useTranslations("systems.ops.settings");
  const ids = useId();
  const held = opsFormOf(ops);
  const [edits, setEdits] = useState<Partial<OpsForm>>({});
  const [shown, setShown] = useState<OpsField[]>([]);
  const form: OpsForm = { ...held, ...edits };
  const save = useSaveOps(project, system);
  const failure = useWriteFailure();
  const changed = (Object.keys(edits) as (keyof OpsForm)[]).some((key) => edits[key] !== held[key]);
  const set = <K extends keyof OpsForm>(key: K, value: OpsForm[K]) => {
    setEdits((current) => ({ ...current, [key]: value }));
    setShown((current) => current.filter((field) => field !== key));
    if (save.isError) save.reset();
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canChange || save.isPending) return;
    const problems = opsProblems(form);
    setShown(problems);
    if (problems.length) {
      document.getElementById(`${ids}-${problems[0]}`)?.focus();
      return;
    }
    try {
      await save.mutateAsync(form);
      setEdits({});
      notify({ tone: "success", text: t("saved"), description: form.enabled ? t("savedOn") : t("savedOff") });
    } catch {
      // The reason shows under the form.
    }
  };
  const error = save.isError
    ? failure(save.error, { 403: t("forbidden"), 409: t("conflict"), 422: t("refused") })
    : null;
  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex flex-col gap-5" aria-labelledby={`${ids}-title`} data-testid="ops-settings">
      <h3 id={`${ids}-title`} className="sr-only">
        {t("title")}
      </h3>
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1">
          <Label htmlFor={`${ids}-enabled`}>{t("enabled")}</Label>
          <p id={`${ids}-enabled-hint`} className="text-[13px] text-pretty text-muted-foreground">
            {t("enabledHint")}
          </p>
        </div>
        <Switch
          id={`${ids}-enabled`}
          checked={form.enabled}
          onCheckedChange={(checked) => set("enabled", checked)}
          disabled={!canChange || save.isPending}
          aria-describedby={`${ids}-enabled-hint`}
          className="mt-1"
          data-testid="ops-enabled"
        />
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        {FIELDS.map(({ field, step, unit }) => {
          const bounds = OPS_BOUNDS[field];
          const wrong = shown.includes(field);
          return (
            <div key={field} className="flex min-w-0 flex-col gap-1.5">
              <Label htmlFor={`${ids}-${field}`}>{t(`fields.${field}`)}</Label>
              <Input
                id={`${ids}-${field}`}
                type="number"
                inputMode={unit === "usd" ? "decimal" : "numeric"}
                min={bounds.min}
                max={bounds.max}
                step={step}
                value={form[field]}
                onChange={(event) => set(field, event.target.value)}
                readOnly={!canChange}
                disabled={save.isPending}
                aria-invalid={wrong ? true : undefined}
                aria-describedby={[`${ids}-${field}-hint`, wrong ? `${ids}-${field}-error` : ""].filter(Boolean).join(" ")}
                className="tabular-nums"
                data-testid={`ops-${field}`}
              />
              <p id={`${ids}-${field}-hint`} className="text-xs text-muted-foreground">
                {t(`hints.${unit}`, { min: bounds.min, max: bounds.max })}
              </p>
              {wrong ? (
                <p id={`${ids}-${field}-error`} className="text-xs font-medium text-danger" data-testid={`ops-${field}-error`}>
                  {t("outOfBounds", { min: bounds.min, max: bounds.max })}
                </p>
              ) : null}
            </div>
          );
        })}
      </div>
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      {canChange ? (
        <div className={cn("flex flex-wrap items-center justify-end gap-2", changed ? "" : "max-md:hidden")}>
          <Button
            type="button"
            variant="ghost"
            disabled={!changed || save.isPending}
            onClick={() => {
              setEdits({});
              setShown([]);
              save.reset();
            }}
            data-testid="ops-reset"
          >
            <Undo2 aria-hidden="true" />
            {t("reset")}
          </Button>
          <Button type="submit" disabled={!changed} busy={save.isPending} data-testid="ops-save">
            <Save aria-hidden="true" />
            {save.isPending ? t("saving") : t("save")}
          </Button>
        </div>
      ) : null}
    </form>
  );
}
