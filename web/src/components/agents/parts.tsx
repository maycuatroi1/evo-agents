"use client";

import { Ban, Info, Star } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { Badge } from "@/components/ui/badge";
import type { Agent } from "@/lib/agents";

/** After an agent's name: Default for the project's own (@<project>), Deleted once it takes no new run. */
export function AgentBadges({ agent }: { agent: Pick<Agent, "is_default" | "deleted_at"> }) {
  const t = useTranslations("agents");
  return (
    <>
      {agent.is_default ? (
        <Tag data-testid="agent-default">
          <Star aria-hidden="true" />
          {t("default")}
        </Tag>
      ) : null}
      {agent.deleted_at ? (
        <Badge variant="secondary" className="shrink-0" data-testid="agent-deleted">
          <Ban aria-hidden="true" />
          {t("deleted")}
        </Badge>
      ) : null}
    </>
  );
}

/** For a writer or reader: they see the agents and dispatch under them, while only an admin changes them. */
export function ReadOnlyNote({ project }: { project: string }) {
  const t = useTranslations("agents");
  return (
    <p
      role="note"
      className="flex items-start gap-2.5 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] text-pretty text-muted-foreground"
      data-testid="agents-read-only"
    >
      <Info className="mt-px size-4 shrink-0" aria-hidden="true" />
      <span>{t("readOnly", { project })}</span>
    </p>
  );
}
