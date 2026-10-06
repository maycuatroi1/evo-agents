"use client";

import { useQuery } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { PageHeader } from "@/components/shell/page-header";
import { QueryView } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
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
import { describedBy, field, FilterBar, FilterField, PageSizeField } from "./filter-bar";
import { NoticeArea, useNotice } from "./notice";
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
  const { notice, show, clear: dismiss } = useNotice();
  const [loginError, setLoginError] = useState<string | null>(null);

  const apply = (form: FormData) => {
    const login = field(form, "login");
    if (login && !LOGIN_NAME.test(login)) {
      setLoginError(tFilters("invalidLogin"));
      document.getElementById(`${ids}-login`)?.focus();
      return;
    }
    setLoginError(null);
    const values = { login, kind: field(form, "kind"), state: field(form, "state"), limit: field(form, "limit") };
    go(tokenSearch(parseTokenFilters(new URLSearchParams(values))), "push");
  };
  const page = (cursor: string) => {
    go(tokenSearch({ ...view, cursor }), "replace");
    document.getElementById(`${ids}-results`)?.scrollIntoView({ block: "start" });
  };
  const hasFilters = Boolean(view.login || view.kind || view.state !== "active");

  return (
    <>
      <PageHeader title={t("title")} />
      <NoticeArea notice={notice} onDismiss={dismiss} />
      <FilterBar
        key={filterKey(view)}
        label={t("title")}
        onApply={apply}
        onClear={() => {
          setLoginError(null);
          go({}, "push");
        }}
        canClear={hasFilters}
        testId="token-filters"
        footer={<PageSizeField id={`${ids}-limit`} value={view.limit} />}
      >
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
            className="h-9 font-mono placeholder:font-sans"
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
          <NativeSelect id={`${ids}-kind`} name="kind" defaultValue={view.kind} className="w-full [&_select]:h-9">
            <NativeSelectOption value="">{tFilters("all")}</NativeSelectOption>
            {TOKEN_KINDS.map((kind) => (
              <NativeSelectOption key={kind} value={kind}>
                {t(`kinds.${kind}`)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </FilterField>
        <FilterField id={`${ids}-state`} label={t("state")}>
          <NativeSelect id={`${ids}-state`} name="state" defaultValue={view.state} className="w-full [&_select]:h-9">
            {TOKEN_STATES.map((value) => (
              <NativeSelectOption key={value} value={value}>
                {t(`states.${value}`)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </FilterField>
      </FilterBar>

      <section id={`${ids}-results`} aria-labelledby={`${ids}-results-title`} className="flex scroll-mt-20 flex-col gap-4">
        <h2 id={`${ids}-results-title`} className="sr-only">
          {t("caption")}
        </h2>
        <QueryView state={state} loading={<TableSkeleton rows={6} />}>
          {(data) =>
            data.items.length === 0 && view.cursor === "" ? (
              <EmptyState icon={KeyRound} title={t("emptyTitle")} description={t("emptyDescription")} />
            ) : (
              <>
                <div aria-busy={busy || undefined} className={busy ? "opacity-60 transition-opacity" : "transition-opacity"}>
                  <TokensTable tokens={data.items} caption={t("caption")} onNotice={show} />
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
