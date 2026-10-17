"use client";

import { ListTree } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Plugin } from "@/lib/plugin-queries";

/** The extension points a manifest may contribute to, in the order the pages name them (docs/plugins.md). */
const POINTS = [
  "pages",
  "routes",
  "mcp_tools",
  "jobs",
  "events",
  "channels",
  "tables",
  "agent_packs",
  "agent_types",
  "runtimes",
  "environments",
  "kg_connectors",
] as const;

/** One fact of a description list: its label and its value, side by side from `sm`. */
export function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-[13px] text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-sm [overflow-wrap:anywhere]">{children}</dd>
    </>
  );
}

/** What a plugin adds, a tag per extension point with how many: "2 routes", "1 page". */
export function ContributionTags({ contributes }: { contributes: Record<string, unknown[]> | undefined }) {
  const t = useTranslations("plugins.points");
  const found = POINTS.filter((point) => (contributes?.[point]?.length ?? 0) > 0);
  if (found.length === 0) return <p className="text-sm text-muted-foreground">{t("nothing")}</p>;
  return (
    <ul className="flex flex-wrap gap-1.5" data-testid="plugin-contributes">
      {found.map((point) => (
        <li key={point}>
          <Tag data-point={point}>{t(point, { count: contributes?.[point]?.length ?? 0 })}</Tag>
        </li>
      ))}
    </ul>
  );
}

/** Who installed the plugin and when, when it came to the store, and when the hub turned it off. */
export function DetailCard({ plugin }: { plugin: Plugin }) {
  const t = useTranslations("plugins.detail");
  return (
    <Card className="min-w-0" data-testid="plugin-details">
      <CardHeader>
        <CardTitle>
          <h2 className="flex items-center gap-2">
            <ListTree className="size-4 text-muted-foreground" aria-hidden="true" />
            {t("detailsTitle")}
          </h2>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-[auto_1fr]">
          <Fact label={t("publisher")}>
            <span className="font-mono text-xs">{plugin.publisher}</span>
          </Fact>
          <Fact label={t("installed")}>
            {plugin.installed_version ? (
              <>
                <span className="font-mono text-xs tabular-nums">{plugin.installed_version}</span>
                {plugin.installed_at ? (
                  <span className="block text-xs text-fg-subtle">
                    {t.rich("installedBy", {
                      login: plugin.installed_by ?? t("theImage"),
                      when: () => <When value={plugin.installed_at} short />,
                    })}
                  </span>
                ) : null}
              </>
            ) : (
              t("notInstalled")
            )}
          </Fact>
          <Fact label={t("latestVersion")}>
            <span className="font-mono text-xs tabular-nums">{plugin.latest_version}</span>
          </Fact>
          <Fact label={t("inStoreSince")}>
            <When value={plugin.created_at} short />
          </Fact>
          {plugin.disabled_at ? (
            <Fact label={t("turnedOffAt")}>
              <When value={plugin.disabled_at} short />
            </Fact>
          ) : null}
        </dl>
      </CardContent>
    </Card>
  );
}
