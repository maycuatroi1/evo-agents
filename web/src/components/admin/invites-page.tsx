"use client";

import { useQuery } from "@tanstack/react-query";
import { Link2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { notify } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectsQuery } from "@/lib/queries";

import { adminInvitesQuery } from "./invite-data";
import { InviteDialog } from "./invite-dialog";
import { InvitesTable } from "./invites-table";

/** Invite links: making one (its link shown once), and the list of them, newest first, with revoke. */
export function AdminInvites({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("admin.invites");
  const state = useHubQuery(adminInvitesQuery(browserApi), initialError);
  const { data: projects } = useQuery(projectsQuery(browserApi));
  const [open, setOpen] = useState(false);
  const create = (
    <Button type="button" onClick={() => setOpen(true)} disabled={projects === undefined} data-testid="invite-open">
      <Link2 aria-hidden="true" />
      {t("new")}
    </Button>
  );

  return (
    <>
      <PageHeader
        title={t("title")}
        sub={t("sub")}
        tags={state.status === "success" ? <Badge variant="secondary">{t("count", { count: state.data.length })}</Badge> : null}
        actions={state.status === "success" ? create : null}
      />
      <section aria-label={t("caption")}>
        <QueryView state={state} loading={<TableSkeleton rows={6} />}>
          {(invites) =>
            invites.length === 0 ? (
              <EmptyState icon={Link2} title={t("emptyTitle")} description={t("emptyDescription")} />
            ) : (
              <DataCard>
                <InvitesTable invites={invites} caption={t("caption")} onNotice={notify} />
              </DataCard>
            )
          }
        </QueryView>
      </section>
      {projects ? <InviteDialog open={open} onOpenChange={setOpen} projects={projects} /> : null}
    </>
  );
}
