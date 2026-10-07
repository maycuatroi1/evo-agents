"use client";

import { useQuery } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { notify } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView } from "@/components/states/query-view";
import { type ActiveFilter, EmptyState, NoResults, TableSkeleton } from "@/components/states/states";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import {
  adminUsersQuery,
  LOGIN_NAME,
  parseTokenFilters,
  TOKEN_KINDS,
  TOKEN_STATES,
  type TokenFilters,
  tokenParams,
  tokenSearch,
  tokensQuery,
} from "./data";
import { describedBy, field, FILTER_INPUT, FILTER_SELECT, FilterBar, FilterField, PageSizeField } from "./filter-bar";
import { Pager } from "./pager";
import { TokensTable } from "./tokens-table";
import { useCursorTrail, useUrlView } from "./url-state";
import { usePagedQuery } from "./use-paged-query";

function filterKey(filters: TokenFilters): string {
  return JSON.stringify({ ...filters, cursor: "" });
}

/** Every user's tokens and web sessions, live ones by default, with revocation. */
export function AdminTokens({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("admin.tokens");
  const tFilters = useTranslations("admin.filters");
  const ids = useId();
  const { view, go } = useUrlView(parseTokenFilters);
  const params = tokenParams(view);
  const { state, stale: busy } = usePagedQuery(tokensQuery(browserApi, params), initialError);
  const { data: users } = useQuery(adminUsersQuery(browserApi));
  const trail = useCursorTrail(filterKey(view), view.cursor);
  const [loginError, setLoginError] = useState<string | null>(null);

  const apply = (form: FormData) => {
    const login = field(form, "login");
    if (login && !LOGIN_NAME.test(login)) {
      setLoginError(tFilters("invalidLogin"));
      document.getElementById(`${ids}-login`)?.focus();
      return false;
    }
    setLoginError(null);
    const values = { login, kind: field(form, "kind"), state: field(form, "state"), limit: field(form, "limit") };
    go(tokenSearch(parseTokenFilters(new URLSearchParams(values))), "push");
    return true;
  };
  const page = (cursor: string) => {
    go(tokenSearch({ ...view, cursor }), "replace");
    document.getElementById(`${ids}-results`)?.scrollIntoView({ block: "start" });
  };
  const hasFilters = Boolean(view.login || view.kind || view.state !== "active");

  const fields = (
    <>
      <FilterField id={`${ids}-login`} label={t("login")} error={loginError}>
        <Input
          id={`${ids}-login`}
          name="login"
          defaultValue={view.login}
          placeholder={t("loginPlaceholder")}
          list={`${ids}-logins`}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          maxLength={100}
          className={`${FILTER_INPUT} font-mono placeholder:font-sans`}
          aria-invalid={loginError ? true : undefined}
          aria-describedby={describedBy(`${ids}-login`, false, loginError)}
        />
        <datalist id={`${ids}-logins`}>
          {(users ?? []).map((user) => (
            <option key={user.login} value={user.login} />
          ))}
        </datalist>
      </FilterField>
      <FilterField id={`${ids}-kind`} label={t("kind")}>
        <NativeSelect id={`${ids}-kind`} name="kind" defaultValue={view.kind} className={FILTER_SELECT}>
          <NativeSelectOption value="">{tFilters("all")}</NativeSelectOption>
          {TOKEN_KINDS.map((kind) => (
            <NativeSelectOption key={kind} value={kind}>
              {t(`kinds.${kind}`)}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </FilterField>
      <FilterField id={`${ids}-state`} label={t("state")}>
        <NativeSelect id={`${ids}-state`} name="state" defaultValue={view.state} className={FILTER_SELECT}>
          {TOKEN_STATES.map((value) => (
            <NativeSelectOption key={value} value={value}>
              {t(`states.${value}`)}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </FilterField>
    </>
  );

  const inUse: ActiveFilter[] = [
    ...(view.login ? [{ label: t("login"), value: view.login }] : []),
    ...(view.kind ? [{ label: t("kind"), value: t(`kinds.${view.kind}`) }] : []),
    ...(view.state !== "active" ? [{ label: t("state"), value: t(`states.${view.state}`) }] : []),
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
            <FilterBar
              resetKey={filterKey(view)}
              active={inUse.length}
              label={t("title")}
              onApply={apply}
              onClear={() => {
                setLoginError(null);
                go({}, "push");
              }}
              canClear={hasFilters}
              testId="token-filters"
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
          <QueryView state={state} loading={<TableSkeleton rows={6} />}>
            {(data) =>
              data.items.length === 0 && view.cursor === "" ? (
                hasFilters ? (
                  <NoResults title={t("noMatchTitle")} filters={inUse} onClear={() => go({}, "push")} />
                ) : (
                  <EmptyState icon={KeyRound} title={t("emptyTitle")} description={t("emptyDescription")} />
                )
              ) : (
                <TokensTable tokens={data.items} caption={t("caption")} onNotice={notify} />
              )
            }
          </QueryView>
        </DataCard>
      </section>
    </>
  );
}
