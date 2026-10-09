"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ExternalLink, FileCode, MessagesSquare, ScrollText } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Identifier } from "@/components/data/identifier";
import { Verbatim } from "@/components/plans/prose";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Ago } from "@/components/workers/ago";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { digestEntry, type EvidenceView, evidenceView, type RepoInfo } from "./model";
import { digestQuery } from "./queries";

/**
 * A finding's or a proposal's evidence, each piece where it leads: a run's event opens the run's log; a session's
 * digest shows the entry it cites, read when asked for (the digest is the project's, under its label); a line of code
 * opens on the forge at the commit the review run read, or the repo's default branch.
 */

const ICONS = { run: ScrollText, session: MessagesSquare, code: FileCode, other: ScrollText } as const;

function SessionEvidence({ project, piece }: { project: string; piece: Extract<EvidenceView, { kind: "session" }> }) {
  const t = useTranslations("curator.evidence");
  const ids = useId();
  const [open, setOpen] = useState(false);
  const digest = useQuery({ ...digestQuery(browserApi, project, piece.session), enabled: open });
  const entry = digest.data ? digestEntry(digest.data.digest as Record<string, unknown>, piece.field, piece.index) : null;
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-muted-foreground">{t("session")}</span>
        <Identifier value={piece.session} title={piece.session} className="max-w-56">
          {piece.session.slice(0, 13)}
        </Identifier>
        {piece.field !== null && piece.index !== null ? (
          <span className="text-muted-foreground">{t("entry", { field: piece.field, index: piece.index })}</span>
        ) : null}
        <Button
          type="button"
          variant="link"
          size="sm"
          className="h-auto px-0 text-xs"
          aria-expanded={open}
          aria-controls={`${ids}-digest`}
          onClick={() => setOpen(!open)}
          data-testid="evidence-digest-toggle"
        >
          <ChevronDown className={cn("transition-transform duration-base motion-reduce:transition-none", open && "rotate-180")} aria-hidden="true" />
          {open ? t("hideEntry") : t("showEntry")}
        </Button>
      </div>
      <div id={`${ids}-digest`} hidden={!open}>
        {open ? (
          digest.isPending ? (
            <div role="status" aria-label={t("loadingDigest")} className="flex flex-col gap-1.5">
              <Skeleton className="h-3 w-48" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : digest.isError ? (
            <p className="text-xs text-danger" data-testid="evidence-digest-error">
              {digest.error.status === 404 ? t("digestGone") : t("digestFailed")}
            </p>
          ) : digest.data ? (
            <div className="flex flex-col gap-1.5" data-testid="evidence-digest">
              <p className="text-xs text-fg-subtle">
                {t.rich("digestBy", {
                  login: digest.data.login,
                  messages: digest.data.messages,
                  cwd: digest.data.cwd,
                  ago: () => <Ago value={digest.data.updated_at} never="-" />,
                })}
              </p>
              {entry !== null ? <Verbatim className="max-h-48 overflow-y-auto text-xs">{entry}</Verbatim> : <p className="text-xs text-muted-foreground">{t("entryMissing")}</p>}
            </div>
          ) : null
        ) : null}
      </div>
    </div>
  );
}

function Piece({ project, piece }: { project: string; piece: EvidenceView }) {
  const t = useTranslations("curator.evidence");
  if (piece.kind === "run") {
    return (
      <Link href={piece.href} className="font-medium text-brand underline-offset-4 hover:underline" data-testid="evidence-run-link">
        {t("run", { run: piece.runId, seq: piece.seq })}
      </Link>
    );
  }
  if (piece.kind === "session") return <SessionEvidence project={project} piece={piece} />;
  if (piece.kind === "code") {
    const where = `${piece.repo}: ${piece.path}${piece.line !== null ? `:${piece.line}` : ""}`;
    return (
      <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        {piece.url ? (
          <a
            href={piece.url}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-flex min-w-0 items-center gap-1 font-mono text-xs text-brand underline-offset-4 [overflow-wrap:anywhere] hover:underline"
            data-testid="evidence-code-link"
          >
            {where}
            <ExternalLink className="size-3.5 shrink-0" aria-hidden="true" />
            <span className="sr-only">{t("opensElsewhere")}</span>
          </a>
        ) : (
          <span className="font-mono text-xs [overflow-wrap:anywhere]" data-testid="evidence-code">
            {where}
          </span>
        )}
        {piece.commit ? <Identifier value={piece.commit}>{piece.commit.slice(0, 7)}</Identifier> : null}
      </span>
    );
  }
  return <code className="font-mono text-xs [overflow-wrap:anywhere]">{piece.text}</code>;
}

/** A list of evidence, each piece with its kind's icon and how the hub found it. */
export function EvidenceList({
  project,
  evidence,
  repos,
  testId = "evidence-list",
}: {
  project: string;
  evidence: readonly Record<string, unknown>[];
  repos: readonly RepoInfo[];
  testId?: string;
}) {
  const t = useTranslations("curator.evidence");
  if (evidence.length === 0) return <p className="text-[13px] text-muted-foreground">{t("none")}</p>;
  return (
    <ul className="flex flex-col divide-y rounded-md border" data-testid={testId}>
      {evidence.map((raw, index) => {
        const piece = evidenceView(raw, project, repos);
        const Icon = ICONS[piece.kind];
        const resolved = typeof raw.resolved === "string" ? raw.resolved : null;
        return (
          <li key={index} className="flex min-w-0 items-start gap-2.5 px-3 py-2.5 text-[13px]" data-kind={piece.kind} data-testid="evidence">
            <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              <Piece project={project} piece={piece} />
              {resolved ? <span className="text-xs text-fg-subtle">{t.has(`resolved.${resolved}` as never) ? t(`resolved.${resolved}` as never) : resolved}</span> : null}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
