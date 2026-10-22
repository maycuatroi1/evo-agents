"use client";

import { useQuery } from "@tanstack/react-query";
import { FileText, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";

import { SafeMarkdown } from "@/components/data/markdown";
import { Segmented } from "@/components/data/segmented";
import { Button } from "@/components/ui/button";
import { diffLines, diffStats } from "@/lib/line-diff";
import { cn } from "@/lib/utils";

import { DocDiff, DocDiffLegend } from "./doc-diff";
import { docVersions, type DocVersion } from "./doc-model";
import type { Feed } from "./feed-model";
import { useAuthorLabel } from "./message-parts";

async function readDoc(url: string, signal: AbortSignal): Promise<string> {
  const response = await fetch(url, { signal, credentials: "same-origin" });
  if (!response.ok) throw new Error(`the hub answered ${response.status}`);
  return new TextDecoder("utf-8").decode(await response.arrayBuffer());
}

function useDocText(version: DocVersion | null) {
  return useQuery({
    queryKey: ["chat-doc", version?.file.url, version?.file.sha256],
    queryFn: ({ signal }) => readDoc(version!.file.url, signal),
    enabled: version !== null,
    staleTime: Infinity,
    retry: 1,
  });
}

type Mode = "read" | "changes";

/**
 * A Markdown document of the chat, read beside it while the composer stays in reach (web/DESIGN.md, Chats). It shows
 * the document's latest version; when a new one comes (an agent sent back the file it fixed, under the same name) it
 * reloads by itself and shows what changed against the version read before: green (+) lines added, red (−) lines
 * removed or replaced, plain lines kept around them. Changes compares any version with an earlier one.
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
  const latestId = latest?.file.id ?? null;

  // The version shown and the one it is compared with: on open, the latest and the one before it.
  const [shownId, setShownId] = useState<number | null>(latestId);
  const [baseId, setBaseId] = useState<number | null>(
    () => versions.at(-2)?.file.id ?? null,
  );
  const [mode, setMode] = useState<Mode>("read");
  const [reloaded, setReloaded] = useState(false);
  const [seenLatest, setSeenLatest] = useState<number | null>(latestId);
  // A new version came while the panel is open: it reloads to it and shows what changed against the one read.
  if (latestId !== seenLatest) {
    setSeenLatest(latestId);
    if (latestId !== null) {
      setBaseId(
        shownId !== latestId ? shownId : (versions.at(-2)?.file.id ?? null),
      );
      setShownId(latestId);
      if (seenLatest !== null) {
        setMode("changes");
        setReloaded(true);
      }
    }
  }

  const shownIndex = versions.findIndex(
    (version) => version.file.id === shownId,
  );
  const shown = shownIndex >= 0 ? versions[shownIndex] : latest;
  const earlier = versions.slice(
    0,
    shownIndex >= 0 ? shownIndex : versions.length - 1,
  );
  const base =
    earlier.find((version) => version.file.id === baseId) ??
    earlier.at(-1) ??
    null;

  const current = useDocText(shown);
  const before = useDocText(mode === "changes" ? base : null);
  if (mode === "changes" && earlier.length === 0) setMode("read");
  const lines = useMemo(
    () =>
      mode === "changes" &&
      current.data !== undefined &&
      before.data !== undefined
        ? diffLines(before.data, current.data)
        : null,
    [mode, current.data, before.data],
  );
  const stats = lines ? diffStats(lines) : null;

  useEffect(() => {
    if (!reloaded) return;
    const timer = window.setTimeout(() => setReloaded(false), 8000);
    return () => window.clearTimeout(timer);
  }, [reloaded, latestId]);

  const label = (version: DocVersion, index: number) =>
    t("version", {
      number: index + 1,
      author: authorLabel(version.message.author, me),
      at: new Date(version.message.created_at),
    });

  return (
    <section
      aria-labelledby="doc-title"
      className={cn("flex min-h-0 flex-col overflow-hidden", className)}
      data-testid="doc-panel"
      data-doc={name}
    >
      <header className="flex items-center gap-2 border-b px-4 py-2.5">
        <FileText
          className="size-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <h2
          id="doc-title"
          className="min-w-0 flex-1 truncate text-sm font-semibold"
          title={name}
        >
          {name}
        </h2>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={t("close")}
          onClick={onClose}
          data-testid="doc-close"
        >
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
              ...(earlier.length > 0
                ? [
                    {
                      value: "changes" as const,
                      label: t("changes"),
                      testId: "doc-mode-changes",
                    },
                  ]
                : []),
            ]}
          />
          <label className="flex min-w-0 items-center gap-1.5">
            <span className="sr-only">{t("shownLabel")}</span>
            <select
              className="h-7 max-w-[16rem] truncate rounded-sm border bg-card px-1.5"
              value={shown.file.id}
              onChange={(event) => {
                setShownId(Number(event.target.value));
                setBaseId(null);
              }}
              data-testid="doc-version"
            >
              {versions.map((version, index) => (
                <option key={version.file.id} value={version.file.id}>
                  {label(version, index)}
                </option>
              ))}
            </select>
          </label>
          {mode === "changes" && earlier.length > 0 ? (
            <label className="flex min-w-0 items-center gap-1.5">
              <span className="text-muted-foreground">{t("against")}</span>
              <select
                className="h-7 max-w-[16rem] truncate rounded-sm border bg-card px-1.5"
                value={base?.file.id}
                onChange={(event) => setBaseId(Number(event.target.value))}
                data-testid="doc-base"
              >
                {earlier.map((version, index) => (
                  <option key={version.file.id} value={version.file.id}>
                    {label(version, index)}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {stats ? (
            <span
              className="ml-auto font-mono tabular-nums"
              data-testid="doc-stats"
            >
              <span className="text-green-700 dark:text-green-300">
                +{stats.added}
              </span>{" "}
              <span className="text-red-700 dark:text-red-300">
                −{stats.removed}
              </span>
            </span>
          ) : null}
        </div>
      ) : null}
      <p
        role="status"
        aria-live="polite"
        className={cn(
          "border-b bg-brand-soft px-4 py-1.5 text-xs",
          !reloaded && "sr-only",
        )}
        data-testid="doc-reloaded"
      >
        {reloaded && latest
          ? t("reloaded", { author: authorLabel(latest.message.author, me) })
          : ""}
      </p>
      <div
        className="min-h-0 flex-1 overflow-auto bg-card"
        tabIndex={0}
        role="region"
        aria-label={t("bodyLabel", { name })}
        data-testid="doc-body"
      >
        {!shown ? (
          <p className="p-4 text-sm text-muted-foreground">{t("gone")}</p>
        ) : current.isError || before.isError ? (
          <p className="p-4 text-sm text-muted-foreground">
            {t("readFailed")}{" "}
            <Button
              type="button"
              variant="link"
              size="sm"
              className="h-auto px-0"
              onClick={() =>
                void Promise.all([current.refetch(), before.refetch()])
              }
            >
              {t("retry")}
            </Button>
          </p>
        ) : current.data === undefined ||
          (mode === "changes" && lines === null) ? (
          <p role="status" className="p-4 text-sm text-muted-foreground">
            {t("loading")}
          </p>
        ) : mode === "changes" && lines ? (
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
