"use client";

import { Laptop } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";

/** "Local session": what tells a member's own Claude Code session from a run of a worker, in lists and on the run page. */
export function LocalRunKindBadge({ className }: { className?: string }) {
  const t = useTranslations("runs.local");
  return (
    <Tag className={className} data-testid="run-kind" data-kind="local">
      <Laptop aria-hidden="true" />
      {t("kind")}
    </Tag>
  );
}

/** The account of a local run as the screens say it: the email, with the claude-profiles profile after it when known. */
export function localAccount(run: { claude_email?: string | null; claude_profile?: string | null }): string {
  const email = run.claude_email ?? "unknown";
  return run.claude_profile ? `${email} (${run.claude_profile})` : email;
}
