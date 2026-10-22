"use client";

import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { projectHref } from "@/components/shell/nav";
import { agentHref } from "@/lib/agent-queries";
import { chatHref } from "@/lib/chat-keys";
import { cn } from "@/lib/utils";

import type { Provenance } from "./queries";

// Underlined, so a link among the muted words is told by more than its colour.
const LINK = "text-foreground underline decoration-border underline-offset-2 transition-colors hover:text-brand";

/**
 * Where a revision of a memory came from, on one line: the Curator's proposal that promoted agents' memories into it,
 * or the run whose agent wrote it, with the agent and its version, the person at the run's origin, the run's hop in its
 * chat and the chat. Each id is a link to its page when the memory's project is known (the routes of runs, proposals
 * and agents sit under the project).
 */
export function ProvenanceLine({
  project,
  provenance,
  className,
}: {
  project: string | null;
  provenance: Provenance;
  className?: string;
}) {
  const t = useTranslations("memories.provenance");
  const link = (href: Route | null, text: string, testId: string) =>
    href ? (
      <Link href={href} className={LINK} data-testid={testId}>
        {text}
      </Link>
    ) : (
      <span data-testid={testId}>{text}</span>
    );
  const parts: ReactNode[] = [];
  if (provenance.proposal != null) {
    const href = project ? projectHref(project, `curator/proposals/${provenance.proposal}`) : null;
    parts.push(link(href, t("proposal", { id: provenance.proposal }), "provenance-proposal"));
  }
  if (provenance.run != null) {
    parts.push(link(project ? projectHref(project, `runs/${provenance.run}`) : null, t("run", { id: provenance.run }), "provenance-run"));
  }
  if (provenance.agent) {
    const text = provenance.agent_version != null ? t("agentVersion", { handle: provenance.agent, version: provenance.agent_version }) : `@${provenance.agent}`;
    parts.push(link(project ? agentHref(project, provenance.agent) : null, text, "provenance-agent"));
  }
  if (provenance.origin) parts.push(<span data-testid="provenance-origin">{t("origin", { login: provenance.origin })}</span>);
  if (provenance.hop != null) parts.push(<span data-testid="provenance-hop">{t("hop", { hop: provenance.hop })}</span>);
  if (provenance.chat != null) parts.push(link(chatHref(provenance.chat, provenance.chat_message), t("chat", { id: provenance.chat }), "provenance-chat"));
  if (parts.length === 0) return null;
  return (
    <span
      className={cn("flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground", className)}
      data-testid="memory-provenance"
      data-run={provenance.run ?? undefined}
      data-proposal={provenance.proposal ?? undefined}
      data-hop={provenance.hop ?? undefined}
    >
      {parts.map((part, index) => (
        <span key={index} className="inline-flex items-center gap-1.5">
          {index > 0 ? <span aria-hidden="true">·</span> : null}
          {part}
        </span>
      ))}
    </span>
  );
}
