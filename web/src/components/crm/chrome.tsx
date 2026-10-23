"use client";

import { useQuery } from "@tanstack/react-query";
import { Eye } from "lucide-react";
import { useTranslations } from "next-intl";

import { Banner } from "@/components/feedback/banner";
import { Badge } from "@/components/ui/badge";
import { browserApi } from "@/lib/api/browser";
import { whoamiQuery } from "@/lib/queries";

import { type CrmStage, isStage } from "./api";

export type CrmRole = "reader" | "writer" | "admin";

/** The visitor's role on the project, null until whoami answers (or for someone with no grant on it). */
export function useCrmRole(project: string): CrmRole | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  const role = data?.grants.find((grant) => grant.project === project)?.role ?? null;
  return role === "reader" || role === "writer" || role === "admin" ? role : null;
}

/** A reader's note: they read the contacts, and nothing on the page writes for them. */
export function ReaderNote({ project }: { project: string }) {
  const t = useTranslations("crm");
  return (
    <Banner icon={Eye} title={t("readerTitle", { project })} testId="crm-read-only">
      {t("readerText")}
    </Banner>
  );
}

/** The badge of each stage: lead plain, invited blue, activated and retained green; its word is always written. */
const STAGE_VARIANT: Record<CrmStage, "outline" | "info" | "success"> = {
  lead: "outline",
  invited: "info",
  activated: "success",
  retained: "success",
};

export function StageBadge({ stage }: { stage: string }) {
  const t = useTranslations("crm.stages");
  return (
    <Badge variant={isStage(stage) ? STAGE_VARIANT[stage] : "outline"} data-testid="crm-stage">
      {isStage(stage) ? t(stage) : stage}
    </Badge>
  );
}

/** Where a contact came from and what a touchpoint is (evo_agents.hub.tables_crm.KINDS). */
const KINDS = ["invite_request", "invite_link", "grant", "issue", "pull_request", "discussion", "note"] as const;
type Kind = (typeof KINDS)[number];

/** The words of a source or a touchpoint's kind, or the hub's own word for one this page does not know yet. */
export function useKindWord() {
  const t = useTranslations("crm.kinds");
  return (kind: string) => (KINDS.includes(kind as Kind) ? t(kind as Kind) : kind);
}
