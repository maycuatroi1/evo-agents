"use client";

import { Activity, Blocks, Info, SlidersHorizontal } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { Identifier } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ListSkeleton } from "@/components/states/states";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { type PluginDetail } from "@/lib/plugin-queries";

import { backendQuery, instanceConfigQuery, pluginQuery } from "./queries";
import { ConfigForm } from "./config-form";
import { ConfigHistory } from "./config-history";
import { fieldsOf } from "./config-model";
import { ContributionTags, DetailCard, Fact } from "./detail-parts";
import { PermissionList, StoreStateBadge, TierTag } from "./parts";
import { isFromImage, StoreActions } from "./store-actions";
import { VersionsTable } from "./versions-table";

function CardHead({ icon: Icon, title, description }: { icon: typeof Info; title: string; description?: string }) {
  return (
    <CardHeader>
      <CardTitle>
        <h2 className="flex items-center gap-2">
          <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
          {title}
        </h2>
      </CardTitle>
      {description ? <CardDescription>{description}</CardDescription> : null}
    </CardHeader>
  );
}

const BACKEND_STATES = ["placing", "starting", "running", "backoff"] as const; // evo_agents.plugins.host.STATES
type BackendState = (typeof BACKEND_STATES)[number];

/** The backend of an isolated plugin as the plugin host last reported it (docs/plugins.md, Plugin host). */
function BackendCard({ name }: { name: string }) {
  const t = useTranslations("plugins.backend");
  const state = useHubQuery(backendQuery(browserApi, name));
  const backendWord = (word: string | null) =>
    word === null ? t("notReported") : BACKEND_STATES.includes(word as BackendState) ? t(`states.${word as BackendState}`) : word;
  return (
    <Card className="min-w-0" data-testid="plugin-backend">
      <CardHead icon={Activity} title={t("title")} description={t("description")} />
      <CardContent>
        <QueryView state={state} loading={<ListSkeleton rows={2} />}>
          {(status) => (
            <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[auto_1fr]">
              <Fact label={t("state")}>
                <span data-testid="plugin-backend-state">{backendWord(status.state)}</span>
              </Fact>
              {status.restarts ? <Fact label={t("restarts")}>{status.restarts}</Fact> : null}
              {status.isolation ? <Fact label={t("isolation")}>{status.isolation}</Fact> : null}
              {status.started_at ? (
                <Fact label={t("startedAt")}>
                  <When value={status.started_at} short />
                </Fact>
              ) : null}
              {status.retry_at ? (
                <Fact label={t("retryAt")}>
                  <When value={status.retry_at} short />
                </Fact>
              ) : null}
              {status.error ? (
                <Fact label={t("error")}>
                  <span className="font-mono text-xs break-words text-danger">{status.error}</span>
                </Fact>
              ) : null}
            </dl>
          )}
        </QueryView>
      </CardContent>
    </Card>
  );
}

/** The hub's own layer of the config, below every project's: its form and its versions. */
function InstanceConfig({ plugin }: { plugin: PluginDetail }) {
  const t = useTranslations("plugins.detail");
  const fields = useMemo(() => fieldsOf(plugin.manifest.config as never), [plugin.manifest.config]);
  const layer = useHubQuery(instanceConfigQuery(browserApi, plugin.name));
  const target = { plugin: plugin.name, project: null };
  return (
    <>
      <Card className="min-w-0" data-testid="plugin-instance-config">
        <CardHead icon={SlidersHorizontal} title={t("configTitle")} description={t("configDescription")} />
        <CardContent className="min-w-0">
          <QueryView state={layer} loading={<ListSkeleton rows={fields.length || 1} />}>
            {(held) => <ConfigForm key={held.version} target={target} fields={fields} layer={held} canWrite testId="instance-config-form" />}
          </QueryView>
        </CardContent>
      </Card>
      <ConfigHistory target={target} canWrite testId="instance-config-history" />
    </>
  );
}

function DetailBody({ plugin }: { plugin: PluginDetail }) {
  const t = useTranslations("plugins.detail");
  const installed = plugin.installed_version !== null;
  const shown = plugin.versions.find((version) => version.version === (plugin.installed_version ?? plugin.latest_version));
  const fromImage = isFromImage(plugin, plugin.versions);
  return (
    <div className="flex flex-col gap-6" data-testid="admin-plugin">
      <PageHeader
        title={plugin.title}
        status={<StoreStateBadge state={plugin.state} size="lg" />}
        tags={
          <>
            <TierTag tier={plugin.tier} />
            <Identifier value={plugin.name} />
          </>
        }
        actions={<StoreActions plugin={plugin} fromImage={fromImage} />}
        sub={
          plugin.disabled_reason ? (
            <span className="text-danger" data-testid="plugin-disabled-reason">
              {t("turnedOff", { reason: plugin.disabled_reason })}
            </span>
          ) : (
            t("publishedBy", { publisher: plugin.publisher })
          )
        }
      />
      <div className="grid min-w-0 gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="flex min-w-0 flex-col gap-6">
          <Card className="min-w-0" data-testid="plugin-about">
            <CardHead icon={Info} title={t("aboutTitle")} />
            <CardContent className="flex min-w-0 flex-col gap-5">
              {plugin.description ? <p className="text-sm text-pretty">{plugin.description}</p> : null}
              <section className="flex flex-col gap-2" aria-labelledby="plugin-asks">
                <h3 id="plugin-asks" className="text-[13px] font-medium text-muted-foreground">
                  {t("asksFor")}
                </h3>
                <PermissionList permissions={shown?.permissions ?? []} testId="plugin-permissions" />
              </section>
              <section className="flex flex-col gap-2" aria-labelledby="plugin-adds">
                <h3 id="plugin-adds" className="flex items-center gap-1.5 text-[13px] font-medium text-muted-foreground">
                  <Blocks className="size-3.5" aria-hidden="true" />
                  {t("contributes")}
                </h3>
                <ContributionTags contributes={plugin.manifest.contributes as Record<string, unknown[]> | undefined} />
              </section>
            </CardContent>
          </Card>
          <VersionsTable plugin={plugin} fromImage={fromImage} />
          {installed && plugin.manifest.config ? <InstanceConfig plugin={plugin} /> : null}
        </div>
        <div className="flex min-w-0 flex-col gap-6">
          <DetailCard plugin={plugin} />
          {installed && plugin.tier === "isolated" ? <BackendCard name={plugin.name} /> : null}
        </div>
      </div>
    </div>
  );
}

/**
 * /admin/plugins/{name}, for hub admins (web/DESIGN.md, Plugins): the plugin's state, tier and id with Install,
 * Upgrade and Remove; what it asks for and adds; every version, newest first, with whether it runs on this hub and who
 * signed it; the hub's own layer of its config and its versions; and, for an isolated plugin, its backend.
 */
export function AdminPluginPage({ name, initialError }: { name: string; initialError: ApiErrorInfo | null }) {
  const state = useHubQuery(pluginQuery(browserApi, name), initialError);
  return (
    <QueryView state={state} loading={<ListSkeleton rows={6} />}>
      {(plugin) => <DetailBody plugin={plugin} />}
    </QueryView>
  );
}
