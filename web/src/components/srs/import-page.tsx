"use client";

import { Eye, FileUp, Lock, Upload } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ChangeEvent, type FormEvent, useId, useState } from "react";

import { Banner } from "@/components/feedback/banner";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { useHubQuery } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type DocumentKind, importMarkdown, type SrsImport, type SrsImportResult, srsDocumentsQuery } from "./api";
import { SrsTabs } from "./documents-page";
import { signInAgainOn401, useDocumentRefresh, useSrsViewer } from "./hooks";
import { ImportPreview, refusesSave } from "./import-preview";
import { documentHref, documentsHref } from "./links";
import { canWrite } from "./model";

const SLUG = /^[a-z0-9][a-z0-9-]{0,99}$/; // evo_agents.hub.tables_srs.SLUG
const MAX_BYTES = 4 * 1024 * 1024; // evo_agents.hub.server.srs.imports.MAX_MARKDOWN_BYTES

type Form = { target: string; title: string; slug: string; kind: DocumentKind; summary: string; markdown: string };

function requestOf(form: Form, dryRun: boolean): SrsImport {
  const next = form.target !== "";
  return {
    markdown: form.markdown,
    summary: form.summary.trim(),
    dry_run: dryRun,
    kind: form.kind,
    ...(next ? { document: form.target } : { ...(form.title.trim() ? { title: form.title.trim() } : {}), ...(form.slug.trim() ? { slug: form.slug.trim() } : {}) }),
  };
}

function ImportForm({ project, documents, initialTarget }: { project: string; documents: { slug: string; title: string }[]; initialTarget: string }) {
  const t = useTranslations("srs.import");
  const tKind = useTranslations("srs.documentKind");
  const ids = useId();
  const router = useRouter();
  const failure = useWriteFailure();
  const [form, setForm] = useState<Form>({ target: initialTarget, title: "", slug: "", kind: "srs", summary: "", markdown: "" });
  const [preview, setPreview] = useState<{ for: string; result: SrsImportResult } | null>(null);
  const [busy, setBusy] = useState<"preview" | "import" | null>(null);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const refresh = useDocumentRefresh(project, form.target || null);
  const set = (patch: Partial<Form>) => setForm((held) => ({ ...held, ...patch }));
  const fingerprint = JSON.stringify(requestOf(form, true));
  const current = preview && preview.for === fingerprint ? preview.result : null;
  const slugBad = form.target === "" && form.slug.trim() !== "" && !SLUG.test(form.slug.trim());
  const tooBig = new Blob([form.markdown]).size > MAX_BYTES;
  const blocked = !form.markdown.trim() || slugBad || tooBig;

  const readFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    set({ markdown: await file.text() });
    event.target.value = "";
  };

  const run = async (dryRun: boolean) => {
    if (blocked || busy) return;
    setBusy(dryRun ? "preview" : "import");
    setRefused(null);
    try {
      const result = await importMarkdown(browserApi(), project, requestOf(form, dryRun));
      if (dryRun) {
        setPreview({ for: fingerprint, result });
        return;
      }
      await refresh();
      notify({
        tone: "success",
        text: result.created ? t("created", { document: result.document, version: result.version ?? 1 }) : t("versioned", { document: result.document, version: result.version ?? 1 }),
        description: t("draftText"),
      });
      router.push(documentHref(project, result.document, { v: result.version }));
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error, { 409: t("slugTaken") }));
    } finally {
      setBusy(null);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(current === null);
  };

  return (
    <div className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_24rem]">
      <form onSubmit={submit} className="flex min-w-0 flex-col gap-4 rounded-md border bg-card p-4 md:p-6" aria-label={t("formLabel")} data-testid="srs-import-form">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-target`}>{t("target")}</Label>
          <NativeSelect id={`${ids}-target`} value={form.target} onChange={(event) => set({ target: event.target.value })} className="w-full" data-testid="srs-import-target">
            <option value="">{t("newDocument")}</option>
            {documents.map((doc) => (
              <option key={doc.slug} value={doc.slug}>
                {t("nextVersionOf", { title: doc.title })}
              </option>
            ))}
          </NativeSelect>
        </div>
        {form.target === "" ? (
          <div className="grid gap-4 sm:grid-cols-2 2xl:grid-cols-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${ids}-title`}>{t("title")}</Label>
              <Input id={`${ids}-title`} value={form.title} onChange={(event) => set({ title: event.target.value })} maxLength={300} placeholder={t("titlePlaceholder")} />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${ids}-slug`}>{t("slug")}</Label>
              <Input
                id={`${ids}-slug`}
                value={form.slug}
                onChange={(event) => set({ slug: event.target.value })}
                maxLength={100}
                placeholder={t("slugPlaceholder")}
                aria-invalid={slugBad || undefined}
                aria-describedby={`${ids}-slug-hint`}
                className="font-mono"
              />
              <p id={`${ids}-slug-hint`} className={slugBad ? "text-xs font-medium text-danger" : "text-xs text-fg-subtle"}>
                {slugBad ? t("slugBad") : t("slugHint")}
              </p>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${ids}-kind`}>{t("kind")}</Label>
              <NativeSelect id={`${ids}-kind`} value={form.kind} onChange={(event) => set({ kind: event.target.value as DocumentKind })} className="w-full">
                <option value="srs">{tKind("srs")}</option>
                <option value="spec">{tKind("spec")}</option>
              </NativeSelect>
            </div>
          </div>
        ) : null}
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-summary`}>{t("summary")}</Label>
          <Input id={`${ids}-summary`} value={form.summary} onChange={(event) => set({ summary: event.target.value })} maxLength={500} placeholder={t("summaryPlaceholder")} />
        </div>
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Label htmlFor={`${ids}-markdown`}>{t("markdown")}</Label>
            <label className="inline-flex min-h-8 cursor-pointer items-center gap-1.5 rounded-sm px-2 text-[13px] font-medium text-brand hover:bg-accent max-md:min-h-11">
              <FileUp className="size-4" aria-hidden="true" />
              {t("readFile")}
              <input type="file" accept=".md,.markdown,text/markdown,text/plain" className="sr-only" onChange={(event) => void readFile(event)} data-testid="srs-import-file" />
            </label>
          </div>
          <Textarea
            id={`${ids}-markdown`}
            value={form.markdown}
            onChange={(event) => set({ markdown: event.target.value })}
            rows={18}
            spellCheck={false}
            required
            aria-describedby={`${ids}-markdown-hint`}
            aria-invalid={tooBig || undefined}
            className="font-mono text-[13px] leading-5"
            data-testid="srs-import-markdown"
          />
          <p id={`${ids}-markdown-hint`} className={tooBig ? "text-xs font-medium text-danger" : "text-xs text-fg-subtle"}>
            {tooBig ? t("tooBig") : t("markdownHint")}
          </p>
        </div>
        {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
        <div className="flex flex-wrap items-center justify-end gap-2">
          <p className="mr-auto text-xs text-fg-subtle">{current ? t("previewed") : t("previewFirst")}</p>
          <Button type="button" variant="secondary" busy={busy === "preview"} disabled={blocked} onClick={() => void run(true)} data-testid="srs-import-preview-run">
            <Eye aria-hidden="true" />
            {t("preview")}
          </Button>
          <Button
            type="button"
            busy={busy === "import"}
            disabled={blocked || current === null || current.preview.problems.some(refusesSave)}
            onClick={() => void run(false)}
            data-testid="srs-import-run"
          >
            <Upload aria-hidden="true" />
            {t("import")}
          </Button>
        </div>
      </form>
      <div className="flex min-w-0 flex-col gap-4">
        {current ? (
          <ImportPreview preview={current.preview} next={form.target !== ""} />
        ) : (
          <Banner icon={Eye} title={t("noPreviewTitle")}>
            {t("noPreviewText")}
          </Banner>
        )}
      </div>
    </div>
  );
}

/**
 * Markdown into a project's BA documents (/p/{project}/x/srs/import, the plugin's page without a menu item): a new
 * document, or the next version of one, whose headings follow the template. The hub's dry run previews what it holds
 * and what is wrong first; nothing is written until Import, which saves a draft. Importing takes the writer role.
 */
export function ImportPage({ project, title, target, initialError }: { project: string; title: string; target: string | null; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("srs.import");
  const viewer = useSrsViewer(project);
  const writer = canWrite(viewer);
  const state = useHubQuery(srsDocumentsQuery(browserApi, project), initialError);
  const documents = state.status === "success" ? state.data : [];
  const known = target && documents.some((doc) => doc.slug === target) ? target : "";
  return (
    <div className="flex flex-col gap-6" data-testid="srs-import">
      <div className="flex flex-col gap-4">
        <PageHeader title={title} />
        <SrsTabs project={project} current="import" writer={writer} />
      </div>
      {viewer && !writer ? (
        <Banner icon={Lock} title={t("readerTitle", { project })} testId="srs-import-reader">
          <Link href={documentsHref(project)} className="text-brand underline underline-offset-4 hover:text-brand-hover">
            {t("backToDocuments")}
          </Link>
        </Banner>
      ) : viewer && state.status !== "loading" ? (
        <ImportForm key={known} project={project} documents={documents} initialTarget={known} />
      ) : null}
    </div>
  );
}
