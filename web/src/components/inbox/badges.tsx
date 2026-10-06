"use client";

import {
  Boxes,
  CircleCheck,
  CircleX,
  DatabaseZap,
  GitMerge,
  type LucideIcon,
  MessageSquare,
  Rocket,
  Send,
  Target,
  ThumbsUp,
  Trash2,
  Upload,
  Wallet,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { Badge } from "@/components/ui/badge";

import type { DecisionCategory, NoticeKind } from "./queries";

type Variant = "success" | "info" | "destructive";

export const CATEGORY_ICON: Record<DecisionCategory, LucideIcon> = {
  deploy: Rocket,
  delete_data: Trash2,
  live_migration: DatabaseZap,
  external_send: Send,
  spend_money: Wallet,
  architecture: Boxes,
  scope: Target,
};

export function DecisionCategoryBadge({ category, className }: { category: DecisionCategory; className?: string }) {
  const t = useTranslations("inbox.category");
  const Icon = CATEGORY_ICON[category];
  return (
    <Tag className={className} data-category={category} data-testid="decision-category">
      <Icon aria-hidden="true" />
      {t(category)}
    </Tag>
  );
}

export const NOTICE_LOOK: Record<NoticeKind, { icon: LucideIcon; variant: Variant }> = {
  push_default_branch: { icon: Upload, variant: "info" },
  merge_default_branch: { icon: GitMerge, variant: "info" },
  plan_finished: { icon: CircleCheck, variant: "success" },
  run_failed: { icon: CircleX, variant: "destructive" },
};

export function NoticeKindBadge({ kind, className }: { kind: NoticeKind; className?: string }) {
  const t = useTranslations("inbox.noticeKind");
  const { icon: Icon, variant } = NOTICE_LOOK[kind];
  return (
    <Badge variant={variant} className={className} data-kind={kind} data-testid="notice-kind">
      <Icon aria-hidden="true" />
      {t(kind)}
    </Badge>
  );
}

/** A decision as a notification: "Decision", with the icon the run pages use for a run that waits for one. */
export function DecisionKindBadge({ className }: { className?: string }) {
  const t = useTranslations("inbox.kind");
  return (
    <Tag className={className} data-kind="decision" data-testid="notification-kind">
      <MessageSquare aria-hidden="true" />
      {t("decision")}
    </Tag>
  );
}

/** The option the agent recommends. */
export function RecommendedBadge({ className }: { className?: string }) {
  const t = useTranslations("inbox.decision");
  return (
    <Badge variant="info" className={className} data-testid="decision-recommended">
      <ThumbsUp aria-hidden="true" />
      {t("recommended")}
    </Badge>
  );
}
