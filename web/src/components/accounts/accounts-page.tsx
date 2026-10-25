"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Plus } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { AccountDialog } from "./account-dialog";
import { LazyAccountSharing, LazyModelsTab, LazyPolicies, LazyServicesTab } from "./account-tabs-lazy";
import { useChangeAccount, useFailure } from "./hooks";
import { LazyLoginDialog, preloadLogin } from "./login-lazy";
import { sharedVariables } from "./model";
import type { ProviderCardData } from "./provider-cards";
import type { ProviderStart } from "./provider-dialog";
import { LazyProviderDeleteDialog, LazyProviderDialog, LazyProviderKeyDialog, preloadProvider } from "./provider-lazy";
import { catalogQuery, providerAccountsQuery } from "./provider-queries";
import { ProvidersTab } from "./providers-tab";
import { type Account, accountListQuery } from "./queries";

const SECRETS_HREF = "/secrets" as Route;

/** The tabs of the page, the first the default; the one shown is `?tab=` of the URL, so a link or a reload keeps it. */
const TABS = ["providers", "models", "services", "rotation", "sharing"] as const;
type Tab = (typeof TABS)[number];
const tabOf = (value: string | null): Tab => (TABS as readonly string[]).includes(value ?? "") ? (value as Tab) : "providers";

/**
 * The tab shown, read from the URL as the page opens. A change shows at once and is written back to the URL through
 * the History API; reading it back from useSearchParams would lag a render behind the arrow keys of the tab list.
 */
function useTab(): [Tab, (next: string) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const [tab, setTab] = useState<Tab>(() => tabOf(params.get("tab")));
  const set = (next: string) => {
    setTab(tabOf(next));
    const search = new URLSearchParams(window.location.search);
    if (next === "providers") search.delete("tab");
    else search.set("tab", next);
    const query = search.toString();
    window.history.replaceState(null, "", `${pathname}${query ? `?${query}` : ""}`);
  };
  return [tab, set];
}

type Dialog<T> = { open: boolean; value: T } | null;
const opened = <T,>(value: T): Dialog<T> => ({ open: true, value });
const toggled = <T,>(current: Dialog<T>, open: boolean): Dialog<T> => (current ? { ...current, open } : current);

/**
 * The visitor's accounts (docs/credentials.md, section "Accounts"), in tabs: Providers, a card per provider their
 * model accounts call with its keys, their usage and cost, and its models; Models, every model their runs may ask
 * for; Service credentials; Rotation, the policy of each project; Sharing, the shares and what they spent. Add
 * provider opens the catalog: Claude signs in on a worker, any other provider is a key and a base URL. Never a value.
 */
export function AccountsPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("accounts");
  const tTabs = useTranslations("accounts.provider.tabs");
  const state = useHubQuery(accountListQuery(browserApi), initialError);
  const accounts = state.status === "success" ? state.data : null;
  const [tab, setTab] = useTab();
  const [editing, setEditing] = useState<{ open: boolean; account: Account | null }>({ open: false, account: null });
  // Each dialog is null until it first opens: it mounts, and loads, only then.
  const [login, setLogin] = useState<Dialog<Account | null>>(null);
  const [provider, setProvider] = useState<Dialog<ProviderStart>>(null);
  const [keyOf, setKeyOf] = useState<Dialog<Account>>(null);
  const [deleting, setDeleting] = useState<Dialog<Account>>(null);
  const [now] = useState(() => Date.now());
  const failureOf = useFailure();
  const { mutateAsync } = useChangeAccount();
  const providers = useQuery({ ...providerAccountsQuery(browserApi), enabled: Boolean(accounts?.some((account) => account.gateway)) });
  const catalog = useQuery({ ...catalogQuery(browserApi), enabled: provider !== null || Boolean(accounts?.some((account) => account.gateway)) });

  const entryOf = useCallback((id: string) => catalog.data?.providers.find((entry) => entry.id === id) ?? null, [catalog.data]);
  const openCatalog = useCallback(() => setProvider(opened<ProviderStart>({ kind: "catalog" })), []);
  const edit = useCallback(
    (account: Account) => {
      const held = account.gateway ? providers.data?.find((item) => item.name === account.name) : undefined;
      if (held) setProvider(opened<ProviderStart>({ kind: "edit", entry: entryOf(held.provider), account: held }));
      else setEditing({ open: true, account });
    },
    [providers.data, entryOf],
  );
  const renew = useCallback((account: Account) => setLogin(opened<Account | null>(account)), []);
  const addTo = useCallback(
    (card: ProviderCardData) => {
      if (card.id === "claude") return setLogin(opened<Account | null>(null));
      const entry = card.entry ?? entryOf(card.id);
      setProvider(opened<ProviderStart>(entry && entry.via !== "login" ? { kind: "add", entry } : { kind: "catalog" }));
    },
    [entryOf],
  );
  const pause = useCallback(
    async (account: Account) => {
      const paused = account.paused_at === null;
      const name = account.name;
      try {
        await mutateAsync({ name, body: { paused } });
      } catch (error) {
        notifyFailure(t(paused ? "pauseFailed" : "resumeFailed", { name }), failureOf(error));
        return;
      }
      notify({ tone: "success", text: t(paused ? "paused" : "resumed", { name }), description: t(paused ? "pausedText" : "resumedText") });
    },
    [mutateAsync, failureOf, t],
  );
  const onPause = useCallback((account: Account) => void pause(account), [pause]);
  const onKey = useCallback((account: Account) => setKeyOf(opened(account)), []);
  const onDelete = useCallback((account: Account) => setDeleting(opened(account)), []);
  const actions = { onEdit: edit, onPause, onRenew: renew, onKey, onDelete };
  const shared = useMemo(() => sharedVariables(accounts ?? []), [accounts]);

  return (
    <>
      <PageHeader
        title={t("title")}
        tags={accounts ? <Badge variant="secondary">{t("count", { count: accounts.length })}</Badge> : null}
        actions={
          <>
            <Button asChild variant="outline">
              <Link href={SECRETS_HREF} data-testid="accounts-secrets-link">
                {t("secrets")}
                <ChevronRight aria-hidden="true" />
              </Link>
            </Button>
            <Button onClick={openCatalog} onPointerEnter={preloadProvider} onFocus={preloadProvider} data-testid="accounts-add">
              <Plus aria-hidden="true" />
              {t("addProvider")}
            </Button>
          </>
        }
        sub={accounts && accounts.length > 0 ? <span data-testid="accounts-list-summary">{t("listSummary")}</span> : null}
      />
      <QueryView state={state} loading={<TableSkeleton rows={4} />}>
        {(list) => (
          <Tabs value={tab} onValueChange={setTab} className="min-w-0">
            <TabsList aria-label={tTabs("label")}>
              {TABS.map((item) => (
                <TabsTrigger key={item} value={item} data-testid={`accounts-tab-${item}`}>
                  {tTabs(item)}
                </TabsTrigger>
              ))}
            </TabsList>
            <TabsContent value="providers" data-testid="accounts-panel-providers">
              <ProvidersTab accounts={list} now={now} onCatalog={openCatalog} onAdd={addTo} {...actions} />
            </TabsContent>
            <TabsContent value="models" data-testid="accounts-panel-models">
              <LazyModelsTab accounts={list} onCatalog={openCatalog} />
            </TabsContent>
            <TabsContent value="services" data-testid="accounts-panel-services">
              <LazyServicesTab accounts={list} shared={shared} now={now} {...actions} />
            </TabsContent>
            <TabsContent value="rotation" data-testid="accounts-panel-rotation">
              <LazyPolicies accounts={list} />
            </TabsContent>
            <TabsContent value="sharing" data-testid="accounts-panel-sharing">
              <LazyAccountSharing accounts={list} now={now} />
            </TabsContent>
          </Tabs>
        )}
      </QueryView>
      {login ? (
        <LazyLoginDialog open={login.open} onOpenChange={(open) => setLogin((current) => toggled(current, open))} account={login.value} accounts={accounts ?? []} />
      ) : null}
      {provider ? (
        <LazyProviderDialog
          open={provider.open}
          onOpenChange={(open) => setProvider((current) => toggled(current, open))}
          start={provider.value}
          onClaude={() => {
            preloadLogin();
            setLogin(opened<Account | null>(null));
          }}
        />
      ) : null}
      {keyOf ? (
        <LazyProviderKeyDialog key={keyOf.value.name} account={keyOf.value} open={keyOf.open} onOpenChange={(open) => setKeyOf((current) => toggled(current, open))} />
      ) : null}
      {deleting ? (
        <LazyProviderDeleteDialog key={deleting.value.name} account={deleting.value} open={deleting.open} onOpenChange={(open) => setDeleting((current) => toggled(current, open))} />
      ) : null}
      <AccountDialog account={editing.account} open={editing.open} onOpenChange={(open) => setEditing((current) => ({ ...current, open }))} />
    </>
  );
}
