"use client";

import { History, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";

import { When } from "@/components/data/when";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

import type { SettingsLayer } from "./settings-data";

type Props = {
  versions: SettingsLayer[];
  /** What a version sets, in a few words. */
  describe: (layer: SettingsLayer) => string;
  onRollback: (version: number, current: number) => void;
  /** The version a rollback to is being written, if any. */
  pending: number | null;
  testId: string;
};

/**
 * Every version of a setting, the newest first, as the hub keeps them (evo_agents.hub.server.hub_settings): who wrote
 * it and when, what it sets, and the older version a rollback copied. Rolling back to an older version writes it again
 * as the next one, so nothing is lost and the rollback itself can be rolled back.
 */
export function SettingsHistory({ versions, describe, onRollback, pending, testId }: Props) {
  const t = useTranslations("admin.settings.history");
  const current = versions[0]?.version ?? 0;
  return (
    <Card className="min-w-0" data-testid={testId}>
      <CardHeader>
        <CardTitle>
          <h2 className="flex items-center gap-2">
            <History className="size-4 text-muted-foreground" aria-hidden="true" />
            {t("title")}
          </h2>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="min-w-0">
        {versions.length === 0 ? (
          <p className="text-sm text-muted-foreground" data-testid={`${testId}-empty`}>
            {t("empty")}
          </p>
        ) : (
          <ol className="flex flex-col divide-y">
            {versions.map((layer) => (
              <li
                key={layer.version}
                className="flex flex-wrap items-center gap-x-4 gap-y-2 py-2.5 first:pt-0 last:pb-0"
                data-testid={`${testId}-version`}
                data-version={layer.version}
              >
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                    <span className="tabular-nums">{t("version", { version: layer.version })}</span>
                    {layer.version === current ? <Badge variant="info">{t("current")}</Badge> : null}
                    {layer.rolled_back_to ? (
                      <Badge variant="secondary">{t("rolledBack", { version: layer.rolled_back_to })}</Badge>
                    ) : null}
                  </p>
                  <p className="text-[13px] leading-[18px] text-muted-foreground [overflow-wrap:anywhere]">{describe(layer)}</p>
                  <p className="text-xs text-fg-subtle">
                    {t.rich("by", { login: layer.changed_by ?? "", when: () => <When value={layer.changed_at} short /> })}
                  </p>
                </div>
                {layer.version === current ? null : (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    busy={pending === layer.version}
                    aria-disabled={pending !== null || undefined}
                    onClick={() => {
                      if (pending === null) onRollback(layer.version, current);
                    }}
                    data-testid={`${testId}-rollback`}
                  >
                    <RotateCcw aria-hidden="true" />
                    {t("rollback", { version: layer.version })}
                  </Button>
                )}
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}
