"use client";

import { Power, PowerOff } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { When } from "@/components/data/when";
import { Banner } from "@/components/feedback/banner";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { BREAKER_CAUSES, type BreakerCause, type MergeBreaker } from "@/lib/merge-queue";
import { useResumeRepo } from "@/lib/use-merge-actions";

import { BreakerBadge } from "./badges";
import { Panel } from "./chrome";

function isCause(cause: string | null): cause is BreakerCause {
  return cause !== null && (BREAKER_CAUSES as readonly string[]).includes(cause);
}

/** What tripped a breaker, in words: "a UAT check failed". */
export function useCauseText() {
  const t = useTranslations("mergeQueue.breaker.cause");
  return (cause: string | null) => (isCause(cause) ? t(cause) : (cause ?? t("unknown")));
}

/**
 * Reset a tripped breaker, after a confirmation that says what it lets go: the paused merge requests wait for their
 * turn again, at their place, and what trips it counts from now. A toast names the merge requests ready again.
 */
export function ResetBreaker({
  project,
  repo,
  size = "sm",
  onDone,
}: {
  project: string;
  repo: string;
  size?: "sm" | "default";
  onDone?: () => void;
}) {
  const t = useTranslations("mergeQueue.breaker");
  const [open, setOpen] = useState(false);
  const write = useResumeRepo(project);
  const failure = useWriteFailure();
  const error = write.isError && open ? failure(write.error, { 403: t("forbidden"), 409: t("notTripped") }) : null;
  const confirm = async () => {
    try {
      const reset = await write.mutateAsync(repo);
      setOpen(false);
      onDone?.();
      const iids = reset.resumed.map((iid) => `!${iid}`).join(", ");
      notify({
        tone: "success",
        text: t("resetDone", { repo }),
        description: reset.resumed.length > 0 ? t("resetResumed", { count: reset.resumed.length, iids }) : t("resetNone"),
      });
    } catch {
      // The reason shows in the dialog, which stays open.
    }
  };
  return (
    <>
      <Button
        type="button"
        variant="secondary"
        size={size}
        onClick={() => {
          write.reset();
          setOpen(true);
        }}
        aria-label={t("resetLabel", { repo })}
        data-testid="breaker-reset"
        data-repo={repo}
      >
        <Power aria-hidden="true" />
        {t("reset")}
      </Button>
      <ConfirmAction
        open={open}
        onOpenChange={setOpen}
        title={t("confirmTitle", { repo })}
        description={
          <>
            <p>{t("confirmResume")}</p>
            <p>{t("confirmCount")}</p>
          </>
        }
        confirmLabel={t("confirm")}
        pendingLabel={t("pending")}
        cancelLabel={t("cancel")}
        pending={write.isPending}
        error={error}
        onConfirm={() => void confirm()}
        testId="breaker-reset-dialog"
      />
    </>
  );
}

/** One banner per tripped breaker, above the queue: merges of that repo stopped, why, and Reset for an admin. */
export function TrippedBanners({ project, breakers, admin }: { project: string; breakers: readonly MergeBreaker[]; admin: boolean }) {
  const t = useTranslations("mergeQueue.breaker");
  const cause = useCauseText();
  if (breakers.length === 0) return null;
  return (
    <div className="flex flex-col gap-2" data-testid="breaker-banners">
      {breakers.map((breaker) => (
        <Banner
          key={breaker.repo}
          icon={PowerOff}
          tone="attention"
          title={t("stopped", { repo: breaker.repo, cause: cause(breaker.cause) })}
          actions={admin ? <ResetBreaker project={project} repo={breaker.repo} /> : null}
          testId="breaker-banner"
        >
          <p className="[overflow-wrap:anywhere]">{breaker.reason ?? t("noReason")}</p>
          <p>
            {t.rich("trippedAt", { when: () => <When value={breaker.tripped_at} short /> })}{" "}
            {admin ? t("stoppedAdmin") : t("stoppedReader")}
          </p>
        </Banner>
      ))}
    </div>
  );
}

/** The breaker of each repo the queue passed over: its state, how often it tripped, who reset it last. */
export function BreakersPanel({ project, breakers, admin }: { project: string; breakers: readonly MergeBreaker[]; admin: boolean }) {
  const t = useTranslations("mergeQueue.breaker");
  const cause = useCauseText();
  const tripped = breakers.filter((breaker) => breaker.state === "tripped").length;
  return (
    <Panel
      title={t("title")}
      count={tripped}
      countWords={t("trippedCount", { count: tripped })}
      tone="attention"
      testId="breakers"
    >
      {breakers.length === 0 ? (
        <p className="px-4 py-3 text-[13px] text-muted-foreground">{t("none")}</p>
      ) : (
        <ul className="flex flex-col">
          {breakers.map((breaker) => (
            <li
              key={breaker.repo}
              className="flex min-w-0 flex-col gap-1.5 border-t px-4 py-3 first:border-t-0"
              data-testid="breaker"
              data-repo={breaker.repo}
              data-state={breaker.state}
            >
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <span className="min-w-0 truncate font-mono text-[13px] font-medium" title={breaker.repo}>
                  {breaker.repo}
                </span>
                <BreakerBadge state={breaker.state} />
                {admin && breaker.state === "tripped" ? (
                  <div className="ml-auto">
                    <ResetBreaker project={project} repo={breaker.repo} />
                  </div>
                ) : null}
              </div>
              <p className="text-xs text-fg-subtle">
                {breaker.state === "tripped"
                  ? t("trippedCause", { cause: cause(breaker.cause) })
                  : breaker.resumed_by
                    ? t.rich("resetBy", { login: breaker.resumed_by, when: () => <When value={breaker.resumed_at} short /> })
                    : t("neverTripped")}
                {breaker.trips > 0 ? ` ${t("trips", { count: breaker.trips })}` : ""}
              </p>
              {breaker.red_since ? (
                <p className="text-xs text-danger">{t.rich("redSince", { when: () => <When value={breaker.red_since} short /> })}</p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
