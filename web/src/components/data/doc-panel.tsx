"use client";

import { useQuery } from "@tanstack/react-query";
import { FileText, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { type DocVersion, MAX_DOC_BYTES } from "@/lib/doc-versions";
import { diffLines, diffStats } from "@/lib/line-diff";
import { textKind } from "@/lib/outputs";
import { cn } from "@/lib/utils";

import { DocDiff, DocDiffLegend } from "./doc-diff";
import { SafeMarkdown } from "./markdown";
import { Segmented } from "./segmented";
import { TrackedMarkdown } from "./tracked-markdown";

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

/** Read: the document as it is. Marked: Markdown rendered with the words changed marked in place. By line: a line diff. */
export type DocMode = "read" | "tracked" | "lines";

/**
 * A text document of a chat or a run, read beside the conversation (web/DESIGN.md, Chats), from the versions its
 * page lists (`lib/doc-versions.ts`). A Markdown document has three views: Read (rendered), Marked (rendered, the
 * words the shown version added on `success-soft` in `success`, those it removed in `danger`, struck through, as
 * `tracked-markdown.tsx` draws them) and By line (`doc-diff.tsx`); any other text (plain, JSON, CSV, YAML, code) has
 * Read, in mono, and By line, which also tints the words that changed in a line. A document with an earlier version
 * opens on its changes: the latest against the one before. When a new version comes while it is open (an agent sent
 * back the file it fixed, under the same name), it moves to it by itself, says so, and shows its changes against the
 * version that was shown. It takes the focus when it opens; Close gives it back to what opened it.
 */
export function DocPanel({
  name,
  versions,
  pending = false,
  authorOf,
  onClose,
  className,
}: {
  name: string;
  /** Oldest first. */
  versions: readonly DocVersion[];
  /** The list is still being read: nothing is said to be gone yet. */
  pending?: boolean;
  /** Who sent a version, as the page names people and agents. */
  authorOf: (version: DocVersion) => string;
  onClose: () => void;
  className?: string;
}) {
  const t = useTranslations("chats.doc");
  const latest = versions.at(-1) ?? null;
  const latestKey = latest?.key ?? null;
  const markdown = textKind(latest?.file ?? { name, media_type: "" }) === "markdown";
  const changeMode: DocMode = markdown ? "tracked" : "lines";

  // The version shown (null: the latest) and the one it is compared with (null: the one right before it).
  const [shownKey, setShownKey] = useState<string | null>(null);
  const [baseKey, setBaseKey] = useState<string | null>(null);
  // The view the reader picked; until then a document with an earlier version shows its changes.
  const [picked, setPicked] = useState<DocMode | null>(null);
  const [reloaded, setReloaded] = useState(false);
  const [seenLatest, setSeenLatest] = useState<string | null>(latestKey);
  // A new version came while the panel is open: it moves to it and shows what changed against the one shown.
  if (latestKey !== seenLatest) {
    setSeenLatest(latestKey);
    if (latestKey !== null && seenLatest !== null && versions.some((version) => version.key === seenLatest)) {
      const viewing = shownKey ?? seenLatest;
      setBaseKey(viewing !== latestKey ? viewing : (versions.at(-2)?.key ?? null));
      setShownKey(null);
      setPicked(changeMode);
      setReloaded(true);
    }
  }

  const shownIndex = shownKey === null ? -1 : versions.findIndex((version) => version.key === shownKey);
  const shown = shownIndex >= 0 ? versions[shownIndex] : latest;
  const earlier = versions.slice(0, shownIndex >= 0 ? shownIndex : Math.max(versions.length - 1, 0));
  const base = earlier.find((version) => version.key === baseKey) ?? earlier.at(-1) ?? null;
  let mode: DocMode = picked ?? (earlier.length > 0 ? changeMode : "read");
  if (mode === "tracked" && !markdown) mode = "lines";
  if (base === null) mode = "read";

  const comparing = mode !== "read" && base !== null;
  const tooBig = shown !== null && (shown.file.size > MAX_DOC_BYTES || (comparing && base.file.size > MAX_DOC_BYTES));
  const current = useDocText(shown);
  const before = useDocText(comparing ? base : null);
  const ready = current.data !== undefined && (!comparing || before.data !== undefined);
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

  const label = (version: DocVersion, index: number) => t("version", { number: index + 1, author: authorOf(version), at: new Date(version.at) });
  const modes: { value: DocMode; label: string; testId: string }[] = [{ value: "read", label: t("read"), testId: "doc-mode-read" }];
  if (earlier.length > 0) {
    if (markdown) modes.push({ value: "tracked", label: t("tracked"), testId: "doc-mode-tracked" });
    modes.push({ value: "lines", label: t("lines"), testId: "doc-mode-lines" });
  }

  return (
    <section
      ref={panel}
      tabIndex={-1}
      aria-labelledby="doc-title"
      className={cn("flex min-h-0 flex-col overflow-hidden outline-none", className)}
      data-testid="doc-panel"
      data-doc={name}
      data-mode={shown ? mode : undefined}
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
          <Segmented<DocMode> label={t("modeLabel")} value={mode} onChange={setPicked} options={modes} />
          <label className="flex min-w-0 max-w-full items-center gap-1.5">
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
            <label className="flex min-w-0 max-w-full items-center gap-1.5">
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
        {reloaded && latest ? t("reloaded", { author: authorOf(latest) }) : ""}
      </p>
      <div className="min-h-0 flex-1 overflow-auto bg-card" tabIndex={0} role="region" aria-label={t("bodyLabel", { name })} data-testid="doc-body">
        {!shown ? (
          pending ? (
            <p role="status" className="p-4 text-sm text-muted-foreground">
              {t("loading")}
            </p>
          ) : (
            <p className="p-4 text-sm text-muted-foreground">{t("gone")}</p>
          )
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
        ) : !ready || current.data === undefined ? (
          <p role="status" className="p-4 text-sm text-muted-foreground">
            {t("loading")}
          </p>
        ) : mode === "tracked" && before.data !== undefined ? (
          <div className="flex flex-col gap-3 px-4 py-3 sm:px-6">
            <DocDiffLegend tracked />
            <TrackedMarkdown before={before.data} after={current.data} className="max-w-[72ch]" testId="doc-tracked" />
          </div>
        ) : mode === "lines" && lines ? (
          <div className="flex flex-col gap-2 p-2">
            <DocDiffLegend />
            <DocDiff lines={lines} />
          </div>
        ) : markdown ? (
          <div className="px-4 py-3 sm:px-6">
            <SafeMarkdown className="max-w-[72ch]" testId="doc-markdown">
              {current.data}
            </SafeMarkdown>
          </div>
        ) : (
          <pre className="px-4 py-3 font-mono text-[13px] leading-5 whitespace-pre-wrap text-foreground [overflow-wrap:anywhere]" data-testid="doc-text">
            {current.data}
          </pre>
        )}
      </div>
    </section>
  );
}
