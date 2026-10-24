"use client";

import { CircleCheck, CircleMinus, CircleSlash, Download, Hourglass, TriangleAlert, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { isApiError } from "@/lib/api/errors";

import { Ago } from "./ago";
import { type BrowserInstallOffer, browserInstallOffer, lastInstallProblem } from "./browser-install";
import { FormError } from "./form-error";
import { useWorkerFailure, useWorkerWrite } from "./hooks";
import type { Worker } from "./queries";
import { setBrowserInstall } from "./update-worker";

/**
 * The browser line at the foot of the Runtimes card: the Chromium the worker's daemon starts for runs dispatched with a
 * browser, or that it has none. Its owner installs one from here (Install browser), cancels an open request, and reads
 * why the last one failed; everyone who may see the worker reads the same line without the buttons.
 */
export function BrowserLine({ worker, isOwner }: { worker: Worker; isOwner: boolean }) {
  const t = useTranslations("workers.browserInstall");
  const offer = browserInstallOffer(worker);
  const problem = offer.kind === "installed" || offer.kind === "open" ? null : lastInstallProblem(worker);
  const controls = isOwner && (offer.kind === "ready" || offer.kind === "open" || (offer.kind === "blocked" && offer.why === "tooOld"));
  return (
    <div className="mt-4 flex flex-col gap-3 border-t pt-4" data-testid="worker-browser" data-offer={offer.kind}>
      <div className="flex items-start gap-2.5">
        {offer.kind === "installed" ? (
          <CircleCheck className="mt-0.5 size-4 shrink-0 text-success" aria-hidden="true" />
        ) : (
          <CircleMinus className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        )}
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="text-sm font-medium">{t("title")}</span>
          <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]" data-testid="worker-browser-detail">
            {worker.browser && offer.kind === "installed"
              ? t("installedDetail", {
                  version: worker.browser.chromium,
                  max: worker.browser.max,
                  sandbox: worker.browser.sandbox ? "on" : "off",
                })
              : t("notInstalled")}
          </span>
        </div>
        <Badge variant={offer.kind === "installed" ? "success" : "outline"}>
          {offer.kind === "installed" ? t("available") : t("missing")}
        </Badge>
      </div>
      {offer.kind === "open" ? <OpenRequest worker={worker} phase={offer.phase} /> : null}
      {problem ? (
        <p className="flex items-start gap-1.5 text-xs text-danger" data-testid="worker-browser-problem" data-outcome={problem.outcome}>
          <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          <span className="font-mono whitespace-pre-wrap [overflow-wrap:anywhere]">{t(problem.outcome, { reason: problem.reason })}</span>
        </p>
      ) : null}
      {controls ? <Controls worker={worker} offer={offer} /> : null}
    </div>
  );
}

/** What an open request waits for, with when it was asked. */
function OpenRequest({ worker, phase }: { worker: Worker; phase: "offline" | "installing" }) {
  const t = useTranslations("workers.browserInstall");
  return (
    <div className="flex items-start gap-2 text-sm" data-testid="worker-browser-status" data-phase={phase} role="status">
      <Hourglass className="mt-0.5 size-4 shrink-0 text-attention" aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-0.5">
        <p className="text-pretty">{t(`phase.${phase}`)}</p>
        {worker.browser_install ? (
          <span className="text-xs text-muted-foreground">
            {t("requested")} <Ago value={worker.browser_install.requested_at} never="" />
          </span>
        ) : null}
      </div>
    </div>
  );
}

/**
 * The owner's button: Install browser, or Cancel while a request is open, greyed with why when the daemon is too old. It
 * waits for the hub, then says so in a toast; a 404 or 409 reloads the page to what the hub holds with the reason in a
 * toast, anything else stays under the button until the next try, as Update now does.
 */
function Controls({ worker, offer }: { worker: Worker; offer: BrowserInstallOffer }) {
  const t = useTranslations("workers.browserInstall");
  const tErrors = useTranslations("workers.errors");
  const id = useId();
  const failure = useWorkerFailure();
  const write = useWorkerWrite((api, ask: boolean) => setBrowserInstall(api, worker.id, ask));
  const ask = offer.kind !== "open";
  const blocked = offer.kind === "blocked";

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
  const hint = blocked ? t("tooOld") : ask ? t("ready") : null;
  const describedBy = [hint ? `${id}-hint` : "", shown ? `${id}-error` : ""].filter(Boolean).join(" ") || undefined;
  const Icon = blocked ? CircleSlash : ask ? Download : X;

  return (
    <>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:gap-3">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void run()}
          aria-disabled={blocked || undefined}
          aria-describedby={describedBy}
          busy={write.isPending}
          className={blocked ? "opacity-45" : undefined}
          data-testid={ask ? "worker-browser-install" : "worker-browser-cancel"}
        >
          <Icon aria-hidden="true" />
          {ask ? (write.isPending ? t("pending") : t("button")) : write.isPending ? t("cancelPending") : t("cancel")}
        </Button>
        {hint ? (
          <p
            id={`${id}-hint`}
            className="max-w-[80ch] text-xs leading-4 text-pretty text-fg-subtle sm:pt-1"
            data-testid="worker-browser-hint"
            data-blocked={blocked || undefined}
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
            testId="worker-browser-error"
          />
        </div>
      ) : null}
    </>
  );
}
