"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { notificationKeys, type PushDevice, removePushDevice, testPushDevice } from "@/lib/notification-queries";

import { currentSubscription, readMark, writeMark } from "./push-browser";

/** A device as its owner knows it: the name they gave it, else its kind in words. */
export function useDeviceName() {
  const t = useTranslations("notifications.devices");
  return (device: Pick<PushDevice, "kind" | "label">) => device.label ?? t(`kind.${device.kind}`);
}

/**
 * Send a test push to a device, and say how it went: taken by its push service; gone, so the hub removed it (and, for
 * this browser, its mark goes too); or why it did not go, the push service's words as a detail. A kind whose sender
 * the hub turned off answers 503.
 */
export function useTestPush() {
  const t = useTranslations("notifications.devices");
  const name = useDeviceName();
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const test = useMutation({
    mutationFn: (device: PushDevice) => testPushDevice(browserApi(), device.id),
    onSuccess: (result, device) => {
      const device_ = name(device);
      if (result.sent) {
        notify({ tone: "success", text: t("testSent", { device: device_ }), description: t("testSentText") });
        return;
      }
      if (result.removed) {
        if (readMark()?.id === device.id) writeMark(null);
        notify({ tone: "info", text: t("testRemoved", { device: device_ }), description: t("testRemovedText") });
        return;
      }
      notify({ tone: "error", text: t("testFailed"), detail: result.error });
    },
    onError: (error, device) => {
      const said: WriteFailure = isApiError(error) && error.info.status === 503 ? { ...failure(error), text: t("testOff") } : failure(error);
      notifyFailure(t("testFailedFor", { device: name(device) }), said);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: notificationKeys.devices }),
  });
  return { send: (device: PushDevice) => test.mutate(device), sending: test.isPending ? test.variables?.id : undefined };
}

/**
 * Remove a device of the member's: it gets no more push. A device already gone is removed all the same. Removing this
 * browser's device also ends its subscription, so the browser holds none the hub does not know.
 */
export function useRemoveDevice(onRemoved?: (device: PushDevice) => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (device: PushDevice) => {
      try {
        await removePushDevice(browserApi(), device.id);
      } catch (error) {
        if (!(isApiError(error) && error.info.status === 404)) throw error;
      }
      if (readMark()?.id === device.id) {
        writeMark(null);
        await currentSubscription()
          .then((subscription) => subscription?.unsubscribe())
          .catch(() => undefined); // the hub no longer sends to it either way
      }
      return device;
    },
    onSuccess: (device) => onRemoved?.(device),
    onSettled: () => queryClient.invalidateQueries({ queryKey: notificationKeys.devices }),
  });
}
