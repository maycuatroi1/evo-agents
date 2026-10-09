"use client";

import { useQuery } from "@tanstack/react-query";
import { Bell, CheckCheck, Inbox, Lightbulb, MessageCircleQuestionMark, SearchX } from "lucide-react";
import type { Route } from "next";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { Pager } from "@/components/admin/pager";
import { usePagedQuery } from "@/components/admin/use-paged-query";
import { FacetGroup } from "@/components/data/facet-group";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { DecisionSheet } from "./decision-sheet";
import { useInboxViewer, useMarkRead, useReadFailure } from "./hooks";
import {
  type InboxFilters,
  inboxQuery,
  inboxSearch,
  isFiltered,
  NO_FILTERS,
  notificationOfDecision,
  notificationOfProposal,
  readInboxFilters,
  splitInbox,
  unreadIds,
} from "./model";
import { NotificationItem } from "./notification-item";
import { ProposalSheet } from "./proposal-sheet";
import { TelegramButton } from "./telegram-dialog";
import {
  MAX_READ_IDS,
  type Notification,
  type NotificationList,
  notificationCountQuery,
  notificationsQuery,
} from "./queries";

/**
 * The filters and the open decision in the URL, changed through the History API (Next.js syncs useSearchParams with
 * it, so no server round trip happens). A filter replaces the URL; opening or closing a decision pushes one, so Back
 * closes it again.
 */
function useInboxUrl() {
  const params = useSearchParams();
  const pathname = usePathname();
  const filters = readInboxFilters(params);
  const href = useCallback((next: InboxFilters) => `${pathname}${inboxSearch(next)}` as Route, [pathname]);
  const replace = (next: InboxFilters) => window.history.replaceState(null, "", href(next));
  const push = (next: InboxFilters) => window.history.pushState(null, "", href(next));
  return { filters, href, replace, push };
}

function ListSection({
  title,
  count,
  testId,
  children,
}: {
  title: string;
  count: number;
  testId: string;
  children: ReactNode;
}) {
  const t = useTranslations("inbox");
  const id = `${testId}-title`;
  return (
    <section aria-labelledby={id} className="flex flex-col gap-2.5" data-testid={testId}>
      <h2 id={id} className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
        {title}
        <span className="rounded-full bg-muted px-1.5 text-xs text-muted-foreground tabular-nums" aria-hidden="true">
          {count}
        </span>
        <span className="sr-only">{` (${t("count", { count })})`}</span>
      </h2>
      <ul className="flex flex-col gap-2.5">{children}</ul>
    </section>
  );
}

/**
 * The member's Inbox: the decisions the agents of their plan runs ask them and the tier 2 proposals of their projects'
 * Curator, open ones first, then the notices (a push or merge into a default branch with its repo, branch and commits, a
 * plan finished, a run failed) and the decisions and proposals already answered, expired or cancelled, newest first. A
 * proposal opens in a sheet of its own (`/inbox?proposal=ID`) with its evidence and, for an admin, its answers. Filters
 * by kind, project and unread live in the URL; a decision opens in a sheet over the list (`/inbox?decision=ID`) with
 * its answer form, a screen of its own on a phone (the kit's MobileDecision), and closes back to the list where it was.
 * The list and the bell are read every 10 seconds.
 */
export function InboxPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("inbox");
  const { filters, href, replace, push } = useInboxUrl();
  const viewer = useInboxViewer();
  // The page's main query: the top bar says from it whether the Inbox is current (every 10 seconds).
  const { state, stale } = usePagedQuery(notificationsQuery(browserApi, inboxQuery(filters)), initialError, { live: true });
  const count = useQuery(notificationCountQuery(browserApi));
  const markRead = useMarkRead();
  const readFailure = useReadFailure();
  const [reading, setReading] = useState<number | "all" | null>(null);
  const list: NotificationList | null = state.status === "success" ? state.data : null;
  const selected = filters.decision;
  const selectedNotification = selected !== null && list ? notificationOfDecision(list.notifications, selected) : null;
  const selectedProposal = filters.proposal;
  const proposalNotification = selectedProposal !== null && list ? notificationOfProposal(list.notifications, selectedProposal) : null;

  const read = (target: { ids: number[] } | { all: true }, marker: number | "all") => {
    setReading(marker);
    markRead.mutate(target, {
      onSettled: () => setReading(null),
      onSuccess: (result) =>
        void notify({ tone: "success", text: marker === "all" ? t("markedAll", { count: result.read }) : t("markedOne") }),
      onError: (error) => void notifyFailure(t("markFailed"), readFailure(error)),
    });
  };

  // Opening a decision or a proposal reads its notification, as answering it would; once per notification and visit.
  const marked = useRef(new Set<number>());
  const { mutate: markOne } = markRead;
  const opened = selectedNotification ?? proposalNotification;
  const openUnread = opened && opened.read_at === null ? opened.id : null;
  useEffect(() => {
    if (openUnread === null || marked.current.has(openUnread)) return;
    marked.current.add(openUnread);
    markOne({ ids: [openUnread] });
  }, [openUnread, markOne]);

  const openDecision = (id: number) => push({ ...filters, decision: id, proposal: null });
  // Closing pushes the list's URL, so Back opens the decision again, as Back from it closed it.
  const closeDecision = () => push({ ...filters, decision: null });
  const openProposal = (id: number) => push({ ...filters, proposal: id, decision: null });
  const closeProposal = () => push({ ...filters, proposal: null });
  const setFilters = (next: InboxFilters) => replace(next);

  const counts = count.data;
  const filtered = isFiltered(filters);
  const pageUnread = list ? unreadIds(list.notifications, MAX_READ_IDS) : [];
  const nothingToRead = filtered ? pageUnread.length === 0 : (counts?.unread ?? 0) === 0;
  const projects = viewer.projects;

  return (
    <>
      <PageHeader
        title={t("title")}
        tags={
          counts ? (
            <>
              <Badge variant={counts.unread > 0 ? "info" : "secondary"} data-testid="inbox-unread-count" data-count={counts.unread}>
                <Bell aria-hidden="true" />
                {t("unreadCount", { count: counts.unread })}
              </Badge>
              {counts.open_decisions > 0 ? (
                <Badge variant="warning" data-testid="inbox-open-count" data-count={counts.open_decisions}>
                  <MessageCircleQuestionMark aria-hidden="true" />
                  {t("openCount", { count: counts.open_decisions })}
                </Badge>
              ) : null}
              {(counts.open_proposals ?? 0) > 0 ? (
                <Badge variant="warning" data-testid="inbox-open-proposals" data-count={counts.open_proposals}>
                  <Lightbulb aria-hidden="true" />
                  {t("openProposals", { count: counts.open_proposals })}
                </Badge>
              ) : null}
            </>
          ) : null
        }
        actions={
          <>
            <TelegramButton />
            <Button
              type="button"
              variant="outline"
              className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
              aria-disabled={nothingToRead || reading !== null || undefined}
              busy={reading === "all"}
              onClick={() => {
                if (nothingToRead || reading !== null) return;
                if (filtered) read({ ids: pageUnread }, "all");
                else read({ all: true }, "all");
              }}
              data-testid="inbox-mark-all"
            >
              <CheckCheck aria-hidden="true" />
              {filtered ? t("markShown") : t("markAll")}
            </Button>
          </>
        }
      />
      <div className="flex flex-col gap-6">
        <section aria-labelledby="inbox-list-title" className="flex min-w-0 flex-col gap-4" data-testid="inbox-list">
          <h2 id="inbox-list-title" className="sr-only">
            {t("listTitle")}
          </h2>
          <div className="flex flex-col gap-3 rounded-md border bg-card shadow-raised p-4" role="search" aria-label={t("filters.label")} data-testid="inbox-filters">
            <FacetGroup
              label={t("filters.show")}
              options={[
                { value: null, label: t("filters.everything") },
                { value: "unread", label: t("filters.unread") },
              ]}
              selected={filters.unread ? "unread" : null}
              onSelect={(value) => setFilters({ ...filters, unread: value === "unread", page: 1 })}
              countLabel={(n) => t("count", { count: n })}
              testId="inbox-facet-unread"
            />
            <FacetGroup
              label={t("filters.kind")}
              options={[
                { value: null, label: t("filters.allKinds") },
                { value: "decision", label: t("filters.decisions"), icon: MessageCircleQuestionMark },
                { value: "notice", label: t("filters.notices"), icon: Bell },
                { value: "proposal", label: t("filters.proposals"), icon: Lightbulb },
              ]}
              selected={filters.kind}
              onSelect={(value) =>
                setFilters({ ...filters, kind: value === "decision" || value === "notice" || value === "proposal" ? value : null, page: 1 })
              }
              countLabel={(n) => t("count", { count: n })}
              testId="inbox-facet-kind"
            />
            {projects.length > 1 || filters.project !== null ? (
              <FacetGroup
                label={t("filters.project")}
                options={[
                  { value: null, label: t("filters.allProjects") },
                  ...[...new Set([...projects, ...(filters.project ? [filters.project] : [])])].sort().map((name) => ({ value: name, label: name, mono: true })),
                ]}
                selected={filters.project}
                onSelect={(value) => setFilters({ ...filters, project: value, page: 1 })}
                countLabel={(n) => t("count", { count: n })}
                testId="inbox-facet-project"
              />
            ) : null}
          </div>
          <QueryView state={state} loading={<TableSkeleton rows={4} />}>
            {(page) => (
              <InboxList
                page={page}
                filters={filters}
                stale={stale}
                selected={selected}
                selectedProposal={selectedProposal}
                reading={reading}
                decisionHref={(id) => href({ ...filters, decision: id, proposal: null })}
                proposalHref={(id) => href({ ...filters, proposal: id, decision: null })}
                onOpenDecision={openDecision}
                onOpenProposal={openProposal}
                onRead={(id) => read({ ids: [id] }, id)}
                onClear={() => setFilters({ ...NO_FILTERS, decision: filters.decision, proposal: filters.proposal })}
                onPage={(next) => setFilters({ ...filters, page: next })}
              />
            )}
          </QueryView>
        </section>
      </div>
      <DecisionSheet
        target={selected !== null ? { id: selected, project: selectedNotification?.project ?? null } : null}
        onClose={closeDecision}
        screen
        returnFocus={(id) => document.querySelector<HTMLElement>(`[data-decision-link="${id}"]`)}
      />
      <ProposalSheet
        target={selectedProposal !== null ? { id: selectedProposal, project: proposalNotification?.project ?? null } : null}
        onClose={closeProposal}
        returnFocus={(id) => document.querySelector<HTMLElement>(`[data-proposal-link="${id}"]`)}
      />
    </>
  );
}

function InboxList({
  page,
  filters,
  stale,
  selected,
  selectedProposal,
  reading,
  decisionHref,
  proposalHref,
  onOpenDecision,
  onOpenProposal,
  onRead,
  onClear,
  onPage,
}: {
  page: NotificationList;
  filters: InboxFilters;
  stale: boolean;
  selected: number | null;
  selectedProposal: number | null;
  reading: number | "all" | null;
  decisionHref: (id: number) => Route;
  proposalHref: (id: number) => Route;
  onOpenDecision: (id: number) => void;
  onOpenProposal: (id: number) => void;
  onRead: (id: number) => void;
  onClear: () => void;
  onPage: (page: number) => void;
}) {
  const t = useTranslations("inbox");
  const { waiting, rest } = splitInbox(page.notifications);
  const filtered = isFiltered(filters);
  const item = (notification: Notification) => (
    <NotificationItem
      key={notification.id}
      notification={notification}
      href={
        notification.kind === "proposal" && notification.proposal_id != null
          ? proposalHref(notification.proposal_id)
          : notification.decision_id !== null
            ? decisionHref(notification.decision_id)
            : null
      }
      selected={
        (notification.kind === "decision" && notification.decision_id === selected) ||
        (notification.kind === "proposal" && notification.proposal_id === selectedProposal)
      }
      onOpenDecision={onOpenDecision}
      onOpenProposal={onOpenProposal}
      onRead={onRead}
      reading={reading === notification.id || reading === "all"}
    />
  );
  return (
    <div className="flex flex-col gap-4" aria-busy={stale || undefined}>
      <p aria-live="polite" className="text-sm text-muted-foreground" data-testid="inbox-summary">
        {filtered ? t("summary.filtered", { count: page.total }) : t("summary.all", { count: page.total })}
      </p>
      {page.notifications.length === 0 ? (
        filtered || filters.page > 1 ? (
          <EmptyState icon={SearchX} title={t("noResults.title")} description={t("noResults.description")}>
            <Button variant="outline" onClick={onClear} data-testid="inbox-clear-filters">
              {t("noResults.clear")}
            </Button>
          </EmptyState>
        ) : (
          <EmptyState icon={Inbox} title={t("empty.title")} description={t("empty.description")} />
        )
      ) : (
        <>
          {waiting.length > 0 ? (
            <ListSection title={t("waitingTitle")} count={waiting.length} testId="inbox-waiting">
              {waiting.map(item)}
            </ListSection>
          ) : null}
          {rest.length > 0 ? (
            <ListSection title={waiting.length > 0 ? t("restTitle") : t("allTitle")} count={rest.length} testId="inbox-rest">
              {rest.map(item)}
            </ListSection>
          ) : null}
        </>
      )}
      <Pager
        label={t("pagerLabel")}
        page={filters.page}
        count={page.notifications.length}
        hasNext={page.offset + page.limit < page.total}
        canGoBack={filters.page > 1}
        atStart={filters.page === 1}
        busy={stale}
        onNext={() => onPage(filters.page + 1)}
        onPrevious={() => onPage(Math.max(1, filters.page - 1))}
        onFirst={() => onPage(1)}
      />
    </div>
  );
}

