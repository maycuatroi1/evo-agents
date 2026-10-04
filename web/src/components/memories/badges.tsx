"use client";

import { Archive, FileQuestion, Lock, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

import { isMemoryType, labelNames, SHARED_TYPES, TYPE_ICONS } from "./memory-meta";

/** A memory's type as an icon and a word; a type only its owner sees carries a lock and says so. */
export function MemoryTypeBadge({ type, className }: { type: string; className?: string }) {
  const t = useTranslations("memories.types");
  const known = isMemoryType(type);
  const Icon = known ? TYPE_ICONS[type] : FileQuestion;
  const owned = !SHARED_TYPES.has(type);
  return (
    <Badge
      variant={owned ? "secondary" : "outline"}
      className={cn("gap-1", className)}
      data-memory-type={type}
      title={owned ? t("ownerOnly") : undefined}
    >
      <Icon aria-hidden="true" />
      {known ? t(type) : type}
      {owned ? (
        <>
          <Lock aria-hidden="true" className="opacity-70" />
          <span className="sr-only">{t("ownerOnly")}</span>
        </>
      ) : null}
    </Badge>
  );
}

/**
 * A label by its level, and its location when that restricts anything (not the project's first, unrestricted
 * one), with a shield: never colour alone.
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
  const { level, location } = labelNames(label);
  if (!level && !location) return null;
  const text = [level, location === unrestricted ? null : location].filter(Boolean).join(" / ");
  return (
    <Badge variant="outline" className={cn("gap-1 font-mono", className)} data-label-level={level ?? ""}>
      <ShieldCheck aria-hidden="true" />
      <span className="sr-only">{t("label")}: </span>
      {text}
    </Badge>
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
