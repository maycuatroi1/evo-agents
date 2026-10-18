"use client";

import { Info, Layers, ListTree, SlidersHorizontal, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { Identifier, Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ListSkeleton } from "@/components/states/states";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { pendingPermissions, pluginOn } from "@/lib/plugin-pages";
import { type EffectiveConfig, pluginPageHref, type ProjectPlugin, projectPluginQuery } from "@/lib/plugin-queries";

import { effectiveConfigQuery, projectConfigQuery } from "./queries";
import { ConfigForm } from "./config-form";
import { ConfigHistory } from "./config-history";
import { fieldsOf, textOf } from "./config-model";
import { useProjectAdmin } from "./data";
import { Fact } from "./detail-parts";
import { PermissionList, ProjectStateBadge, TierTag } from "./parts";
import { ProjectActions } from "./project-actions";

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

/** What the plugin gets in the project: each field from the highest layer that sets it, and what does not fit. */
function EffectiveCard({ project, plugin }: { project: string; plugin: ProjectPlugin }) {
  const t = useTranslations("plugins.effective");
  const state = useHubQuery(effectiveConfigQuery(browserApi, project, plugin.name));
  const fields = useMemo(() => fieldsOf(plugin.config_schema as never), [plugin.config_schema]);
  const titleOf = (name: string) => fields.find((field) => field.name === name)?.title ?? name;
  const kindOf = (name: string) => fields.find((field) => field.name === name)?.kind ?? "json";
  const layerWord = (field: EffectiveConfig["fields"][number]) =>
    field.version ? t("layerVersion", { layer: t(`layers.${field.layer}`), version: field.version }) : t(`layers.${field.layer}`);
  return (
    <Card className="min-w-0" data-testid="plugin-effective-config">
      <CardHead icon={Layers} title={t("title")} description={t("description")} />
      <CardContent className="flex min-w-0 flex-col gap-4">
        <QueryView state={state} loading={<ListSkeleton rows={2} />}>
          {(effective) => (
            <>
              {effective.fields.length === 0 ? <p className="text-sm text-muted-foreground">{t("empty")}</p> : null}
              <dl className="grid gap-x-6 gap-y-2.5 sm:grid-cols-[minmax(8rem,auto)_1fr]">
                {effective.fields.map((field) => (
                  <div key={field.name} className="contents" data-effective-field={field.name}>
                    <dt className="text-[13px] text-muted-foreground">{titleOf(field.name)}</dt>
                    <dd className="flex min-w-0 flex-wrap items-center gap-2">
                      <span className="font-mono text-[13px] [overflow-wrap:anywhere]">
                        {field.secret ? t("secret") : textOf(kindOf(field.name), field.value)}
                      </span>
                      <Tag data-layer={field.layer}>{layerWord(field)}</Tag>
                    </dd>
                  </div>
                ))}
              </dl>
              {effective.problems.length > 0 ? (
                <ul className="flex flex-col gap-1.5 rounded-md bg-attention-soft px-3 py-2.5 text-[13px] text-attention" data-testid="plugin-config-problems">
                  {effective.problems.map((problem) => (
                    <li key={problem} className="flex items-start gap-2">
                      <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
                      <span className="[overflow-wrap:anywhere]">{problem}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
              <p className="text-xs text-fg-subtle">
                {t("digest")} <code className="font-mono break-all">{effective.digest}</code>
              </p>
            </>
          )}
        </QueryView>
      </CardContent>
    </Card>
  );
}

/** The project's layer of the config: its form for an admin, what it sets for anyone else, and its versions. */
function ProjectConfig({ project, plugin, admin }: { project: string; plugin: ProjectPlugin; admin: boolean }) {
  const t = useTranslations("plugins.project");
  const fields = useMemo(() => fieldsOf(plugin.config_schema as never), [plugin.config_schema]);
  const layer = useHubQuery(projectConfigQuery(browserApi, project, plugin.name));
  const target = { plugin: plugin.name, project };
  return (
    <>
      <Card className="min-w-0" data-testid="plugin-project-config">
        <CardHead icon={SlidersHorizontal} title={t("configTitle")} description={t("configDescription")} />
        <CardContent className="min-w-0">
          <QueryView state={layer} loading={<ListSkeleton rows={fields.length || 1} />}>
            {(held) => (
              <ConfigForm key={held.version} target={target} fields={fields} layer={held} canWrite={admin} testId="project-config-form" />
            )}
          </QueryView>
        </CardContent>
      </Card>
      <EffectiveCard project={project} plugin={plugin} />
      <ConfigHistory target={target} canWrite={admin} testId="project-config-history" />
    </>
  );
}

function DetailBody({ project, plugin }: { project: string; plugin: ProjectPlugin }) {
  const t = useTranslations("plugins.project");
  const admin = useProjectAdmin(project);
  const on = pluginOn(plugin);
  const configurable = Boolean(plugin.config_schema && Object.keys((plugin.config_schema.properties as object) ?? {}).length);
  return (
    <div className="flex flex-col gap-6" data-testid="project-plugin">
      <PageHeader
        title={plugin.title}
        status={<ProjectStateBadge plugin={plugin} size="lg" />}
        tags={
          <>
            <TierTag tier={plugin.tier} />
            <Identifier value={plugin.name} />
          </>
        }
        actions={admin ? <ProjectActions project={project} plugin={plugin} /> : null}
        sub={
          plugin.disabled_reason ? (
            <span className="text-danger">{t("turnedOff", { reason: plugin.disabled_reason })}</span>
          ) : (
            t("versionFrom", { version: plugin.version, publisher: plugin.publisher })
          )
        }
      />
      <div className="grid min-w-0 gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="flex min-w-0 flex-col gap-6">
          <Card className="min-w-0" data-testid="plugin-about">
            <CardHead icon={Info} title={t("aboutTitle")} />
            <CardContent className="flex min-w-0 flex-col gap-5">
              {plugin.description ? <p className="text-sm text-pretty">{plugin.description}</p> : null}
              <section className="flex flex-col gap-2" aria-labelledby="project-plugin-asks">
                <h3 id="project-plugin-asks" className="text-[13px] font-medium text-muted-foreground">
                  {plugin.enabled ? t("granted") : t("asksFor")}
                </h3>
                <PermissionList permissions={plugin.permissions} pending={pendingPermissions(plugin)} testId="plugin-permissions" />
              </section>
              {plugin.pages.length > 0 ? (
                <section className="flex flex-col gap-2" aria-labelledby="project-plugin-pages">
                  <h3 id="project-plugin-pages" className="text-[13px] font-medium text-muted-foreground">
                    {t("pagesTitle")}
                  </h3>
                  <ul className="flex flex-col gap-1.5">
                    {plugin.pages.map((page) => (
                      <li key={page.name} className="text-sm">
                        {on ? (
                          <Link href={pluginPageHref(project, plugin.name, page.name)} className="text-brand hover:text-brand-hover" data-testid={`plugin-page-link-${page.name}`}>
                            {page.title}
                          </Link>
                        ) : (
                          page.title
                        )}
                        {page.description ? <span className="text-muted-foreground">{`: ${page.description}`}</span> : null}
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}
            </CardContent>
          </Card>
          {configurable && on ? <ProjectConfig project={project} plugin={plugin} admin={admin} /> : null}
          {configurable && !on ? <p className="text-sm text-muted-foreground" data-testid="plugin-config-off">{t("configWhenEnabled")}</p> : null}
        </div>
        <Card className="min-w-0 self-start" data-testid="plugin-details">
          <CardHead icon={ListTree} title={t("detailsTitle")} />
          <CardContent>
            <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-[auto_1fr]">
              <Fact label={t("publisher")}>
                <span className="font-mono text-xs">{plugin.publisher}</span>
              </Fact>
              <Fact label={t("versionLabel")}>
                <span className="font-mono text-xs tabular-nums">{plugin.version}</span>
              </Fact>
              {plugin.enabled_at ? (
                <Fact label={t("enabledAt")}>
                  {t.rich("by", { login: plugin.enabled_by ?? "", when: () => <When value={plugin.enabled_at} short /> })}
                </Fact>
              ) : null}
              {plugin.granted_at ? (
                <Fact label={t("grantedAt")}>
                  {t.rich("by", { login: plugin.granted_by ?? "", when: () => <When value={plugin.granted_at} short /> })}
                </Fact>
              ) : null}
            </dl>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

/**
 * A plugin in a project (/p/{project}/plugins/{name}; web/DESIGN.md, Plugins): its state there, what it asks for (and
 * what an upgrade added), the pages it adds, the project's layer of its config with its versions, and the config it
 * gets in the end with the layer each field comes from. An admin of the project enables, disables and configures it;
 * every other member reads.
 */
export function ProjectPluginPage({ project, name, initialError }: { project: string; name: string; initialError: ApiErrorInfo | null }) {
  const state = useHubQuery(projectPluginQuery(browserApi, project, name), initialError);
  return (
    <QueryView state={state} loading={<ListSkeleton rows={6} />}>
      {(plugin) => <DetailBody project={project} plugin={plugin} />}
    </QueryView>
  );
}
