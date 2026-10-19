"use client";

import {
  AppWindow,
  BookA,
  BookCheck,
  CircleCheck,
  CircleDashed,
  CircleMinus,
  Eye,
  Gauge,
  Heading,
  History,
  type LucideIcon,
  Minus,
  MoveRight,
  PencilLine,
  Plus,
  Route as RouteIcon,
  Scale,
  Signature,
} from "lucide-react";
import type { Route } from "next";
import { useTranslations } from "next-intl";

import { Identifier, Tag } from "@/components/data/identifier";
import { type StatusLook, StatusPill, type StatusPillProps } from "@/components/status/status-badge";

import type { ItemKind, SrsDiffItem, VersionState } from "./api";

/**
 * The SRS pages' pills and tags. Only these pages show a version's state or an item's change, so their looks live here
 * rather than in `STATUS_LOOKS` (web/DESIGN.md, status-badge.tsx), with their words under `srs` in both message files.
 */

/** A version's state: a draft, in review (violet, waiting to be checked), approved (green), superseded by a newer one. */
export const VERSION_LOOKS = {
  draft: { tone: "neutral", icon: CircleDashed },
  in_review: { tone: "review", icon: Eye },
  approved: { tone: "success", icon: CircleCheck },
  superseded: { tone: "outline", icon: CircleMinus },
} as const satisfies Record<VersionState, StatusLook>;

export function VersionBadge({ state, ...props }: StatusPillProps & { state: VersionState }) {
  const t = useTranslations("srs.versionState");
  return <StatusPill look={VERSION_LOOKS[state]} label={t(state)} data-status={state} data-testid="srs-version-state" {...props} />;
}

/** What became of an item between two versions. */
export const CHANGE_LOOKS = {
  changed: { tone: "review", icon: PencilLine },
  added: { tone: "success", icon: Plus },
  removed: { tone: "danger", icon: Minus },
  moved: { tone: "neutral", icon: MoveRight },
} as const satisfies Record<SrsDiffItem["change"], StatusLook>;

export function ChangeBadge({ change, ...props }: StatusPillProps & { change: SrsDiffItem["change"] }) {
  const t = useTranslations("srs.change");
  return <StatusPill look={CHANGE_LOOKS[change]} label={t(change)} data-status={change} data-testid="srs-change" {...props} />;
}

export const KIND_ICONS: Record<ItemKind, LucideIcon> = {
  section: Heading,
  use_case: RouteIcon,
  business_rule: Scale,
  screen: AppWindow,
  nfr: Gauge,
  cbr: BookCheck,
  glossary: BookA,
  approval_page: Signature,
  revision_history: History,
};

/** What an item is: Use case, Business rule, Screen. */
export function KindTag({ kind }: { kind: ItemKind }) {
  const t = useTranslations("srs.itemKind");
  const Icon = KIND_ICONS[kind];
  return (
    <Tag data-kind={kind}>
      <Icon aria-hidden="true" />
      {t(kind)}
    </Tag>
  );
}

/** An item's code in a mono chip, linking to the item's page when given one. */
export function CodeChip({ code, href, testId }: { code: string; href?: Route; testId?: string }) {
  return <Identifier value={code} href={href} testId={testId} data-code={code} />;
}

/** A version as `v8`, in a mono chip, linking where given. */
export function VersionChip({ number, href }: { number: number; href?: Route }) {
  return <Identifier value={`v${number}`} href={href} data-version={number} />;
}
