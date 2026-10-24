"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ListChecks } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { NAME_LINK, Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton, StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { kgKeys } from "@/lib/kg/queries";
import { dismissReviewItem, groupReview, kgReviewQuery, MAX_REASON, nodeNames, REVIEW_RULES, type ReviewGroup } from "@/lib/kg/review";
import { clusterHref, nodeHref, type ReviewFilter, type ReviewState, reviewHref } from "@/lib/kg/routes";
import type { ReviewItem, ReviewQueue } from "@/lib/kg/types";

import { KindBadge } from "./badges";
import { ReviewDecisions } from "./decisions";
import { KgHeader } from "./kg-header";

const STATES: readonly ReviewState[] = ["open", "dismissed", "all"];

function FilterLink({ href, active, children, testId }: { href: ReturnType<typeof reviewHref>; active: boolean; children: React.ReactNode; testId?: string }) {
  return (
    <Button asChild size="sm" variant={active ? "secondary" : "ghost"}>
      <Link href={href} aria-current={active ? "page" : undefined} data-testid={testId}>
        {children}
      </Link>
    </Button>
  );
}

function Filters({ project, filter, queue }: { project: string; filter: Required<ReviewFilter>; queue: ReviewQueue }) {
  const t = useTranslations("kg.review");
  const counts = new Map(queue.counts.map((c) => [c.rule, c]));
  const clusterLabel = queue.items.find((item) => item.cluster === filter.cluster)?.cluster_label ?? filter.cluster;
  return (
    <nav aria-label={t("filters")} className="flex flex-col gap-2" data-testid="kg-review-filters">
      <div className="flex flex-wrap items-center gap-1">
        <span className="mr-1 text-xs text-muted-foreground">{t("rule")}</span>
        <FilterLink href={reviewHref(project, { ...filter, rule: null })} active={filter.rule === null}>
          {t("allRules")}
        </FilterLink>
        {REVIEW_RULES.map((rule) => (
          <FilterLink key={rule} href={reviewHref(project, { ...filter, rule })} active={filter.rule === rule} testId={`kg-review-rule-${rule}`}>
            {t(`rules.${rule}`)}
            <span className="tabular-nums text-muted-foreground">{counts.get(rule)?.open ?? 0}</span>
          </FilterLink>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-1">
        <span className="mr-1 text-xs text-muted-foreground">{t("state")}</span>
        {STATES.map((state) => (
          <FilterLink key={state} href={reviewHref(project, { ...filter, state })} active={filter.state === state} testId={`kg-review-state-${state}`}>
            {t(`states.${state}`)}
          </FilterLink>
        ))}
        {filter.cluster ? (
          <>
            <Tag>{t("clusterFilter", { label: clusterLabel ?? "" })}</Tag>
            <FilterLink href={reviewHref(project, { ...filter, cluster: null })} active={false} testId="kg-review-clear-cluster">
              {t("clearCluster")}
            </FilterLink>
          </>
        ) : null}
      </div>
    </nav>
  );
}

function DismissForm({ project, item, onDone }: { project: string; item: ReviewItem; onDone: () => void }) {
  const t = useTranslations("kg.review");
  const id = useId();
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [reason, setReason] = useState("");
  const [error, setError] = useState<WriteFailure | null>(null);
  const save = useMutation({
    mutationFn: () => dismissReviewItem(browserApi(), project, item.fingerprint, reason.trim()),
    onSuccess: async () => {
      setError(null);
      notify({ tone: "success", text: t("dismissed") });
      onDone();
      await queryClient.invalidateQueries({ queryKey: kgKeys.review(project) });
    },
    onError: (cause) => setError(failure(cause, { 403: t("forbidden"), 404: t("gone") })),
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (reason.trim()) save.mutate();
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 pt-2" data-testid="kg-review-dismiss-form">
      <Label htmlFor={id}>{t("reasonLabel")}</Label>
      <Textarea
        id={id}
        value={reason}
        maxLength={MAX_REASON}
        rows={2}
        placeholder={t("reasonPlaceholder")}
        onChange={(event) => setReason(event.target.value)}
        data-testid="kg-review-reason"
      />
      {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
      <div className="flex gap-2">
        <Button type="submit" size="sm" variant="secondary" disabled={!reason.trim()} aria-busy={save.isPending} data-testid="kg-review-dismiss-save">
          {save.isPending ? t("dismissing") : t("dismiss")}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onDone}>
          {t("cancel")}
        </Button>
      </div>
    </form>
  );
}

function Detail({ item }: { item: ReviewItem }) {
  const t = useTranslations("kg.review");
  const { detail } = item;
  const parts: string[] = [];
  if (typeof detail.mentions === "number") parts.push(t("mentions", { count: detail.mentions }));
  if (Array.isArray(detail.samples) && detail.samples.length > 0) {
    parts.push(t("samples", { samples: detail.samples.slice(0, 3).join("; ") }));
  }
  if (typeof detail.path === "string") parts.push(detail.path);
  if (typeof detail.where === "string" && detail.where) parts.push(detail.where);
  if (item.weight > 0) parts.push(t("edges", { count: item.weight }));
  return parts.length > 0 ? <span className="text-xs break-all text-muted-foreground">{parts.join(", ")}</span> : null;
}

function Item({ project, item, canDismiss }: { project: string; item: ReviewItem; canDismiss: boolean }) {
  const t = useTranslations("kg.review");
  const [open, setOpen] = useState(false);
  const { names, more } = nodeNames(item);
  return (
    <li className="flex flex-col gap-1 border-t py-3 first:border-t-0" data-testid="kg-review-item" data-fingerprint={item.fingerprint}>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        {item.nodes.slice(0, names.length).map((node, index) => (
          <span key={node.id} className="inline-flex items-baseline gap-1">
            <KindBadge kind={node.kind} />
            <Link href={nodeHref(project, node.id)} className={NAME_LINK} data-testid="kg-review-node">
              {names[index]}
            </Link>
          </span>
        ))}
        {more > 0 ? <span className="text-xs text-muted-foreground">{t("more", { count: more })}</span> : null}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Tag>{t(`actions.${item.action}` as Parameters<typeof t>[0])}</Tag>
        <Detail item={item} />
      </div>
      {item.dismissal ? (
        <p className="text-xs text-muted-foreground" data-testid="kg-review-dismissal">
          {t("dismissedBy", { by: item.dismissal.by, reason: item.dismissal.reason })} <When value={item.dismissal.at} short />
        </p>
      ) : canDismiss ? (
        open ? (
          <DismissForm project={project} item={item} onDone={() => setOpen(false)} />
        ) : (
          <Button size="sm" variant="outline" className="self-start" onClick={() => setOpen(true)} data-testid="kg-review-dismiss">
            {t("dismiss")}
          </Button>
        )
      ) : null}
    </li>
  );
}

function RuleSection({ project, group, canDismiss }: { project: string; group: ReviewGroup; canDismiss: boolean }) {
  const t = useTranslations("kg.review");
  return (
    <Card role="region" aria-labelledby={`kg-review-${group.rule}`} data-testid={`kg-review-group-${group.rule}`}>
      <CardHeader>
        <CardTitle id={`kg-review-${group.rule}`} className="flex items-baseline justify-between gap-3 text-[15px] leading-[22px] font-semibold">
          <h2>{t(`rules.${group.rule}`)}</h2>
          <span className="text-xs font-normal text-muted-foreground tabular-nums">{t("ruleItems", { count: group.items })}</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {group.clusters.map((cluster) => (
          <section key={cluster.cluster ?? ""} className="flex flex-col" data-testid="kg-review-cluster">
            <h3 className="text-sm font-medium">
              {cluster.cluster ? (
                <Link href={clusterHref(project, cluster.cluster)} className={NAME_LINK}>
                  {cluster.label || cluster.cluster}
                </Link>
              ) : (
                <span className="text-muted-foreground">{t("noCluster")}</span>
              )}
            </h3>
            <ul>
              {cluster.items.map((item) => (
                <Item key={item.fingerprint} project={project} item={item} canDismiss={canDismiss} />
              ))}
            </ul>
          </section>
        ))}
      </CardContent>
    </Card>
  );
}

function Queue({ project, filter, queue }: { project: string; filter: Required<ReviewFilter>; queue: ReviewQueue }) {
  const t = useTranslations("kg.review");
  if (!queue.computed) {
    return <StatePanel icon={ListChecks} title={t("notComputed")} description={queue.reason ?? undefined} testId="kg-review-none" />;
  }
  const groups = groupReview(queue.items);
  return (
    <>
      <Filters project={project} filter={filter} queue={queue} />
      {!queue.can_dismiss ? <p className="text-xs text-muted-foreground">{t("readerNote")}</p> : null}
      {groups.length === 0 ? (
        <StatePanel icon={ListChecks} title={t("noItems")} testId="kg-review-empty" />
      ) : (
        groups.map((group) => <RuleSection key={group.rule} project={project} group={group} canDismiss={queue.can_dismiss} />)
      )}
    </>
  );
}

/** /p/{project}/kg/review: the review queue the reader can see, by rule then cluster, with dismissal for writers. */
export function KgReviewPage({ project, filter, error }: { project: string; filter: Required<ReviewFilter>; error: ApiErrorInfo | null }) {
  const t = useTranslations("kg.review");
  const state = useHubQuery(kgReviewQuery(browserApi, project, filter), error);
  return (
    <>
      <KgHeader project={project} current="review" />
      <div className="flex flex-col gap-1">
        <h2 className="text-[15px] leading-[22px] font-semibold">{t("title")}</h2>
        <p className="max-w-3xl text-sm text-pretty text-muted-foreground">{t("intro")}</p>
      </div>
      <QueryView state={state} loading={<PageSkeleton />}>
        {(queue) => <Queue project={project} filter={filter} queue={queue} />}
      </QueryView>
      <ReviewDecisions project={project} />
    </>
  );
}
