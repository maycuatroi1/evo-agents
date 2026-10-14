"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";

import { When } from "@/components/data/when";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { notify } from "@/components/feedback/toast";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { agentsHref, agentVersionsQuery } from "@/lib/agent-queries";
import type { Agent } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";

import { useAgentFailure, useDeleteAgent } from "./hooks";
import { type VersionChange, versionChanges } from "./model";

/** Versions listed on an agent's page, the latest first. */
const SHOWN_VERSIONS = 8;

/**
 * Every edit of an agent is a version, and a run keeps the version it went under: the latest versions, each with who
 * made it, when, and what it changed against the one before (version 1 created the agent).
 */
export function AgentHistory({ project, agent }: { project: string; agent: Agent }) {
  const t = useTranslations("agents.detail.history");
  const tChange = useTranslations("agents.detail.history.changes");
  const format = useFormatter();
  const state = useQuery(agentVersionsQuery(browserApi, project, agent.handle));
  const versions = state.data ?? [];
  const what = (changes: VersionChange[]) =>
    changes.length ? t("changed", { what: format.list(changes.map((change) => tChange(change)), { type: "conjunction" }) }) : t("noChange");
  return (
    <Card data-testid="agent-history">
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {state.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : state.isError ? (
          <p className="text-sm text-danger" role="alert">
            {t("failed")}
          </p>
        ) : (
          <ol className="flex flex-col gap-3" data-testid="agent-versions">
            {versions.slice(0, SHOWN_VERSIONS).map((version, index) => {
              const older = versions[index + 1];
              return (
                <li key={version.version} className="flex min-w-0 flex-col gap-0.5" data-testid="agent-version-row" data-version={version.version}>
                  <span className="flex flex-wrap items-baseline gap-x-2 text-[13px]">
                    <span className="font-mono text-xs font-medium text-foreground tabular-nums">{t("version", { version: version.version })}</span>
                    <span className="text-muted-foreground">{t("by", { login: version.changed_by })}</span>
                    <span className="text-xs text-fg-subtle">
                      <When value={version.changed_at} short />
                    </span>
                  </span>
                  <span className="text-xs text-pretty text-fg-subtle">
                    {version.version === 1 || !older ? t("created") : what(versionChanges(version.body, older.body))}
                  </span>
                </li>
              );
            })}
            {versions.length > SHOWN_VERSIONS ? (
              <li className="text-xs text-fg-subtle">{t("older", { count: versions.length - SHOWN_VERSIONS })}</li>
            ) : null}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * Delete, after a confirmation that says what it means: the agent takes no new run, its runs keep its name and avatar,
 * and its handle stays taken. On success the visitor goes back to the agents, with a toast.
 */
export function DeleteAgent({ project, agent, open, onOpenChange }: { project: string; agent: Agent; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("agents.detail.delete");
  const failure = useAgentFailure();
  const router = useRouter();
  const write = useDeleteAgent(project, agent.handle);
  const confirm = async () => {
    try {
      await write.mutateAsync();
    } catch {
      return; // shown in the dialog, which stays open
    }
    onOpenChange(false);
    notify({ tone: "success", text: t("done", { handle: agent.handle }), description: t("doneText") });
    router.push(agentsHref(project));
  };
  return (
    <ConfirmAction
      open={open}
      onOpenChange={(next) => {
        if (next) write.reset();
        onOpenChange(next);
      }}
      title={t("title", { handle: agent.handle })}
      description={
        <>
          <p>{t("runs")}</p>
          <p>{t("handle", { handle: agent.handle })}</p>
        </>
      }
      confirmLabel={t("confirm")}
      pendingLabel={t("pending")}
      cancelLabel={t("cancel")}
      pending={write.isPending}
      error={open && write.isError ? failure(write.error) : null}
      onConfirm={() => void confirm()}
      testId="agent-delete-dialog"
    />
  );
}
