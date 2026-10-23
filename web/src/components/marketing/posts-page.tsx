"use client";

import { Check, Copy, ExternalLink, ListChecks } from "lucide-react";
import { useTranslations } from "next-intl";
import { type RefObject, useRef } from "react";

import { useClipboard } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type MarketingPost, marketingPostsQuery } from "./api";
import { canWrite, MarketingTabs, ReaderNote, useMarketingRole, useMoney } from "./chrome";

/** The groups of the gate's result, in the order a writer looks at them, and the post states each holds. */
const GROUPS = [
  { id: "drafts", states: ["draft"] },
  { id: "waiting", states: ["proposed", "judging"] },
  { id: "passed", states: ["queued", "posting"] },
  { id: "posted", states: ["posted"] },
  { id: "stopped", states: ["failed", "withdrawn"] },
] as const;
type GroupId = (typeof GROUPS)[number]["id"];

const STATE_VARIANT: Record<string, "outline" | "info" | "success" | "warning" | "destructive"> = {
  proposed: "outline",
  judging: "outline",
  queued: "info",
  posting: "info",
  posted: "success",
  draft: "warning",
  failed: "destructive",
  withdrawn: "outline",
};
const STATES = ["proposed", "judging", "queued", "posting", "posted", "draft", "failed", "withdrawn"] as const;
type PostState = (typeof STATES)[number];

/** Copies a draft's text for the member who shares it by hand, or selects it where the clipboard is refused. */
function CopyDraft({ post, target }: { post: MarketingPost; target: RefObject<HTMLElement | null> }) {
  const t = useTranslations("marketing.posts");
  const { state, copy } = useClipboard(post.body, target, { copied: t("copied"), selected: t("selected") });
  return (
    <Button type="button" size="sm" variant="secondary" onClick={() => void copy()} data-testid="marketing-copy">
      {state === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      {state === "copied" ? t("copiedShort") : t("copy")}
    </Button>
  );
}

function PostCard({ post }: { post: MarketingPost }) {
  const t = useTranslations("marketing.posts");
  const money = useMoney();
  const body = useRef<HTMLParagraphElement>(null);
  const state = STATES.includes(post.state as PostState) ? (post.state as PostState) : null;
  return (
    <li className="flex min-w-0 flex-col gap-2 py-3" data-testid="marketing-post" data-state={post.state}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium">{post.channel}</span>
        <span className="min-w-0 font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{post.account}</span>
        <Badge variant={STATE_VARIANT[post.state] ?? "outline"}>{state ? t(`states.${state}`) : post.state}</Badge>
        {post.dry_run ? <Badge variant="outline">{t("dryRun")}</Badge> : null}
        <span className="ml-auto text-xs text-muted-foreground">
          <When value={post.posted_at ?? post.created_at} short />
        </span>
      </div>
      <p ref={body} className="text-[13px] whitespace-pre-wrap [overflow-wrap:anywhere]" data-testid="marketing-post-body">
        {post.body}
      </p>
      {post.reason ? (
        <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]" data-testid="marketing-post-reason">
          {t("reason", { reason: post.reason })}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        {post.state === "draft" ? <CopyDraft post={post} target={body} /> : null}
        {post.posted_url ? (
          <Button asChild size="sm" variant="ghost">
            <a href={post.posted_url} target="_blank" rel="noreferrer">
              <ExternalLink aria-hidden="true" />
              {t("open")}
            </a>
          </Button>
        ) : null}
        {post.cost_usd > 0 ? <span className="text-xs text-muted-foreground tabular-nums">{t("cost", { cost: money(post.cost_usd) })}</span> : null}
      </div>
    </li>
  );
}

function Group({ id, posts }: { id: GroupId; posts: MarketingPost[] }) {
  const t = useTranslations("marketing.posts");
  return (
    <Card role="region" aria-labelledby={`marketing-${id}-title`} data-testid={`marketing-group-${id}`}>
      <CardHeader>
        <CardTitle id={`marketing-${id}-title`} className="flex flex-wrap items-baseline gap-x-3 text-[15px] leading-[22px] font-semibold">
          <h2>{t(`groups.${id}`)}</h2>
          <span className="text-xs font-normal text-fg-subtle tabular-nums">{t("count", { count: posts.length })}</span>
        </CardTitle>
        <p className="text-[13px] text-muted-foreground">{t(`groupText.${id}`)}</p>
      </CardHeader>
      <CardContent className="min-w-0">
        {posts.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">{t("none")}</p>
        ) : (
          <ul className="divide-y">
            {posts.map((post) => (
              <PostCard key={post.id} post={post} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function Posts({ posts }: { posts: MarketingPost[] }) {
  const t = useTranslations("marketing.posts");
  if (posts.length === 0) return <EmptyState icon={ListChecks} title={t("emptyTitle")} description={t("emptyText")} testId="marketing-posts-empty" />;
  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="marketing-posts">
      {GROUPS.map((group) => (
        <Group key={group.id} id={group.id} posts={posts.filter((post) => (group.states as readonly string[]).includes(post.state))} />
      ))}
    </div>
  );
}

function WriterPosts({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const state = useHubQuery(marketingPostsQuery(browserApi, project), initialError, { live: true });
  return (
    <QueryView state={state} loading={<ListSkeleton rows={6} />}>
      {(posts) => <Posts posts={posts} />}
    </QueryView>
  );
}

/**
 * The posts of a project (/p/{project}/x/marketing/posts), for its writers: grouped by what the gate did with them,
 * drafts first, each draft with a Copy button for the member who shares it by hand (a personal account, a Facebook
 * group). A reader reads no post's text: a note says so.
 */
export function PostsPage({ project, title, initialError }: { project: string; title: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("marketing");
  const role = useMarketingRole(project);
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="marketing-posts-page">
      <PageHeader title={title} sub={t("posts.sub")} />
      <MarketingTabs project={project} current="posts" />
      {role === null ? (
        <ListSkeleton rows={4} />
      ) : canWrite(role) ? (
        <WriterPosts project={project} initialError={initialError} />
      ) : (
        <ReaderNote project={project} text={t("posts.readerText")} />
      )}
    </div>
  );
}
