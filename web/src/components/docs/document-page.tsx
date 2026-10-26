"use client";

import { History } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useId, useMemo } from "react";

import { SafeMarkdown } from "@/components/data/markdown";
import { Banner } from "@/components/feedback/banner";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type DocsDocument, docsDocumentQuery } from "./api";
import { DocumentHead } from "./document-head";
import { documentHref } from "./links";
import { imageResolver } from "./model";

/** The versions of a document as a select that opens the one picked. */
function VersionPicker({ project, doc }: { project: string; doc: DocsDocument }) {
  const t = useTranslations("docs.read");
  const id = useId();
  const router = useRouter();
  if (doc.versions.length < 2) return null;
  return (
    <div className="flex items-center gap-2">
      <Label htmlFor={id} className="text-xs text-muted-foreground">
        {t("version")}
      </Label>
      <NativeSelect
        id={id}
        value={doc.shown.number}
        onChange={(event) => router.push(documentHref(project, doc.slug, Number(event.target.value)))}
        data-testid="docs-version-picker"
      >
        {doc.versions.map((version) => (
          <option key={version.number} value={version.number}>
            {version.number === doc.latest_version ? t("latestOption", { number: version.number }) : `v${version.number}`}
          </option>
        ))}
      </NativeSelect>
    </div>
  );
}

function Body({ project, doc }: { project: string; doc: DocsDocument }) {
  const t = useTranslations("docs.read");
  const images = useMemo(() => imageResolver(doc.shown.images), [doc.shown.images]);
  const older = doc.shown.number !== doc.latest_version;
  return (
    <div className="flex flex-col gap-4">
      {older ? (
        <Banner
          icon={History}
          tone="attention"
          title={t("older", { number: doc.shown.number, latest: doc.latest_version })}
          actions={
            <Button asChild variant="secondary" size="sm">
              <Link href={documentHref(project, doc.slug)}>{t("readLatest")}</Link>
            </Button>
          }
          testId="docs-older-version"
        />
      ) : null}
      <article className="max-w-3xl" aria-label={doc.shown.title} data-testid="docs-body">
        {doc.shown.body.trim() ? (
          <SafeMarkdown variant="page" images={images} testId="docs-markdown">
            {doc.shown.body}
          </SafeMarkdown>
        ) : (
          <p className="text-[13px] text-fg-subtle">{t("empty")}</p>
        )}
      </article>
    </div>
  );
}

/**
 * A document of a project (/p/{project}/x/docs/documents/{slug}?v=N, the hub's feature docs): the version asked, the
 * latest by default, as Markdown with the images it shows, which the hub serves from its own origin to the document's
 * readers. An older version says so and links the latest.
 */
export function DocumentPage({
  project,
  slug,
  version,
  initialError,
}: {
  project: string;
  slug: string;
  version: number | null;
  initialError: ApiErrorInfo | null;
}) {
  const state = useHubQuery(docsDocumentQuery(browserApi, project, slug, version), initialError);
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(doc) => (
        <div className="flex flex-col gap-6" data-testid="docs-document">
          <DocumentHead project={project} doc={doc} current="read" shown={doc.shown} actions={<VersionPicker project={project} doc={doc} />} />
          <Body project={project} doc={doc} />
        </div>
      )}
    </QueryView>
  );
}
