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
  Trash2,
  Upload,
  Wallet,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { cn } from "@/lib/utils";

import type { DecisionCategory, NoticeKind } from "./queries";

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

export const NOTICE_LOOK: Record<NoticeKind, { icon: LucideIcon }> = {
  push_default_branch: { icon: Upload },
  merge_default_branch: { icon: GitMerge },
  plan_finished: { icon: CircleCheck },
  run_failed: { icon: CircleX },
};

/** What a notice is about, as the kit's tag: a kind is a name with square corners, never a round state pill. */
export function NoticeKindBadge({ kind, className }: { kind: NoticeKind; className?: string }) {
  const t = useTranslations("inbox.noticeKind");
  const { icon: Icon } = NOTICE_LOOK[kind];
  return (
    <Tag className={className} data-kind={kind} data-testid="notice-kind">
      <Icon aria-hidden="true" />
      {t(kind)}
    </Tag>
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

/** The option the agent recommends, as the kit's brand tag "Agent's pick". */
export function AgentPickTag({ className }: { className?: string }) {
  const t = useTranslations("inbox.decision");
  return (
    <Tag className={cn("bg-brand-soft text-brand", className)} data-testid="decision-recommended">
      {t("agentsPick")}
    </Tag>
  );
}
