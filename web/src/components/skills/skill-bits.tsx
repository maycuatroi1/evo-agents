"use client";

import { Check, Copy, ExternalLink, FolderGit2, GitCommitHorizontal, Globe } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useRef } from "react";

import { Tag, useClipboard } from "@/components/data/identifier";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { shortHash, sizeParts } from "./format";
import { githubCommitUrl, type RepoOrigin } from "./source";

export function ByteSize({ bytes }: { bytes: number }) {
  const format = useFormatter();
  const t = useTranslations("skills");
  const { value, unit } = sizeParts(bytes);
  return (
    <span className="tabular-nums" title={t("bytes", { count: bytes })}>
      {format.number(value, {
        style: "unit",
        unit,
        // "230 bytes", not the short form's "230 byte"; kB and MB keep their symbols.
        unitDisplay: unit === "byte" ? "long" : "short",
        maximumFractionDigits: unit === "byte" ? 0 : 1,
      })}
    </span>
  );
}

/** A SHA-256 shortened for reading, whole for copying and for assistive technology. */
export function HashText({ sha256, full = false }: { sha256: string; full?: boolean }) {
  const t = useTranslations("skills");
  const code = useRef<HTMLElement>(null);
  const { state, copy } = useClipboard(sha256, code, { copied: t("copied") });
  const copied = state === "copied";
  return (
    <span className="inline-flex max-w-full items-center gap-1">
      <code
        ref={code}
        className={cn("rounded bg-muted px-1.5 py-0.5 font-mono text-xs", full && "[overflow-wrap:anywhere]")}
        title={sha256}
        data-sha256={sha256}
      >
        {full ? (
          sha256
        ) : (
          <>
            <span aria-hidden="true">{shortHash(sha256)}…</span>
            <span className="sr-only">{sha256}</span>
          </>
        )}
      </code>
      <Button type="button" variant="ghost" size="icon-sm" onClick={() => void copy()} aria-label={t("copyHash")}>
        {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      </Button>
    </span>
  );
}

/** Where a version was published from, linked to the commit on GitHub when the hub can tell which repo it is. */
export function SourceLink({
  repo,
  commit,
  repos = [],
}: {
  repo: string | null;
  commit: string | null;
  repos?: readonly RepoOrigin[];
}) {
  const t = useTranslations("skills");
  if (!repo || !commit) return <span className="text-muted-foreground">{t("noSource")}</span>;
  const url = githubCommitUrl(repo, commit, repos);
  const text = (
    <>
      <GitCommitHorizontal className="mr-1 inline size-3.5 align-[-3px]" aria-hidden="true" />
      <span className="[overflow-wrap:anywhere]">{repo}</span>
      <span className="text-muted-foreground">@</span>
      <span className="whitespace-nowrap">
        {shortHash(commit, 7)}
        {url ? <ExternalLink className="ml-1 inline size-3 align-[-2px]" aria-hidden="true" /> : null}
      </span>
    </>
  );
  if (!url) {
    return (
      <span className="font-mono text-xs" title={`${repo}@${commit}`}>
        {text}
      </span>
    );
  }
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="font-mono text-xs text-brand underline-offset-4 hover:underline"
      title={`${repo}@${commit}`}
      data-testid="source-link"
    >
      {text}
      <span className="sr-only"> {t("onGitHub")}</span>
    </a>
  );
}

export function ScopeBadge({ project }: { project: string | null }) {
  const t = useTranslations("skills");
  return project === null ? (
    <Tag>
      <Globe aria-hidden="true" />
      {t("scopeGlobal")}
    </Tag>
  ) : (
    <Tag>
      <FolderGit2 aria-hidden="true" />
      {t("scopeProject", { project })}
    </Tag>
  );
}
