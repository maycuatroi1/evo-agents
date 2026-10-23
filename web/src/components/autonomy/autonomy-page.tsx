"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Info, OctagonX, TrendingDown } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { useAutonomySource } from "@/components/data/autonomy";
import { Banner } from "@/components/feedback/banner";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ListSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { ClassList } from "./class-list";
import { LevelPicker } from "./level-picker";
import { type Draft, draftOf, isChanged, loweredClasses, prefBody } from "./model";
import { type AutonomySettings, autonomyKeys, settingsQuery, writePref } from "./queries";

/** What is out of the ordinary: a stop that holds the viewer, classes the ceiling lowers below the preference, or no
 * preference set at all. Nothing shows when everything is as the viewer set it. */
function Notes({ settings }: { settings: AutonomySettings }) {
  const t = useTranslations("autonomy.settings");
  const format = useFormatter();
  const source = useAutonomySource(settings.principal);
  const lowered = loweredClasses(settings);
  const { stop } = settings;
  return (
    <>
      {stop ? (
        <Banner icon={OctagonX} tone="danger" role="status" title={t("stopTitle", { scope: stop.scope, name: stop.unit ?? stop.login ?? "" })} testId="autonomy-stop">
          {t("stopText", { when: format.dateTime(new Date(stop.stopped_at), { dateStyle: "medium", timeStyle: "short" }), reason: stop.reason ?? "-" })}
        </Banner>
      ) : null}
      {lowered.length > 0 ? (
        <Banner icon={TrendingDown} tone="attention" title={t("loweredTitle", { count: lowered.length })} testId="autonomy-lowered">
          {t("loweredText", { classes: lowered.join(", "), ceiling: settings.ceiling, source: source(settings.ceiling_source) })}
        </Banner>
      ) : null}
      {settings.pref_level === null ? (
        <Banner icon={Info} title={t("noPrefTitle")} testId="autonomy-no-pref">
          {t("noPrefText")}
        </Banner>
      ) : null}
    </>
  );
}

function SettingsForm({ settings }: { settings: AutonomySettings }) {
  const t = useTranslations("autonomy.settings");
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [draft, setDraft] = useState<Draft>(() => draftOf(settings));
  const [error, setError] = useState<WriteFailure | null>(null);
  const save = useMutation({
    mutationFn: () => writePref(browserApi(), prefBody(settings, draft)),
    onSettled: () => queryClient.invalidateQueries({ queryKey: autonomyKeys.settings }),
  });
  const changed = isChanged(settings, draft);

  const submit = async () => {
    setError(null);
    try {
      const written = await save.mutateAsync();
      notify({ tone: "success", text: t("saved", { level: written.level, revision: written.revision }) });
    } catch (caught) {
      setError(failure(caught, { 403: t("forbidden") }));
    }
  };

  return (
    <form
      className="flex min-w-0 flex-col gap-6"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
      data-testid="autonomy-form"
    >
      <LevelPicker settings={settings} value={draft.level} onChange={(level) => setDraft((current) => ({ ...current, level }))} />
      <ClassList settings={settings} draft={draft} onChange={(klass, mode) => setDraft((current) => ({ ...current, classes: { ...current.classes, [klass]: mode } }))} />
      <div className="flex flex-wrap items-center justify-end gap-2 border-t pt-4">
        {error ? (
          <div className="mr-auto min-w-0 basis-full sm:basis-auto">
            <InlineError text={error.text} detail={error.detail} requestId={error.requestId} />
          </div>
        ) : null}
        <Button type="button" variant="outline" disabled={!changed || save.isPending} onClick={() => setDraft(draftOf(settings))} data-testid="autonomy-discard">
          {t("discard")}
        </Button>
        <Button type="submit" disabled={!changed || save.isPending} data-testid="autonomy-save">
          {save.isPending ? t("saving") : t("save")}
        </Button>
      </div>
    </form>
  );
}

/**
 * The viewer's own autonomy (plan autonomy-levels, step 7): the level their agents work at and each class's mode
 * with the source that sets it, the preference they choose up to the ceilings set above them (an option above one
 * shows dimmed, with its reason), and each class's last 30 days. A stop that holds them, or a class the ceiling
 * lowers below their preference, is flagged; nothing else is.
 */
export function AutonomyPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("autonomy.settings");
  const state = useHubQuery(settingsQuery(browserApi), initialError);
  const settings = state.status === "success" ? state.data : null;
  const source = useAutonomySource(settings?.principal);
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        title={t("title")}
        testId="autonomy-header"
        tags={
          settings ? (
            <Badge variant="secondary" data-testid="autonomy-level">
              {t("level", { level: settings.level })}
            </Badge>
          ) : null
        }
        sub={settings ? <span data-testid="autonomy-level-source">{t("levelFrom", { source: source(settings.level_source) })}</span> : null}
      />
      <QueryView state={state} loading={<ListSkeleton rows={8} />}>
        {(data) => (
          <div className="flex min-w-0 flex-col gap-6">
            <Notes settings={data} />
            <SettingsForm key={`${data.revision ?? 0}`} settings={data} />
          </div>
        )}
      </QueryView>
    </div>
  );
}
