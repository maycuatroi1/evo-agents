"use client";

import { useQuery } from "@tanstack/react-query";
import { CloudOff, Package, TriangleAlert } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useMemo } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { Identifier } from "@/components/data/identifier";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectQuery } from "@/lib/queries";

import { DownloadBundle } from "./download-bundle";
import { bundleFileName } from "./format";
import {
  blobStoreQuery,
  type BlobStoreState,
  type SkillHistory,
  type SkillPlace,
  skillQuery,
  type SkillVersion,
} from "./queries";
import { ByteSize, HashText, ScopeBadge, SourceLink } from "./skill-bits";
import type { RepoOrigin } from "./source";

const NARROW_HIDDEN = {
  sha256: "hidden sm:table-cell",
  size: "hidden md:table-cell",
  source: "hidden md:table-cell",
  published: "hidden lg:table-cell",
};

export function SkillDetail({
  place,
  name,
  initialError,
}: {
  place: SkillPlace;
  name: string;
  initialError: ApiErrorInfo | null;
}) {
  const t = useTranslations("skills.detail");
  const state = useHubQuery(skillQuery(browserApi, place, name), initialError);
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={t("notFoundTitle", { name })} description={t("notFoundDescription")} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(skill) => <Detail place={place} skill={skill} />}
    </QueryView>
  );
}

function Detail({ place, skill }: { place: SkillPlace; skill: SkillHistory }) {
  const t = useTranslations("skills");
  const { data: blobStore = "unknown" } = useQuery(blobStoreQuery(browserApi));
  const project = useHubQuery({
    ...projectQuery(browserApi, place.kind === "project" ? place.project : ""),
    enabled: place.kind === "project",
  });
  const repos = project.status === "success" ? project.data.repos : [];
  const latest = skill.versions[0];

  return (
    <>
      <PageHeader
        title={skill.name}
        tags={
          <>
            <ScopeBadge project={skill.project} />
            {latest ? <Identifier value={`v${latest.version}`} testId="skill-version" /> : null}
          </>
        }
        sub={latest?.description}
      />
      <BlobStoreNotice state={blobStore} />
      {latest ? (
        <>
          <Latest place={place} skill={skill} latest={latest} blobStore={blobStore} repos={repos} />
          <Versions place={place} skill={skill} blobStore={blobStore} repos={repos} />
        </>
      ) : (
        <p className="text-sm text-muted-foreground">{t("detail.noVersions")}</p>
      )}
    </>
  );
}

function BlobStoreNotice({ state }: { state: BlobStoreState }) {
  const t = useTranslations("skills.blobStore");
  if (state === "unconfigured") {
    return (
      <Alert role="note" data-testid="blob-store-unconfigured">
        <CloudOff aria-hidden="true" />
        <AlertTitle>{t("unconfiguredTitle")}</AlertTitle>
        <AlertDescription>{t("unconfiguredDescription")}</AlertDescription>
      </Alert>
    );
  }
  if (state === "unavailable") {
    return (
      <Alert role="note" data-testid="blob-store-unavailable">
        <TriangleAlert aria-hidden="true" />
        <AlertTitle>{t("unavailableTitle")}</AlertTitle>
        <AlertDescription>{t("unavailableDescription")}</AlertDescription>
      </Alert>
    );
  }
  return null;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 [overflow-wrap:anywhere]">{children}</dd>
    </>
  );
}

function Latest({
  place,
  skill,
  latest,
  blobStore,
  repos,
}: {
  place: SkillPlace;
  skill: SkillHistory;
  latest: SkillVersion;
  blobStore: BlobStoreState;
  repos: readonly RepoOrigin[];
}) {
  const t = useTranslations("skills.detail");
  const format = useFormatter();
  return (
    <Card data-testid="skill-latest">
      <CardHeader>
        <CardTitle>
          <h2 className="flex items-center gap-2">
            <Package className="size-4" aria-hidden="true" />
            {t("latestTitle", { version: latest.version })}
          </h2>
        </CardTitle>
        <CardDescription>{t("latestDescription")}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-x-8 gap-y-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <div className="flex flex-col gap-4">
          <DownloadBundle place={place} name={skill.name} version={latest.version} blobStore={blobStore} />
          <p className="text-xs text-pretty text-muted-foreground">
            {t.rich("verify", {
              code: (chunks) => <code className="rounded bg-muted px-1 font-mono text-foreground">{chunks}</code>,
              file: bundleFileName(skill.name, latest.version),
            })}
          </p>
        </div>
        <dl className="grid grid-cols-1 gap-x-4 gap-y-2.5 text-sm sm:grid-cols-[auto_1fr]">
          <Field label={t("file")}>
            <span className="font-mono text-xs">{bundleFileName(skill.name, latest.version)}</span>
          </Field>
          <Field label="SHA-256">
            <HashText sha256={latest.sha256} full />
          </Field>
          <Field label={t("size")}>
            <ByteSize bytes={latest.size} />
          </Field>
          <Field label={t("source")}>
            <SourceLink repo={latest.source_repo} commit={latest.source_commit} repos={repos} />
          </Field>
          <Field label={t("published")}>
            <time dateTime={latest.published_at} className="tabular-nums">
              {format.dateTime(new Date(latest.published_at), { dateStyle: "medium", timeStyle: "short" })}
            </time>
            <span className="block text-xs text-muted-foreground">{t("by", { login: latest.published_by })}</span>
          </Field>
          <Field label={t("created")}>
            <time dateTime={skill.created_at} className="tabular-nums">
              {format.dateTime(new Date(skill.created_at), { dateStyle: "medium" })}
            </time>
          </Field>
        </dl>
      </CardContent>
    </Card>
  );
}

function Versions({
  place,
  skill,
  blobStore,
  repos,
}: {
  place: SkillPlace;
  skill: SkillHistory;
  blobStore: BlobStoreState;
  repos: readonly RepoOrigin[];
}) {
  const t = useTranslations("skills");
  const format = useFormatter();
  const columns = useMemo(() => {
    const helper = dataTableColumns<SkillVersion>();
    return helper.columns([
      helper.accessor("version", {
        header: () => t("columns.version"),
        sortFn: "basic",
        cell: (info) => (
          <div className="flex flex-col gap-1">
            <span className="flex items-center gap-2">
              <span className="font-mono text-xs font-medium tabular-nums">v{info.getValue()}</span>
              {info.getValue() === skill.versions[0]?.version ? (
                <Badge variant="secondary">{t("detail.latest")}</Badge>
              ) : null}
            </span>
            {/* The SHA-256 column a phone hides, under the version instead. */}
            <span className="sm:hidden">
              <HashText sha256={info.row.original.sha256} />
            </span>
          </div>
        ),
      }),
      helper.accessor("sha256", {
        header: () => "SHA-256",
        enableSorting: false,
        cell: (info) => <HashText sha256={info.getValue()} />,
      }),
      helper.accessor("size", {
        header: () => t("columns.size"),
        sortFn: "basic",
        cell: (info) => <ByteSize bytes={info.getValue()} />,
      }),
      helper.accessor((row) => row.source_repo ?? "", {
        id: "source",
        header: () => t("columns.source"),
        enableSorting: false,
        cell: (info) => (
          <SourceLink repo={info.row.original.source_repo} commit={info.row.original.source_commit} repos={repos} />
        ),
      }),
      helper.accessor((row) => new Date(row.published_at), {
        id: "published",
        header: () => t("columns.published"),
        sortFn: "datetime",
        cell: (info) => (
          <div className="flex flex-col text-xs">
            <time dateTime={info.row.original.published_at} className="tabular-nums">
              {format.dateTime(info.getValue(), { dateStyle: "medium", timeStyle: "short" })}
            </time>
            <span className="text-muted-foreground">{t("by", { login: info.row.original.published_by })}</span>
          </div>
        ),
      }),
      helper.display({
        id: "download",
        header: () => <span className="sr-only">{t("columns.download")}</span>,
        cell: (info) => (
          <DownloadBundle
            place={place}
            name={skill.name}
            version={info.row.original.version}
            blobStore={blobStore}
            variant="outline"
            compact
          />
        ),
      }),
    ]);
  }, [t, format, place, skill, blobStore, repos]);

  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>
          <h2>{t("detail.versionsTitle")}</h2>
        </CardTitle>
        <CardDescription>{t("detail.versionsDescription", { count: skill.versions.length })}</CardDescription>
      </CardHeader>
      <CardContent>
        <DataTable
          data={skill.versions}
          columns={columns}
          caption={t("detail.versionsTitle")}
          getRowId={(row) => String(row.version)}
          initialSorting={[{ id: "version", desc: true }]}
          columnClassNames={NARROW_HIDDEN}
          testId="skill-versions"
        />
      </CardContent>
    </Card>
  );
}
