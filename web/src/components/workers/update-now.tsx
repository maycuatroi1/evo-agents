"use client";

import { CircleSlash, Hourglass, RefreshCw, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { isApiError } from "@/lib/api/errors";

import { Ago } from "./ago";
import { FormError } from "./form-error";
import { useWorkerFailure, useWorkerWrite } from "./hooks";
import type { Worker } from "./queries";
import { type UpdateNowOffer, updateNowOffer, updateNowPhase } from "./self-update";
import { setUpdateNow } from "./update-worker";

/**
 * Update now, at the foot of the Version card. Its owner sees a button while the worker runs another commit than
 * nightly or its last update failed, greyed with why when the hub would refuse it (a locked version, a daemon that
 * cannot update itself or is too old); with a request open, everyone who may see the worker reads what it waits for,
 * and its owner may cancel it. A hub admin who is not the owner only reads. Nothing shows while the worker runs nightly.
 */
export function UpdateNow({ worker, isOwner }: { worker: Worker; isOwner: boolean }) {
  const offer = updateNowOffer(worker);
  const last = worker.update_now;
  const impossible = offer.kind !== "open" && last?.outcome === "impossible" && last.reason ? last.reason : null;
  if (offer.kind === "hidden" || (!isOwner && offer.kind !== "open")) return null;
  return (
    <div className="mt-5 flex flex-col gap-3 border-t pt-5" data-testid="update-now" data-offer={offer.kind}>
      {offer.kind === "open" ? <OpenRequest worker={worker} /> : null}
      {isOwner ? <Controls worker={worker} offer={offer} /> : null}
      {impossible ? <ImpossibleLine reason={impossible} /> : null}
    </div>
  );
}

function ImpossibleLine({ reason }: { reason: string }) {
  const t = useTranslations("workers.updateNow");
  return (
    <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]" data-testid="update-now-impossible">
      {t("impossible", { reason })}
    </p>
  );
}

/** What an open request waits for, with when it was asked. */
function OpenRequest({ worker }: { worker: Worker }) {
  const t = useTranslations("workers.updateNow");
  const phase = updateNowPhase(worker);
  return (
    <div className="flex items-start gap-2 text-sm" data-testid="update-now-status" data-phase={phase} role="status">
      <Hourglass className="mt-0.5 size-4 shrink-0 text-attention" aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-0.5">
        <p className="text-pretty">{t(`phase.${phase}`, { count: worker.held_runs })}</p>
        {worker.update_now ? (
          <span className="text-xs text-muted-foreground">
            {t("requested")} <Ago value={worker.update_now.requested_at} never="" />
          </span>
        ) : null}
      </div>
    </div>
  );
}

/**
 * The owner's button: Update now, or Cancel while a request is open. It waits for the hub, then says so in a toast. What
 * the hub refuses with 403, 422 or 503 stays under the button until the next try; a 404 or 409 means the worker changed
 * or the hub has a reason of its own (its version was locked, it runs nightly already), so the page reloads to what the
 * hub holds and the reason stays in a toast, as the Lock version switch does.
 */
function Controls({ worker, offer }: { worker: Worker; offer: UpdateNowOffer }) {
  const t = useTranslations("workers.updateNow");
  const tErrors = useTranslations("workers.errors");
  const id = useId();
  const failure = useWorkerFailure();
  const write = useWorkerWrite((api, ask: boolean) => setUpdateNow(api, worker.id, ask));
  const ask = offer.kind !== "open";
  const blocked = offer.kind === "blocked" ? offer.why : null;

  const run = async () => {
    if (write.isPending || blocked) return;
    try {
      await write.mutateAsync(ask); // resolves once the worker queries were reloaded
    } catch (error) {
      const failed = failure(error, { 409: t("refused") });
      if (failed.status === 404 || failed.status === 409) {
        const detail = failed.status === 409 && isApiError(error) ? tErrors("apiMessage", { message: error.info.message }) : failed.detail;
        notifyFailure(t(ask ? "askFailed" : "cancelFailed", { name: worker.name }), { ...failed, detail });
      }
      return; // anything else is said under the button, from the mutation's state
    }
    notify(
      ask
        ? { tone: "success", text: t("asked", { name: worker.name }), description: t("askedText") }
        : { tone: "success", text: t("cancelled", { name: worker.name }), description: t("cancelledText") },
    );
  };

  const failed = write.isError ? failure(write.error) : null;
  const shown = failed && failed.status !== 404 && failed.status !== 409 ? failed : null;
  const hint = blocked ? t(`blocked.${blocked}`) : ask ? t(worker.self_update?.error ? "readyFailed" : "ready") : null;
  const describedBy = [hint ? `${id}-hint` : "", shown ? `${id}-error` : ""].filter(Boolean).join(" ") || undefined;
  const Icon = blocked ? CircleSlash : ask ? RefreshCw : X;

  return (
    <>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:gap-3">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void run()}
          aria-disabled={blocked !== null || undefined}
          aria-describedby={describedBy}
          busy={write.isPending}
          className={blocked ? "opacity-45" : undefined}
          data-testid={ask ? "update-now-button" : "update-now-cancel"}
        >
          <Icon aria-hidden="true" />
          {ask ? (write.isPending ? t("pending") : t("button")) : write.isPending ? t("cancelPending") : t("cancel")}
        </Button>
        {hint ? (
          <p
            id={`${id}-hint`}
            className="max-w-[80ch] text-xs leading-4 text-pretty text-fg-subtle sm:pt-1"
            data-testid="update-now-hint"
            data-blocked={blocked ?? undefined}
          >
            {hint}
          </p>
        ) : null}
      </div>
      {shown ? (
        <div id={`${id}-error`}>
          <FormError
            title={t(write.variables === false ? "cancelFailed" : "askFailed", { name: worker.name })}
            text={shown.text}
            detail={shown.detail}
            requestId={shown.requestId}
            testId="update-now-error"
          />
        </div>
      ) : null}
    </>
  );
}
