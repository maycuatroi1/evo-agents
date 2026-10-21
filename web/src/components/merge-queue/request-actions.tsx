"use client";

import { useQuery } from "@tanstack/react-query";
import { Rocket, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { policyQuery, type QueuedMergeRequest } from "@/lib/merge-queue";
import { useApproveRelease, useAskRevert } from "@/lib/use-merge-actions";

import { canApproveRelease, canAskRevert, requestName } from "./model";

/** The repo's `prod_release` in the policy now; null until the policy is read. */
export function useProdRelease(project: string, repo: string): string | null {
  const policy = useQuery(policyQuery(browserApi, project));
  const repos = policy.data?.effective?.repos as Record<string, { prod_release?: string }> | undefined;
  return repos?.[repo]?.prod_release ?? null;
}

/** Approve the PROD release of a merge the policy releases by hand: the hub's next pass runs its deploy job. */
export function ApproveRelease({ project, request, size = "sm" }: { project: string; request: QueuedMergeRequest; size?: "sm" | "default" }) {
  const t = useTranslations("mergeQueue.actions");
  const write = useApproveRelease(project);
  const failure = useWriteFailure();
  const name = requestName(request);
  const approve = async () => {
    if (write.isPending) return;
    try {
      await write.mutateAsync(request.id);
      notify({ tone: "success", text: t("releaseDone", { name, repo: request.repo }), description: t("releaseDoneText") });
    } catch (error) {
      notifyFailure(t("releaseFailed", { name }), failure(error, { 409: t("releaseConflict"), 403: t("forbidden") }));
    }
  };
  return (
    <Button type="button" size={size} busy={write.isPending} onClick={() => void approve()} data-testid="merge-release" aria-label={t("releaseLabel", { name, repo: request.repo })}>
      <Rocket aria-hidden="true" />
      {write.isPending ? t("releasing") : t("release")}
    </Button>
  );
}

/** Ask the hub to revert a merge it followed, after a confirmation: it opens the revert first in the queue. */
export function AskRevert({ project, request, size = "sm" }: { project: string; request: QueuedMergeRequest; size?: "sm" | "default" }) {
  const t = useTranslations("mergeQueue.actions");
  const [open, setOpen] = useState(false);
  const write = useAskRevert(project);
  const failure = useWriteFailure();
  const name = requestName(request);
  const error = write.isError && open ? failure(write.error, { 409: t("revertConflict"), 403: t("forbidden") }) : null;
  const confirm = async () => {
    try {
      await write.mutateAsync(request.id);
      setOpen(false);
      notify({ tone: "success", text: t("revertDone", { name, repo: request.repo }), description: t("revertDoneText") });
    } catch {
      // The reason shows in the dialog, which stays open.
    }
  };
  return (
    <>
      <Button
        type="button"
        variant="quiet-danger"
        size={size}
        onClick={() => {
          write.reset();
          setOpen(true);
        }}
        aria-label={t("revertLabel", { name, repo: request.repo })}
        data-testid="merge-revert"
      >
        <Undo2 aria-hidden="true" />
        {t("revert")}
      </Button>
      <ConfirmAction
        open={open}
        onOpenChange={setOpen}
        title={t("revertTitle", { name, repo: request.repo })}
        description={
          <>
            <p>{t("revertOpens", { branch: request.target_branch })}</p>
            <p>{t("revertCounts")}</p>
          </>
        }
        confirmLabel={t("revertConfirm")}
        pendingLabel={t("reverting")}
        cancelLabel={t("cancel")}
        pending={write.isPending}
        error={error}
        onConfirm={() => void confirm()}
        testId="merge-revert-dialog"
      />
    </>
  );
}

/** What an admin may do to a merge request now: approve its PROD release, ask for its revert; nothing for a reader. */
export function RequestActions({ project, request, admin }: { project: string; request: QueuedMergeRequest; admin: boolean }) {
  const release = useProdRelease(project, request.repo);
  if (!admin) return null;
  const releasable = canApproveRelease(request, release);
  const revertable = canAskRevert(request);
  if (!releasable && !revertable) return null;
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="merge-actions">
      {releasable ? <ApproveRelease project={project} request={request} /> : null}
      {revertable ? <AskRevert project={project} request={request} /> : null}
    </div>
  );
}
