"use client";

import { Globe, RotateCw, Send, Smartphone, Trash2 } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useState, useSyncExternalStore } from "react";

import { Ago } from "@/components/data/ago";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { PushDevice } from "@/lib/notification-queries";

import { SettingsCard, StateBlock } from "./parts";
import { readMark } from "./push-browser";
import { useDeviceName, useRemoveDevice, useTestPush } from "./use-device-actions";

const noSubscribe = () => () => {};

export type DevicesState = { status: "loading" } | { status: "error"; retry: () => void } | { status: "success"; devices: PushDevice[] };

/** One device: its kind's icon, its name and This browser on the one this is, when it was added and last reached. */
function DeviceRow({ device, here, onRemove, test }: { device: PushDevice; here: boolean; onRemove: () => void; test: ReturnType<typeof useTestPush> }) {
  const t = useTranslations("notifications.devices");
  const format = useFormatter();
  const name = useDeviceName()(device);
  const Icon = device.kind === "webpush" ? Globe : Smartphone;
  const sending = test.sending === device.id;
  return (
    <li className="flex min-w-0 flex-col gap-2 border-t px-4 py-3 first:border-t-0 sm:flex-row sm:items-center sm:gap-4" data-testid="push-device" data-device-id={device.id} data-kind={device.kind}>
      <div className="flex min-w-0 flex-1 items-start gap-3">
        <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <div className="flex min-w-0 flex-col gap-0.5">
          <p className="flex min-w-0 flex-wrap items-center gap-2 text-sm leading-5 font-medium text-foreground">
            <span className="[overflow-wrap:anywhere]" data-testid="push-device-name">
              {name}
            </span>
            {here ? (
              <span className="rounded-xs bg-muted px-1.5 py-0.5 text-xs font-medium text-muted-foreground" data-testid="push-device-here">
                {t("thisBrowser")}
              </span>
            ) : null}
          </p>
          <p className="text-xs leading-4 text-fg-subtle" data-testid="push-device-facts">
            {t("added", { date: format.dateTime(new Date(device.created_at), { dateStyle: "medium" }) })}
            {", "}
            {device.last_sent_at ? t.rich("lastPush", { time: () => <Ago value={device.last_sent_at} never="-" /> }) : t("noPush")}
          </p>
        </div>
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2 pl-7 sm:pl-0">
        <Button type="button" variant="outline" size="sm" busy={sending} onClick={() => test.send(device)} aria-label={t("testLabel", { device: name })} data-testid="push-device-test">
          <Send aria-hidden="true" />
          {sending ? t("testing") : t("test")}
        </Button>
        <Button type="button" variant="quiet-danger" size="sm" onClick={onRemove} aria-label={t("removeLabel", { device: name })} data-testid="push-device-remove">
          <Trash2 aria-hidden="true" />
          {t("remove")}
        </Button>
      </div>
    </li>
  );
}

/**
 * The member's devices that push reaches, newest first, each with a test push and Remove (confirmed, since the device
 * then gets nothing until push is turned on there again). Empty, the card says where push is turned on.
 */
export function DevicesCard({ state, className }: { state: DevicesState; className?: string }) {
  const t = useTranslations("notifications.devices");
  const name = useDeviceName();
  const failure = useWriteFailure();
  const test = useTestPush();
  const [removing, setRemoving] = useState<PushDevice | null>(null);
  const remove = useRemoveDevice((device) => {
    setRemoving(null);
    notify({ tone: "success", text: t("removed", { device: name(device) }) });
  });
  const here = useSyncExternalStore(noSubscribe, () => readMark()?.id ?? null, () => null);

  let body;
  if (state.status === "loading") {
    body = (
      <div className="flex flex-col gap-3 px-4 py-4" role="status" aria-label={t("title")}>
        <Skeleton className="h-4 w-1/2 rounded-xs" />
        <Skeleton className="h-3 w-1/3 rounded-xs" />
      </div>
    );
  } else if (state.status === "error") {
    body = (
      <div className="flex flex-wrap items-center gap-2 px-4 py-4 text-sm text-danger" role="alert" data-testid="push-devices-error">
        {t("loadFailed")}
        <Button type="button" variant="outline" size="sm" onClick={state.retry}>
          <RotateCw aria-hidden="true" />
          {t("retry")}
        </Button>
      </div>
    );
  } else if (state.devices.length === 0) {
    body = (
      <StateBlock icon={Smartphone} title={t("empty")} testId="push-devices-empty" state="empty">
        {t("emptyText")}
      </StateBlock>
    );
  } else {
    body = (
      <ul aria-label={t("title")} data-testid="push-devices">
        {state.devices.map((device) => (
          <DeviceRow key={device.id} device={device} here={device.id === here} test={test} onRemove={() => setRemoving(device)} />
        ))}
      </ul>
    );
  }

  const count = state.status === "success" ? state.devices.length : null;
  return (
    <SettingsCard
      title={t("title")}
      action={count ? <span className="text-xs text-fg-subtle tabular-nums" data-testid="push-devices-count">{t("count", { count })}</span> : null}
      testId="push-devices-card"
      className={className}
    >
      {body}
      <ConfirmAction
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open) {
            setRemoving(null);
            remove.reset();
          }
        }}
        title={removing ? t("removeTitle", { device: name(removing) }) : ""}
        description={<p>{t("removeText")}</p>}
        confirmLabel={t("removeConfirm")}
        pendingLabel={t("removing")}
        cancelLabel={t("keep")}
        pending={remove.isPending}
        error={remove.error ? failure(remove.error) : null}
        onConfirm={() => removing && remove.mutate(removing)}
        testId="push-device-remove-dialog"
      />
    </SettingsCard>
  );
}
