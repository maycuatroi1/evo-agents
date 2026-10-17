"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ListSkeleton } from "@/components/states/states";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { FEATURE_IDS, type FeatureId, featuresQuery } from "@/lib/hub-settings";
import { projectsQuery } from "@/lib/queries";

import { FeatureLayerForm } from "./features-forms";
import { SettingsHistory } from "./settings-history";
import { featureLayerQuery, featureVersionsQuery, rollbackFeatures, type SettingsLayer } from "./settings-data";
import { useSettingsWrite } from "./use-settings-write";

/** The versions of one layer of features, with a rollback to each older one. */
function FeatureHistory({ project }: { project: string | null }) {
  const t = useTranslations("admin.features");
  const tHistory = useTranslations("admin.settings.history");
  const failure = useWriteFailure();
  const versions = useHubQuery(featureVersionsQuery(browserApi, project));
  const rollback = useSettingsWrite(rollbackFeatures);
  const [pending, setPending] = useState<number | null>(null);
  const describe = (layer: SettingsLayer) => {
    const set = Object.entries(layer.values as Record<string, boolean>).map(([id, on]) =>
      t(on ? "setOn" : "setOff", { feature: FEATURE_IDS.includes(id as FeatureId) ? t(`names.${id as FeatureId}`) : id }),
    );
    return set.length ? set.join(", ") : tHistory("setsNothing");
  };
  const roll = async (version: number, current: number) => {
    setPending(version);
    try {
      await rollback.mutateAsync({ project, body: { version, if_version: current } });
      notify({ tone: "success", text: tHistory("rolledBackTo", { version }) });
    } catch (error) {
      notifyFailure(tHistory("rollbackFailed", { version }), failure(error, { 409: t("conflict") }));
    } finally {
      setPending(null);
    }
  };
  return (
    <QueryView state={versions} loading={<ListSkeleton rows={3} />}>
      {(list) => (
        <SettingsHistory
          versions={list}
          describe={describe}
          onRollback={(version, current) => void roll(version, current)}
          pending={pending}
          testId={`features-history-${project ?? "hub"}`}
        />
      )}
    </QueryView>
  );
}

/** One layer: its form once it is read. */
function FeatureLayer({ project, ids }: { project: string | null; ids: readonly FeatureId[] }) {
  const layer = useHubQuery(featureLayerQuery(browserApi, project));
  const { data: hub } = useQuery(featuresQuery(browserApi));
  return (
    <QueryView state={layer} loading={<ListSkeleton rows={ids.length} />}>
      {(held) => <FeatureLayerForm key={`${project}:${held.version}`} layer={held} project={project} ids={ids} hub={hub} />}
    </QueryView>
  );
}

/** A project's layer: the project picked, its form and its versions. */
function ProjectFeatures({ ids }: { ids: readonly FeatureId[] }) {
  const t = useTranslations("admin.features");
  const selectId = useId();
  const { data: projects } = useQuery(projectsQuery(browserApi));
  const names = (projects ?? []).map((project) => project.name).sort();
  const [picked, setPicked] = useState<string | null>(null);
  const project = picked && names.includes(picked) ? picked : (names[0] ?? null);
  return (
    <>
      <Card className="min-w-0" data-testid="features-project">
        <CardHeader>
          <CardTitle>
            <h2>{t("projectTitle")}</h2>
          </CardTitle>
          <CardDescription>{t("projectDescription")}</CardDescription>
        </CardHeader>
        <CardContent className="flex min-w-0 flex-col gap-4">
          {project === null ? (
            <p className="text-sm text-muted-foreground">{t("noProjects")}</p>
          ) : (
            <>
              <div className="flex max-w-sm flex-col gap-1.5">
                <Label htmlFor={selectId}>{t("project")}</Label>
                <NativeSelect
                  id={selectId}
                  value={project}
                  onChange={(event) => setPicked(event.target.value)}
                  className="w-full"
                  data-testid="features-project-select"
                >
                  {names.map((name) => (
                    <NativeSelectOption key={name} value={name}>
                      {name}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
              </div>
              <FeatureLayer key={project} project={project} ids={ids} />
            </>
          )}
        </CardContent>
      </Card>
      {project === null ? null : <FeatureHistory key={project} project={project} />}
    </>
  );
}

/**
 * /admin/features, for hub admins: which features of the hub are on, for the whole hub and for each project (a
 * project's choice over the hub's), and every version of each layer with a rollback. A feature that is off leaves the
 * menus, and its pages and API routes answer 404 for the projects it is off for. Anyone else gets the API's 403.
 */
export function AdminFeaturesPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("admin.features");
  const catalogue = useHubQuery(featuresQuery(browserApi), initialError);
  return (
    <>
      <PageHeader title={t("title")} />
      <QueryView state={catalogue} loading={<ListSkeleton rows={4} />}>
        {(features) => {
          const ids = features.features.map((feature) => feature.id as FeatureId).filter((id) => FEATURE_IDS.includes(id));
          const perProject = features.features.filter((f) => f.layers.includes("project")).map((f) => f.id as FeatureId);
          return (
            <div className="grid min-w-0 gap-6 xl:grid-cols-2">
              <div className="flex min-w-0 flex-col gap-6">
                <Card className="min-w-0" data-testid="features-hub">
                  <CardHeader>
                    <CardTitle>
                      <h2>{t("hubTitle")}</h2>
                    </CardTitle>
                    <CardDescription>{t("hubDescription")}</CardDescription>
                  </CardHeader>
                  <CardContent className="min-w-0">
                    <FeatureLayer project={null} ids={ids} />
                  </CardContent>
                </Card>
                <FeatureHistory project={null} />
              </div>
              <div className="flex min-w-0 flex-col gap-6">
                <ProjectFeatures ids={perProject.filter((id) => FEATURE_IDS.includes(id))} />
              </div>
            </div>
          );
        }}
      </QueryView>
    </>
  );
}
