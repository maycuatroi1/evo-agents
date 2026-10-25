"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { browserApi } from "@/lib/api/browser";
import { type NotificationSettings, type NotificationSettingsIn, notificationKeys, putNotificationSettings } from "@/lib/notification-queries";

/**
 * A change of the member's notification settings that applies as soon as it is made (a switch, a tier): the hub's
 * answer replaces the settings the page holds, and a toast says what changed or why the hub refused it. While it is
 * on its way, `pending` names what it sets, so the control shows the value it is sending.
 */
export function useSettingsWrite<V>({
  body,
  saved,
  failed,
}: {
  body: (value: V) => NotificationSettingsIn;
  saved: (value: V) => string;
  failed: (value: V) => string;
}) {
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const write = useMutation({
    mutationFn: (value: V) => putNotificationSettings(browserApi(), body(value)),
    onSuccess: (settings: NotificationSettings, value) => {
      queryClient.setQueryData(notificationKeys.settings, settings);
      notify({ tone: "success", text: saved(value) });
    },
    onError: (error, value) => notifyFailure(failed(value), failure(error)),
    onSettled: () => queryClient.invalidateQueries({ queryKey: notificationKeys.settings }),
  });
  return {
    write: (value: V) => write.mutate(value),
    pending: write.isPending ? (write.variables as V) : undefined,
    busy: write.isPending,
  };
}
