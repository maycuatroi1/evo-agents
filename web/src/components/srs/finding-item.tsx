"use client";

import { ExternalLink, MessagesSquare } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Identifier } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { nodeHref } from "@/lib/kg/routes";

import type { SrsFinding, SrsFindingSource } from "./api";
import { CodeChip, VersionChip } from "./badges";
import { FindingKindTag, FindingLevelTag, FindingStatusBadge } from "./finding-badges";
import { chatTarget, settlerOf } from "./findings-model";
import { documentHref, itemHref } from "./links";

const LINK = "inline-flex items-center gap-1.5 text-[13px] text-brand underline-offset-4 hover:text-brand-hover hover:underline";

/**
 * A finding's chat link: the settle chat's first message while it is open, the message that settled it after. A path
 * of the hub's web opens in place; an http(s) URL in a new tab, without an opener or a referrer.
 */
export function ChatLink({ link, label, testId }: { link: string | null; label: string; testId?: string }) {
  const t = useTranslations("srs.findings");
  const target = chatTarget(link);
  if (!target) return null;
  if (!target.external) {
    return (
      <Link href={target.href as Route} className={LINK} data-testid={testId}>
        <MessagesSquare className="size-4 shrink-0" aria-hidden="true" />
        {label}
      </Link>
    );
  }
  return (
    <a href={target.href} target="_blank" rel="noopener noreferrer" className={LINK} data-testid={testId}>
      <MessagesSquare className="size-4 shrink-0" aria-hidden="true" />
      {label}
      <ExternalLink className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="sr-only">{t("newTab")}</span>
    </a>
  );
}

/** One source of a finding: an item's code (linking to that item where the reader sees its document), a node of the knowledge graph, a file or a later version, with the note it carries. */
function Source({ project, slug, source }: { project: string; slug: string; source: SrsFindingSource }) {
  const t = useTranslations("srs.findings.sourceKind");
  const { kind, ref } = source;
  const version = kind === "version" && /^[1-9][0-9]{0,8}$/.test(ref) ? Number(ref) : null;
  const chip =
    kind === "code" ? (
      <CodeChip code={ref} href={source.document ? itemHref(project, source.document, ref) : undefined} testId="srs-finding-source-code" />
    ) : kind === "node" ? (
      <Identifier value={ref} href={nodeHref(project, ref)} title={ref} />
    ) : version !== null ? (
      <VersionChip number={version} href={documentHref(project, slug, { v: version })} />
    ) : (
      <Identifier value={ref} title={ref} />
    );
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5" data-source-kind={kind}>
      <span className="sr-only">{t(kind)}: </span>
      {chip}
      {source.note ? <span className="min-w-0 text-muted-foreground [overflow-wrap:anywhere]">{source.note}</span> : null}
    </li>
  );
}

function Sources({ project, slug, sources }: { project: string; slug: string; sources: SrsFindingSource[] }) {
  return (
    <ul className="flex min-w-0 flex-col gap-1" role="list">
      {sources.map((source) => (
        <Source key={`${source.kind}:${source.ref}`} project={project} slug={slug} source={source} />
      ))}
    </ul>
  );
}

/** How a settled finding was settled: by whom (a member, the knowledge graph, @ba's waiver), when, its words and the message that settled it. */
function Settlement({ project, slug, finding }: { project: string; slug: string; finding: SrsFinding }) {
  const t = useTranslations("srs.findings");
  const settler = settlerOf(finding);
  if (!settler) return null;
  const who =
    settler.by === "member"
      ? t(finding.status === "waived" ? "waivedBy" : "resolvedBy", { login: settler.login })
      : settler.by === "graph"
        ? t("resolvedFromGraph")
        : t("waivedByAgent");
  return (
    <div className="flex min-w-0 flex-col gap-1.5 rounded-sm bg-surface-sunken px-3 py-2" data-testid="srs-finding-settled">
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-medium text-foreground" data-testid="srs-finding-settler">
          {who}
        </span>
        {finding.settled_at ? (
          <span className="text-xs text-fg-subtle">
            <When value={finding.settled_at} />
          </span>
        ) : null}
      </p>
      {finding.resolution ? <p className="whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]">{finding.resolution}</p> : null}
      {finding.resolution_sources.length > 0 ? (
        <div className="flex min-w-0 flex-col gap-1">
          <p className="text-xs text-fg-subtle">{t("settledWith")}</p>
          <Sources project={project} slug={slug} sources={finding.resolution_sources} />
        </div>
      ) : null}
      <ChatLink link={finding.chat_link} label={t("settledMessage")} testId="srs-finding-chat" />
    </div>
  );
}

/**
 * A finding as the Findings card lists it: its level, kind and state, its number and the version it was found on, its
 * summary, the item it is about and the sources that show it; once settled, how; while open, its settle chat.
 */
export function FindingItem({ project, slug, finding }: { project: string; slug: string; finding: SrsFinding }) {
  const t = useTranslations("srs.findings");
  const about = finding.item_code ?? finding.item;
  return (
    <li
      className="flex min-w-0 flex-col gap-2 px-4 py-3 text-[13px]"
      data-testid="srs-finding"
      data-finding={finding.id}
      data-status={finding.status}
      data-level={finding.level}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <FindingLevelTag level={finding.level} />
        <FindingKindTag kind={finding.kind} />
        <FindingStatusBadge status={finding.status} />
        <span className="ml-auto text-xs whitespace-nowrap text-fg-subtle tabular-nums">{t("foundOn", { id: finding.id, version: finding.version })}</span>
      </div>
      <p className="text-sm text-foreground [overflow-wrap:anywhere]" data-testid="srs-finding-summary">
        {finding.summary}
      </p>
      <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-3 gap-y-1.5">
        <dt className="text-xs text-fg-subtle">{t("about")}</dt>
        <dd className="min-w-0">
          {about ? (
            <CodeChip code={about} href={itemHref(project, slug, about, finding.version)} testId="srs-finding-item" />
          ) : (
            <span className="text-muted-foreground">{t("wholeDocument")}</span>
          )}
        </dd>
        <dt className="text-xs text-fg-subtle">{t("sources")}</dt>
        <dd className="min-w-0" data-testid="srs-finding-sources">
          <Sources project={project} slug={slug} sources={finding.sources} />
        </dd>
      </dl>
      {finding.status === "open" ? (
        <ChatLink link={finding.chat_link} label={t("openChat")} testId="srs-finding-chat" />
      ) : (
        <Settlement project={project} slug={slug} finding={finding} />
      )}
    </li>
  );
}
