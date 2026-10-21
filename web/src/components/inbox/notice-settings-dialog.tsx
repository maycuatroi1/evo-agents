"use client";

import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { type ApiClient, call } from "@/lib/api/client";
import { browserApi } from "@/lib/api/browser";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * Notices in the Inbox's header (docs/notifications.md, Notices): a `secondary` button that opens a dialog of the kinds
 * of notice a member may turn off (GET and PUT /v1/me/notification-settings), each a switch that applies once flipped,
 * with what it brings. Today that is `run_outputs`, the files an agent sends from a run the member dispatched; the
 * questions of their runs always come. Off, the hub sends that kind on no channel, the Inbox included. A change is a
 * toast; a refusal too, and the switch shows what the hub holds.
 */
type NoticeSetting = components["schemas"]["NoticeSetting"];
type NoticeKind = NoticeSetting["kind"];

const settingsKey = ["me", "notification-settings"] as const;

const settingsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: settingsKey,
    queryFn: ({ signal }) => call(api().GET("/v1/me/notification-settings", { signal })),
  });

async function putSetting(api: ApiClient, kind: NoticeKind, on: boolean) {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/me/notification-settings", { body: { notices: [{ kind, on }] }, headers }));
}

function SettingRow({ setting }: { setting: NoticeSetting }) {
  const t = useTranslations("inbox.settings");
  const id = useId();
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const label = t(`kind.${setting.kind}.label`);
  const change = useMutation({
    mutationFn: (on: boolean) => putSetting(browserApi(), setting.kind, on),
    onSuccess: (settings, on) => {
      queryClient.setQueryData(settingsKey, settings);
      notify({ tone: "success", text: t("saved", { on: String(on), kind: label }) });
    },
    onError: (error) => notifyFailure(t("failed"), failure(error)),
    onSettled: () => queryClient.invalidateQueries({ queryKey: settingsKey }),
  });
  const on = change.isPending ? (change.variables ?? setting.on) : setting.on;
  return (
    <li className="flex items-start justify-between gap-4 py-3" data-testid={`notice-setting-${setting.kind}`}>
      <div className="flex min-w-0 flex-col gap-0.5">
        <label htmlFor={id} className="text-sm font-medium text-foreground">
          {label}
        </label>
        <p id={`${id}-hint`} className="text-[13px] leading-[18px] text-muted-foreground">
          {t(`kind.${setting.kind}.hint`)}
        </p>
      </div>
      <Switch
        id={id}
        checked={on}
        aria-describedby={`${id}-hint`}
        aria-busy={change.isPending || undefined}
        disabled={change.isPending}
        onCheckedChange={(next) => change.mutate(next)}
        className="mt-0.5"
        data-testid={`notice-setting-${setting.kind}-switch`}
      />
    </li>
  );
}

export function NoticeSettingsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("inbox.settings");
  const settings = useQuery({ ...settingsQuery(browserApi), enabled: open });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" data-testid="notice-settings-dialog">
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>
        {settings.isPending ? (
          <div role="status" aria-label={t("title")} className="flex flex-col gap-2 py-3">
            <Skeleton className="h-5 w-1/2" />
            <Skeleton className="h-4 w-full" />
          </div>
        ) : settings.isError ? (
          <div className="flex flex-wrap items-center gap-2 text-sm text-danger" role="alert">
            {t("loadFailed")}
            <Button type="button" variant="outline" size="sm" onClick={() => void settings.refetch()}>
              {t("retry")}
            </Button>
          </div>
        ) : (
          <ul className="divide-y border-y">
            {settings.data.notices.map((setting) => (
              <SettingRow key={setting.kind} setting={setting} />
            ))}
          </ul>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            {t("close")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
