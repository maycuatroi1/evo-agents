"use client";

import { OctagonPause, Power, PowerOff } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { Banner } from "@/components/feedback/banner";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import type { ProjectLoop } from "@/lib/loop-queries";

import { useLoopResume, useLoopSwitch } from "./hooks";
import { openCount } from "./model";

/**
 * Turn the Loop on or off, for an admin of the project, after a confirmation: turned off, it cancels its unfinished
 * work items and hands out nothing; turned on, it reads the project's events from now and hands out runs within the
 * policy's caps. Either is a revision of the autonomy policy (`loop.enabled`), kept in its history.
 */
export function LoopSwitch({ project, loop }: { project: string; loop: ProjectLoop }) {
  const t = useTranslations("loop.switch");
  const [open, setOpen] = useState(false);
  const write = useLoopSwitch(project);
  const failure = useWriteFailure();
  const next = !loop.enabled;
  const error = write.isError && open ? failure(write.error, { 403: t("forbidden"), 409: t("conflict") }) : null;
  const unfinished = openCount(loop.items);
  const confirm = async () => {
    try {
      await write.mutateAsync({ enabled: next, note: next ? t("noteOn") : t("noteOff") });
      setOpen(false);
      notify({ tone: "success", text: next ? t("onDone") : t("offDone"), description: next ? t("onDoneText") : t("offDoneText") });
    } catch {
      // The reason shows in the dialog, which stays open.
    }
  };
  return (
    <>
      <Button
        type="button"
        variant={next ? "default" : "secondary"}
        onClick={() => {
          write.reset();
          setOpen(true);
        }}
        data-testid={next ? "loop-turn-on" : "loop-turn-off"}
      >
        {next ? <Power aria-hidden="true" /> : <PowerOff aria-hidden="true" />}
        {next ? t("turnOn") : t("turnOff")}
      </Button>
      <ConfirmAction
        open={open}
        onOpenChange={setOpen}
        title={next ? t("confirmOnTitle") : t("confirmOffTitle")}
        description={
          next ? (
            <p>{t("confirmOnText")}</p>
          ) : (
            <>
              <p>{t("confirmOffText", { count: unfinished })}</p>
              <p>{t("confirmOffAgain")}</p>
            </>
          )
        }
        confirmLabel={next ? t("turnOn") : t("turnOff")}
        pendingLabel={next ? t("turningOn") : t("turningOff")}
        cancelLabel={t("cancel")}
        pending={write.isPending}
        error={error}
        onConfirm={() => void confirm()}
        testId="loop-switch-dialog"
      />
    </>
  );
}

/** Reset the tripped breaker, for an admin, after a confirmation that says what follows. */
function ResetBreaker({ project }: { project: string }) {
  const t = useTranslations("loop.breaker");
  const [open, setOpen] = useState(false);
  const write = useLoopResume(project);
  const failure = useWriteFailure();
  const error = write.isError && open ? failure(write.error, { 403: t("forbidden"), 409: t("notTripped") }) : null;
  const confirm = async () => {
    try {
      await write.mutateAsync();
      setOpen(false);
      notify({ tone: "success", text: t("resetDone"), description: t("resetDoneText") });
    } catch {
      // The reason shows in the dialog, which stays open.
    }
  };
  return (
    <>
      <Button
        type="button"
        variant="secondary"
        size="sm"
        onClick={() => {
          write.reset();
          setOpen(true);
        }}
        data-testid="loop-breaker-reset"
      >
        <Power aria-hidden="true" />
        {t("reset")}
      </Button>
      <ConfirmAction
        open={open}
        onOpenChange={setOpen}
        title={t("confirmTitle")}
        description={<p>{t("confirmText")}</p>}
        confirmLabel={t("reset")}
        pendingLabel={t("resetting")}
        cancelLabel={t("cancel")}
        pending={write.isPending}
        error={error}
        onConfirm={() => void confirm()}
        testId="loop-breaker-dialog"
      />
    </>
  );
}

/** The Loop stopped by its breaker: why and since when, and Reset for an admin. Nothing while the breaker is armed. */
export function BreakerBanner({ project, loop, admin }: { project: string; loop: ProjectLoop; admin: boolean }) {
  const t = useTranslations("loop.breaker");
  const format = useFormatter();
  if (loop.breaker !== "tripped") return null;
  const since = loop.tripped_at ? format.dateTime(new Date(loop.tripped_at), { dateStyle: "medium", timeStyle: "short" }) : null;
  return (
    <Banner
      icon={OctagonPause}
      tone="attention"
      title={t("title")}
      actions={admin ? <ResetBreaker project={project} /> : null}
      testId="loop-breaker-banner"
    >
      {[loop.breaker_reason, since ? t("since", { since }) : null].filter(Boolean).join(". ")}
      {admin ? null : <span className="block">{t("adminResets")}</span>}
    </Banner>
  );
}
