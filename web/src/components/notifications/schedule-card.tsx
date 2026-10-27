"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useMemo, useState, useSyncExternalStore } from "react";

import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { browserApi } from "@/lib/api/browser";
import { type NotificationSettings, notificationKeys, putNotificationSettings } from "@/lib/notification-queries";

import {
  overnight,
  scheduleBody,
  scheduleChanged,
  type ScheduleDraft,
  scheduleDraft,
  type ScheduleErrors,
  scheduleErrors,
  type ScheduleField,
  sameZone,
} from "./model";
import { SettingsCard } from "./parts";

const noSubscribe = () => () => {};

/** The browser's own time zone once the page runs in it; null on the server, so both render the same form. */
function useOwnZone(): string | null {
  return useSyncExternalStore(
    noSubscribe,
    () => {
      try {
        return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
      } catch {
        return null;
      }
    },
    () => null,
  );
}

/** The time zones this browser knows, offered as the field's suggestions; the hub checks the one saved. */
function useZones(own: string | null): string[] {
  return useMemo(() => {
    if (own === null) return [];
    try {
      return Intl.supportedValuesOf("timeZone");
    } catch {
      return [own];
    }
  }, [own]);
}

function FieldProblem({ id, problem }: { id: string; problem: string | undefined }) {
  const t = useTranslations("notifications.schedule.problem");
  if (!problem) return null;
  return (
    <p id={id} className="text-xs font-medium text-danger" data-testid="schedule-problem">
      {t(problem as "time")}
    </p>
  );
}

function TimeField({
  id,
  label,
  value,
  problem,
  onChange,
  testId,
  lead = false,
}: {
  id: string;
  label: string;
  value: string;
  problem: string | undefined;
  onChange: (value: string) => void;
  testId: string;
  /** A field of its own, labelled as the form's other fields; else one of the two times of the quiet hours. */
  lead?: boolean;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <Label htmlFor={id} className={lead ? "text-sm font-medium text-foreground" : "text-xs font-medium text-muted-foreground"}>
        {label}
      </Label>
      <Input
        id={id}
        type="time"
        step={60}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-invalid={problem ? true : undefined}
        aria-describedby={problem ? `${id}-problem` : undefined}
        className="tabular-nums"
        data-testid={testId}
      />
      <FieldProblem id={`${id}-problem`} problem={problem} />
    </div>
  );
}

/**
 * Quiet hours, the time zone and the summary push, saved together: push of the interrupt tier waits for the end of the
 * quiet hours, a question's too, and the summary of Today comes once a day at its
 * time when the member was away. A time zone the member never chose starts as this browser's, said under the field,
 * and saving keeps it. Nothing goes out while a field is wrong; the hub's refusal (a time zone Postgres does not know)
 * shows above the buttons.
 */
export function ScheduleCard({ settings, className }: { settings: NotificationSettings; className?: string }) {
  const t = useTranslations("notifications.schedule");
  const ids = useId();
  const own = useOwnZone();
  const zones = useZones(own);
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [edited, setEdited] = useState<Partial<ScheduleDraft>>({});
  const [errors, setErrors] = useState<ScheduleErrors>({});
  const [error, setError] = useState<WriteFailure | null>(null);
  const draft: ScheduleDraft = { ...scheduleDraft(settings, own), ...edited };
  const changed = scheduleChanged(settings, draft);
  const save = useMutation({
    mutationFn: () => putNotificationSettings(browserApi(), scheduleBody(draft)),
    onSuccess: (saved) => {
      queryClient.setQueryData(notificationKeys.settings, saved);
      setEdited({});
      notify({ tone: "success", text: t("saved") });
    },
    onError: (caught) => setError(failure(caught, { 422: t("refused") })),
    onSettled: () => queryClient.invalidateQueries({ queryKey: notificationKeys.settings }),
  });

  const set = (field: keyof ScheduleDraft, value: string | boolean) => {
    setEdited((current) => ({ ...current, [field]: value }));
    if (field in errors) setErrors((current) => ({ ...current, [field as ScheduleField]: undefined }));
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    const found = scheduleErrors(draft);
    setErrors(found);
    const first = (["start", "end", "timeZone", "digestAt"] as const).find((field) => found[field]);
    if (first) {
      document.getElementById(`${ids}-${first}`)?.focus();
      return;
    }
    save.mutate();
  };

  const browserZone = !settings.time_zone_chosen && own !== null && !sameZone(own, settings.time_zone) && edited.timeZone === undefined;
  return (
    <SettingsCard title={t("title")} testId="schedule-card" className={className}>
      <form className="flex flex-col gap-5 px-4 py-4" onSubmit={submit} noValidate aria-label={t("title")} data-testid="schedule-form">
        <fieldset className="m-0 flex min-w-0 flex-col gap-3 border-0 p-0">
          <legend className="sr-only">{t("quiet")}</legend>
          <div className="flex items-start justify-between gap-4">
            <div className="flex min-w-0 flex-col gap-0.5">
              <Label htmlFor={`${ids}-quiet`} className="text-sm font-medium text-foreground">
                {t("quiet")}
              </Label>
              <p id={`${ids}-quiet-hint`} className="text-[13px] leading-[18px] text-pretty text-muted-foreground">
                {t("quietHint")}
              </p>
            </div>
            <Switch
              id={`${ids}-quiet`}
              checked={draft.quiet}
              onCheckedChange={(on) => set("quiet", on)}
              aria-describedby={`${ids}-quiet-hint`}
              className="mt-0.5"
              data-testid="schedule-quiet"
            />
          </div>
          {draft.quiet ? (
            <div className="grid grid-cols-2 gap-3" data-testid="schedule-quiet-times">
              <TimeField id={`${ids}-start`} label={t("start")} value={draft.start} problem={errors.start} onChange={(value) => set("start", value)} testId="schedule-start" />
              <TimeField id={`${ids}-end`} label={t("end")} value={draft.end} problem={errors.end} onChange={(value) => set("end", value)} testId="schedule-end" />
              {!errors.start && !errors.end && draft.start !== draft.end && overnight(draft.start, draft.end) ? (
                <p className="col-span-2 text-xs text-fg-subtle" data-testid="schedule-overnight">
                  {t("overnight", { start: draft.start, end: draft.end })}
                </p>
              ) : null}
            </div>
          ) : null}
        </fieldset>
        <div className="flex min-w-0 flex-col gap-1.5">
          <Label htmlFor={`${ids}-timeZone`} className="text-sm font-medium text-foreground">
            {t("timeZone")}
          </Label>
          <Input
            id={`${ids}-timeZone`}
            value={draft.timeZone}
            onChange={(event) => set("timeZone", event.target.value)}
            list={`${ids}-zones`}
            autoComplete="off"
            spellCheck={false}
            aria-invalid={errors.timeZone ? true : undefined}
            aria-describedby={`${ids}-timeZone-hint${errors.timeZone ? ` ${ids}-timeZone-problem` : ""}`}
            className="font-mono"
            data-testid="schedule-time-zone"
          />
          <datalist id={`${ids}-zones`}>
            {zones.map((zone) => (
              <option key={zone} value={zone} />
            ))}
          </datalist>
          <p id={`${ids}-timeZone-hint`} className="text-xs text-pretty text-fg-subtle" data-testid="schedule-time-zone-hint">
            {browserZone ? t("timeZoneBrowser", { zone: settings.time_zone }) : t("timeZoneHint")}
          </p>
          <FieldProblem id={`${ids}-timeZone-problem`} problem={errors.timeZone} />
        </div>
        <div className="flex min-w-0 flex-col gap-1.5">
          <TimeField id={`${ids}-digestAt`} label={t("digestAt")} value={draft.digestAt} problem={errors.digestAt} onChange={(value) => set("digestAt", value)} testId="schedule-digest" lead />
          <p className="text-xs text-pretty text-fg-subtle">{t("digestHint")}</p>
        </div>
        {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
        <div className="flex flex-wrap items-center justify-end gap-2 border-t pt-4">
          <Button
            type="button"
            variant="outline"
            disabled={Object.keys(edited).length === 0 || save.isPending}
            onClick={() => {
              setEdited({});
              setErrors({});
              setError(null);
            }}
            data-testid="schedule-discard"
          >
            {t("discard")}
          </Button>
          <Button type="submit" busy={save.isPending} disabled={!changed} data-testid="schedule-save">
            {save.isPending ? t("saving") : t("save")}
          </Button>
        </div>
      </form>
    </SettingsCard>
  );
}
