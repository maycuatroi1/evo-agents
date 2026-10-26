"use client";

import { ImagePlus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";

import { SafeMarkdown } from "@/components/data/markdown";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";

import { type DocsDocument, saveDocument, uploadImage } from "./api";
import { signInAgainOn401, useDocumentRefresh } from "./hooks";
import { documentHref, documentsHref } from "./links";
import {
  IMAGE_TYPES,
  imageMarkdown,
  imageResolver,
  MAX_BODY,
  MAX_IMAGE_BYTES,
  MAX_SUMMARY,
  MAX_TITLE,
  shownImages,
  SLUG,
  slugOf,
  uploadName,
} from "./model";

type Held = { sha256: string; url: string };

/** The images the editor holds, by name: the latest version's, then those uploaded here, previewed from the file. */
function useImages(doc: DocsDocument | null) {
  const [images, setImages] = useState<Map<string, Held>>(
    () => new Map((doc?.shown.images ?? []).map((image) => [image.name, { sha256: image.sha256, url: image.url }])),
  );
  const local = useRef<string[]>([]);
  useEffect(() => () => local.current.forEach((url) => URL.revokeObjectURL(url)), []);
  const add = (name: string, sha256: string, file: File) => {
    const url = URL.createObjectURL(file);
    local.current.push(url);
    setImages((held) => new Map(held).set(name, { sha256, url }));
  };
  return { images, add };
}

/**
 * The editor of a document (web/DESIGN.md, Docs): a new one (`doc` null: its title, the slug made from it until edited,
 * the Markdown) or the next version of one, on the latest version it read. Add images uploads PNG, JPEG, WebP or GIF
 * files to the hub and puts their Markdown at the cursor; Preview draws the Markdown as readers will see it. Save
 * sends only the images the Markdown shows; a version someone saved meanwhile refuses it (409), with the hub's words.
 */
export function DocumentEditor({ project, doc }: { project: string; doc: DocsDocument | null }) {
  const t = useTranslations("docs.editor");
  const ids = useId();
  const router = useRouter();
  const refresh = useDocumentRefresh(project);
  const failure = useWriteFailure();
  const body = useRef<HTMLTextAreaElement>(null);
  const files = useRef<HTMLInputElement>(null);
  const { images, add } = useImages(doc);
  const [title, setTitle] = useState(doc?.shown.title ?? "");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [text, setText] = useState(doc?.shown.body ?? "");
  const [summary, setSummary] = useState("");
  const [uploading, setUploading] = useState(0);
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const [missing, setMissing] = useState<"title" | "slug" | null>(null);
  const resolve = useMemo(() => imageResolver([...images].map(([name, held]) => ({ name, url: held.url }))), [images]);
  const shownSlug = doc ? doc.slug : slugEdited ? slug : slugOf(title);

  const insert = (markdown: string) => {
    const at = body.current?.selectionStart;
    setText((current) => {
      const cut = at ?? current.length;
      const before = current.slice(0, cut);
      const gap = before && !before.endsWith("\n") ? "\n\n" : "";
      return `${before}${gap}${markdown}\n${current.slice(cut)}`;
    });
  };

  const addImages = async (picked: FileList | null) => {
    const taken = new Set(images.keys());
    const added: string[] = [];
    for (const file of Array.from(picked ?? [])) {
      if (file.size > MAX_IMAGE_BYTES || !(IMAGE_TYPES as readonly string[]).includes(file.type)) {
        notify({ tone: "error", text: t("imageRefused", { name: file.name }), description: t("imageRule") });
        continue;
      }
      const name = uploadName(file.name, taken);
      taken.add(name);
      setUploading((count) => count + 1);
      try {
        const uploaded = await uploadImage(project, file);
        add(name, uploaded.sha256, file);
        added.push(imageMarkdown(name, file.name.replace(/\.[^.]+$/, "")));
      } catch (error) {
        signInAgainOn401(error);
        const reason = isApiError(error) ? error.info.message : String(error);
        notify({ tone: "error", text: t("imageFailed", { name: file.name }), description: reason });
      } finally {
        setUploading((count) => count - 1);
      }
    }
    if (added.length) insert(added.join("\n\n"));
    if (files.current) files.current.value = "";
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || uploading) return;
    if (!title.trim()) {
      setMissing("title");
      document.getElementById(`${ids}-title`)?.focus();
      return;
    }
    if (!SLUG.test(shownSlug)) {
      setMissing("slug");
      setSlugEdited(true);
      setSlug(shownSlug);
      document.getElementById(`${ids}-slug`)?.focus();
      return;
    }
    const shown = shownImages(text, new Set(images.keys())).map((name) => ({ name, sha256: images.get(name)!.sha256 }));
    setBusy(true);
    setRefused(null);
    try {
      const saved = await saveDocument(browserApi(), project, shownSlug, {
        title: title.trim(),
        body: text,
        summary: summary.trim(),
        images: shown,
        base_version: doc ? doc.latest_version : 0,
      });
      await refresh(shownSlug);
      const said = saved.changed ? t("saved", { version: saved.version }) : t("unchanged", { version: saved.version });
      notify({ tone: "success", text: said });
      router.push(documentHref(project, shownSlug));
    } catch (error) {
      signInAgainOn401(error);
      const found = failure(error, { 409: doc ? t("conflict") : t("slugTaken") });
      setRefused({ ...found, detail: found.detail ?? (isApiError(error) ? error.info.message : null) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex max-w-4xl flex-col gap-5" data-testid="docs-editor">
      <div className="grid gap-4 sm:grid-cols-[2fr_1fr]">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-title`}>{t("title")}</Label>
          <Input
            id={`${ids}-title`}
            value={title}
            onChange={(event) => {
              setTitle(event.target.value);
              setMissing(null);
            }}
            maxLength={MAX_TITLE}
            aria-invalid={missing === "title" || undefined}
            aria-describedby={missing === "title" ? `${ids}-title-error` : undefined}
            data-testid="docs-editor-title"
          />
          {missing === "title" ? (
            <p id={`${ids}-title-error`} className="text-xs font-medium text-danger">
              {t("titleMissing")}
            </p>
          ) : null}
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-slug`}>{t("slug")}</Label>
          <Input
            id={`${ids}-slug`}
            value={shownSlug}
            readOnly={doc !== null}
            onChange={(event) => {
              setSlug(event.target.value);
              setSlugEdited(true);
              setMissing(null);
            }}
            maxLength={100}
            className="font-mono"
            aria-invalid={missing === "slug" || undefined}
            aria-describedby={`${ids}-slug-hint`}
            data-testid="docs-editor-slug"
          />
          <p id={`${ids}-slug-hint`} className={missing === "slug" ? "text-xs font-medium text-danger" : "text-xs text-fg-subtle"}>
            {doc ? t("slugFixed") : t("slugHint")}
          </p>
        </div>
      </div>
      <Tabs defaultValue="write" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <TabsList>
            <TabsTrigger value="write">{t("write")}</TabsTrigger>
            <TabsTrigger value="preview" data-testid="docs-editor-preview-tab">
              {t("preview")}
            </TabsTrigger>
          </TabsList>
          <input
            ref={files}
            type="file"
            accept={IMAGE_TYPES.join(",")}
            multiple
            className="sr-only"
            tabIndex={-1}
            aria-hidden="true"
            onChange={(event) => void addImages(event.target.files)}
            data-testid="docs-editor-files"
          />
          <Button type="button" variant="secondary" size="sm" busy={uploading > 0} onClick={() => files.current?.click()} data-testid="docs-editor-add-images">
            <ImagePlus aria-hidden="true" />
            {t("addImages")}
          </Button>
        </div>
        <TabsContent value="write" className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-body`} className="sr-only">
            {t("body")}
          </Label>
          <Textarea
            ref={body}
            id={`${ids}-body`}
            value={text}
            onChange={(event) => setText(event.target.value)}
            rows={24}
            maxLength={MAX_BODY}
            aria-describedby={`${ids}-body-hint`}
            className="font-mono text-[13px] leading-5"
            data-testid="docs-editor-body"
          />
          <p id={`${ids}-body-hint`} className="text-xs text-fg-subtle">
            {t("bodyHint")}
          </p>
        </TabsContent>
        <TabsContent value="preview" className="min-h-48 rounded-md border border-border p-4" data-testid="docs-editor-preview">
          {text.trim() ? (
            <SafeMarkdown variant="page" images={resolve} testId="docs-editor-markdown">
              {text}
            </SafeMarkdown>
          ) : (
            <p className="text-[13px] text-fg-subtle">{t("previewEmpty")}</p>
          )}
        </TabsContent>
      </Tabs>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-summary`}>{t("summary")}</Label>
        <Input
          id={`${ids}-summary`}
          value={summary}
          onChange={(event) => setSummary(event.target.value)}
          maxLength={MAX_SUMMARY}
          placeholder={t("summaryPlaceholder")}
          data-testid="docs-editor-summary"
        />
      </div>
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <div className="flex flex-wrap justify-end gap-2">
        <Button asChild variant="ghost">
          <Link href={doc ? documentHref(project, doc.slug) : documentsHref(project)}>{t("cancel")}</Link>
        </Button>
        <Button type="submit" busy={busy} disabled={uploading > 0} data-testid="docs-editor-save">
          {doc ? t("saveVersion", { version: doc.latest_version + 1 }) : t("create")}
        </Button>
      </div>
    </form>
  );
}
