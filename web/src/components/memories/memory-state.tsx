"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Archive, ArrowUpCircle, FileWarning } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { When } from "@/components/data/when";
import { InlineError, useWriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { projectHref } from "@/components/shell/nav";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { agentHref } from "@/lib/agent-queries";
import { browserApi } from "@/lib/api/browser";

import { memoryStatus } from "./memory-meta";
import { type Memory, memoryKeys, restoreMemory } from "./queries";

const LINK = "font-medium underline underline-offset-2";

/** The project role that may restore an archived memory. */
const writes = (role: string | null | undefined) => role === "writer" || role === "admin";

/**
 * Where an agent's memory stands, above its content: archived by the daily upkeep (out of its runs' index until a
 * writer restores it, which a writer of the project does here), stale (a path it cites is gone), or promoted by the
 * Curator into a shared memory, linked. Nothing for an active memory or one that is no agent's.
 */
export function MemoryStateNote({ memory, role }: { memory: Memory; role: string | null | undefined }) {
  const t = useTranslations("memories.state");
  if (!memory.agent || memory.deleted) return null;
  const status = memoryStatus(memory);
  if (status === "archived") {
    return (
      <Alert role="note" data-testid="memory-archived">
        <Archive aria-hidden="true" />
        <AlertTitle>{t("archivedTitle")}</AlertTitle>
        <AlertDescription>
          <p>{t("archivedDescription", { handle: memory.agent })}</p>
          {writes(role) ? <Restore memory={memory} /> : <p>{t("archivedReader")}</p>}
        </AlertDescription>
      </Alert>
    );
  }
  if (status === "promoted") {
    const into = memory.promoted_into;
    return (
      <Alert role="note" data-testid="memory-promoted">
        <ArrowUpCircle aria-hidden="true" />
        <AlertTitle>{t("promotedTitle")}</AlertTitle>
        <AlertDescription>
          {into != null && memory.project ? (
            <p>
              {t.rich("promotedInto", {
                link: (chunks) => (
                  <Link href={projectHref(memory.project ?? "", `memories/${into}`)} className={LINK} data-testid="memory-promoted-into">
                    {chunks}
                  </Link>
                ),
                id: into,
              })}
            </p>
          ) : (
            <p>{t("promotedDescription")}</p>
          )}
        </AlertDescription>
      </Alert>
    );
  }
  if (status === "stale") {
    return (
      <Alert role="note" data-testid="memory-stale">
        <FileWarning aria-hidden="true" />
        <AlertTitle>{t("staleTitle")}</AlertTitle>
        <AlertDescription>{t("staleDescription")}</AlertDescription>
      </Alert>
    );
  }
  return null;
}

function Restore({ memory }: { memory: Memory }) {
  const t = useTranslations("memories.state");
  const failure = useWriteFailure();
  const queryClient = useQueryClient();
  const restore = useMutation({
    mutationFn: () => restoreMemory(browserApi(), memory.id),
    onSuccess: (restored) => {
      queryClient.setQueryData(memoryKeys.one(memory.id), restored);
      notify({ tone: "success", text: t("restored", { name: memory.name }), description: t("restoredText") });
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: memoryKeys.all }),
  });
  const failed = restore.error ? failure(restore.error) : null;
  return (
    <div className="mt-2 flex flex-col items-start gap-2">
      <Button size="sm" variant="outline" onClick={() => restore.mutate()} busy={restore.isPending} data-testid="memory-restore">
        <Archive aria-hidden="true" />
        {t("restore")}
      </Button>
      {failed ? <InlineError text={`${t("restoreFailed")} ${failed.text}`} detail={failed.detail} requestId={failed.requestId} /> : null}
    </div>
  );
}

/** The rows of a memory's details only an agent's memory has: its agent, linked, and when a run of it last read it. */
export function AgentFacts({ memory }: { memory: Memory }) {
  const t = useTranslations("memories.state");
  if (!memory.agent) return null;
  return (
    <>
      <dt className="text-muted-foreground">{t("agent")}</dt>
      <dd className="min-w-0 [overflow-wrap:anywhere]">
        {memory.project ? (
          <Link href={agentHref(memory.project, memory.agent)} className={`font-mono ${LINK}`} data-testid="memory-agent">
            @{memory.agent}
          </Link>
        ) : (
          <span className="font-mono">@{memory.agent}</span>
        )}
      </dd>
      <dt className="text-muted-foreground">{t("read")}</dt>
      <dd className="min-w-0">
        <When value={memory.read_at} never={t("neverRead")} />
      </dd>
    </>
  );
}
