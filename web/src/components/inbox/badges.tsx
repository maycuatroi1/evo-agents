"use client";

import {
  Ban,
  Boxes,
  CircleCheck,
  CircleDot,
  CircleX,
  DatabaseZap,
  GitMerge,
  type LucideIcon,
  MessageCircleQuestionMark,
  Rocket,
  Send,
  Target,
  ThumbsUp,
  TimerOff,
  Trash2,
  Upload,
  Wallet,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";

import type { DecisionCategory, DecisionState, NoticeKind } from "./queries";

type Variant = "success" | "info" | "warning" | "destructive" | "secondary" | "outline";

/** Each look pairs an icon with a word: colour is never the only signal. */
export const DECISION_STATE_LOOK: Record<DecisionState, { icon: LucideIcon; variant: Variant }> = {
  open: { icon: CircleDot, variant: "warning" },
  answered: { icon: CircleCheck, variant: "success" },
  expired: { icon: TimerOff, variant: "secondary" },
  cancelled: { icon: Ban, variant: "secondary" },
};

export function DecisionStateBadge({ state, className }: { state: DecisionState; className?: string }) {
  const t = useTranslations("inbox.decisionState");
  const { icon: Icon, variant } = DECISION_STATE_LOOK[state];
  return (
    <Badge variant={variant} className={className} data-state={state} data-testid="decision-state">
      <Icon aria-hidden="true" />
      {t(state)}
    </Badge>
  );
}

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
    <Badge variant="outline" className={className} data-category={category} data-testid="decision-category">
      <Icon aria-hidden="true" />
      {t(category)}
    </Badge>
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
    <Badge variant="outline" className={className} data-kind="decision" data-testid="notification-kind">
      <MessageCircleQuestionMark aria-hidden="true" />
      {t("decision")}
    </Badge>
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
