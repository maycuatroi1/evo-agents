"use client";

import { History, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { When } from "@/components/data/when";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ListSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import { type PluginConfigLayer } from "@/lib/plugin-queries";

import { layerVersionsQuery } from "./queries";
import { type LayerTarget, useLayerRollback, usePluginFailure } from "./data";

/** What a version of a layer sets, in a few words: its fields, a secret by name only. */
function useDescribe() {
  const t = useTranslations("plugins.config.history");
  return (layer: PluginConfigLayer) => {
    const names = [...Object.keys(layer.values), ...layer.secrets];
    return names.length ? t("sets", { fields: names.join(", ") }) : t("setsNothing");
  };
}

/**
 * Every version of one stored layer of a plugin's config, the newest first, as the hub keeps them: who wrote it and
 * when, which fields it sets (never a secret's value), and the older version a rollback copied. Rolling back writes an
 * older version again as the next one, with the version the layer is at, so a change made meanwhile is refused.
 */
export function ConfigHistory({ target, canWrite, testId }: { target: LayerTarget; canWrite: boolean; testId: string }) {
  const t = useTranslations("plugins.config.history");
  const describe = useDescribe();
  const versions = useHubQuery(layerVersionsQuery(browserApi, target.plugin, target.project));
  const rollback = useLayerRollback(target);
  const failure = usePluginFailure();
  const [pending, setPending] = useState<number | null>(null);

  const roll = async (version: number, current: number) => {
    setPending(version);
    try {
      await rollback.mutateAsync({ version, if_version: current });
      notify({ tone: "success", text: t("rolledBackTo", { version }) });
    } catch (error) {
      notifyFailure(t("rollbackFailed", { version }), failure(error));
    } finally {
      setPending(null);
    }
  };

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
        <QueryView state={versions} loading={<ListSkeleton rows={2} />}>
          {(list) => {
            const current = list[0]?.version ?? 0;
            if (list.length === 0) return <p className="text-sm text-muted-foreground">{t("empty")}</p>;
            return (
              <ol className="flex flex-col divide-y">
                {list.map((layer) => (
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
                    {canWrite && layer.version !== current ? (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        busy={pending === layer.version}
                        aria-disabled={pending !== null || undefined}
                        onClick={() => {
                          if (pending === null) void roll(layer.version, current);
                        }}
                        data-testid={`${testId}-rollback`}
                      >
                        <RotateCcw aria-hidden="true" />
                        {t("rollback", { version: layer.version })}
                      </Button>
                    ) : null}
                  </li>
                ))}
              </ol>
            );
          }}
        </QueryView>
      </CardContent>
    </Card>
  );
}
