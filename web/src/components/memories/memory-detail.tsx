"use client";

import { ArrowLeft, Archive, Code, Eye, History } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, LoadingState, NotFoundState, PageSkeleton } from "@/components/states/states";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { DeletedBadge, LabelBadge, MemoryTypeBadge } from "./badges";
import { SafeMarkdown } from "./markdown";
import { memoriesHref, memoryHref } from "./memory-browser";
import { labelNames, parseFrontmatter, SHARED_TYPES } from "./memory-meta";
import { type Memory, memoryQuery, memoryRevisionQuery, type MemoryScope } from "./queries";
import { RevisionHistory } from "./revision-history";

type DetailProps = {
  scope: MemoryScope;
  id: number;
  /** The revision in the URL (?revision=N), null for the current one. */
  revision: number | null;
  initialError: ApiErrorInfo | null;
  revisionsError: ApiErrorInfo | null;
};

/** A memory belongs to the page it is opened on: a project's under /p/{project}, a personal one under /memories. */
function belongsTo(memory: Memory, scope: MemoryScope): boolean {
  if (scope.kind === "personal") return memory.scope === "personal";
  return memory.scope === "project" && memory.project === scope.project;
}

export function MemoryDetail({ scope, id, revision, initialError, revisionsError }: DetailProps) {
  const t = useTranslations("memories.detail");
  const state = useHubQuery(memoryQuery(browserApi, id), initialError);
  const notFound = <NotFoundState title={t("notFoundTitle")} description={t("notFoundDescription")} />;
  if (state.status === "error" && state.error.status === 404) return notFound;
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(memory) =>
        belongsTo(memory, scope) ? (
          <Detail scope={scope} memory={memory} revision={revision} revisionsError={revisionsError} />
        ) : (
          notFound
        )
      }
    </QueryView>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 [overflow-wrap:anywhere]">{children}</dd>
    </>
  );
}

function Mono({ children }: { children: ReactNode }) {
  return <span className="font-mono text-xs">{children}</span>;
}

function When({ iso }: { iso: string }) {
  const format = useFormatter();
  return (
    <time dateTime={iso} className="tabular-nums">
      {format.dateTime(new Date(iso), { dateStyle: "medium", timeStyle: "short" })}
    </time>
  );
}

function Detail({
  scope,
  memory,
  revision,
  revisionsError,
}: {
  scope: MemoryScope;
  memory: Memory;
  revision: number | null;
  revisionsError: ApiErrorInfo | null;
}) {
  const t = useTranslations("memories");
  const viewing = revision !== null && revision !== memory.revision ? revision : null;
  // The project's first location restricts nothing; badges leave it out. Without the project they show it all.
  const project = useHubQuery({
    ...projectQuery(browserApi, scope.kind === "project" ? scope.project : ""),
    enabled: scope.kind === "project",
  });
  const unrestricted = project.status === "success" ? project.data.locations[0] : undefined;
  const { fields } = parseFrontmatter(memory.body);
  const title = fields.name || memory.name;
  const back = memoriesHref(scope);

  return (
    <>
      <div>
        <Button asChild variant="ghost" size="sm" className="-ml-2.5">
          <Link href={back}>
            <ArrowLeft aria-hidden="true" />
            {scope.kind === "project" ? t("detail.backProject") : t("detail.backPersonal")}
          </Link>
        </Button>
      </div>
      <PageHeader
        eyebrow={<span className="font-mono normal-case">{memory.name}</span>}
        title={title}
        description={fields.description}
        meta={
          <>
            <MemoryTypeBadge type={memory.type} />
            {memory.scope === "project" ? <LabelBadge label={memory.label} unrestricted={unrestricted} /> : null}
            {memory.deleted ? <DeletedBadge /> : null}
          </>
        }
      />
      {memory.deleted && viewing === null ? (
        <Alert role="note">
          <Archive aria-hidden="true" />
          <AlertTitle>{t("detail.deletedTitle", { revision: memory.revision })}</AlertTitle>
          <AlertDescription>{t("detail.deletedDescription")}</AlertDescription>
        </Alert>
      ) : null}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-6">
          {viewing === null ? (
            memory.deleted ? null : (
              <Content body={memory.body} />
            )
          ) : (
            <OldRevision scope={scope} memory={memory} revision={viewing} unrestricted={unrestricted} />
          )}
        </div>
        <div className="flex min-w-0 flex-col gap-6">
          <Details memory={memory} unrestricted={unrestricted} />
          <Card>
            <CardHeader>
              <CardTitle>
                <h2 className="flex items-center gap-2">
                  <History className="size-4" aria-hidden="true" />
                  {t("history.title")}
                </h2>
              </CardTitle>
              <CardDescription>{t("history.description")}</CardDescription>
            </CardHeader>
            <CardContent>
              <RevisionHistory
                memory={memory}
                selected={viewing ?? memory.revision}
                hrefOf={(number) =>
                  (number === memory.revision
                    ? memoryHref(scope, memory.id)
                    : `${memoryHref(scope, memory.id)}?revision=${number}`) as Route
                }
                initialError={revisionsError}
                unrestricted={unrestricted}
              />
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  );
}

function OldRevision({
  scope,
  memory,
  revision,
  unrestricted,
}: {
  scope: MemoryScope;
  memory: Memory;
  revision: number;
  unrestricted?: string;
}) {
  const t = useTranslations("memories");
  const state = useHubQuery(memoryRevisionQuery(browserApi, memory.id, revision));
  if (state.status === "error" && state.error.status === 404) {
    return (
      <NotFoundState
        title={t("detail.revisionNotFoundTitle", { revision })}
        description={t("detail.revisionNotFoundDescription")}
      />
    );
  }
  if (state.status === "error") return <ApiErrorState error={state.error} onRetry={state.retry} />;
  if (state.status === "loading") {
    return (
      <LoadingState>
        <Skeleton className="h-64 w-full rounded-xl" />
      </LoadingState>
    );
  }
  const old = state.data;
  return (
    <>
      <Alert role="note" data-testid="old-revision">
        <History aria-hidden="true" />
        <AlertTitle>{t("detail.oldTitle", { revision: old.revision, current: memory.revision })}</AlertTitle>
        <AlertDescription>
          <p>
            {t.rich("detail.oldDescription", {
              actor: old.actor,
              when: () => <When iso={old.created_at} />,
            })}
          </p>
          <p>
            <Link href={memoryHref(scope, memory.id)} className="font-medium">
              {t("detail.toLatest", { revision: memory.revision })}
            </Link>
          </p>
        </AlertDescription>
      </Alert>
      {old.type !== memory.type || labelNames(old.label).level !== labelNames(memory.label).level ? (
        <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          {t("detail.thenWas")}
          <MemoryTypeBadge type={old.type} />
          {memory.scope === "project" ? <LabelBadge label={old.label} unrestricted={unrestricted} /> : null}
        </div>
      ) : null}
      {old.deleted ? (
        <Alert role="note">
          <Archive aria-hidden="true" />
          <AlertDescription>{t("detail.tombstone")}</AlertDescription>
        </Alert>
      ) : (
        <Content body={old.body} />
      )}
    </>
  );
}

/** The file, rendered (frontmatter apart, Markdown as HTML it builds itself) or as it is written. */
function Content({ body }: { body: string }) {
  const t = useTranslations("memories.detail");
  const [raw, setRaw] = useState(false);
  const { raw: frontmatter, body: markdown } = parseFrontmatter(body);
  return (
    <Card data-testid="memory-content">
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
        <CardTitle>
          <h2>{t("content")}</h2>
        </CardTitle>
        <div role="group" aria-label={t("view")} className="flex gap-1 rounded-lg border p-0.5">
          <Button
            type="button"
            size="sm"
            variant={raw ? "ghost" : "secondary"}
            aria-pressed={!raw}
            onClick={() => setRaw(false)}
          >
            <Eye aria-hidden="true" />
            {t("rendered")}
          </Button>
          <Button
            type="button"
            size="sm"
            variant={raw ? "secondary" : "ghost"}
            aria-pressed={raw}
            onClick={() => setRaw(true)}
            data-testid="view-raw"
          >
            <Code aria-hidden="true" />
            {t("raw")}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {raw ? (
          <pre
            tabIndex={0}
            aria-label={t("raw")}
            className={cn(
              "max-h-[70vh] overflow-auto rounded-lg border bg-muted/40 p-3 font-mono text-xs leading-relaxed",
              "whitespace-pre-wrap [overflow-wrap:anywhere]",
            )}
            data-testid="memory-raw"
          >
            {body}
          </pre>
        ) : (
          <>
            {frontmatter !== null ? (
              <details className="group rounded-lg border bg-muted/30 text-xs">
                <summary
                  className={cn(
                    "cursor-pointer rounded-lg px-3 py-2 font-medium text-muted-foreground select-none",
                    "hover:text-foreground",
                  )}
                >
                  {t("frontmatter")}
                </summary>
                <pre
                  tabIndex={0}
                  aria-label={t("frontmatter")}
                  className="overflow-x-auto border-t px-3 py-2 font-mono leading-relaxed"
                >
                  {frontmatter}
                </pre>
              </details>
            ) : null}
            {markdown.trim() ? (
              <SafeMarkdown>{markdown}</SafeMarkdown>
            ) : (
              <p className="text-sm text-muted-foreground">{t("emptyBody")}</p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function Details({ memory, unrestricted }: { memory: Memory; unrestricted?: string }) {
  const t = useTranslations("memories.detail");
  const tLabel = useTranslations("memories.label");
  const label = labelNames(memory.label);
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>{t("details")}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2.5 text-sm" data-testid="memory-details">
          <Field label={t("file")}>
            <Mono>{memory.name}</Mono>
          </Field>
          <Field label={t("scope")}>
            {memory.scope === "project" ? t("scopeProject", { project: memory.project ?? "" }) : t("scopePersonal")}
          </Field>
          <Field label={t("location")}>
            <Mono>{memory.location}</Mono>
          </Field>
          <Field label={t("owner")}>
            {memory.owner}
            {!SHARED_TYPES.has(memory.type) || memory.scope === "personal" ? (
              <span className="block text-xs text-muted-foreground">{t("ownerOnly")}</span>
            ) : null}
          </Field>
          {memory.scope === "project" ? (
            <>
              <Field label={tLabel("level")}>
                <Mono>{label.level ?? "-"}</Mono>
              </Field>
              <Field label={tLabel("location")}>
                {label.location === null || label.location === unrestricted ? (
                  <>
                    {tLabel("anyLocation")}
                    {label.location ? (
                      <span className="ml-1.5 font-mono text-xs text-muted-foreground">{label.location}</span>
                    ) : null}
                  </>
                ) : (
                  <Mono>{label.location}</Mono>
                )}
              </Field>
              <Field label={tLabel("integrity")}>
                {label.integrity === "T" ? tLabel("trusted") : tLabel("untrusted")}
              </Field>
            </>
          ) : null}
          <Field label={t("revision")}>
            <Mono>{memory.revision}</Mono>
          </Field>
          <Field label={t("updated")}>
            <When iso={memory.updated_at} />
            <span className="block text-xs text-muted-foreground">{t("by", { login: memory.updated_by })}</span>
          </Field>
          <Field label={t("created")}>
            <When iso={memory.created_at} />
          </Field>
        </dl>
      </CardContent>
    </Card>
  );
}

