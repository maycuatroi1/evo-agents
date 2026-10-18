"use client";

import { Package } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { Agent } from "@/lib/agents";

/**
 * The agent packs an agent attaches, read only: each pack as `<plugin>/<name>` with the version of its plugin the
 * agent pins. The default agent attaches none and shows no card.
 */
export function AgentPacks({ agent }: { agent: Agent }) {
  const t = useTranslations("agents.detail.packs");
  if (agent.is_default) return null;
  const packs = agent.packs ?? [];
  return (
    <Card data-testid="agent-packs">
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
        <CardDescription>{t("hint")}</CardDescription>
      </CardHeader>
      <CardContent>
        {packs.length === 0 ? (
          <p className="text-sm text-muted-foreground" data-testid="agent-packs-none">
            {t("none")}
          </p>
        ) : (
          <ul className="flex min-w-0 flex-col gap-2" aria-label={t("title")}>
            {packs.map((pack) => (
              <li key={pack.pack} className="flex min-w-0 items-center gap-2" data-value={pack.pack} data-version={pack.version}>
                <Package className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="min-w-0 font-mono text-[13px] [overflow-wrap:anywhere]">{pack.pack}</span>
                <Tag>{t("version", { version: pack.version })}</Tag>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
