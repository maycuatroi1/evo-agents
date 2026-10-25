"use client";

import {
  AtSign,
  CalendarClock,
  Boxes,
  CircleCheck,
  CirclePause,
  CircleX,
  ClipboardCheck,
  ClipboardList,
  DatabaseZap,
  FlaskConical,
  GitMerge,
  Goal,
  Hammer,
  Hourglass,
  Images,
  IterationCw,
  LogIn,
  KeyRound,
  type LucideIcon,
  MessageCircleQuestionMark,
  MessageSquare,
  Puzzle,
  OctagonPause,
  PowerOff,
  Rocket,
  Send,
  ShieldAlert,
  ShieldCheck,
  ShieldQuestionMark,
  Store,
  Sunrise,
  Tags,
  Target,
  Trash2,
  TriangleAlert,
  Unplug,
  Upload,
  UserPlus,
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
  // The categories of the classes of autonomy K5, K8 and K12 at ask: a merge, a credential, a grant or a policy.
  merge: GitMerge,
  credential: KeyRound,
  access: ShieldCheck,
  // What the agent of a local Claude Code session asks that is none of the above.
  question: MessageCircleQuestionMark,
  // What the agent of a chat run asks while it triages a failed run.
  triage: MessageSquare,
  // What a stopped release cycle asks its owner: go on with one more cap, close it, or pause the schedule.
  release: Tags,
  // What the Loop asks a person after a work item failed a third time: retry it, drop it, or take it over.
  loop: IterationCw,
  // What the agent of a run with a browser asks when a page wants a sign-in: Done, or Skip.
  login: LogIn,
  // What the hub asks the principal of a run before it merges, deploys or releases for it (K5, K6 at ask).
  autonomy: ShieldQuestionMark,
  // What the agent of a plan run opens to wait for a time or an event of the hub, which the hub answers itself.
  wait: Hourglass,
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
  curator_brief: { icon: Sunrise },
  curator_paused: { icon: CirclePause },
  author_waiting: { icon: MessageCircleQuestionMark },
  chat_mention: { icon: AtSign },
  chat_added: { icon: UserPlus },
  plugin_disabled: { icon: Unplug },
  builder_proposal: { icon: Hammer },
  canvas_baseline: { icon: ClipboardCheck },
  merge_paused: { icon: PowerOff },
  release: { icon: Tags },
  pm_daily: { icon: ClipboardList },
  pm_escalation: { icon: TriangleAlert },
  run_outputs: { icon: Images },
  loop_paused: { icon: OctagonPause },
  goal_review: { icon: Goal },
  loop_brief: { icon: IterationCw },
  plugin_notice: { icon: Puzzle },
  browser_needs_login: { icon: LogIn },
  autonomy: { icon: ShieldCheck },
  worker_doctor: { icon: ShieldAlert },
  curator_probe_report: { icon: FlaskConical },
  opportunity_window: { icon: CalendarClock },
  market_install_request: { icon: Store },
};

/** Whether the web has a name and an icon for a kind of notice; a core plugin's own kind may have none. */
export function isNoticeKind(kind: string): kind is NoticeKind {
  return Object.hasOwn(NOTICE_LOOK, kind);
}

/** What a notice is about, as the kit's tag: a kind is a name with square corners, never a round state pill. */
export function NoticeKindBadge({ kind, className }: { kind: string; className?: string }) {
  const t = useTranslations("inbox.noticeKind");
  const Icon = isNoticeKind(kind) ? NOTICE_LOOK[kind].icon : Puzzle;
  return (
    <Tag className={className} data-kind={kind} data-testid="notice-kind">
      <Icon aria-hidden="true" />
      {isNoticeKind(kind) ? t(kind) : kind}
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
/** An option that says not yet: choosing it keeps the decision open, and the agent hears nothing. */
export function WaitsTag({ className }: { className?: string }) {
  const t = useTranslations("inbox.decision");
  return (
    <Tag className={cn("bg-surface-sunken text-muted-foreground", className)} data-testid="decision-option-waits">
      {t("keepsOpen")}
    </Tag>
  );
}

export function AgentPickTag({ className }: { className?: string }) {
  const t = useTranslations("inbox.decision");
  return (
    <Tag className={cn("bg-brand-soft text-brand", className)} data-testid="decision-recommended">
      {t("agentsPick")}
    </Tag>
  );
}
