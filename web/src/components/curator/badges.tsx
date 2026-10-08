"use client";

import { Glasses, Layers, MoonStar, Shapes } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { cn } from "@/lib/utils";

import { isLens } from "./model";

/**
 * The names a proposal carries, as tags (web/DESIGN.md, Shape: a kind is square-ish, a state is round): its tier, its
 * kind of change and the lens it was seen through. A tier is a fact the hub computed once, not a state that moves.
 */

/** "Tier 2", with what the tier means as its tooltip and, for screen readers, in words. */
export function TierTag({ tier, className }: { tier: number; className?: string }) {
  const t = useTranslations("curator.tier");
  const meaning = tier >= 0 && tier <= 3 ? t(`meaning.${tier as 0 | 1 | 2 | 3}`) : "";
  return (
    <Tag className={cn(tier === 3 && "text-foreground", className)} title={meaning} data-testid="proposal-tier" data-tier={tier}>
      <Layers aria-hidden="true" />
      {t("tag", { tier })}
      {meaning ? <span className="sr-only">: {meaning}</span> : null}
    </Tag>
  );
}

/** The kind of change, in words; a kind the web has no words for shows as the hub wrote it. */
export function useKindText() {
  const t = useTranslations("curator.kinds");
  return (kind: string) => (t.has(kind as never) ? t(kind as never) : kind);
}

export function useLensText() {
  const t = useTranslations("curator.lenses");
  return (lens: string) => (isLens(lens) ? t(lens) : lens);
}

export function KindTag({ kind, className }: { kind: string; className?: string }) {
  const text = useKindText();
  return (
    <Tag className={className} data-testid="proposal-kind" data-kind={kind}>
      <Shapes aria-hidden="true" />
      {text(kind)}
    </Tag>
  );
}

export function LensTag({ lens, className }: { lens: string; className?: string }) {
  const t = useTranslations("curator");
  const text = useLensText();
  return (
    <Tag className={className} data-testid="proposal-lens" data-lens={lens}>
      <Glasses aria-hidden="true" />
      <span className="sr-only">{t("lensWord")} </span>
      {text(lens)}
    </Tag>
  );
}

/** "Review run": what tells the night's review from a run of a plan, on the run page. A kind, so a tag. */
export function ReviewRunKindBadge({ className }: { className?: string }) {
  const t = useTranslations("curator");
  return (
    <Tag className={className} data-testid="run-kind" data-kind="review">
      <MoonStar aria-hidden="true" />
      {t("reviewRun")}
    </Tag>
  );
}
