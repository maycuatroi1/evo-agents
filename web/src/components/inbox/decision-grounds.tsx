"use client";

import { ExternalLink, Lightbulb, OctagonAlert, Paperclip, Undo2 } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { cn } from "@/lib/utils";

import { CredentialNeeds } from "./credential-decisions";
import { credentialRequest } from "./credential-model";
import { type EvidenceLink, evidenceLink } from "./evidence-links";
import type { Decision } from "./queries";

const LINK = "rounded-xs font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline [overflow-wrap:anywhere]";

function EvidenceItem({ link, screen }: { link: EvidenceLink; screen: boolean }) {
  const t = useTranslations("inbox.grounds.ref");
  const text = t(link.words, link.values);
  const label = <span className={cn(link.mono && "font-mono text-[13px]")}>{text}</span>;
  return (
    <li className={cn("flex min-w-0 items-start gap-2", screen && "min-h-11 items-center")} data-testid="decision-evidence-item" data-ref={link.ref}>
      <Paperclip className="mt-0.5 size-3.5 shrink-0 text-fg-subtle" aria-hidden="true" />
      {link.href === null ? (
        <span className="min-w-0 text-muted-foreground [overflow-wrap:anywhere]" title={link.ref}>
          {label}
        </span>
      ) : link.external ? (
        <a href={link.href} target="_blank" rel="noopener noreferrer" className={cn(LINK, "inline-flex min-w-0 items-center gap-1")} title={link.ref} data-testid="decision-evidence-link">
          {label}
          <ExternalLink className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="sr-only">{t("newTab")}</span>
        </a>
      ) : (
        <Link href={link.href as Route} className={LINK} title={link.ref} data-testid="decision-evidence-link">
          {label}
        </Link>
      )}
    </li>
  );
}

/**
 * What a question rests on, under its context on the DecisionCard and on the phone's decision screen: what a credential
 * request asks for, why the agent recommends its option, whether the act it asks about can be undone and how, and its evidence, each ref a link to the
 * hub's page of it (a run, a run's files, a problem's evidence, a memory, a file in the knowledge graph) or to its
 * https page in a new tab. A question that says none of them shows nothing.
 */
export function DecisionGrounds({ decision, screen = false }: { decision: Decision; screen?: boolean }) {
  const t = useTranslations("inbox.grounds");
  const ids = useId();
  const reason = decision.recommended_reason ?? null;
  const pick = reason ? (decision.options.find((option) => option.key === decision.recommended)?.label ?? null) : null;
  const reversible = decision.reversible ?? null;
  const evidence = decision.evidence ?? [];
  const asks = credentialRequest(decision) !== null;
  if (!asks && !reason && reversible === null && evidence.length === 0) return null;
  const text = screen ? "text-[15px] leading-[22px]" : "text-[13px] leading-[19px]";
  return (
    <section aria-label={t("label")} className={cn("flex min-w-0 flex-col gap-3 rounded-md border bg-surface-sunken p-3", screen && "p-3.5")} data-testid="decision-grounds">
      <CredentialNeeds decision={decision} text={text} />
      {reason ? (
        <div className="flex min-w-0 items-start gap-2.5" data-testid="decision-reason">
          <Lightbulb className="mt-0.5 size-4 shrink-0 text-brand" aria-hidden="true" />
          <p className={cn("min-w-0 text-pretty text-foreground [overflow-wrap:anywhere]", text)}>
            <span className="font-medium">{pick ? t("why", { option: pick }) : t("whyPick")}</span> <span className="text-muted-foreground">{reason}</span>
          </p>
        </div>
      ) : null}
      {reversible === true ? (
        <div className="flex min-w-0 items-start gap-2.5" data-testid="decision-undo">
          <Undo2 className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <p className={cn("min-w-0 text-pretty text-foreground [overflow-wrap:anywhere]", text)}>
            <span className="font-medium">{t("reversible")}</span> <span className="text-muted-foreground">{decision.undo ?? t("undoUnsaid")}</span>
          </p>
        </div>
      ) : reversible === false ? (
        <div className="flex min-w-0 items-start gap-2.5" data-testid="decision-irreversible">
          <OctagonAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden="true" />
          <p className={cn("min-w-0 font-medium text-pretty text-foreground", text)}>{t("irreversible")}</p>
        </div>
      ) : null}
      {evidence.length > 0 ? (
        <div className="flex min-w-0 flex-col gap-1.5" data-testid="decision-evidence">
          <p id={`${ids}-evidence`} className="text-xs font-medium text-muted-foreground">
            {t("evidence", { count: evidence.length })}
          </p>
          <ul aria-labelledby={`${ids}-evidence`} className={cn("flex min-w-0 flex-col gap-1", text)}>
            {evidence.map((ref, index) => (
              <EvidenceItem key={`${index}-${ref}`} link={evidenceLink(ref, decision)} screen={screen} />
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
