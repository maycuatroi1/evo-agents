"use client";

import { ScanEye } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { cn } from "@/lib/utils";

import { Tag } from "./identifier";

/**
 * Visibility is the product word for a label level (web/DESIGN.md, Voice): how far a person's grant reaches in a
 * project's label ladder, or how far a memory reaches. The four levels of the default ladder read as words (Public,
 * Internal, Customer, Secret) with the code the CLI and the API use in the tooltip; a level a project named itself
 * shows as it is written. API fields (`max_level`), URL parameters and form values keep the codes.
 */
export const VISIBILITY_LEVELS = ["public", "internal", "customer", "secret"] as const;
export type KnownVisibility = (typeof VISIBILITY_LEVELS)[number];

export function isKnownVisibility(level: string): level is KnownVisibility {
  return (VISIBILITY_LEVELS as readonly string[]).includes(level);
}

/** The words of a level: "Internal" for `internal`, a level of a project's own ladder as written. */
export function useVisibilityName(): (level: string) => string {
  const t = useTranslations("visibility.levels");
  return useCallback((level: string) => (isKnownVisibility(level) ? t(level) : level), [t]);
}

/** A level in running text or a table cell: its words, the code in the tooltip; "-" when there is none. */
export function VisibilityLevel({
  level,
  className,
  testId,
}: {
  level: string | null | undefined;
  className?: string;
  testId?: string;
}) {
  const name = useVisibilityName();
  if (!level) {
    return (
      <span className={cn("text-muted-foreground", className)} data-testid={testId}>
        -
      </span>
    );
  }
  return (
    <span
      className={className}
      title={isKnownVisibility(level) ? level : undefined}
      data-level={level}
      data-testid={testId}
    >
      {name(level)}
    </span>
  );
}

/** "Visibility: Internal" as a tag beside a page's title: what the visitor's grant lets them see in the project. */
export function VisibilityTag({ level, testId }: { level: string; testId?: string }) {
  const t = useTranslations("visibility");
  const name = useVisibilityName();
  return (
    <Tag title={isKnownVisibility(level) ? level : undefined} data-level={level} data-testid={testId}>
      <ScanEye aria-hidden="true" />
      {t("value", { level: name(level) })}
    </Tag>
  );
}
