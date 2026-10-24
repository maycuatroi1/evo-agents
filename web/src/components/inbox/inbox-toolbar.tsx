"use client";

import { Bell, Lightbulb, MessageCircleQuestionMark, X } from "lucide-react";
import { useTranslations } from "next-intl";

import { FacetGroup } from "@/components/data/facet-group";
import { ToolbarField, ToolbarFilters } from "@/components/data/filter-sheet";
import { SearchField } from "@/components/data/search-field";
import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";

import { type InboxFilters, MAX_QUERY_CHARS, NO_FILTERS } from "./model";

/**
 * The filters on one line: the search of titles and bodies, unread or all, the kind, and the project as a select (a
 * member of many projects would otherwise get a wall of chips); under 768 px the search beside "Filters (n)", whose
 * sheet holds the rest (`ToolbarFilters`). A filter by run, set from a run's row, shows as a chip that clears it.
 */
export function InboxToolbar({
  filters,
  projects,
  onChange,
}: {
  filters: InboxFilters;
  projects: readonly string[];
  onChange: (next: InboxFilters) => void;
}) {
  const t = useTranslations("inbox");
  const active = (filters.unread ? 1 : 0) + (filters.kind ? 1 : 0) + (filters.project ? 1 : 0);
  const names = [...new Set([...projects, ...(filters.project ? [filters.project] : [])])].sort();
  return (
    <div className="flex flex-col gap-2" role="search" aria-label={t("filters.label")} data-testid="inbox-filters">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <SearchField
          value={filters.q}
          onCommit={(q) => onChange({ ...filters, q, page: 1 })}
          label={t("filters.search")}
          placeholder={t("filters.searchPlaceholder")}
          clearLabel={t("filters.clearSearch")}
          maxLength={MAX_QUERY_CHARS}
          className="sm:w-64 max-md:flex-1"
          testId="inbox-search"
        />
        <ToolbarFilters
          active={active}
          onClear={() => onChange({ ...NO_FILTERS, q: filters.q, run: filters.run, decision: filters.decision, proposal: filters.proposal })}
          testId="inbox-filter-sheet"
        >
          <FacetGroup
            label={t("filters.show")}
            options={[
              { value: null, label: t("filters.everything") },
              { value: "unread", label: t("filters.unread") },
            ]}
            selected={filters.unread ? "unread" : null}
            onSelect={(value) => onChange({ ...filters, unread: value === "unread", page: 1 })}
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
              onChange({ ...filters, kind: value === "decision" || value === "notice" || value === "proposal" ? value : null, page: 1 })
            }
            countLabel={(n) => t("count", { count: n })}
            testId="inbox-facet-kind"
          />
          {names.length > 1 || filters.project !== null ? (
            <ToolbarField label={t("filters.project")}>
              {(id) => (
                <NativeSelect
                  id={id}
                  size="sm"
                  className="max-md:w-full"
                  value={filters.project ?? ""}
                  onChange={(event) => onChange({ ...filters, project: event.target.value || null, page: 1 })}
                  data-testid="inbox-facet-project"
                >
                  <NativeSelectOption value="">{t("filters.allProjects")}</NativeSelectOption>
                  {names.map((name) => (
                    <NativeSelectOption key={name} value={name}>
                      {name}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
              )}
            </ToolbarField>
          ) : null}
        </ToolbarFilters>
      </div>
      {filters.run !== null ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="border-brand bg-surface-selected font-semibold"
            aria-label={t("filters.clearRun", { id: filters.run })}
            onClick={() => onChange({ ...filters, run: null, page: 1 })}
            data-testid="inbox-run-filter"
          >
            {t("filters.run", { id: filters.run })}
            <X aria-hidden="true" />
          </Button>
        </div>
      ) : null}
    </div>
  );
}
