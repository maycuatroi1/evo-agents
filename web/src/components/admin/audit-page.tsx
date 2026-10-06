"use client";

import { useQuery } from "@tanstack/react-query";
import { ScrollText } from "lucide-react";
import { useTimeZone, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { PageHeader } from "@/components/shell/page-header";
import { QueryView } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
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
import { describedBy, field, FilterBar, FilterField, PageSizeField } from "./filter-bar";
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
      return;
    }
    if (next.action && !ACTION_NAME.test(next.action)) next.action = "";
    go(auditSearch(next), "push");
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

  return (
    <>
      <PageHeader title={t("title")} />
      {/* Keyed by the URL's filters, so Back and Clear put the fields back to what the list shows. */}
      <FilterBar
        key={filterKey(view)}
        label={t("title")}
        onApply={apply}
        onClear={clear}
        canClear={hasFilters}
        testId="audit-filters"
        note={<p id={`${ids}-zone`}>{t("timeZone", { zone: timeZone })}</p>}
        footer={<PageSizeField id={`${ids}-limit`} value={view.limit} />}
      >
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
            className="h-9 font-mono placeholder:font-sans"
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
          <NativeSelect id={`${ids}-action`} name="action" defaultValue={view.action} className="w-full [&_select]:h-9">
            <NativeSelectOption value="">{tFilters("all")}</NativeSelectOption>
            {[...knownActions].sort().map((action) => (
              <NativeSelectOption key={action} value={action}>
                {actionName(action) ? `${actionName(action)} (${action})` : action}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </FilterField>
        <FilterField id={`${ids}-project`} label={t("project")}>
          <NativeSelect id={`${ids}-project`} name="project" defaultValue={view.project} className="w-full [&_select]:h-9">
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
            className="h-9"
            aria-describedby={`${ids}-zone`}
          />
        </FilterField>
        <FilterField id={`${ids}-to`} label={t("to")} error={errors.to}>
          <Input
            id={`${ids}-to`}
            name="to"
            type="date"
            defaultValue={view.to}
            className="h-9"
            aria-invalid={errors.to ? true : undefined}
            aria-describedby={[`${ids}-zone`, describedBy(`${ids}-to`, false, errors.to)].filter(Boolean).join(" ")}
          />
        </FilterField>
      </FilterBar>

      <section id={`${ids}-results`} aria-labelledby={`${ids}-results-title`} className="flex scroll-mt-20 flex-col gap-4">
        <h2 id={`${ids}-results-title`} className="sr-only">
          {t("caption")}
        </h2>
        <QueryView state={state} loading={<TableSkeleton rows={8} />}>
          {(data) =>
            data.items.length === 0 && view.cursor === "" ? (
              <EmptyState icon={ScrollText} title={t("emptyTitle")} description={t("emptyDescription")} />
            ) : (
              <>
                <div aria-busy={busy || undefined} className={busy ? "opacity-60 transition-opacity" : "transition-opacity"}>
                  <AuditTable rows={data.items} caption={t("caption")} />
                </div>
                <Pager
                  label={t("title")}
                  page={trail.page}
                  count={data.items.length}
                  hasNext={data.next_cursor !== null}
                  canGoBack={trail.canGoBack}
                  atStart={view.cursor === ""}
                  busy={busy}
                  onNext={() => {
                    if (!data.next_cursor) return;
                    trail.forward();
                    page(data.next_cursor);
                  }}
                  onPrevious={() => page(trail.back())}
                  onFirst={() => page("")}
                />
              </>
            )
          }
        </QueryView>
      </section>
    </>
  );
}
