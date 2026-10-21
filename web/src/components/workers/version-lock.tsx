"use client";

import { useTranslations } from "next-intl";
import { useId } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { Switch } from "@/components/ui/switch";

import { FormError } from "./form-error";
import { useWorkerFailure, useWorkerWrite } from "./hooks";
import type { Worker } from "./queries";
import { updateWorker } from "./update-worker";

/**
 * The owner's "Lock version" switch, at the foot of the Settings card: on, the daemon stays on the evo-agents it runs;
 * off, it updates itself to the tag nightly whenever it is idle. It saves as soon as it is flipped (PATCH
 * /v1/workers/{id} with version_locked) and shows what the hub holds: it moves once the hub answered and the worker
 * was read again, and the result is a toast. What the hub refuses stays under the switch until the next flip; a 404 or
 * 409 means the worker changed elsewhere (revoked, or no longer the visitor's), so the page reloads to what the hub
 * holds and the reason stays in a toast, as the header's actions do.
 */
export function VersionLockSwitch({ worker }: { worker: Worker }) {
  const t = useTranslations("workers.versionLock");
  const id = useId();
  const failure = useWorkerFailure();
  const write = useWorkerWrite((api, locked: boolean) => updateWorker(api, worker.id, { version_locked: locked }));

  const change = async (locked: boolean) => {
    if (write.isPending) return;
    try {
      await write.mutateAsync(locked); // resolves once the worker queries were reloaded
    } catch (error) {
      const failed = failure(error);
      if (failed.status === 404 || failed.status === 409) {
        notifyFailure(t(locked ? "lockFailed" : "unlockFailed", { name: worker.name }), failed);
      }
      return; // anything else is said under the switch, from the mutation's state
    }
    notify(
      locked
        ? { tone: "success", text: t("locked", { name: worker.name }), description: t("lockedText") }
        : { tone: "success", text: t("unlocked", { name: worker.name }), description: t("unlockedText") },
    );
  };

  const failed = write.isError ? failure(write.error) : null;
  const shown = failed && failed.status !== 404 && failed.status !== 409 ? failed : null;

  return (
    <div className="mt-5 flex flex-col gap-3 border-t pt-5" data-testid="worker-version-lock">
      <div className="flex items-start gap-3">
        <Switch
          id={`${id}-switch`}
          checked={worker.version_locked}
          onCheckedChange={(on) => void change(on)}
          aria-describedby={[`${id}-hint`, shown ? `${id}-error` : ""].filter(Boolean).join(" ")}
          aria-busy={write.isPending || undefined}
          className="mt-0.5"
          data-testid="worker-version-lock-switch"
        />
        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor={`${id}-switch`} className="cursor-pointer text-sm font-medium">
            {t("label")}
          </label>
          <p id={`${id}-hint`} className="max-w-[80ch] text-xs leading-4 text-pretty text-fg-subtle">
            {t("hint")}
          </p>
        </div>
      </div>
      {shown ? (
        <div id={`${id}-error`}>
          <FormError
            title={t(write.variables ? "lockFailed" : "unlockFailed", { name: worker.name })}
            text={shown.text}
            detail={shown.detail}
            requestId={shown.requestId}
            testId="worker-version-lock-error"
          />
        </div>
      ) : null}
    </div>
  );
}
