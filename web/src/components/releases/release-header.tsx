"use client";

import { CirclePlay, Pause, Play } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { Identifier } from "@/components/data/identifier";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import type { WriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { StatusPill } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";

import { PAUSED_LOOK } from "./badges";
import { useReleaseFailure, useReleaseRights, usePauseRelease, useRunReleaseCheck } from "./hooks";
import { githubRepoUrl } from "./model";
import { Relative } from "./parts";
import { type ReleaseCheck, type ReleaseStatus, runHref } from "./queries";

/** What a check run now found, as its toast says it: a cycle opened links to its first run. */
function useCheckedNotice(project: string) {
  const t = useTranslations("releases.checked");
  return (check: ReleaseCheck) => {
    if (check.outcome === "opened" && check.cycle_id !== null) {
      const run = check.run_id;
      notify({
        tone: "success",
        text: t("opened.title", { cycle: check.cycle_id }),
        description: run !== null ? t("opened.text", { run }) : null,
        link: run !== null ? { label: t("openRun", { run }), href: runHref(project, run) } : null,
      });
      return;
    }
    notify({
      tone: check.outcome === "error" ? "error" : "info",
      text: t(`${check.outcome}.title`),
      description: check.reason,
    });
  };
}

/** Run now, which checks through the gates of the daily check: busy while the hub reads GitHub. */
function RunNow({ project }: { project: string }) {
  const t = useTranslations("releases");
  const mutation = useRunReleaseCheck(project);
  const failure = useReleaseFailure();
  const checked = useCheckedNotice(project);
  return (
    <Button
      variant="secondary"
      busy={mutation.isPending}
      onClick={() => {
        if (mutation.isPending) return;
        mutation.mutate(undefined, { onSuccess: checked, onError: (error) => notifyFailure(t("runFailed"), failure(error)) });
      }}
      data-testid="release-run-now"
    >
      <Play aria-hidden="true" />
      {mutation.isPending ? t("checking") : t("runNow")}
    </Button>
  );
}

/** Pause, confirmed first since it cancels the release runs still queued, or Resume while paused, the primary button. */
function PauseControl({ project, status }: { project: string; status: ReleaseStatus }) {
  const t = useTranslations("releases");
  const mutation = usePauseRelease(project);
  const failure = useReleaseFailure();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<WriteFailure | null>(null);
  const schedule = status.schedule;
  if (!schedule) return null;

  const run = (pause: boolean) => {
    setError(null);
    mutation.mutate(pause, {
      onSuccess: () => {
        setConfirming(false);
        notify({
          tone: "success",
          text: pause ? t("paused.title") : t("resumed.title"),
          description: pause ? t("paused.text") : t("resumed.text", { at: schedule.at, timezone: schedule.timezone }),
        });
      },
      onError: (failed) => {
        const said = failure(failed);
        if (pause) setError(said);
        else notifyFailure(t("resumeFailed"), said);
      },
    });
  };

  if (status.paused) {
    return (
      <Button
        variant="default"
        busy={mutation.isPending}
        onClick={() => {
          if (!mutation.isPending) run(false);
        }}
        data-testid="release-resume"
      >
        <CirclePlay aria-hidden="true" />
        {mutation.isPending ? t("resuming") : t("resume")}
      </Button>
    );
  }
  return (
    <>
      <Button variant="secondary" onClick={() => setConfirming(true)} data-testid="release-pause">
        <Pause aria-hidden="true" />
        {t("pause")}
      </Button>
      <ConfirmAction
        open={confirming}
        onOpenChange={(open) => {
          setConfirming(open);
          if (!open) setError(null);
        }}
        title={t("confirmPause.title")}
        description={<p>{t("confirmPause.text", { project })}</p>}
        confirmLabel={t("confirmPause.confirm")}
        pendingLabel={t("pausing")}
        cancelLabel={t("confirmPause.cancel")}
        pending={mutation.isPending}
        error={error}
        onConfirm={() => run(true)}
        testId="release-pause-dialog"
      />
    </>
  );
}

/** One line under the title: when the schedule checks and as whom, and the next check; or who paused it and when. */
function Sub({ status }: { status: ReleaseStatus }) {
  const t = useTranslations("releases.sub");
  const schedule = status.schedule;
  if (!schedule) return null;
  if (status.paused && schedule.paused_at) {
    const ago = () => <Relative value={schedule.paused_at as string} />;
    return schedule.paused_by ? t.rich("paused", { login: schedule.paused_by, ago }) : t.rich("pausedByHub", { ago });
  }
  const repo = schedule.github_repo ?? schedule.repo;
  const chip = () => <Identifier value={repo} externalHref={githubRepoUrl(schedule.github_repo)} linkLabel={t("repoLink", { repo })} className="align-baseline" />;
  const next = () => (status.next_check_at ? <Relative value={status.next_check_at} testId="release-next-check" /> : t("noNext"));
  return t.rich("daily", { repo: chip, at: schedule.at, timezone: schedule.timezone, owner: schedule.owner, next });
}

/**
 * The head of the Releases page: "Releases", a pill only while the schedule is paused, Run now and Pause or Resume for
 * an admin of the project, and one line under it that says when the schedule checks, or who paused it.
 */
export function ReleaseHeader({ project, status }: { project: string; status: ReleaseStatus }) {
  const t = useTranslations("releases");
  const rights = useReleaseRights(project);
  const actions: ReactNode =
    rights.admin && status.schedule ? (
      <>
        {status.paused ? null : <RunNow project={project} />}
        <PauseControl project={project} status={status} />
      </>
    ) : null;
  return (
    <PageHeader
      title={t("title")}
      status={status.paused ? <StatusPill look={PAUSED_LOOK} label={t("state.paused")} size="lg" data-status="paused" data-testid="release-state" /> : null}
      actions={actions}
      sub={status.schedule ? <Sub status={status} /> : null}
      testId="release-header"
    />
  );
}
