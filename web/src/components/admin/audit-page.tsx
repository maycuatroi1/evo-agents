"use client";

import { useQuery } from "@tanstack/react-query";
import { ScrollText } from "lucide-react";
import { useTimeZone, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView } from "@/components/states/query-view";
import { type ActiveFilter, EmptyState, NoResults, TableSkeleton } from "@/components/states/states";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectsQuery } from "@/lib/queries";

import { AuditTable, useActionName } from "./audit-table";
import {
  ACTION_NAME,
  adminUsersQuery,
  type AuditFilters,
  auditActionsQuery,
  auditParams,
  auditQuery,
  auditSearch,
  LOGIN_NAME,
  PAGE_SIZES,
  parseAuditFilters,
} from "./data";
import { describedBy, field, FILTER_INPUT, FILTER_SELECT, FilterBar, FilterField, PageSizeField } from "./filter-bar";
import { Pager } from "./pager";
import { useCursorTrail, useUrlView } from "./url-state";
import { usePagedQuery } from "./use-paged-query";

function filterKey(filters: AuditFilters): string {
  return JSON.stringify({ ...filters, cursor: "" });
}

/** The audit trail: filters in the URL, newest rows first, paged forward by the API's cursor. */
export function AdminAudit({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("admin.audit");
  const actionName = useActionName();
  const tFilters = useTranslations("admin.filters");
  const timeZone = useTimeZone() ?? "UTC";
  const ids = useId();
  const { view, go } = useUrlView(parseAuditFilters);
  const params = auditParams(view, timeZone);
  const { state, stale: busy } = usePagedQuery(auditQuery(browserApi, params), initialError);
  const { data: actions } = useQuery(auditActionsQuery(browserApi));
  const { data: projects } = useQuery(projectsQuery(browserApi));
  const { data: users } = useQuery(adminUsersQuery(browserApi));
  const trail = useCursorTrail(filterKey(view), view.cursor);
  const [errors, setErrors] = useState<{ actor?: string; to?: string }>({});

  const apply = (form: FormData) => {
    const next: AuditFilters = {
      actor: field(form, "actor"),
      action: field(form, "action"),
      project: field(form, "project"),
      from: field(form, "from"),
      to: field(form, "to"),
      limit: view.limit,
      cursor: "",
    };
    const size = Number(field(form, "limit"));
    next.limit = PAGE_SIZES.find((value) => value === size) ?? view.limit;
    const found: typeof errors = {};
    if (next.actor && !LOGIN_NAME.test(next.actor)) found.actor = tFilters("invalidLogin");
    if (next.from && next.to && next.from > next.to) found.to = tFilters("invalidRange");
    setErrors(found);
    if (found.actor || found.to) {
      document.getElementById(`${ids}-${found.actor ? "actor" : "to"}`)?.focus();
      return false;
    }
    if (next.action && !ACTION_NAME.test(next.action)) next.action = "";
    go(auditSearch(next), "push");
    return true;
  };
  const clear = () => {
    setErrors({});
    go(auditSearch({ limit: view.limit }), "push");
  };
  const page = (cursor: string) => {
    go(auditSearch({ ...view, cursor }), "replace");
    document.getElementById(`${ids}-results`)?.scrollIntoView({ block: "start" });
  };

  const knownActions = new Set(actions ?? []);
  if (view.action) knownActions.add(view.action);
  const hasFilters = Boolean(view.actor || view.action || view.project || view.from || view.to);

  const fields = (
    <>
      <FilterField id={`${ids}-actor`} label={t("actor")} error={errors.actor}>
        <Input
          id={`${ids}-actor`}
          name="actor"
          defaultValue={view.actor}
          placeholder={t("actorPlaceholder")}
          list={`${ids}-actors`}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          maxLength={100}
          className={`${FILTER_INPUT} font-mono placeholder:font-sans`}
          aria-invalid={errors.actor ? true : undefined}
          aria-describedby={describedBy(`${ids}-actor`, false, errors.actor)}
        />
        <datalist id={`${ids}-actors`}>
          {(users ?? []).map((user) => (
            <option key={user.login} value={user.login} />
          ))}
        </datalist>
      </FilterField>
      <FilterField id={`${ids}-action`} label={t("action")}>
        <NativeSelect id={`${ids}-action`} name="action" defaultValue={view.action} className={FILTER_SELECT}>
          <NativeSelectOption value="">{tFilters("all")}</NativeSelectOption>
          {[...knownActions].sort().map((action) => (
            <NativeSelectOption key={action} value={action}>
              {actionName(action) ? `${actionName(action)} (${action})` : action}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </FilterField>
      <FilterField id={`${ids}-project`} label={t("project")}>
        <NativeSelect id={`${ids}-project`} name="project" defaultValue={view.project} className={FILTER_SELECT}>
          <NativeSelectOption value="">{tFilters("all")}</NativeSelectOption>
          {view.project && !projects?.some((p) => p.name === view.project) ? (
            <NativeSelectOption value={view.project}>{view.project}</NativeSelectOption>
          ) : null}
          {(projects ?? []).map((project) => (
            <NativeSelectOption key={project.name} value={project.name}>
              {project.name}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </FilterField>
      <FilterField id={`${ids}-from`} label={t("from")}>
        <Input
          id={`${ids}-from`}
          name="from"
          type="date"
          defaultValue={view.from}
          className={FILTER_INPUT}
          aria-describedby={`${ids}-zone`}
        />
      </FilterField>
      <FilterField id={`${ids}-to`} label={t("to")} error={errors.to}>
        <Input
          id={`${ids}-to`}
          name="to"
          type="date"
          defaultValue={view.to}
          className={FILTER_INPUT}
          aria-invalid={errors.to ? true : undefined}
          aria-describedby={[`${ids}-zone`, describedBy(`${ids}-to`, false, errors.to)].filter(Boolean).join(" ")}
        />
      </FilterField>
    </>
  );

  const inUse: ActiveFilter[] = [
    ...(view.actor ? [{ label: t("actor"), value: view.actor }] : []),
    ...(view.action ? [{ label: t("action"), value: actionName(view.action) ?? view.action }] : []),
    ...(view.project ? [{ label: t("project"), value: view.project }] : []),
    ...(view.from ? [{ label: t("from"), value: view.from }] : []),
    ...(view.to ? [{ label: t("to"), value: view.to }] : []),
  ];
  // The number of results, while they fit on one page; otherwise the pager under the list says which page this is.
  const count =
    state.status === "success" && view.cursor === "" && state.data.next_cursor === null
      ? t("count", { count: state.data.items.length })
      : null;

  return (
    <>
      <PageHeader title={t("title")} />
      <section id={`${ids}-results`} aria-labelledby={`${ids}-results-title`} className="scroll-mt-20">
        <h2 id={`${ids}-results-title`} className="sr-only">
          {t("caption")}
        </h2>
        <DataCard
          busy={busy}
          toolbar={
            // Reset by the URL's filters, so Back and Clear put the fields back to what the list shows.
            <FilterBar
              resetKey={filterKey(view)}
              active={inUse.length}
              label={t("title")}
              onApply={apply}
              onClear={clear}
              canClear={hasFilters}
              testId="audit-filters"
              note={<p id={`${ids}-zone`}>{t("timeZone", { zone: timeZone })}</p>}
              footer={<PageSizeField id={`${ids}-limit`} value={view.limit} />}
              count={count}
            >
              {fields}
            </FilterBar>
          }
          footer={
            state.status === "success" ? (
              <Pager
                label={t("title")}
                page={trail.page}
                count={state.data.items.length}
                hasNext={state.data.next_cursor !== null}
                canGoBack={trail.canGoBack}
                atStart={view.cursor === ""}
                busy={busy}
                onNext={() => {
                  if (!state.data.next_cursor) return;
                  trail.forward();
                  page(state.data.next_cursor);
                }}
                onPrevious={() => page(trail.back())}
                onFirst={() => page("")}
              />
            ) : null
          }
        >
          <QueryView state={state} loading={<TableSkeleton rows={8} />}>
            {(data) =>
              data.items.length === 0 && view.cursor === "" ? (
                hasFilters ? (
                  <NoResults title={t("noMatchTitle")} filters={inUse} onClear={clear} />
                ) : (
                  <EmptyState icon={ScrollText} title={t("emptyTitle")} description={t("emptyDescription")} />
                )
              ) : (
                <AuditTable rows={data.items} caption={t("caption")} />
              )
            }
          </QueryView>
        </DataCard>
      </section>
    </>
  );
}
