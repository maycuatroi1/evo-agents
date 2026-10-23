"use client";

import { ExternalLink, ShieldAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { When } from "@/components/data/when";
import { Banner } from "@/components/feedback/banner";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ListSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type MarketingWeekly, marketingWeeklyQuery } from "./api";
import { BudgetMeter, MarketingTabs, useMoney } from "./chrome";

function Section({ id, title, count, children }: { id: string; title: string; count?: number; children: ReactNode }) {
  const t = useTranslations("marketing.weekly");
  return (
    <Card role="region" aria-labelledby={`weekly-${id}-title`} data-testid={`weekly-${id}`} className="min-w-0">
      <CardHeader>
        <CardTitle id={`weekly-${id}-title`} className="flex flex-wrap items-baseline gap-x-3 text-[15px] leading-[22px] font-semibold">
          <h2>{title}</h2>
          {count !== undefined ? <span className="text-xs font-normal text-fg-subtle tabular-nums">{t("count", { count })}</span> : null}
        </CardTitle>
      </CardHeader>
      <CardContent className="min-w-0">{children}</CardContent>
    </Card>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="text-[13px] text-muted-foreground">{children}</p>;
}

/** Each active Goal with a metric: its signal over the window against its target, and whether it is met. */
function Goals({ week }: { week: MarketingWeekly }) {
  const t = useTranslations("marketing.weekly");
  const format = useFormatter();
  const figure = (value: number | null) => (value === null ? t("noValue") : format.number(value));
  return (
    <Section id="goals" title={t("goals")} count={week.goals.length}>
      {week.goals.length === 0 ? (
        <Empty>{t("noGoals")}</Empty>
      ) : (
        <Table scrollLabel={t("goals")}>
          <TableHeader>
            <TableRow>
              <TableHead>{t("goal")}</TableHead>
              <TableHead>{t("signal")}</TableHead>
              <TableHead className="text-right">{t("value")}</TableHead>
              <TableHead className="text-right">{t("target")}</TableHead>
              <TableHead>{t("met")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {week.goals.map((goal) => (
              <TableRow key={goal.code} data-testid="weekly-goal">
                <TableCell className="max-w-72 whitespace-normal">
                  <span className="font-mono text-xs">{goal.code}</span> <span className="[overflow-wrap:anywhere]">{goal.statement}</span>
                </TableCell>
                <TableCell className="max-w-56 font-mono text-xs whitespace-normal [overflow-wrap:anywhere]">{goal.metric}</TableCell>
                <TableCell className="text-right tabular-nums">{figure(goal.value)}</TableCell>
                <TableCell className="text-right tabular-nums">{figure(goal.target)}</TableCell>
                <TableCell>
                  {goal.reached === null ? (
                    <Badge variant="outline">{t("unknown")}</Badge>
                  ) : goal.reached ? (
                    <Badge variant="success">{t("reached")}</Badge>
                  ) : (
                    <Badge variant="warning">{t("notReached")}</Badge>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Section>
  );
}

function Week({ week }: { week: MarketingWeekly }) {
  const t = useTranslations("marketing.weekly");
  const money = useMoney();
  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="weekly-report">
      {week.killed ? (
        <Banner icon={ShieldAlert} tone="danger" title={t("killed")} testId="weekly-killed">
          {week.reason}
        </Banner>
      ) : null}
      <Goals week={week} />
      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        <Section id="posted" title={t("posted")} count={week.posted.length}>
          {week.posted.length === 0 ? (
            <Empty>{t("noPosts")}</Empty>
          ) : (
            <ul className="divide-y">
              {week.posted.map((post) => (
                <li key={post.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2" data-testid="weekly-post">
                  <span className="font-medium">{post.channel}</span>
                  <span className="min-w-0 font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{post.account}</span>
                  {post.posted_url ? (
                    <a href={post.posted_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[13px] text-brand underline underline-offset-4 hover:text-brand-hover">
                      <ExternalLink aria-hidden="true" className="size-3.5" />
                      {t("open")}
                    </a>
                  ) : null}
                  <span className="ml-auto text-xs text-muted-foreground">
                    <When value={post.posted_at} short />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Section>
        <Section id="breakers" title={t("breakers")} count={week.breakers.length}>
          {week.breakers.length === 0 ? (
            <Empty>{t("noBreakers")}</Empty>
          ) : (
            <ul className="divide-y">
              {week.breakers.map((item) => (
                <li key={item.channel_id} className="flex flex-col gap-1 py-2" data-testid="weekly-breaker">
                  <div className="flex flex-wrap items-baseline gap-x-3">
                    <span className="font-medium">{item.channel}</span>
                    <span className="min-w-0 font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{item.account}</span>
                    <span className="ml-auto text-xs text-muted-foreground">
                      <When value={item.paused_at} short />
                    </span>
                  </div>
                  {item.reason ? <p className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{item.reason}</p> : null}
                </li>
              ))}
            </ul>
          )}
        </Section>
        <Section id="cost" title={t("cost")}>
          <div className="flex flex-col gap-4">
            <p className="text-[13px]">
              {t("weekCost")} <span className="font-semibold tabular-nums" data-testid="weekly-week-cost">{money(week.week_cost_usd)}</span>
            </p>
            <BudgetMeter budget={week.budget} />
          </div>
        </Section>
        <Section id="proposals" title={week.proposals_project ? t("proposalsFor", { project: week.proposals_project }) : t("proposals")} count={week.proposals.length}>
          {week.proposals_project ? null : <p className="pb-2 text-[13px] text-muted-foreground">{t("noProposalsProject")}</p>}
          {week.proposals.length === 0 ? (
            <Empty>{t("noProposals")}</Empty>
          ) : (
            <ul className="divide-y">
              {week.proposals.map((item) => (
                <li key={item.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2" data-testid="weekly-proposal">
                  <span className="font-medium">{item.channel}</span>
                  <span className="min-w-0 font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{item.account}</span>
                  <Badge variant="outline">{item.state}</Badge>
                  <span className="ml-auto text-xs text-muted-foreground">
                    <When value={item.created_at} short />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Section>
      </div>
    </div>
  );
}

/**
 * The Growth section of the project's weekly bulletin (/p/{project}/x/marketing/weekly), the link of the Monday notice:
 * each Goal's signal against its target, the posts that went out, the breakers, the cost against the month's cap, and
 * the drafts kept for the blog project. Every member reads it.
 */
export function WeeklyPage({ project, title, initialError }: { project: string; title: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("marketing.weekly");
  const format = useFormatter();
  const state = useHubQuery(marketingWeeklyQuery(browserApi, project), initialError, { live: true });
  const sub =
    state.status === "success"
      ? t("sub", { since: format.dateTime(new Date(state.data.since), { dateStyle: "medium" }), until: format.dateTime(new Date(state.data.until), { dateStyle: "medium" }) })
      : undefined;
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="marketing-weekly">
      <PageHeader title={title} sub={sub} />
      <MarketingTabs project={project} current="weekly" />
      <QueryView state={state} loading={<ListSkeleton rows={6} />}>
        {(week) => <Week week={week} />}
      </QueryView>
    </div>
  );
}
