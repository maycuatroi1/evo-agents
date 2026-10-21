"use client";

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Identifier } from "@/components/data/identifier";

import { Card, Fact, When } from "./run-facts";
import type { Run } from "./queries";

/**
 * The details of a local run, a member's own Claude Code session: its machine, Claude account and profile, directory,
 * session and last sign of life. No worker, lease or attempt, which a worker's run has.
 */
export function LocalRunDetails({ run, plan, step }: { run: Run; plan: ReactNode; step: ReactNode }) {
  const t = useTranslations("runs.detail.local");
  return (
    <Card title={t("title")} testId="run-details">
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
        <Fact label={t("machine")} testId="run-machine">
          <Identifier value={run.machine ?? t("none")} />
        </Fact>
        <Fact label={t("account")} testId="run-account">
          <span className="font-mono text-xs">{run.claude_email ?? t("unknown")}</span>
        </Fact>
        {run.claude_profile ? (
          <Fact label={t("profile")} testId="run-claude-profile">
            <span className="font-mono text-xs">{run.claude_profile}</span>
          </Fact>
        ) : null}
        <Fact label={t("directory")} testId="run-cwd">
          {run.cwd ? <span className="font-mono text-xs">{run.cwd}</span> : <span className="text-muted-foreground">{t("none")}</span>}
        </Fact>
        {plan ? (
          <Fact label={t("plan")} testId="run-local-plan">
            {plan}
          </Fact>
        ) : null}
        {step ? (
          <Fact label={t("step")} testId="run-local-step">
            {step}
          </Fact>
        ) : null}
        <Fact label={t("session")}>
          {run.session_id ? <Identifier value={run.session_id} /> : <span className="text-muted-foreground">{t("none")}</span>}
        </Fact>
        <Fact label={t("lastActivity")} testId="run-last-activity">
          {run.last_activity_at ? <When at={run.last_activity_at} /> : <span className="text-muted-foreground">{t("none")}</span>}
        </Fact>
        <Fact label={t("started")}>
          <When at={run.queued_at} />
        </Fact>
      </dl>
      <p className="mt-3 text-xs text-pretty text-muted-foreground" data-testid="run-local-note">
        {t("readOnly")}
      </p>
    </Card>
  );
}
