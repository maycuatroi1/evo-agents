"use client";

import { CircleDashed, Send, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";

import { type SrsDocument, type SrsVersion, submitVersion } from "./api";
import { Banner } from "./banner";
import { signInAgainOn401, useDocumentRefresh } from "./hooks";
import { returnedBy } from "./model";

function messages(error: unknown): string[] {
  if (!isApiError(error)) return [];
  return (error.info.detail ?? [])
    .map((entry) => (entry && typeof entry === "object" ? (entry as { message?: unknown }).message : null))
    .filter((message): message is string => typeof message === "string");
}

/**
 * A draft waiting for its writers: who wrote it and why, the request for changes that sent it back when one did, and,
 * for a writer, Submit for review. The hub refuses a draft with an error (422 with its problems), said here.
 */
export function DraftBanner({ project, doc, version, writer }: { project: string; doc: SrsDocument; version: SrsVersion; writer: boolean }) {
  const t = useTranslations("srs.draft");
  const failure = useWriteFailure();
  const refresh = useDocumentRefresh(project, doc.slug);
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<{ failure: WriteFailure; problems: string[] } | null>(null);
  const returned = returnedBy(version);

  const submit = async () => {
    setBusy(true);
    setRefused(null);
    try {
      await submitVersion(browserApi(), project, doc.slug, version.number);
      await refresh();
      notify({ tone: "success", text: t("submitted", { version: version.number }), description: t("submittedText") });
    } catch (error) {
      signInAgainOn401(error);
      setRefused({ failure: failure(error), problems: messages(error) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-2" data-testid="srs-draft">
      <Banner
        icon={returned ? Undo2 : CircleDashed}
        tone={returned ? "attention" : "neutral"}
        title={
          returned
            ? t("returned", { version: version.number, reviewer: returned.reviewer })
            : t("title", { version: version.number, author: version.author })
        }
        actions={
          writer ? (
            <Button type="button" size="sm" busy={busy} onClick={() => void submit()} data-testid="srs-submit">
              <Send aria-hidden="true" />
              {t("submit", { version: version.number })}
            </Button>
          ) : null
        }
      >
        {returned?.body ? <p className="whitespace-pre-wrap">{returned.body}</p> : null}
        <p>{version.summary || t("noSummary")}</p>
      </Banner>
      {refused ? (
        <div className="flex flex-col gap-2">
          <InlineError text={refused.failure.text} detail={refused.failure.detail} requestId={refused.failure.requestId} />
          {refused.problems.length ? (
            <ul className="list-disc pl-6 text-[13px] text-danger" data-testid="srs-submit-problems">
              {refused.problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
