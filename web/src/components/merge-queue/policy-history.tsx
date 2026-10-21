"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, GitCompareArrows } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { policyCompareQuery, type PolicyRevision } from "@/lib/merge-queue";
import { cn } from "@/lib/utils";

import { Panel, TEXT_LINK } from "./chrome";
import { useDottedLabel, useValueText } from "./policy-value";

const CHANGED_SHOWN = 4;

export type PolicyLinks = {
  revision: (revision: number | null) => Route;
  compare: (from: number, to: number) => Route;
};

/** Every revision of the policy, the newest first: who wrote it, when, why, and the keys whose value it changed. */
export function RevisionsPanel({ revisions, shown, links }: { revisions: readonly PolicyRevision[]; shown: number; links: PolicyLinks }) {
  const t = useTranslations("policy.history");
  const dotted = useDottedLabel();
  const newest = revisions[0]?.revision ?? 0;
  return (
    <Panel title={t("title")} count={revisions.length} countWords={t("count", { count: revisions.length })} testId="policy-revisions">
      {revisions.length === 0 ? (
        <p className="px-4 py-3 text-[13px] text-muted-foreground">{t("none")}</p>
      ) : (
        <ol className="flex flex-col">
          {revisions.map((revision) => {
            const current = revision.revision === shown;
            const more = revision.changed.length - CHANGED_SHOWN;
            return (
              <li
                key={revision.revision}
                className={cn("flex min-w-0 flex-col gap-1.5 border-t px-4 py-3 first:border-t-0", current && "bg-surface-selected")}
                data-testid="policy-revision"
                data-revision={revision.revision}
              >
                <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-[13px]">
                  <Link
                    href={links.revision(revision.revision === newest ? null : revision.revision)}
                    aria-current={current ? "page" : undefined}
                    className="font-medium tabular-nums underline-offset-4 hover:text-brand hover:underline max-md:py-2.5"
                    data-testid="policy-revision-link"
                  >
                    {t("number", { revision: revision.revision })}
                  </Link>
                  <span className="font-mono text-xs text-muted-foreground">{revision.created_by}</span>
                  <span className="text-xs text-fg-subtle">
                    <When value={revision.created_at} short />
                  </span>
                  <Link href={links.compare(revision.revision - 1, revision.revision)} className={cn(TEXT_LINK, "ml-auto text-xs max-md:py-2.5")} data-testid="policy-revision-compare">
                    {t("compareBefore")}
                  </Link>
                </div>
                {revision.note ? <p className="text-xs text-pretty text-muted-foreground [overflow-wrap:anywhere]">{revision.note}</p> : null}
                {revision.changed.length > 0 ? (
                  <ul className="flex flex-wrap gap-1" aria-label={t("changed", { count: revision.changed.length })}>
                    {revision.changed.slice(0, CHANGED_SHOWN).map((key) => (
                      <li key={key}>
                        <Tag title={key}>{dotted(key)}</Tag>
                      </li>
                    ))}
                    {more > 0 ? (
                      <li>
                        <Tag>{t("more", { count: more })}</Tag>
                      </li>
                    ) : null}
                  </ul>
                ) : (
                  <p className="text-xs text-fg-subtle">{t("nothingChanged")}</p>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </Panel>
  );
}

/**
 * Two revisions side by side: the keys whose value differs, each before and after, as the two read now. Revision 0 is
 * the project before any policy, every repo off.
 */
export function ComparePanel({
  project,
  revisions,
  from,
  to,
  onChange,
}: {
  project: string;
  revisions: readonly PolicyRevision[];
  from: number;
  to: number;
  onChange: (from: number, to: number) => void;
}) {
  const t = useTranslations("policy.compare");
  const ids = useId();
  const dotted = useDottedLabel();
  const value = useValueText();
  const comparison = useQuery(policyCompareQuery(browserApi, project, from, to));
  const choices = [...revisions.map((revision) => revision.revision), 0];
  const option = (revision: number) => (
    <NativeSelectOption key={revision} value={revision}>
      {revision === 0 ? t("none") : t("revision", { revision })}
    </NativeSelectOption>
  );
  return (
    <Panel title={t("title")} testId="policy-compare">
      <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-end gap-2 border-b px-4 py-3">
        <div className="flex min-w-0 flex-col gap-1.5">
          <label htmlFor={`${ids}-from`} className="text-xs font-medium text-muted-foreground">
            {t("from")}
          </label>
          <NativeSelect id={`${ids}-from`} className="w-full" value={from} onChange={(event) => onChange(Number(event.target.value), to)} data-testid="policy-compare-from">
            {choices.map(option)}
          </NativeSelect>
        </div>
        <ArrowRight className="mb-2 size-4 text-muted-foreground max-md:mb-3.5" aria-hidden="true" />
        <div className="flex min-w-0 flex-col gap-1.5">
          <label htmlFor={`${ids}-to`} className="text-xs font-medium text-muted-foreground">
            {t("to")}
          </label>
          <NativeSelect id={`${ids}-to`} className="w-full" value={to} onChange={(event) => onChange(from, Number(event.target.value))} data-testid="policy-compare-to">
            {choices.map(option)}
          </NativeSelect>
        </div>
      </div>
      {comparison.isPending ? (
        <div className="flex flex-col gap-2 px-4 py-3" role="status" aria-label={t("loading")}>
          <Skeleton className="h-5 w-2/3" />
          <Skeleton className="h-5 w-1/2" />
        </div>
      ) : comparison.isError ? (
        <p className="px-4 py-3 text-[13px] text-danger" role="alert">
          {t("failed")}
        </p>
      ) : comparison.data.changes.length === 0 ? (
        <p className="flex items-center gap-2 px-4 py-3 text-[13px] text-muted-foreground" data-testid="policy-compare-same">
          <GitCompareArrows className="size-4" aria-hidden="true" />
          {t("same")}
        </p>
      ) : (
        <ul className="flex flex-col" data-testid="policy-compare-changes" aria-label={t("changes", { count: comparison.data.changes.length })}>
          {comparison.data.changes.map((change) => (
            <li key={change.key} className="grid min-w-0 gap-1 border-t px-4 py-2.5 first:border-t-0 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]" data-testid="policy-change" data-key={change.key}>
              <p className="text-[13px] font-medium [overflow-wrap:anywhere] sm:col-span-2" title={change.key}>
                {dotted(change.key)}
              </p>
              <p className="min-w-0 text-xs [overflow-wrap:anywhere]">
                <span className="text-muted-foreground">{t("before")} </span>
                <span className="text-danger line-through decoration-danger/40" data-testid="policy-change-before">
                  {value(change.key, change.before)}
                </span>
              </p>
              <p className="min-w-0 text-xs [overflow-wrap:anywhere]">
                <span className="text-muted-foreground">{t("after")} </span>
                <span className="font-medium text-success" data-testid="policy-change-after">
                  {value(change.key, change.after)}
                </span>
              </p>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
