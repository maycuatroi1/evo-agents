"use client";

import { Check, Info, Lock } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { VisibilityLevel, VisibilityTag } from "@/components/data/visibility";
import { PageHeader } from "@/components/shell/page-header";
import { RoleBadge } from "@/components/shell/role-badge";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

function labelText(label: Record<string, unknown>): string {
  return ["level", "location", "integrity"]
    .map((key) => label[key])
    .filter((value): value is string => typeof value === "string" && value.length > 0)
    .join(" / ");
}

function Ladder({ project }: { project: Project }) {
  const t = useTranslations("project.ladder");
  const reach = project.max_level ? project.levels.indexOf(project.max_level) : -1;
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <ol className="flex flex-col gap-1.5" data-testid="label-ladder">
          {project.levels.map((level, index) => {
            const visible = index <= reach;
            return (
              <li
                key={level}
                className={cn(
                  "flex items-center justify-between gap-3 rounded-md border px-3 py-2",
                  visible ? "border-brand/30 bg-surface-selected text-foreground" : "bg-muted/40 text-muted-foreground",
                )}
              >
                <span className="flex items-center gap-2 text-sm font-medium">
                  <span className="w-4 text-right text-xs tabular-nums opacity-70">{index + 1}</span>
                  <VisibilityLevel level={level} />
                </span>
                <span className="flex items-center gap-1.5 text-xs">
                  {visible ? <Check className="size-3.5" aria-hidden="true" /> : <Lock className="size-3.5" aria-hidden="true" />}
                  {visible ? t("visible") : t("hidden")}
                </span>
              </li>
            );
          })}
        </ol>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
          <dt className="text-muted-foreground">{t("default")}</dt>
          <dd className="font-mono text-xs">{labelText(project.default_label) || "-"}</dd>
          <dt className="text-muted-foreground">{t("locations")}</dt>
          <dd className="flex flex-wrap gap-1">
            {project.locations.map((location) => (
              <Badge key={location} variant="outline" className="font-mono">
                {location}
              </Badge>
            ))}
          </dd>
        </dl>
      </CardContent>
    </Card>
  );
}

function SimpleTable({
  caption,
  headers,
  rows,
  empty,
}: {
  caption: string;
  headers: string[];
  rows: ReactNode[][];
  empty: string;
}) {
  if (rows.length === 0) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <div className="overflow-hidden rounded-md border">
      <Table scrollLabel={caption}>
        <TableCaption className="sr-only">{caption}</TableCaption>
        <TableHeader className="bg-muted/50">
          <TableRow className="hover:bg-transparent">
            {headers.map((header) => (
              <TableHead key={header} className="h-9 px-3 text-xs text-muted-foreground">
                {header}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((cells, index) => (
            <TableRow key={index}>
              {cells.map((cell, column) => (
                <TableCell key={column} className="px-3 py-2 align-top whitespace-normal">
                  {cell}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function Mono({ children }: { children: ReactNode }) {
  return <span className="font-mono text-xs [overflow-wrap:anywhere]">{children}</span>;
}

function Repos({ project }: { project: Project }) {
  const t = useTranslations("project.repos");
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent>
        <SimpleTable
          caption={t("title")}
          headers={[t("name"), t("branch"), t("origin"), t("path")]}
          empty={t("empty")}
          rows={project.repos.map((repo) => [
            <span key="name" className="font-medium">
              {repo.name}
            </span>,
            <Mono key="branch">{repo.default_branch ?? "-"}</Mono>,
            <Mono key="origin">{repo.origin ?? "-"}</Mono>,
            <Mono key="path">{repo.path ?? "-"}</Mono>,
          ])}
        />
      </CardContent>
    </Card>
  );
}

function Sinks({ project }: { project: Project }) {
  const t = useTranslations("project.sinks");
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent>
        <SimpleTable
          caption={t("title")}
          headers={[t("id"), t("kind"), t("clearance")]}
          empty={t("empty")}
          rows={project.sinks.map((sink) => [
            <span key="id" className="font-medium">
              {sink.id}
            </span>,
            <Badge key="kind" variant="outline" className="font-mono">
              {sink.kind}
            </Badge>,
            <Mono key="clearance">
              {[sink.clearance.level, sink.clearance.location].filter(Boolean).join(" / ")}
            </Mono>,
          ])}
        />
      </CardContent>
    </Card>
  );
}

function Details({ project }: { project: Project }) {
  const t = useTranslations("project");
  const format = useFormatter();
  const when = (iso: string) => (
    <time dateTime={iso} className="tabular-nums">
      {format.dateTime(new Date(iso), { dateStyle: "medium", timeStyle: "short" })}
    </time>
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>{t("details.title")}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-1 gap-x-4 gap-y-3 text-sm sm:grid-cols-[auto_1fr]">
          <dt className="text-muted-foreground">{t("harness")}</dt>
          <dd>{project.harness ? <Mono>{project.harness.name}</Mono> : t("noHarness")}</dd>
          {project.harness ? (
            <>
              <dt className="text-muted-foreground">{t("details.workspace")}</dt>
              <dd>
                <Mono>{project.harness.workspace}</Mono>
              </dd>
              <dt className="text-muted-foreground">{t("details.path")}</dt>
              <dd>
                <Mono>{project.harness.path}</Mono>
              </dd>
            </>
          ) : null}
          <dt className="text-muted-foreground">{t("details.created")}</dt>
          <dd>{when(project.created_at)}</dd>
          <dt className="text-muted-foreground">{t("details.updated")}</dt>
          <dd>{when(project.updated_at)}</dd>
        </dl>
      </CardContent>
    </Card>
  );
}

function Overview({ project }: { project: Project }) {
  const t = useTranslations("project");
  return (
    <>
      <PageHeader
        title={project.name}
        tags={
          <>
            <RoleBadge role={project.role} />
            {project.max_level ? <VisibilityTag level={project.max_level} testId="project-visibility" /> : null}
          </>
        }
      />
      {project.role === null ? (
        <Alert>
          <Info aria-hidden="true" />
          <AlertDescription>{t("adminWithoutGrant")}</AlertDescription>
        </Alert>
      ) : null}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <div className="flex flex-col gap-6">
          <Ladder project={project} />
          <Details project={project} />
        </div>
        <div className="flex min-w-0 flex-col gap-6">
          <Repos project={project} />
          <Sinks project={project} />
        </div>
      </div>
    </>
  );
}

export function ProjectOverview({
  name,
  initialError,
  invalid = false,
}: {
  name: string;
  initialError: ApiErrorInfo | null;
  invalid?: boolean;
}) {
  const t = useTranslations("states");
  const state = useHubQuery({ ...projectQuery(browserApi, name), enabled: !invalid }, initialError);
  const notFound = (
    <NotFoundState title={t("projectNotFoundTitle", { name })} description={t("projectNotFoundDescription")} />
  );
  if (invalid) return notFound;
  if (state.status === "error" && state.error.status === 404) return notFound;
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(project) => <Overview project={project} />}
    </QueryView>
  );
}
