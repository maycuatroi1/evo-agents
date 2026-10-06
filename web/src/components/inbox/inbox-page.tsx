"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Bell, CheckCheck, Inbox, Loader2, MessageCircleQuestionMark, SearchX, X } from "lucide-react";
import type { Route } from "next";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { NoticeArea, useNotice } from "@/components/admin/notice";
import { Pager } from "@/components/admin/pager";
import { usePagedQuery } from "@/components/admin/use-paged-query";
import { FacetGroup } from "@/components/data/facet-group";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView } from "@/components/states/query-view";
import { ApiErrorState, EmptyState, LoadingState, StatePanel, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { DecisionView } from "./decision-view";
import { useInboxViewer, useMarkRead, useReadFailure } from "./hooks";
import {
  type InboxFilters,
  inboxQuery,
  inboxSearch,
  isFiltered,
  NO_FILTERS,
  notificationOfDecision,
  readInboxFilters,
  splitInbox,
  unreadIds,
} from "./model";
import { NotificationItem } from "./notification-item";
import {
  decisionQuery,
  locateDecisionQuery,
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
 * The decision shown beside the list. Its project comes from its notification on the page when there is one; a link
 * that names only the decision (`/inbox?decision=ID`, as the notifications and the plan page give it) asks each project
 * the visitor holds a grant on. The question's heading takes focus when the visitor opened it from the list.
 */
function DecisionPanel({
  id,
  projectHint,
  focus,
  onClose,
}: {
  id: number;
  projectHint: string | null;
  focus: boolean;
  onClose: () => void;
}) {
  const t = useTranslations("inbox.panel");
  const viewer = useInboxViewer();
  const located = useQuery({
    ...locateDecisionQuery(browserApi, id, viewer.projects),
    enabled: projectHint === null && viewer.login !== null,
  });
  const project = projectHint ?? located.data?.project ?? null;
  const decision = useQuery({
    ...decisionQuery(browserApi, project ?? "-", id),
    enabled: project !== null,
    initialData: located.data && located.data.project === project ? located.data.decision : undefined,
  });
  const heading = useRef<HTMLHeadingElement>(null);
  const loaded = decision.data !== undefined;
  useEffect(() => {
    if (focus && loaded) heading.current?.focus();
  }, [focus, loaded, id]);

  const close = (
    <>
      <Button type="button" variant="outline" size="sm" className="lg:hidden" onClick={onClose} data-testid="decision-back">
        <ArrowLeft aria-hidden="true" />
        {t("back")}
      </Button>
      <Button type="button" variant="ghost" size="sm" className="hidden lg:inline-flex" onClick={onClose} data-testid="decision-close">
        <X aria-hidden="true" />
        {t("close")}
      </Button>
    </>
  );

  let body: ReactNode;
  const notFound =
    (projectHint === null && located.isSuccess && located.data === null) ||
    (decision.isError && decision.error.info.status === 404) ||
    (projectHint === null && viewer.login !== null && viewer.projects.length === 0);
  if (notFound) {
    body = (
      <StatePanel icon={SearchX} title={t("notFoundTitle", { id })} description={t("notFoundDescription")} testId="decision-not-found" className="border-solid">
        {close}
      </StatePanel>
    );
  } else if (decision.data) {
    body = <DecisionView decision={decision.data} where="inbox" headingRef={heading} actions={close} />;
  } else if (decision.isError || located.isError) {
    const error = decision.error ?? located.error;
    body = error ? <ApiErrorState error={error.info} onRetry={() => void (decision.isError ? decision.refetch() : located.refetch())} /> : null;
  } else {
    body = (
      <LoadingState>
        <div className="flex flex-col gap-3">
          <Skeleton className="h-4 w-40" />
          <Skeleton className="h-6 w-full" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      </LoadingState>
    );
  }
  return (
    <div className="min-w-0 rounded-md border bg-card shadow-raised p-4 md:p-5" data-testid="decision-panel" data-decision-id={id}>
      {body}
    </div>
  );
}

/**
 * The member's Inbox: the decisions the agents of their plan runs ask them, open ones first, then the notices (a push or
 * merge into a default branch with its repo, branch and commits, a plan finished, a run failed) and the decisions
 * already answered, expired or cancelled, newest first. Filters by kind, project and unread live in the URL; a decision
 * opens beside the list with its answer form. The list and the bell are read every 10 seconds.
 */
export function InboxPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("inbox");
  const { filters, href, replace, push } = useInboxUrl();
  const viewer = useInboxViewer();
  const { state, stale } = usePagedQuery(notificationsQuery(browserApi, inboxQuery(filters)), initialError);
  const count = useQuery(notificationCountQuery(browserApi));
  const markRead = useMarkRead();
  const readFailure = useReadFailure();
  const { notice, show, clear } = useNotice();
  const [reading, setReading] = useState<number | "all" | null>(null);
  const [focusDecision, setFocusDecision] = useState<number | null>(null);
  const [returnTo, setReturnTo] = useState<number | null>(null);
  const list: NotificationList | null = state.status === "success" ? state.data : null;
  const selected = filters.decision;
  const selectedNotification = selected !== null && list ? notificationOfDecision(list.notifications, selected) : null;

  const read = (target: { ids: number[] } | { all: true }, marker: number | "all") => {
    setReading(marker);
    markRead.mutate(target, {
      onSettled: () => setReading(null),
      onSuccess: (result) => {
        if (marker === "all") show({ tone: "success", text: t("markedAll", { count: result.read }) });
      },
      onError: (error) => show({ tone: "error", ...readFailure(error) }),
    });
  };

  // Opening a decision reads its notification, as answering it would; once per notification and visit.
  const marked = useRef(new Set<number>());
  const { mutate: markOne } = markRead;
  const openUnread = selectedNotification && selectedNotification.read_at === null ? selectedNotification.id : null;
  useEffect(() => {
    if (openUnread === null || marked.current.has(openUnread)) return;
    marked.current.add(openUnread);
    markOne({ ids: [openUnread] });
  }, [openUnread, markOne]);

  const openDecision = (id: number) => {
    setFocusDecision(id);
    setReturnTo(null);
    push({ ...filters, decision: id });
  };
  const closeDecision = () => {
    setFocusDecision(null);
    setReturnTo(selected);
    push({ ...filters, decision: null });
  };
  // Once the panel is gone and the list shows again, focus goes back to the decision's link in it, if the page has it.
  useEffect(() => {
    if (selected !== null || returnTo === null) return;
    document.querySelector<HTMLElement>(`[data-decision-link="${returnTo}"]`)?.focus();
  }, [selected, returnTo]);
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
        description={t("description")}
        meta={
          <>
            {counts ? (
              <>
                <Badge variant={counts.unread > 0 ? "info" : "secondary"} className="h-7 px-2.5" data-testid="inbox-unread-count" data-count={counts.unread}>
                  <Bell aria-hidden="true" />
                  {t("unreadCount", { count: counts.unread })}
                </Badge>
                {counts.open_decisions > 0 ? (
                  <Badge variant="warning" className="h-7 px-2.5" data-testid="inbox-open-count" data-count={counts.open_decisions}>
                    <MessageCircleQuestionMark aria-hidden="true" />
                    {t("openCount", { count: counts.open_decisions })}
                  </Badge>
                ) : null}
              </>
            ) : null}
            <Button
              type="button"
              size="lg"
              variant="outline"
              className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
              aria-disabled={nothingToRead || reading !== null || undefined}
              onClick={() => {
                if (nothingToRead || reading !== null) return;
                if (filtered) read({ ids: pageUnread }, "all");
                else read({ all: true }, "all");
              }}
              data-testid="inbox-mark-all"
            >
              {reading === "all" ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <CheckCheck aria-hidden="true" />}
              {filtered ? t("markShown") : t("markAll")}
            </Button>
          </>
        }
      />
      <NoticeArea notice={notice} onDismiss={clear} />
      <div className={selected !== null ? "grid items-start gap-6 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]" : "flex flex-col gap-6"}>
        <section
          aria-labelledby="inbox-list-title"
          className={selected !== null ? "hidden min-w-0 flex-col gap-4 lg:flex" : "flex min-w-0 flex-col gap-4"}
          data-testid="inbox-list"
        >
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
              ]}
              selected={filters.kind}
              onSelect={(value) => setFilters({ ...filters, kind: value === "decision" || value === "notice" ? value : null, page: 1 })}
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
                reading={reading}
                decisionHref={(id) => href({ ...filters, decision: id })}
                onOpenDecision={openDecision}
                onRead={(id) => read({ ids: [id] }, id)}
                onClear={() => setFilters({ ...NO_FILTERS, decision: filters.decision })}
                onPage={(next) => setFilters({ ...filters, page: next })}
              />
            )}
          </QueryView>
        </section>
        {selected !== null ? (
          <DecisionPanel
            key={selected}
            id={selected}
            projectHint={selectedNotification?.project ?? null}
            focus={focusDecision === selected}
            onClose={closeDecision}
          />
        ) : null}
      </div>
    </>
  );
}

function InboxList({
  page,
  filters,
  stale,
  selected,
  reading,
  decisionHref,
  onOpenDecision,
  onRead,
  onClear,
  onPage,
}: {
  page: NotificationList;
  filters: InboxFilters;
  stale: boolean;
  selected: number | null;
  reading: number | "all" | null;
  decisionHref: (id: number) => Route;
  onOpenDecision: (id: number) => void;
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
      href={notification.decision_id !== null ? decisionHref(notification.decision_id) : null}
      selected={notification.kind === "decision" && notification.decision_id === selected}
      onOpenDecision={onOpenDecision}
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
            <Button variant="outline" size="lg" onClick={onClear} data-testid="inbox-clear-filters">
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

