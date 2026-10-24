"use client";

import { useTranslations } from "next-intl";
import { useId } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

import { useWorkerFailure, useWorkerWrite } from "./hooks";
import type { Worker } from "./queries";
import { updateWorker } from "./update-worker";

/**
 * The owner's switch that shares the worker with the writers of its projects: on, it also takes their runs, one
 * member's runs at a time, with their accounts and secrets; off, it takes the owner's runs alone. It saves as soon as
 * it is flipped (PATCH /v1/workers/{id} with shared) and shows what the hub holds: it moves once the hub answered and
 * the worker was read again. The result is a toast; a failure stays until dismissed. With `locked`, the reason its
 * doctor gives, the reason shows under it, and a worker narrowed to units that is not shared cannot be shared again
 * until the doctor says yes; a shared one may always stop.
 */
export function SharedSwitch({ worker, locked = null, className }: { worker: Worker; locked?: string | null; className?: string }) {
  const t = useTranslations("workers.shared");
  const id = useId();
  const failure = useWorkerFailure();
  const write = useWorkerWrite((api, shared: boolean) => updateWorker(api, worker.id, { shared }));

  const blocked = Boolean(locked);
  const lock = blocked && !worker.shared;

  const change = async (shared: boolean) => {
    if (write.isPending || (shared && lock)) return;
    try {
      await write.mutateAsync(shared); // resolves once the worker queries were reloaded
    } catch (error) {
      notifyFailure(t("failed", { name: worker.name }), failure(error));
      return;
    }
    notify(
      shared
        ? { tone: "success", text: t("sharedSuccess", { name: worker.name }), description: t("sharedSuccessText") }
        : { tone: "success", text: t("privateSuccess", { name: worker.name }), description: t("privateSuccessText") },
    );
  };

  return (
    <div className={cn("mt-4 flex items-start gap-3 border-t pt-4", className)} data-testid="worker-shared" data-locked={lock ? "true" : undefined}>
      <Switch
        id={`${id}-switch`}
        checked={worker.shared}
        onCheckedChange={(on) => void change(on)}
        disabled={lock}
        aria-describedby={blocked ? `${id}-hint ${id}-lock` : `${id}-hint`}
        aria-busy={write.isPending || undefined}
        className="mt-0.5"
        data-testid="worker-shared-switch"
      />
      <div className="flex min-w-0 flex-col gap-1">
        <label htmlFor={`${id}-switch`} className="cursor-pointer text-sm font-medium">
          {t("label")}
        </label>
        <p id={`${id}-hint`} className="max-w-[80ch] text-xs leading-4 text-pretty text-fg-subtle">
          {t("hint")}
        </p>
        {blocked ? (
          <p id={`${id}-lock`} className="max-w-[80ch] text-xs leading-4 text-pretty text-attention" data-testid="worker-shared-locked">
            {t("locked", { reason: locked ?? "" })}
          </p>
        ) : null}
      </div>
    </div>
  );
}
