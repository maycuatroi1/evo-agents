"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, OctagonX, Play } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { ModeMark } from "@/components/data/autonomy";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import type { WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";

import { useOrgView } from "./context";
import { useOrgFailure, useOrgWrite } from "./hooks";
import { type HeldStop, type OrgSeat, type OrgUnit, type StopSet, stopPreviewQuery } from "./queries";
import { targetName } from "./stops";

const CLASSES = ["K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9", "K10", "K11", "K12"] as const;

/** A stop that holds, flagged, with Lift for a viewer who may lift it. */
export function StopFlag({ stop, testId }: { stop: HeldStop; testId: string }) {
  const t = useTranslations("org.stops");
  const format = useFormatter();
  const { mayStop, units } = useOrgView();
  const write = useOrgWrite();
  const failure = useOrgFailure();
  const target: StopSet = { scope: stop.scope, login: stop.login, unit: stop.unit };
  const name = targetName(units, target);
  const lift = async () => {
    try {
      await write.mutateAsync({ kind: "lift", body: target });
    } catch (caught) {
      const failed = failure(caught);
      notify({ tone: "error", text: failed.text });
      return;
    }
    notify({ tone: "success", text: name === null ? t("liftedAll") : t("lifted", { name }) });
  };
  const since = format.dateTime(new Date(stop.stopped_at), { dateStyle: "medium", timeStyle: "short" });
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5" data-testid={testId}>
      <Badge variant="destructive" title={stop.reason ? t("stoppedWhy", { since, reason: stop.reason }) : t("stoppedSince", { since })}>
        <OctagonX aria-hidden="true" />
        {t("stopped")}
      </Badge>
      {mayStop(target) ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          busy={write.isPending}
          onClick={() => void lift()}
          aria-label={name === null ? t("liftAllLabel") : t("liftLabel", { name })}
          data-testid={`${testId}-lift`}
        >
          <Play aria-hidden="true" />
          {t("lift")}
        </Button>
      ) : null}
    </span>
  );
}

/** A seat's effective mode of each class, behind a disclosure; nothing until the hub's answer is in. */
export function SeatClasses({ seat }: { seat: OrgSeat }) {
  const t = useTranslations("org.stops");
  const { autonomy } = useOrgView();
  const [open, setOpen] = useState(false);
  const panel = useId();
  const held = autonomy?.seats.find((one) => one.id === seat.id);
  if (!held) return null;
  const bounded = CLASSES.filter((klass) => held.classes[klass] && held.classes[klass]?.mode !== "auto").length;
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-controls={panel}
        className="inline-flex w-fit items-center gap-1 rounded-sm text-xs font-medium text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 max-md:min-h-11"
        data-testid="org-seat-autonomy"
      >
        <ChevronRight className={`size-3.5 transition-transform ${open ? "rotate-90" : ""}`} aria-hidden="true" />
        {t("classes", { count: bounded })}
      </button>
      <ul id={panel} hidden={!open} className="grid min-w-0 grid-cols-2 gap-x-3 gap-y-1 sm:grid-cols-3 lg:grid-cols-4" data-testid="org-seat-classes">
        {CLASSES.map((klass) => {
          const one = held.classes[klass];
          return one ? (
            <li key={klass} className="flex min-w-0 items-center gap-1.5" data-class={klass} data-testid="org-seat-class">
              <span className="w-7 shrink-0 font-mono text-xs">{klass}</span>
              <ModeMark mode={one.mode} />
            </li>
          ) : null;
        })}
      </ul>
    </div>
  );
}

/**
 * Asks before a stop, saying how many runs it cancels or asks to stop and how many people it covers; the hub counts
 * them when the dialog opens.
 */
export function StopDialog({ target, units, open, onOpenChange }: { target: StopSet | null; units: readonly OrgUnit[]; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("org.stops");
  const write = useOrgWrite();
  const failure = useOrgFailure();
  const [error, setError] = useState<WriteFailure | null>(null);
  const preview = useQuery({ ...stopPreviewQuery(browserApi, target ?? { scope: "all" }), enabled: open && target !== null });
  if (!target) return null;
  const name = targetName(units, target);
  const confirm = async () => {
    setError(null);
    try {
      await write.mutateAsync({ kind: "stop", body: target });
    } catch (caught) {
      setError(failure(caught));
      return;
    }
    notify({ tone: "success", text: name === null ? t("stoppedAll") : t("stoppedOne", { name }) });
    onOpenChange(false);
  };
  const count = preview.data ? (
    <p className="font-medium text-foreground" data-testid="org-stop-runs" data-runs={preview.data.runs}>
      {preview.data.people === null ? t("countAll", { runs: preview.data.runs }) : t("count", { runs: preview.data.runs, people: preview.data.people })}
    </p>
  ) : preview.isError ? (
    <p data-testid="org-stop-runs">{t("countFailed")}</p>
  ) : (
    <p className="text-muted-foreground" data-testid="org-stop-counting">
      {t("counting")}
    </p>
  );
  return (
    <ConfirmAction
      open={open}
      onOpenChange={(next) => {
        if (!next) setError(null);
        onOpenChange(next);
      }}
      title={name === null ? t("titleAll") : t("title", { name })}
      description={
        <>
          {count}
          <p>{target.scope === "login" ? t("textLogin") : target.scope === "unit" ? t("textUnit") : t("textAll")}</p>
        </>
      }
      confirmLabel={t("confirm")}
      pendingLabel={t("stopping")}
      cancelLabel={t("cancel")}
      pending={write.isPending}
      error={error}
      onConfirm={() => void confirm()}
      testId="org-stop"
    />
  );
}
