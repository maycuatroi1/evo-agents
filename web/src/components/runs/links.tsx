"use client";

import { useQuery } from "@tanstack/react-query";
import { ExternalLink as ExternalIcon } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useCallback, useMemo } from "react";

import { browserApi } from "@/lib/api/browser";
import { projectQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { type RepoWeb, repoWeb, splitLinks } from "./forge";

/**
 * Links out of the hub on a run's page: a repo, a branch or a commit on its forge, and the http and https addresses in
 * the trace and the result (forge.ts decides which are safe). Each opens a new tab without an opener or a referrer,
 * and its name says so to a screen reader after its visible text ("api (opens in a new tab)"), which keeps the text
 * of the element the same for the eye and for the specs.
 */

export const EXTERNAL_REL = "noopener noreferrer";

/** On a card: `brand`, with a quiet underline so colour is not all that sets the link apart from the text around it. */
const ON_CARD = "rounded-xs text-brand underline decoration-brand/40 underline-offset-4 transition-colors hover:text-brand-hover hover:decoration-current";
/** On the terminal surface of the trace (`term-bg`): the agent's cobalt of that surface, underlined. */
const ON_TERM = "rounded-xs text-term-agent underline decoration-term-agent/50 underline-offset-2 hover:decoration-current";

/** The accessible name of a link that opens a new tab: its visible text, then the warning. */
export function useNewTabLabel() {
  const t = useTranslations("runs.detail");
  return useCallback((label: string) => t("newTab", { label }), [t]);
}

export function ExternalLink({
  href,
  label,
  children,
  tone = "card",
  icon = false,
  className,
  testId,
}: {
  href: string;
  /** The visible text, which the accessible name starts with. */
  label: string;
  /** What shows, when it is not `label` itself. */
  children?: ReactNode;
  tone?: "card" | "term";
  /** The outward arrow after the text, for a link that stands alone rather than inside a line of words. */
  icon?: boolean;
  className?: string;
  testId?: string;
}) {
  const named = useNewTabLabel();
  return (
    <a
      href={href}
      target="_blank"
      rel={EXTERNAL_REL}
      aria-label={named(label)}
      className={cn(tone === "term" ? ON_TERM : ON_CARD, icon && "inline-flex max-w-full min-w-0 items-center gap-1", className)}
      data-testid={testId}
    >
      {icon ? <span className="min-w-0 [overflow-wrap:anywhere]">{children ?? label}</span> : (children ?? label)}
      {icon ? <ExternalIcon className="size-3 shrink-0" aria-hidden="true" /> : null}
    </a>
  );
}

/** `text` as it is, each http or https address in it a link to a new tab; anything else stays text. */
export function LinkedText({ text, tone = "card" }: { text: string; tone?: "card" | "term" }) {
  const pieces = useMemo(() => splitLinks(text), [text]);
  if (pieces.length === 1 && pieces[0].href === null) return <>{text}</>;
  return (
    <>
      {pieces.map((piece, index) =>
        piece.href ? (
          <ExternalLink key={index} href={piece.href} label={piece.text} tone={tone} testId="external-link" />
        ) : (
          piece.text
        ),
      )}
    </>
  );
}

/**
 * The web page of each repo of `project` by name, from the origins the project registered. Until the project is read,
 * or when it cannot be, every repo has none, so its name shows as text.
 */
export function useRepoWeb(project: string): (name: string | null | undefined) => RepoWeb | null {
  const { data } = useQuery(projectQuery(browserApi, project));
  const origins = useMemo(() => new Map((data?.repos ?? []).map((repo) => [repo.name, repo.origin ?? null])), [data]);
  return useCallback((name) => (name ? repoWeb(origins.get(name)) : null), [origins]);
}
