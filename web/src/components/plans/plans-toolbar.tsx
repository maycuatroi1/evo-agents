"use client";

import { useTranslations } from "next-intl";

import { DataToolbar } from "@/components/data/data-card";
import { ToolbarField, ToolbarFilters } from "@/components/data/filter-sheet";
import { SearchField } from "@/components/data/search-field";
import { Segmented } from "@/components/data/segmented";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import {
  activeFilters,
  PLAN_GROUPS,
  PLAN_SORTS,
  type PlanGroupBy,
  type PlanListFilters,
  type PlanListView,
  type PlanSort,
} from "@/lib/plan-list";

const SELECT = "md:[&_select]:h-7 md:[&_select]:text-xs";

/** A select among the toolbar's filters: "All owners" first (the empty value), then each choice. */
function ChoiceSelect({
  label,
  all,
  value,
  choices,
  onChange,
  testId,
}: {
  label: string;
  all: string;
  value: string | null;
  choices: readonly string[];
  onChange: (value: string | null) => void;
  testId: string;
}) {
  return (
    <ToolbarField label={label}>
      {(id) => (
        <NativeSelect id={id} value={value ?? ""} onChange={(event) => onChange(event.target.value || null)} className={SELECT} data-testid={testId}>
          <NativeSelectOption value="">{all}</NativeSelectOption>
          {/* A value from the address the plans no longer hold stays chosen, so the filter can be seen and cleared. */}
          {[...choices, ...(value && !choices.includes(value) ? [value] : [])].map((choice) => (
            <NativeSelectOption key={choice} value={choice}>
              {choice}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      )}
    </ToolbarField>
  );
}

/**
 * The toolbar of the plans list: Open or Completed with their counts, the search (`/` focuses it), then Owner, Problem
 * and Repo, Group and Sort; under 768 px the selects move into the "Filters (n)" sheet. Every choice is written to the
 * address by the page.
 */
export function PlansToolbar({
  filters,
  counts,
  choices,
  shown,
  onChange,
}: {
  filters: PlanListFilters;
  counts: Record<PlanListView, number>;
  choices: { owners: string[]; problems: string[]; repos: string[] };
  /** The count said at the end of the toolbar. */
  shown: string;
  onChange: (filters: PlanListFilters) => void;
}) {
  const t = useTranslations("plans.list");
  const set = (next: Partial<PlanListFilters>) => onChange({ ...filters, ...next });
  return (
    <DataToolbar label={t("title")} count={shown} countTestId="plans-count">
      <Segmented<PlanListView>
        label={t("views")}
        value={filters.view}
        onChange={(view) => set({ view })}
        options={[
          { value: "open", label: t("viewOpen", { count: counts.open }), testId: "plans-view-open" },
          { value: "completed", label: t("viewCompleted", { count: counts.completed }), testId: "plans-view-completed" },
        ]}
        className="max-md:w-full max-md:[&>button]:flex-1 max-md:[&>button]:justify-center"
      />
      <SearchField
        value={filters.q}
        onCommit={(q) => set({ q })}
        label={t("search")}
        placeholder={t("searchPlaceholder")}
        clearLabel={t("clearSearch")}
        debounce={150}
        maxLength={200}
        className="sm:w-64 max-md:flex-1"
        testId="plans-search"
      />
      <ToolbarFilters
        active={activeFilters(filters)}
        onClear={() => set({ owner: null, problem: null, repo: null })}
        summary={shown}
        testId="plans-filters"
      >
        <ChoiceSelect label={t("owner")} all={t("allOwners")} value={filters.owner} choices={choices.owners} onChange={(owner) => set({ owner })} testId="plans-owner" />
        <ChoiceSelect label={t("problem")} all={t("allProblems")} value={filters.problem} choices={choices.problems} onChange={(problem) => set({ problem })} testId="plans-problem" />
        <ChoiceSelect label={t("repo")} all={t("allRepos")} value={filters.repo} choices={choices.repos} onChange={(repo) => set({ repo })} testId="plans-repo" />
        <ToolbarField label={t("group")}>
          {(id) => (
            <NativeSelect id={id} value={filters.group} onChange={(event) => set({ group: event.target.value as PlanGroupBy })} className={SELECT} data-testid="plans-group">
              {PLAN_GROUPS.map((group) => (
                <NativeSelectOption key={group} value={group}>
                  {t(`groupBy.${group}`)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          )}
        </ToolbarField>
        <ToolbarField label={t("sort")}>
          {(id) => (
            <NativeSelect id={id} value={filters.sort} onChange={(event) => set({ sort: event.target.value as PlanSort })} className={SELECT} data-testid="plans-sort">
              {PLAN_SORTS.map((sort) => (
                <NativeSelectOption key={sort} value={sort}>
                  {t(`sortBy.${sort}`)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          )}
        </ToolbarField>
      </ToolbarFilters>
    </DataToolbar>
  );
}
