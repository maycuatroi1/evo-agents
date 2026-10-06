"use client";

import { Archive } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { type ApiErrorInfo, toInfo } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

import { LabelBadge, MemoryTypeBadge } from "./badges";
import { labelNames } from "./memory-meta";
import { type Memory, memoryRevisionsQuery, olderRevisions, type RevisionSummary } from "./queries";

type Props = {
  memory: Memory;
  selected: number;
  hrefOf: (revision: number) => Route;
  initialError: ApiErrorInfo | null;
  /** The project's first location, left out of label badges. */
  unrestricted?: string;
};

/**
 * The revisions of a memory the visitor may read, newest first, each a link to the file as it was. The API leaves
 * out a revision whose label was above the visitor's grant, so the list can skip numbers; it never shows them.
 */
export function RevisionHistory({ memory, selected, hrefOf, initialError, unrestricted }: Props) {
  const state = useHubQuery(memoryRevisionsQuery(browserApi, memory.id), initialError);
  return (
    <QueryView
      state={state}
      loading={
        <div className="flex flex-col gap-2">
          {Array.from({ length: 3 }, (_, i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      }
    >
      {(first) => (
        <Entries
          key={`${memory.id}-${memory.revision}`}
          memory={memory}
          first={first.items}
          nextBefore={first.next_before}
          selected={selected}
          hrefOf={hrefOf}
          unrestricted={unrestricted}
        />
      )}
    </QueryView>
  );
}

function Entries({
  memory,
  first,
  nextBefore,
  selected,
  hrefOf,
  unrestricted,
}: {
  memory: Memory;
  first: RevisionSummary[];
  nextBefore: number | null;
  selected: number;
  hrefOf: (revision: number) => Route;
  unrestricted?: string;
}) {
  const t = useTranslations("memories.history");
  const format = useFormatter();
  const [older, setOlder] = useState<RevisionSummary[]>([]);
  const [before, setBefore] = useState(nextBefore);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState<ApiErrorInfo | null>(null);
  const items = [...first, ...older];

  const more = async () => {
    if (before === null) return;
    setLoading(true);
    setFailed(null);
    try {
      const page = await olderRevisions(browserApi, memory.id, before);
      setOlder((current) => [...current, ...page.items]);
      setBefore(page.next_before);
    } catch (error) {
      setFailed(toInfo(error));
    } finally {
      setLoading(false);
    }
  };

  if (items.length === 0) return <p className="text-sm text-muted-foreground">{t("empty")}</p>;

  return (
    <div className="flex flex-col gap-3">
      <ol className="flex flex-col gap-1.5" data-testid="revision-history" aria-label={t("title")}>
        {items.map((item, index) => {
          const previous = items[index + 1];
          const current = item.revision === selected;
          const relabelled = previous && labelNames(previous.label).level !== labelNames(item.label).level;
          const retyped = previous && previous.type !== item.type;
          return (
            <li key={item.revision} data-revision={item.revision}>
              <Link
                href={hrefOf(item.revision)}
                aria-current={current ? "page" : undefined}
                scroll={false}
                className={cn(
                  "flex flex-col gap-1 rounded-md border px-3 py-2 text-sm transition-colors",
                  current ? "border-brand/40 bg-surface-selected text-foreground" : "hover:bg-muted",
                )}
              >
                <span className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{t("revision", { revision: item.revision })}</span>
                  {item.revision === memory.revision ? <Badge variant="secondary">{t("latest")}</Badge> : null}
                  {current ? <span className="sr-only">{t("viewing")}</span> : null}
                  {item.deleted ? (
                    <Badge variant="warning" className="gap-1">
                      <Archive aria-hidden="true" />
                      {t("deleted")}
                    </Badge>
                  ) : (
                    <span
                      className={cn(
                        "ml-auto text-xs tabular-nums",
                        current ? "text-accent-foreground" : "text-muted-foreground",
                      )}
                    >
                      {t("size", { size: item.size })}
                    </span>
                  )}
                </span>
                <span className={cn("text-xs", current ? "text-accent-foreground" : "text-muted-foreground")}>
                  {t.rich("by", {
                    actor: item.actor,
                    when: () => (
                      <time dateTime={item.created_at} className="tabular-nums">
                        {format.dateTime(new Date(item.created_at), { dateStyle: "medium", timeStyle: "short" })}
                      </time>
                    ),
                  })}
                </span>
                {retyped || relabelled ? (
                  <span className="flex flex-wrap items-center gap-1.5 text-xs">
                    {retyped ? <MemoryTypeBadge type={item.type} /> : null}
                    {relabelled && memory.scope === "project" ? (
                      <LabelBadge label={item.label} unrestricted={unrestricted} />
                    ) : null}
                  </span>
                ) : null}
              </Link>
            </li>
          );
        })}
      </ol>
      {failed ? (
        <p role="alert" className="text-sm text-danger">
          {t("moreFailed", { status: failed.status || "-" })}
        </p>
      ) : null}
      {before !== null ? (
        <Button variant="outline" size="sm" onClick={() => void more()} busy={loading} className="self-start">
          {loading ? t("loadingMore") : t("more")}
        </Button>
      ) : null}
    </div>
  );
}
