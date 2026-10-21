"use client";

import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { useByteSize } from "@/components/data/output-tile";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { type ApiClient, call, type Project } from "@/lib/api/client";
import { browserApi } from "@/lib/api/browser";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * The project's Run outputs card on its overview (docs/hub.md, Run outputs): whether the machines take the images of
 * tools' results (a browser's screenshot, a Read of an image) as files of the run, a switch an admin of the project
 * flips (PUT /v1/projects/{p}/output-settings) and anyone else reads, and the hub's limits of a file and of a run.
 * Off, the machines send no such image and the hub refuses them; the files an agent sends stay either way.
 */
type OutputSettings = components["schemas"]["OutputSettings"];

const settingsKey = (project: string) => ["projects", project, "output-settings"] as const;

const settingsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: settingsKey(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/output-settings", { params: { path: { project } }, signal })),
  });

async function putToolImages(api: ApiClient, project: string, on: boolean): Promise<OutputSettings> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/output-settings", { params: { path: { project } }, body: { tool_images: on }, headers }));
}

export function OutputSettingsCard({ project }: { project: Project }) {
  const t = useTranslations("project.outputs");
  const id = useId();
  const size = useByteSize();
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const settings = useQuery(settingsQuery(browserApi, project.name));
  const admin = project.role === "admin";
  const change = useMutation({
    mutationFn: (on: boolean) => putToolImages(browserApi(), project.name, on),
    onSuccess: (saved) => {
      queryClient.setQueryData(settingsKey(project.name), saved);
      notify({ tone: "success", text: saved.tool_images ? t("turnedOn") : t("turnedOff") });
    },
    onError: (error) => notifyFailure(t("failed"), failure(error)),
    onSettled: () => queryClient.invalidateQueries({ queryKey: settingsKey(project.name) }),
  });
  const data = settings.data;
  const on = change.isPending ? (change.variables ?? data?.tool_images) : data?.tool_images;
  return (
    <Card data-testid="project-output-settings">
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {settings.isPending ? (
          <div role="status" aria-label={t("title")} className="flex flex-col gap-2">
            <Skeleton className="h-5 w-2/3" />
            <Skeleton className="h-4 w-full" />
          </div>
        ) : settings.isError || !data ? (
          <p className="text-sm text-danger" role="alert">
            {t("loadFailed")}
          </p>
        ) : (
          <>
            <div className="flex items-start justify-between gap-4">
              <div className="flex min-w-0 flex-col gap-0.5">
                <label htmlFor={id} className="text-sm font-medium text-foreground">
                  {t("toolImages")}
                </label>
                <p id={`${id}-hint`} className="text-[13px] leading-[18px] text-muted-foreground">
                  {t("toolImagesHint")} {admin ? null : t("adminOnly")}
                </p>
              </div>
              <Switch
                id={id}
                checked={Boolean(on)}
                disabled={!admin || change.isPending}
                aria-busy={change.isPending || undefined}
                aria-describedby={`${id}-hint`}
                onCheckedChange={(next) => change.mutate(next)}
                className="mt-0.5"
                data-testid="project-tool-images"
              />
            </div>
            <p className="text-xs text-fg-subtle" data-testid="project-output-limits">
              {t("limits", { file: size(data.max_file_bytes), count: data.max_outputs, run: size(data.max_run_bytes) })}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
