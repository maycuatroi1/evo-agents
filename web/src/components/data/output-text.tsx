"use client";

import { useQuery } from "@tanstack/react-query";
import { FileWarning } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { Button } from "@/components/ui/button";
import { CSV_ROWS, parseCsv, prettyJson, TEXT_PREVIEW_BYTES } from "@/lib/output-text";
import { extensionOf, type RunOutput, textKind } from "@/lib/outputs";
import { cn } from "@/lib/utils";

import { SafeMarkdown } from "./markdown";
import { NoPreview } from "./output-frame";
import { useByteSize } from "./byte-size";

/**
 * A text output in the viewer: its bytes read from the hub's own origin (connect-src 'self'), the first
 * TEXT_PREVIEW_BYTES of a longer file through a byte range, then drawn by what it holds (`textKind`): Markdown through
 * SafeMarkdown (never HTML, no image loaded), JSON indented, a CSV or TSV as a table of its first CSV_ROWS rows, code
 * and plain text in mono. The hub answers every kind of text as text/plain, so nothing here runs.
 */

async function readText(url: string, size: number, signal: AbortSignal): Promise<{ text: string; cut: boolean }> {
  const cut = size > TEXT_PREVIEW_BYTES;
  const response = await fetch(url, {
    signal,
    credentials: "same-origin",
    headers: cut ? { Range: `bytes=0-${TEXT_PREVIEW_BYTES - 1}` } : undefined,
  });
  if (!response.ok) throw new Error(`the hub answered ${response.status}`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  // A range may end inside a character: decoding without `fatal` turns its bytes into U+FFFD, which is dropped.
  const text = new TextDecoder("utf-8").decode(bytes);
  return { text: cut ? text.replace(/�+$/, "") : text, cut };
}

function CsvTable({ text, delimiter }: { text: string; delimiter: string }) {
  const t = useTranslations("outputs.viewer");
  const { rows, more } = useMemo(() => parseCsv(text, delimiter), [text, delimiter]);
  const [head, ...body] = rows;
  if (!head) return <p className="p-4 text-sm text-muted-foreground">{t("emptyFile")}</p>;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-auto bg-card" tabIndex={0} role="region" aria-label={t("tableLabel")} data-testid="output-csv">
        <table className="min-w-full border-separate border-spacing-0 text-[13px] leading-5">
          <thead className="sticky top-0 bg-muted">
            <tr>
              {head.map((cell, index) => (
                <th key={index} scope="col" className="border-b px-3 py-2 text-left font-semibold whitespace-nowrap">
                  {cell}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, index) => (
              <tr key={index} className="hover:bg-accent">
                {head.map((_, column) => (
                  <td key={column} className="max-w-[24rem] border-b px-3 py-1.5 align-top [overflow-wrap:anywhere] tabular-nums">
                    {row[column] ?? ""}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {more ? <p className="shrink-0 border-t bg-card px-4 py-2 text-xs text-muted-foreground">{t("csvMore", { count: CSV_ROWS })}</p> : null}
    </div>
  );
}

export function OutputText({ output }: { output: RunOutput }) {
  const t = useTranslations("outputs.viewer");
  const size = useByteSize();
  const kind = textKind(output);
  const read = useQuery({
    queryKey: ["output-text", output.url, output.sha256],
    queryFn: ({ signal }) => readText(output.url, output.size, signal),
    staleTime: Infinity,
    retry: 1,
  });
  if (read.isPending) {
    return (
      <div role="status" className="flex flex-1 items-center justify-center text-sm text-muted-foreground" data-testid="output-text-loading">
        {t("loading")}
      </div>
    );
  }
  if (read.isError) {
    return (
      <NoPreview output={output} icon={FileWarning}>
        {t("readFailed")}{" "}
        <Button type="button" variant="link" size="sm" className="h-auto px-0" onClick={() => void read.refetch()}>
          {t("retry")}
        </Button>
      </NoPreview>
    );
  }
  const { text, cut } = read.data;
  const tsv = extensionOf(output.name) === "tsv" || output.media_type === "text/tab-separated-values";
  const mono = "min-h-0 flex-1 overflow-auto bg-card p-4 font-mono text-[13px] leading-5 whitespace-pre text-foreground [font-variant-ligatures:none]";
  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="output-text" data-kind={kind}>
      {kind === "markdown" ? (
        <div className="min-h-0 flex-1 overflow-auto bg-card px-4 py-3 sm:px-6" tabIndex={0} role="region" aria-label={t("textLabel")}>
          <SafeMarkdown className="max-w-[72ch]" testId="output-markdown">
            {text}
          </SafeMarkdown>
        </div>
      ) : kind === "csv" ? (
        <CsvTable text={text} delimiter={tsv ? "\t" : ","} />
      ) : (
        <pre className={cn(mono, kind === "plain" && "whitespace-pre-wrap [overflow-wrap:anywhere]")} tabIndex={0} aria-label={t("textLabel")} data-testid="output-pre">
          {kind === "json" ? prettyJson(text) : text}
        </pre>
      )}
      {cut ? (
        <p className="shrink-0 border-t bg-card px-4 py-2 text-xs text-muted-foreground" data-testid="output-text-cut">
          {t("textCut", { size: size(TEXT_PREVIEW_BYTES) })}
        </p>
      ) : null}
    </div>
  );
}
