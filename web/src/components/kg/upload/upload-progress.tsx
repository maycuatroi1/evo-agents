"use client";

import { CircleX, RotateCcw, Square } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { useByteSize } from "@/components/data/byte-size";
import { Meter } from "@/components/data/meter";
import { Banner } from "@/components/feedback/banner";
import { Button } from "@/components/ui/button";
import { partRanges } from "@/lib/kg/upload";

import { bytesSent, type UploadPhase } from "./use-upload";

const percent = (part: number, whole: number) => (whole > 0 ? Math.min(100, (part / whole) * 100) : 100);

/** Each part of the multipart upload as its own meter: what of it is sent, so a slow or retried part shows. */
function Parts({ size, partSize, sent }: { size: number; partSize: number; sent: Record<number, number> }) {
  const t = useTranslations("kgUpload.progress");
  const ranges = [...partRanges(size, partSize)];
  return (
    <section className="flex flex-col gap-2" aria-label={t("parts")}>
      <h3 className="text-xs font-medium text-muted-foreground">{t("parts")}</h3>
      <ol className="grid grid-cols-[repeat(auto-fill,minmax(7.5rem,1fr))] gap-x-4 gap-y-2" data-testid="kg-upload-parts">
        {ranges.map(([number, [start, end]]) => {
          const value = Math.round(percent(sent[number] ?? 0, end - start));
          return (
            <li key={number} className="flex flex-col gap-1" data-testid="kg-upload-part" data-part={number} data-percent={value}>
              <span className="flex justify-between text-xs text-muted-foreground tabular-nums">
                <span>{t("part", { number })}</span>
                <span>{value}%</span>
              </span>
              <Meter value={value} label={t("part", { number })} valueText={t("partValue", { number, percent: value })} />
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function Line({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <p className="text-sm text-foreground tabular-nums" aria-live="polite">
        {children}
      </p>
      {action}
    </div>
  );
}

/**
 * Where the upload in this page stands: the file being checked (its SHA-256, read slice by slice), the upload starting,
 * the parts going (the whole, then each part), the parts being joined, then what to do next. Only a failure takes a
 * colour; an upload that went through says where its report will be, with no badge.
 */
export function UploadProgress({
  state,
  onCancel,
  onRetry,
  onAnother,
  doneText,
}: {
  state: UploadPhase;
  onCancel: () => void;
  onRetry: () => void;
  onAnother?: () => void;
  /** What a finished upload says, when the page has its own words (a new project's link). */
  doneText?: ReactNode;
}) {
  const t = useTranslations("kgUpload.progress");
  const size = useByteSize();
  if (state.phase === "idle") return null;
  const stop = (
    <Button type="button" variant="outline" size="sm" onClick={onCancel} data-testid="kg-upload-cancel">
      <Square aria-hidden="true" />
      {t("cancel")}
    </Button>
  );
  const body = (() => {
    switch (state.phase) {
      case "hashing":
        return (
          <>
            <Line action={stop}>{t("hashing", { read: size(state.read), size: size(state.file.size) })}</Line>
            <Meter value={percent(state.read, state.file.size)} label={t("hashingLabel")} />
          </>
        );
      case "starting":
        return <Line>{t("starting")}</Line>;
      case "sending": {
        const { started, sent } = state;
        const total = bytesSent(sent);
        return (
          <>
            <Line action={stop}>{t("sending", { sent: size(total), size: size(started.size) })}</Line>
            <Meter value={percent(total, started.size)} label={t("sendingLabel")} />
            {started.resumed && started.done.length > 0 ? (
              <p className="text-xs text-muted-foreground" data-testid="kg-upload-resumed">
                {t("resumed", { count: started.done.length })}
              </p>
            ) : null}
            <Parts size={started.size} partSize={started.part_size} sent={sent} />
          </>
        );
      }
      case "completing":
        return (
          <>
            <Line>{t("completing")}</Line>
            <Meter value={100} label={t("sendingLabel")} />
          </>
        );
      case "done":
        return (
          <Line
            action={
              onAnother ? (
                <Button type="button" variant="outline" size="sm" onClick={onAnother} data-testid="kg-upload-another">
                  {t("another")}
                </Button>
              ) : null
            }
          >
            {doneText ?? t("done", { name: state.file.name })}
          </Line>
        );
      case "cancelled":
        return (
          <Line
            action={
              <Button type="button" variant="outline" size="sm" onClick={onRetry} data-testid="kg-upload-retry">
                <RotateCcw aria-hidden="true" />
                {t("retry")}
              </Button>
            }
          >
            {t("cancelled", { name: state.file.name })}
          </Line>
        );
      case "failed":
        return null;
    }
  })();
  if (state.phase === "failed") {
    return (
      <Banner
        icon={CircleX}
        tone="danger"
        role="alert"
        title={t("failed", { name: state.file.name })}
        actions={
          <Button type="button" variant="outline" size="sm" onClick={onRetry} data-testid="kg-upload-retry">
            <RotateCcw aria-hidden="true" />
            {t("retry")}
          </Button>
        }
        testId="kg-upload-failed"
      >
        <span className="[overflow-wrap:anywhere]">{state.error.message}</span>
      </Banner>
    );
  }
  return (
    <div className="flex flex-col gap-3 rounded-md border bg-card p-4" data-testid="kg-upload-progress" data-phase={state.phase}>
      {body}
    </div>
  );
}
