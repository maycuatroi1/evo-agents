"use client";

import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";

import type { Run } from "./queries";
import { Card, Fact } from "./run-facts";

type Routed = NonNullable<Run["routing"]>;

const percent = (share: number | undefined) => (share === undefined ? null : Math.round(Math.max(0, Math.min(1, share)) * 100));

/** Who rated the run: a rule of the hub (plan, rule or role), the worker's scorer, or nobody (medium by default). */
function sourceOf(record: Routed): "rules" | "scorer" | "fallback" | "pending" {
  if (record.pending) return "pending";
  if (record.source === "plan" || record.source === "rule" || record.source === "role") return "rules";
  if (record.source === "fallback" || (!record.scored && record.source !== "scorer")) return "fallback";
  return "scorer";
}

/**
 * Why the run got its model under the project's Token strategy (plan token-and-quota-economy): the difficulty and who
 * rated it, the branch the strategy took (the ladder while the subscription has its week to spend, the overflow's
 * cheaper model as a cost step-down), the hub's own reason, a step up after a failed run, and the quota the hub read.
 */
export function RunRouting({ run }: { run: Run }) {
  const t = useTranslations("runs.detail.routing");
  const record = run.routing;
  if (!record) return null;
  const source = sourceOf(record);
  const weeklyLeft = percent(record.quota?.weekly_left);
  const fiveHourUsed = percent(record.quota?.five_hour_used);
  const model = record.model ? (record.gateway && !record.model.startsWith(`${record.gateway}/`) ? `${record.gateway}/${record.model}` : record.model) : null;
  return (
    <Card
      title={t("title")}
      testId="run-routing"
      status={
        record.escalated_from ? (
          <Badge variant="warning" data-testid="run-routing-escalated">
            {t("escalated")}
          </Badge>
        ) : null
      }
    >
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
        <Fact label={t("difficulty")} testId="run-routing-difficulty">
          {t(`levels.${record.difficulty}`)}
          <span className="block text-xs text-muted-foreground" data-testid="run-routing-source">
            {source === "scorer" ? t("source.scorer", { scorer: record.scorer }) : t(`source.${source}`)}
          </span>
          {record.escalated_from ? (
            <span className="block text-xs text-muted-foreground" data-testid="run-routing-escalated-from">
              {t("escalatedFrom", { from: t(`levels.${record.escalated_from}`), to: t(`levels.${record.difficulty}`) })}
            </span>
          ) : null}
        </Fact>
        <Fact label={t("model")} testId="run-routing-model">
          {model ? <span className="font-mono text-xs [overflow-wrap:anywhere]">{model}</span> : <span className="text-muted-foreground">{t("noModel")}</span>}
        </Fact>
        {record.branch ? (
          <Fact label={t("why")} testId="run-routing-branch">
            {t(`branch.${record.branch}`)}
            {record.strategy ? <span className="block text-xs text-muted-foreground">{t(`strategies.${record.strategy}`)}</span> : null}
          </Fact>
        ) : null}
        {record.reason ? (
          <Fact label={t("reason")} testId="run-routing-reason">
            <span className="text-xs [overflow-wrap:anywhere]">{record.reason}</span>
          </Fact>
        ) : null}
        {weeklyLeft !== null || fiveHourUsed !== null ? (
          <Fact label={t("quota")} testId="run-routing-quota">
            {weeklyLeft !== null ? <span className="block tabular-nums">{t("weeklyLeft", { percent: weeklyLeft })}</span> : null}
            {fiveHourUsed !== null ? <span className="block tabular-nums">{t("fiveHourUsed", { percent: fiveHourUsed })}</span> : null}
          </Fact>
        ) : null}
        {record.error ? (
          <Fact label={t("error")} testId="run-routing-error">
            <span className="text-xs [overflow-wrap:anywhere]">{record.error}</span>
          </Fact>
        ) : null}
      </dl>
    </Card>
  );
}
