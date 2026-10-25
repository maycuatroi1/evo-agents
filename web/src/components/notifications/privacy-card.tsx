"use client";

import { useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { Switch } from "@/components/ui/switch";
import type { NotificationSettings } from "@/lib/notification-queries";

import { SettingsCard } from "./parts";
import { useSettingsWrite } from "./use-settings-write";

type NoticeKind = NotificationSettings["notices"][number]["kind"];

/** A setting that applies as soon as it is flipped: its label and hint on the left, the switch on the right. */
function SwitchRow({
  label,
  hint,
  checked,
  busy,
  onChange,
  testId,
}: {
  label: string;
  hint: ReactNode;
  checked: boolean;
  busy: boolean;
  onChange: (on: boolean) => void;
  testId: string;
}) {
  const id = useId();
  return (
    <div className="flex items-start justify-between gap-4 border-t px-4 py-3 first:border-t-0" data-testid={testId}>
      <div className="flex min-w-0 flex-col gap-0.5">
        <label htmlFor={id} className="text-sm font-medium text-foreground">
          {label}
        </label>
        <p id={`${id}-hint`} className="text-[13px] leading-[18px] text-pretty text-muted-foreground">
          {hint}
        </p>
      </div>
      <Switch
        id={id}
        checked={checked}
        aria-describedby={`${id}-hint`}
        aria-busy={busy || undefined}
        onCheckedChange={onChange}
        className="mt-0.5"
        data-testid={`${testId}-switch`}
      />
    </div>
  );
}

/**
 * What a push may show on a locked screen (docs/notifications.md, What a push carries): with details hidden, only the
 * kind of thing, the project and the link. Applied as soon as it is flipped.
 */
export function LockScreenCard({ settings, className }: { settings: NotificationSettings; className?: string }) {
  const t = useTranslations("notifications.privacy");
  const hide = useSettingsWrite<boolean>({
    body: (on) => ({ hide_details: on }),
    saved: (on) => (on ? t("hidden") : t("shown")),
    failed: () => t("failed"),
  });
  return (
    <SettingsCard title={t("title")} testId="lock-screen-card" className={className}>
      <SwitchRow
        label={t("hide")}
        hint={t("hideHint")}
        checked={hide.busy ? Boolean(hide.pending) : settings.hide_details}
        busy={hide.busy}
        onChange={(on) => hide.write(on)}
        testId="hide-details"
      />
    </SettingsCard>
  );
}

/**
 * The kinds of notice a member may turn off for every channel, the Inbox included, as the Inbox's Notices dialog
 * offers them (today the files an agent sends from the member's runs).
 */
export function NoticesCard({ settings, className }: { settings: NotificationSettings; className?: string }) {
  const t = useTranslations("inbox.settings");
  const tCard = useTranslations("notifications.notices");
  const notice = useSettingsWrite<{ kind: NoticeKind; on: boolean }>({
    body: ({ kind, on }) => ({ notices: [{ kind, on }] }),
    saved: ({ kind, on }) => t("saved", { on: String(on), kind: t(`kind.${kind}.label`) }),
    failed: () => t("failed"),
  });
  return (
    <SettingsCard title={tCard("title")} testId="notices-card" className={className}>
      <p className="px-4 py-3 text-[13px] leading-[18px] text-pretty text-muted-foreground">{tCard("description")}</p>
      {settings.notices.map((setting) => (
        <SwitchRow
          key={setting.kind}
          label={t(`kind.${setting.kind}.label`)}
          hint={t(`kind.${setting.kind}.hint`)}
          checked={notice.pending?.kind === setting.kind ? notice.pending.on : setting.on}
          busy={notice.pending?.kind === setting.kind}
          onChange={(on) => notice.write({ kind: setting.kind, on })}
          testId={`notice-${setting.kind}`}
        />
      ))}
    </SettingsCard>
  );
}
