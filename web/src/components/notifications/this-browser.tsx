"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Bell, BellOff, BellRing, MonitorX, RotateCw, Send, SquarePlus, TriangleAlert, Upload } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";

import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { addWebPushDevice, notificationKeys, type PushConfig, type PushDevice } from "@/lib/notification-queries";

import { SettingsCard, StateBlock } from "./parts";
import {
  type BrowserFacts,
  browserFacts,
  currentSubscription,
  type DeviceMark,
  deviceLabel,
  pushSupport,
  readMark,
  subscribe,
  subscriptionJson,
  writeMark,
} from "./push-browser";
import { useRemoveDevice, useTestPush } from "./use-device-actions";

type Facts = BrowserFacts & { permission: NotificationPermission | null };

/** The hub's push configuration as the page reads it beside the settings. */
export type ConfigState = { status: "loading" } | { status: "error"; retry: () => void } | { status: "success"; config: PushConfig };

const noSubscribe = () => () => {};

/** What this browser can do, once the page runs in it; null on the server and while hydrating, so both render alike. */
function useFacts(): Facts | null {
  const raw = useSyncExternalStore(
    noSubscribe,
    () => {
      const facts = browserFacts();
      return JSON.stringify({ ...facts, permission: facts.notification ? Notification.permission : null });
    },
    () => null,
  );
  return useMemo(() => (raw === null ? null : (JSON.parse(raw) as Facts)), [raw]);
}

/**
 * This browser's subscription and the device of the hub it is, read once the browser can push. A subscription the
 * browser replaced since this browser's device was added goes to the hub at once, as the service worker's
 * `pushsubscriptionchange` does; one the member never turned on here is left alone.
 */
function useHeld(active: boolean) {
  const queryClient = useQueryClient();
  const [held, setHeld] = useState<{ endpoint: string | null; mark: DeviceMark | null } | null>(null);
  useEffect(() => {
    if (!active) return;
    let live = true;
    const read = async () => {
      const subscription = await currentSubscription().catch(() => null);
      let mark = readMark();
      if (subscription && mark && mark.endpoint !== subscription.endpoint) {
        const device = await addWebPushDevice(browserApi(), subscriptionJson(subscription), deviceLabel(navigator.userAgent)).catch(() => null);
        if (device) {
          mark = { id: device.id, endpoint: subscription.endpoint };
          writeMark(mark);
          void queryClient.invalidateQueries({ queryKey: notificationKeys.devices });
        }
      }
      if (live) setHeld({ endpoint: subscription?.endpoint ?? null, mark });
    };
    void read();
    return () => {
      live = false;
    };
  }, [active, queryClient]);
  return [held, setHeld] as const;
}

function HomeScreenSteps() {
  const t = useTranslations("notifications.browser");
  const steps = [
    { icon: Upload, text: t("homeScreenShare") },
    { icon: SquarePlus, text: t("homeScreenAdd") },
    { icon: BellRing, text: t("homeScreenOpen") },
  ];
  return (
    <ol className="flex flex-col gap-2 border-t px-4 py-3" aria-label={t("homeScreenSteps")} data-testid="push-home-screen-steps">
      {steps.map(({ icon: Icon, text }, index) => (
        <li key={index} className="flex items-start gap-3 text-sm text-foreground">
          <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-muted text-xs font-medium tabular-nums text-muted-foreground" aria-hidden="true">
            {index + 1}
          </span>
          <span className="flex min-w-0 flex-wrap items-center gap-1.5 pt-0.5 text-pretty">
            <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            {text}
          </span>
        </li>
      ))}
    </ol>
  );
}

/**
 * Push on this browser: whether the hub sends Web Push at all, whether this browser can take it (an iPhone first needs
 * the hub on its Home Screen, with the steps to put it there), whether the site may notify, and then one button that
 * turns push on, the only place the page asks the browser for permission. Once on: a test push and Turn off, which
 * removes the device and ends the subscription.
 */
export function ThisBrowser({ config: state, devices, className }: { config: ConfigState; devices: PushDevice[] | undefined; className?: string }) {
  const t = useTranslations("notifications.browser");
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const facts = useFacts();
  const support = facts ? pushSupport(facts) : null;
  const config = state.status === "success" ? state.config : null;
  const hubOn = config !== null && config.senders.webpush && config.vapid_public_key !== null;
  const [held, setHeld] = useHeld(support === "supported" && hubOn);
  const [permission, setPermission] = useState<NotificationPermission | null>(null);
  const [busy, setBusy] = useState<"on" | "off" | null>(null);
  const [error, setError] = useState<WriteFailure | null>(null);
  const test = useTestPush();
  const remove = useRemoveDevice();
  const allowed = permission ?? facts?.permission ?? null;
  const device = held?.mark ? (devices?.find((item) => item.id === held.mark?.id) ?? null) : null;
  const on = Boolean(held?.endpoint && device);

  const turnOn = async () => {
    setError(null);
    setBusy("on");
    try {
      // The first thing the click does: Safari grants permission only from a person's gesture.
      const answer = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
      setPermission(answer);
      if (answer === "default") setError({ text: t("dismissed"), detail: null, requestId: null, status: 0 });
      if (answer !== "granted") return;
      let subscription: PushSubscription;
      try {
        subscription = await subscribe(config!.vapid_public_key!);
      } catch (caught) {
        // A DOMException (AbortError, NotAllowedError) says why in its message.
        const message = typeof caught === "object" && caught !== null && "message" in caught ? String(caught.message) : String(caught);
        setError({ text: t("enableFailed"), detail: t("subscribeFailed", { message }), requestId: null, status: 0 });
        return;
      }
      const added = await addWebPushDevice(browserApi(), subscriptionJson(subscription), deviceLabel(navigator.userAgent));
      const mark = { id: added.id, endpoint: subscription.endpoint };
      writeMark(mark);
      setHeld({ endpoint: subscription.endpoint, mark });
      await queryClient.invalidateQueries({ queryKey: notificationKeys.devices });
      notify({ tone: "success", text: t("enabled"), description: t("enabledText") });
    } catch (caught) {
      setError(failure(caught, { 409: t("tooMany") }));
    } finally {
      setBusy(null);
    }
  };

  const turnOff = async () => {
    if (!device) return;
    setError(null);
    setBusy("off");
    try {
      await remove.mutateAsync(device);
      setHeld({ endpoint: null, mark: null });
      notify({ tone: "success", text: t("disabled") });
    } catch (caught) {
      setError({ ...failure(caught), text: t("disableFailed") });
    } finally {
      setBusy(null);
    }
  };

  let body;
  if (state.status === "error") {
    body = (
      <StateBlock
        icon={TriangleAlert}
        tone="danger"
        title={t("configFailed")}
        testId="push-browser"
        state="error"
        actions={
          <Button type="button" variant="outline" onClick={state.retry}>
            <RotateCw aria-hidden="true" />
            {t("retry")}
          </Button>
        }
      />
    );
  } else if (state.status === "success" && !hubOn) {
    body = (
      <StateBlock icon={BellOff} title={t("hubOff")} testId="push-browser" state="hub-off">
        {t("hubOffText")}
      </StateBlock>
    );
  } else if (state.status === "loading" || facts === null || (support === "supported" && (held === null || devices === undefined))) {
    body = (
      <div className="flex items-center gap-3 px-4 py-4" role="status" aria-label={t("checking")} data-testid="push-browser-checking">
        <Skeleton className="size-8 rounded-full" />
        <div className="flex flex-1 flex-col gap-1.5">
          <Skeleton className="h-3.5 w-48 rounded-xs" />
          <Skeleton className="h-3 w-64 max-w-full rounded-xs" />
        </div>
      </div>
    );
  } else if (support === "homeScreen") {
    body = (
      <>
        <StateBlock icon={SquarePlus} title={t("homeScreen")} testId="push-browser" state="home-screen">
          {t("homeScreenText")}
        </StateBlock>
        <HomeScreenSteps />
      </>
    );
  } else if (support === "unsupported") {
    body = (
      <StateBlock icon={MonitorX} title={t("unsupported")} testId="push-browser" state="unsupported">
        {t("unsupportedText")}
      </StateBlock>
    );
  } else if (on && device) {
    body = (
      <StateBlock
        icon={BellRing}
        tone="success"
        title={t("on")}
        testId="push-browser"
        state="on"
        actions={
          <>
            <Button type="button" variant="outline" busy={test.sending === device.id} onClick={() => test.send(device)} data-testid="push-browser-test">
              <Send aria-hidden="true" />
              {test.sending === device.id ? t("testing") : t("test")}
            </Button>
            <Button type="button" variant="quiet-danger" busy={busy === "off"} onClick={() => void turnOff()} data-testid="push-browser-off">
              <BellOff aria-hidden="true" />
              {busy === "off" ? t("turningOff") : t("turnOff")}
            </Button>
          </>
        }
      >
        {t("onText", { device: device.label ?? deviceLabel(navigator.userAgent) })}
      </StateBlock>
    );
  } else if (allowed === "denied") {
    body = (
      <StateBlock icon={BellOff} title={t("denied")} testId="push-browser" state="denied">
        {t("deniedText")}
      </StateBlock>
    );
  } else {
    body = (
      <StateBlock
        icon={Bell}
        title={t("off")}
        testId="push-browser"
        state="off"
        actions={
          <Button type="button" busy={busy === "on"} onClick={() => void turnOn()} data-testid="push-browser-on">
            <BellRing aria-hidden="true" />
            {busy === "on" ? t("turningOn") : t("turnOn")}
          </Button>
        }
      >
        {t("offText")}
      </StateBlock>
    );
  }

  return (
    <SettingsCard title={t("title")} testId="push-browser-card" className={className}>
      {body}
      {error ? (
        <div className="px-4 pb-4">
          <InlineError text={error.text} detail={error.detail} requestId={error.requestId} />
        </div>
      ) : null}
    </SettingsCard>
  );
}
