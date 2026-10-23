"use client";

import { UsersRound } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { Card } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

import { CRM_STAGES, type CrmContacts, type CrmStage, contactHref, crmContactsQuery } from "./api";
import { ReaderNote, StageBadge, useCrmRole, useKindWord } from "./chrome";

/**
 * The pipeline by stage: a button per stage with how many contacts are in it, in the order a contact moves through
 * them, and All. The one pressed filters the list below; the counts are the hub's, over every contact the visitor sees.
 */
function Pipeline({ counts, total, picked, onPick }: { counts: Record<string, number>; total: number; picked: CrmStage | null; onPick: (stage: CrmStage | null) => void }) {
  const t = useTranslations("crm");
  const cells: { id: CrmStage | null; label: string; count: number }[] = [
    { id: null, label: t("all"), count: total },
    ...CRM_STAGES.map((stage) => ({ id: stage, label: t(`stages.${stage}`), count: counts[stage] ?? 0 })),
  ];
  return (
    <section aria-labelledby="crm-pipeline-title" className="flex flex-col gap-3">
      <h2 id="crm-pipeline-title" className="text-[15px] leading-[22px] font-semibold">
        {t("pipeline")}
      </h2>
      <div role="group" aria-label={t("pipelineFilter")} className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5" data-testid="crm-pipeline">
        {cells.map((cell) => (
          <button
            key={cell.id ?? "all"}
            type="button"
            aria-pressed={picked === cell.id}
            onClick={() => onPick(cell.id)}
            className={cn(
              "flex min-h-11 min-w-0 flex-col items-start gap-0.5 rounded-md border bg-card px-3 py-2 text-left transition-colors hover:bg-accent",
              picked === cell.id && "border-brand ring-1 ring-brand",
            )}
            data-testid={`crm-stage-${cell.id ?? "all"}`}
          >
            <span className="text-[13px] text-muted-foreground">{cell.label}</span>
            <span className="text-xl font-semibold tabular-nums" data-testid="crm-stage-count">
              {cell.count}
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}

function ContactTable({ project, data, picked }: { project: string; data: CrmContacts; picked: CrmStage | null }) {
  const t = useTranslations("crm");
  const word = useKindWord();
  const rows = picked ? data.contacts.filter((contact) => contact.stage === picked) : data.contacts;
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={UsersRound}
        title={picked ? t("emptyStage", { stage: t(`stages.${picked}`) }) : t("emptyTitle")}
        description={t("emptyText")}
        testId="crm-empty"
      />
    );
  }
  return (
    <Card className="min-w-0 gap-0 py-0">
      <Table scrollLabel={t("tableLabel")} data-testid="crm-contacts">
        <TableHeader>
          <TableRow>
            <TableHead>{t("columns.login")}</TableHead>
            <TableHead>{t("columns.stage")}</TableHead>
            <TableHead>{t("columns.source")}</TableHead>
            <TableHead>{t("columns.firstSeen")}</TableHead>
            <TableHead>{t("columns.stageAt")}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((contact) => (
            <TableRow key={contact.login} data-testid="crm-contact-row">
              <TableCell className="max-w-64 [overflow-wrap:anywhere] whitespace-normal">
                <Link href={contactHref(project, contact.login)} className="font-mono text-brand underline underline-offset-4 hover:text-brand-hover" data-testid="crm-contact-link">
                  {contact.login}
                </Link>
              </TableCell>
              <TableCell>
                <StageBadge stage={contact.stage} />
              </TableCell>
              <TableCell>{word(contact.source)}</TableCell>
              <TableCell>
                <When value={contact.first_seen_at} short />
              </TableCell>
              <TableCell>
                <When value={contact.stage_at} short />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Card>
  );
}

/**
 * The contacts of a project (/p/{project}/x/crm/contacts): the people who found it themselves, the pipeline by stage,
 * then the list, each opening its touchpoints and history. Readers see a note that they only read.
 */
export function ContactsPage({ project, title, initialError }: { project: string; title: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("crm");
  const role = useCrmRole(project);
  const [picked, setPicked] = useState<CrmStage | null>(null);
  const state = useHubQuery(crmContactsQuery(browserApi, project), initialError, { live: true });
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="crm-contacts-page">
      <PageHeader title={title} sub={t("sub")} />
      {role === "reader" ? <ReaderNote project={project} /> : null}
      <QueryView state={state} loading={<ListSkeleton rows={6} metrics={5} />}>
        {(data) => (
          <>
            <Pipeline counts={data.counts} total={data.contacts.length} picked={picked} onPick={setPicked} />
            <ContactTable project={project} data={data} picked={picked} />
          </>
        )}
      </QueryView>
    </div>
  );
}
