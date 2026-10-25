"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { type NotificationSettings, notificationSettingsQuery, pushConfigQuery, pushDevicesQuery } from "@/lib/notification-queries";

import { type DevicesState, DevicesCard } from "./devices-card";
import { LockScreenCard, NoticesCard } from "./privacy-card";
import { ScheduleCard } from "./schedule-card";
import { type ConfigState, ThisBrowser } from "./this-browser";
import { TiersCard } from "./tiers-card";

function PageSkeleton() {
  return (
    <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_360px] xl:grid-cols-[minmax(0,1fr)_400px]">
      {[0, 1].map((column) => (
        <div key={column} className="flex flex-col gap-6">
          {[0, 1].map((card) => (
            <div key={card} className="overflow-hidden rounded-md border bg-card shadow-raised">
              <div className="flex h-12 items-center border-b px-4">
                <Skeleton className="h-3.5 w-32 rounded-xs" />
              </div>
              <div className="flex flex-col gap-2 px-4 py-4">
                <Skeleton className="h-3.5 w-2/3 rounded-xs" />
                <Skeleton className="h-3 w-1/2 rounded-xs" />
              </div>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

/**
 * The cards, in two columns from the lg breakpoint (a fluid one, a side one of fixed width) and one below. On a phone
 * they stack in the order a member sets push up in: this browser, the devices, the quiet hours and summary, the lock
 * screen, then the tiers of every kind and the notices. Each column is `contents` below lg, so `order` places its
 * cards in the one column.
 */
function Settings({ settings, config, devices }: { settings: NotificationSettings; config: ConfigState; devices: DevicesState }) {
  return (
    <div className="flex flex-col gap-6 lg:grid lg:grid-cols-[minmax(0,1fr)_360px] lg:items-start xl:grid-cols-[minmax(0,1fr)_400px]" data-testid="notification-settings">
      <div className="contents lg:flex lg:min-w-0 lg:flex-col lg:gap-6">
        <ThisBrowser config={config} devices={devices.status === "success" ? devices.devices : undefined} className="order-1 lg:order-none" />
        <DevicesCard state={devices} className="order-2 lg:order-none" />
        <TiersCard tiers={settings.tiers} className="order-5 lg:order-none" />
      </div>
      <div className="contents lg:flex lg:min-w-0 lg:flex-col lg:gap-6">
        <ScheduleCard settings={settings} className="order-3 lg:order-none" />
        <LockScreenCard settings={settings} className="order-4 lg:order-none" />
        <NoticesCard settings={settings} className="order-6 lg:order-none" />
      </div>
    </div>
  );
}

/**
 * Notifications (`/settings/notifications`, from the user menu; docs/notifications.md): push on this browser, the
 * member's devices, when each kind of notification pushes, quiet hours with their time zone and the time of the
 * summary push, and what a push shows on a locked screen. The settings are the page's query; the hub's push
 * configuration and the devices are read beside them, and a failure of either stays inside its own card.
 */
export function NotificationsPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("notifications");
  const state = useHubQuery(notificationSettingsQuery(browserApi), initialError);
  const configQuery = useQuery(pushConfigQuery(browserApi));
  const devicesQuery = useQuery(pushDevicesQuery(browserApi));
  const config: ConfigState = configQuery.isSuccess
    ? { status: "success", config: configQuery.data }
    : configQuery.isError
      ? { status: "error", retry: () => void configQuery.refetch() }
      : { status: "loading" };
  const devices: DevicesState = devicesQuery.isSuccess
    ? { status: "success", devices: devicesQuery.data.devices }
    : devicesQuery.isError
      ? { status: "error", retry: () => void devicesQuery.refetch() }
      : { status: "loading" };
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader title={t("title")} testId="notifications-header" />
      <QueryView state={state} loading={<PageSkeleton />}>
        {(settings) => <Settings settings={settings} config={config} devices={devices} />}
      </QueryView>
    </div>
  );
}
