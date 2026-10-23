"use client";

import { useQuery } from "@tanstack/react-query";
import { FileText, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";

import { SafeMarkdown } from "@/components/data/markdown";
import { Segmented } from "@/components/data/segmented";
import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { diffLines, diffStats } from "@/lib/line-diff";
import { cn } from "@/lib/utils";

import { DocDiff, DocDiffLegend } from "./doc-diff";
import { docVersions, type DocVersion, MAX_DOC_BYTES } from "./doc-model";
import type { Feed } from "./feed-model";
import { useAuthorLabel } from "./message-parts";

async function readDoc(url: string, signal: AbortSignal): Promise<string> {
  const response = await fetch(url, { signal, credentials: "same-origin" });
  if (!response.ok) throw new Error(`the hub answered ${response.status}`);
  return new TextDecoder("utf-8").decode(await response.arrayBuffer());
}

/** A version's text, read once per file (its URL names it); none for a version too big for the panel. */
function useDocText(version: DocVersion | null) {
  const readable = version !== null && version.file.size <= MAX_DOC_BYTES;
  return useQuery({
    queryKey: ["chat-doc", version?.file.url],
    queryFn: ({ signal }) => readDoc(version!.file.url, signal),
    enabled: readable,
    staleTime: Infinity,
    retry: 1,
  });
}

type Mode = "read" | "changes";

/**
 * A Markdown document of the chat, read beside it while the composer stays in reach (web/DESIGN.md, Chats). It shows
 * the document's latest version; when a new one comes (an agent sent back the file it fixed, under the same name) it
 * reloads by itself and shows what changed against the version read before: lines added (+) on `success-soft`, lines
 * removed or replaced (−) on `danger-soft`, the lines kept around them plain. Changes compares any version with an
 * earlier one. It takes the focus when it opens; Close gives it back to what opened it (`chat-view.tsx`).
 */
export function DocPanel({
  feed,
  name,
  me,
  onClose,
  className,
}: {
  feed: Feed;
  name: string;
  me: string | null;
  onClose: () => void;
  className?: string;
}) {
  const t = useTranslations("chats.doc");
  const authorLabel = useAuthorLabel();
  const versions = useMemo(() => docVersions(feed, name), [feed, name]);
  const latest = versions.at(-1) ?? null;
  const latestKey = latest?.key ?? null;

  // The version shown and the one it is compared with: on open, the latest and the one before it.
  const [shownKey, setShownKey] = useState<string | null>(latestKey);
  const [baseKey, setBaseKey] = useState<string | null>(() => versions.at(-2)?.key ?? null);
  const [mode, setMode] = useState<Mode>("read");
  const [reloaded, setReloaded] = useState(false);
  const [seenLatest, setSeenLatest] = useState<string | null>(latestKey);
  // A new version came while the panel is open: it reloads to it and shows what changed against the one read.
  if (latestKey !== seenLatest) {
    setSeenLatest(latestKey);
    if (latestKey !== null) {
      setBaseKey(shownKey !== latestKey ? shownKey : (versions.at(-2)?.key ?? null));
      setShownKey(latestKey);
      if (seenLatest !== null) {
        setMode("changes");
        setReloaded(true);
      }
    }
  }

  const shownIndex = versions.findIndex((version) => version.key === shownKey);
  const shown = shownIndex >= 0 ? versions[shownIndex] : latest;
  const earlier = versions.slice(0, shownIndex >= 0 ? shownIndex : versions.length - 1);
  const base = earlier.find((version) => version.key === baseKey) ?? earlier.at(-1) ?? null;
  if (mode === "changes" && earlier.length === 0) setMode("read");

  const comparing = mode === "changes" && base !== null;
  const tooBig = shown !== null && (shown.file.size > MAX_DOC_BYTES || (comparing && base.file.size > MAX_DOC_BYTES));
  const current = useDocText(shown);
  const before = useDocText(comparing ? base : null);
  const lines = useMemo(
    () => (comparing && current.data !== undefined && before.data !== undefined ? diffLines(before.data, current.data) : null),
    [comparing, current.data, before.data],
  );
  const stats = lines ? diffStats(lines) : null;

  useEffect(() => {
    if (!reloaded) return;
    const timer = window.setTimeout(() => setReloaded(false), 8000);
    return () => window.clearTimeout(timer);
  }, [reloaded, latestKey]);

  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    panel.current?.focus();
  }, []);

  const label = (version: DocVersion, index: number) =>
    t("version", { number: index + 1, author: authorLabel(version.message.author, me), at: new Date(version.message.created_at) });

  return (
    <section
      ref={panel}
      tabIndex={-1}
      aria-labelledby="doc-title"
      className={cn("flex min-h-0 flex-col overflow-hidden outline-none", className)}
      data-testid="doc-panel"
      data-doc={name}
    >
      <header className="flex items-center gap-2 border-b px-4 py-2.5">
        <FileText className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <h2 id="doc-title" className="min-w-0 flex-1 truncate text-sm font-semibold" title={name}>
          {name}
        </h2>
        <Button type="button" variant="ghost" size="icon-sm" aria-label={t("close")} onClick={onClose} data-testid="doc-close">
          <X aria-hidden="true" />
        </Button>
      </header>
      {shown ? (
        <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2 text-xs">
          <Segmented<Mode>
            label={t("modeLabel")}
            value={mode}
            onChange={setMode}
            options={[
              { value: "read", label: t("read"), testId: "doc-mode-read" },
              ...(earlier.length > 0 ? [{ value: "changes" as const, label: t("changes"), testId: "doc-mode-changes" }] : []),
            ]}
          />
          <label className="flex min-w-0 items-center gap-1.5">
            <span className="sr-only">{t("shownLabel")}</span>
            <NativeSelect
              size="sm"
              className="max-w-[16rem]"
              value={shown.key}
              onChange={(event) => {
                setShownKey(event.target.value);
                setBaseKey(null);
              }}
              data-testid="doc-version"
            >
              {versions.map((version, index) => (
                <NativeSelectOption key={version.key} value={version.key}>
                  {label(version, index)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </label>
          {comparing ? (
            <label className="flex min-w-0 items-center gap-1.5">
              <span className="text-muted-foreground">{t("against")}</span>
              <NativeSelect size="sm" className="max-w-[16rem]" value={base.key} onChange={(event) => setBaseKey(event.target.value)} data-testid="doc-base">
                {earlier.map((version, index) => (
                  <NativeSelectOption key={version.key} value={version.key}>
                    {label(version, index)}
                  </NativeSelectOption>
                ))}
              </NativeSelect>
            </label>
          ) : null}
          {stats ? (
            <span className="ml-auto font-mono tabular-nums" data-testid="doc-stats">
              <span className="text-success">+{stats.added}</span> <span className="text-danger">−{stats.removed}</span>
            </span>
          ) : null}
        </div>
      ) : null}
      <p role="status" aria-live="polite" className={cn("border-b bg-brand-soft px-4 py-1.5 text-xs", !reloaded && "sr-only")} data-testid="doc-reloaded">
        {reloaded && latest ? t("reloaded", { author: authorLabel(latest.message.author, me) }) : ""}
      </p>
      <div className="min-h-0 flex-1 overflow-auto bg-card" tabIndex={0} role="region" aria-label={t("bodyLabel", { name })} data-testid="doc-body">
        {!shown ? (
          <p className="p-4 text-sm text-muted-foreground">{t("gone")}</p>
        ) : tooBig ? (
          <p className="p-4 text-sm text-muted-foreground" data-testid="doc-too-big">
            {t("tooBig")}
          </p>
        ) : current.isError || before.isError ? (
          <p className="p-4 text-sm text-muted-foreground">
            {t("readFailed")}{" "}
            <Button
              type="button"
              variant="link"
              size="sm"
              className="h-auto px-0"
              onClick={() => void Promise.all([current, before].filter((read) => read.isError).map((read) => read.refetch()))}
            >
              {t("retry")}
            </Button>
          </p>
        ) : current.data === undefined || (comparing && lines === null) ? (
          <p role="status" className="p-4 text-sm text-muted-foreground">
            {t("loading")}
          </p>
        ) : comparing && lines ? (
          <div className="flex flex-col gap-2 p-2">
            <DocDiffLegend />
            <DocDiff lines={lines} />
          </div>
        ) : (
          <div className="px-4 py-3 sm:px-6">
            <SafeMarkdown className="max-w-[72ch]" testId="doc-markdown">
              {current.data}
            </SafeMarkdown>
          </div>
        )}
      </div>
    </section>
  );
}
