"use client";

import { useTranslations } from "next-intl";
import { useId } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { Switch } from "@/components/ui/switch";

import { useWorkerFailure, useWorkerWrite } from "./hooks";
import { type DispatchFrom, setDispatchFrom, type Worker } from "./queries";

/**
 * The owner's switch between runs dispatched with any of their credentials and runs dispatched from the web only. The
 * hub takes the change from a web session alone, and the switch shows what the hub holds: it moves once the hub
 * answers and the worker is read again. The result is a toast; a failure stays until dismissed.
 */
export function DispatchFromSwitch({ worker }: { worker: Worker }) {
  const t = useTranslations("workers.dispatchFrom");
  const id = useId();
  const failure = useWorkerFailure();
  const write = useWorkerWrite((api, value: DispatchFrom) => setDispatchFrom(api, worker.id, value));

  const change = async (webOnly: boolean) => {
    if (write.isPending) return;
    try {
      await write.mutateAsync(webOnly ? "web" : "any"); // resolves once the worker queries were reloaded
    } catch (error) {
      notifyFailure(t("failed", { name: worker.name }), failure(error));
      return;
    }
    notify(
      webOnly
        ? { tone: "success", text: t("webSuccess", { name: worker.name }), description: t("webSuccessText") }
        : { tone: "success", text: t("anySuccess", { name: worker.name }), description: t("anySuccessText") },
    );
  };

  return (
    <div className="mt-4 flex items-start gap-3 border-t pt-4" data-testid="worker-dispatch-from">
      <Switch
        id={`${id}-switch`}
        checked={worker.dispatch_from === "web"}
        onCheckedChange={(on) => void change(on)}
        aria-describedby={`${id}-hint`}
        aria-busy={write.isPending || undefined}
        className="mt-0.5"
        data-testid="worker-dispatch-from-switch"
      />
      <div className="flex min-w-0 flex-col gap-1">
        <label htmlFor={`${id}-switch`} className="cursor-pointer text-sm font-medium">
          {t("label")}
        </label>
        <p id={`${id}-hint`} className="text-xs leading-4 text-pretty text-fg-subtle">
          {t("hint")}
        </p>
      </div>
    </div>
  );
}
