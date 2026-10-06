"use client";

import { Archive, FileQuestion, Lock, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { useVisibilityName } from "@/components/data/visibility";
import { Badge } from "@/components/ui/badge";

import { isMemoryType, labelNames, SHARED_TYPES, TYPE_ICONS } from "./memory-meta";

/**
 * A memory's type as an icon and a word on a tag: a kind, not a state, so square-ish corners (web/DESIGN.md, Shape).
 * A type only its owner sees carries a lock and says so.
 */
export function MemoryTypeBadge({ type, className }: { type: string; className?: string }) {
  const t = useTranslations("memories.types");
  const known = isMemoryType(type);
  const Icon = known ? TYPE_ICONS[type] : FileQuestion;
  const owned = !SHARED_TYPES.has(type);
  return (
    <Tag className={className} data-memory-type={type} title={owned ? t("ownerOnly") : undefined}>
      <Icon aria-hidden="true" />
      {known ? t(type) : type}
      {owned ? (
        <>
          <Lock aria-hidden="true" className="opacity-70" />
          <span className="sr-only">{t("ownerOnly")}</span>
        </>
      ) : null}
    </Tag>
  );
}

/**
 * A label by its visibility, in words (Internal, with the code `internal` in the tooltip), and its location when that
 * restricts anything (not the project's first, unrestricted one), with a shield: never colour alone.
 */
export function LabelBadge({
  label,
  unrestricted,
  className,
}: {
  label: Record<string, unknown>;
  /** The project's first location, which restricts nothing and is left out. */
  unrestricted?: string;
  className?: string;
}) {
  const t = useTranslations("memories.label");
  const visibility = useVisibilityName();
  const { level, location } = labelNames(label);
  if (!level && !location) return null;
  const where = location === unrestricted ? null : location;
  const text = [level ? visibility(level) : null, where].filter(Boolean).join(" / ");
  const codes = [level, where].filter(Boolean).join(" / ");
  return (
    <Tag className={className} title={codes} data-label-level={level ?? ""}>
      <ShieldCheck aria-hidden="true" />
      <span className="sr-only">{t("label")}: </span>
      {text}
    </Tag>
  );
}

export function DeletedBadge() {
  const t = useTranslations("memories");
  return (
    <Badge variant="warning" className="gap-1">
      <Archive aria-hidden="true" />
      {t("deleted")}
    </Badge>
  );
}
