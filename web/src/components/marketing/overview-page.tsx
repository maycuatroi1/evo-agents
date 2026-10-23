"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CirclePlay, Megaphone, OctagonX, ShieldAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { ConfirmAction } from "@/components/feedback/confirm-action";
import { Banner } from "@/components/feedback/banner";
import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { closeBreaker, killPosting, type MarketingChannel, type MarketingStatus, marketingChannelsQuery, marketingKeys, marketingStatusQuery, resumePosting } from "./api";
import { BudgetMeter, canWrite, MarketingTabs, type MarketingRole, ReaderNote, useMarketingRole } from "./chrome";

/** A switch of the whole project: Stop all posting for a writer (confirmed), and Resume for an admin once stopped. */
function KillSwitch({ project, status, role }: { project: string; status: MarketingStatus; role: MarketingRole | null }) {
  const t = useTranslations("marketing.kill");
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<WriteFailure | null>(null);
  const stopping = !status.killed;
  const write = useMutation({
    mutationFn: () => (stopping ? killPosting(browserApi(), project) : resumePosting(browserApi(), project)),
    onSuccess: async () => {
      setConfirming(false);
      notify({ tone: "success", text: stopping ? t("stopped") : t("resumed") });
      await queryClient.invalidateQueries({ queryKey: marketingKeys.all(project) });
    },
    onError: (cause) => setError(failure(cause, { 403: t("forbidden") })),
  });
  if (stopping ? !canWrite(role) : role !== "admin") return null;
  return (
    <>
      <Button
        type="button"
        variant={stopping ? "quiet-danger" : "secondary"}
        onClick={() => {
          setError(null);
          setConfirming(true);
        }}
        data-testid={stopping ? "marketing-kill" : "marketing-unkill"}
      >
        {stopping ? <OctagonX aria-hidden="true" /> : <CirclePlay aria-hidden="true" />}
        {stopping ? t("stop") : t("resume")}
      </Button>
      <ConfirmAction
        open={confirming}
        onOpenChange={setConfirming}
        title={stopping ? t("stopTitle") : t("resumeTitle")}
        description={stopping ? t("stopText") : t("resumeText")}
        confirmLabel={stopping ? t("stopConfirm") : t("resumeConfirm")}
        pendingLabel={t("pending")}
        cancelLabel={t("cancel")}
        pending={write.isPending}
        error={error}
        onConfirm={() => write.mutate()}
        testId="marketing-kill-confirm"
      />
    </>
  );
}

/** An admin's Close breaker on a paused channel: nothing closes it by itself. */
function CloseBreaker({ project, channel }: { project: string; channel: MarketingChannel }) {
  const t = useTranslations("marketing.channels");
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const close = useMutation({
    mutationFn: () => closeBreaker(browserApi(), project, channel.id),
    onSuccess: async () => {
      notify({ tone: "success", text: t("closed", { channel: channel.channel, account: channel.account }) });
      await queryClient.invalidateQueries({ queryKey: marketingKeys.all(project) });
    },
    onError: (cause) => notify({ tone: "error", text: failure(cause).text }),
  });
  return (
    <Button type="button" size="sm" variant="secondary" onClick={() => close.mutate()} aria-busy={close.isPending} data-testid="marketing-close-breaker">
      {t("close")}
    </Button>
  );
}

function ChannelTable({ project, channels, role }: { project: string; channels: MarketingChannel[]; role: MarketingRole | null }) {
  const t = useTranslations("marketing.channels");
  if (channels.length === 0) {
    return <EmptyState icon={Megaphone} title={t("emptyTitle")} description={t("emptyText")} testId="marketing-channels-empty" />;
  }
  return (
    <Table scrollLabel={t("tableLabel")} data-testid="marketing-channels">
      <TableHeader>
        <TableRow>
          <TableHead>{t("channel")}</TableHead>
          <TableHead>{t("mode")}</TableHead>
          <TableHead>{t("breaker")}</TableHead>
          <TableHead>{t("session")}</TableHead>
          <TableHead>{t("health")}</TableHead>
          {role === "admin" ? <TableHead className="sr-only">{t("actions")}</TableHead> : null}
        </TableRow>
      </TableHeader>
      <TableBody>
        {channels.map((channel) => (
          <TableRow key={channel.id} data-testid="marketing-channel-row" data-paused={channel.paused}>
            <TableCell className="max-w-64 whitespace-normal">
              <div className="flex flex-col">
                <span className="font-medium">{channel.channel}</span>
                <span className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">
                  {channel.account}
                  {channel.owner ? ` (${t("ownedBy", { owner: channel.owner })})` : ""}
                </span>
              </div>
            </TableCell>
            <TableCell>
              <Badge variant={channel.mode === "autonomous" ? "info" : "outline"}>{channel.mode === "autonomous" ? t("autonomous") : t("draftOnly")}</Badge>
            </TableCell>
            <TableCell className="max-w-72 whitespace-normal">
              {channel.paused ? (
                <div className="flex flex-col gap-1" data-testid="marketing-breaker-open">
                  <Badge variant="destructive">{t("open")}</Badge>
                  {channel.paused_reason ? <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{channel.paused_reason}</span> : null}
                </div>
              ) : (
                <Badge variant="success">{t("shut")}</Badge>
              )}
            </TableCell>
            <TableCell>
              {channel.adapter !== "browser" ? (
                <span className="text-xs text-muted-foreground">{t("noSession")}</span>
              ) : channel.session_state === "stale" ? (
                <Badge variant="warning" data-testid="marketing-session-stale">{t("stale")}</Badge>
              ) : channel.session_state === "ok" ? (
                <Badge variant="success">{t("sessionOk")}</Badge>
              ) : (
                <Badge variant="outline">{t("sessionNone")}</Badge>
              )}
            </TableCell>
            <TableCell className="max-w-72 whitespace-normal">
              <div className="flex flex-col gap-1" data-testid="marketing-health" data-health={channel.health}>
                <Badge variant={channel.health === "ok" ? "success" : channel.health === "failing" ? "destructive" : "outline"}>
                  {channel.health === "ok" ? t("healthOk") : channel.health === "failing" ? t("healthFailing") : channel.health === "n/a" ? t("healthNa") : t("healthUnknown")}
                </Badge>
                {channel.health_reason ? <span className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{channel.health_reason}</span> : null}
              </div>
            </TableCell>
            {role === "admin" ? <TableCell>{channel.paused ? <CloseBreaker project={project} channel={channel} /> : null}</TableCell> : null}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function Overview({ project, status, role }: { project: string; status: MarketingStatus; role: MarketingRole | null }) {
  const t = useTranslations("marketing");
  const channels = useHubQuery(marketingChannelsQuery(browserApi, project), null, { live: true });
  return (
    <>
      {status.killed ? (
        <Banner icon={ShieldAlert} tone="danger" title={t("kill.killedTitle")} testId="marketing-killed">
          {status.reason ? t("kill.killedReason", { reason: status.reason }) : t("kill.killedText")}
        </Banner>
      ) : null}
      <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <Card role="region" aria-labelledby="marketing-month-title">
          <CardHeader>
            <CardTitle id="marketing-month-title" className="text-[15px] leading-[22px] font-semibold">
              <h2>{t("month")}</h2>
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <BudgetMeter budget={status.budget} />
            <dl className="grid grid-cols-2 gap-3 text-[13px]">
              <div>
                <dt className="text-muted-foreground">{t("channelCount")}</dt>
                <dd className="text-xl font-semibold tabular-nums" data-testid="marketing-channel-count">{status.channels}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">{t("breakerCount")}</dt>
                <dd className={status.open_breakers > 0 ? "text-xl font-semibold text-danger tabular-nums" : "text-xl font-semibold tabular-nums"} data-testid="marketing-breaker-count">
                  {status.open_breakers}
                </dd>
              </div>
            </dl>
          </CardContent>
        </Card>
        <Card role="region" aria-labelledby="marketing-channels-title" className="min-w-0">
          <CardHeader>
            <CardTitle id="marketing-channels-title" className="text-[15px] leading-[22px] font-semibold">
              <h2>{t("channels.title")}</h2>
            </CardTitle>
          </CardHeader>
          <CardContent className="min-w-0">
            <QueryView state={channels}>{(rows) => <ChannelTable project={project} channels={rows} role={role} />}</QueryView>
          </CardContent>
        </Card>
      </div>
    </>
  );
}

/**
 * The marketing of a project (/p/{project}/x/marketing/overview): the kill switch, the month's cost against its cap,
 * and each channel account with its breaker, its health and, for a browser channel, its login session.
 */
export function OverviewPage({ project, title, initialError }: { project: string; title: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("marketing");
  const role = useMarketingRole(project);
  const state = useHubQuery(marketingStatusQuery(browserApi, project), initialError, { live: true });
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="marketing-overview">
      <PageHeader
        title={title}
        sub={t("sub")}
        actions={state.status === "success" ? <KillSwitch project={project} status={state.data} role={role} /> : undefined}
        status={state.status === "success" && state.data.killed ? <Badge variant="destructive">{t("kill.killedBadge")}</Badge> : undefined}
      />
      <MarketingTabs project={project} current="overview" />
      {role === "reader" ? <ReaderNote project={project} /> : null}
      <QueryView state={state}>{(status) => <Overview project={project} status={status} role={role} />}</QueryView>
    </div>
  );
}
