"use client";

import { useQuery } from "@tanstack/react-query";
import { BadgeCheck, ExternalLink, Info, KeyRound, ListChecks, Send, ShieldAlert, ShieldCheck } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Fact, Facts } from "@/components/data/facts";
import { Identifier, Tag } from "@/components/data/identifier";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ListSkeleton } from "@/components/states/states";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { whoamiQuery } from "@/lib/queries";

import { InstallActions, RequestInstall } from "./install-actions";
import { requestableProjects } from "./model";
import { MarketStatusBadge, NoInstallReason, PriceTag, TierTag } from "./parts";
import { type MarketPluginDetail, marketPluginQuery } from "./queries";

function CardHead({ icon: Icon, title, description }: { icon: typeof Info; title: string; description?: string }) {
  return (
    <CardHeader>
      <CardTitle>
        <h2 className="flex items-center gap-2">
          <Icon className="size-4 text-muted-foreground" aria-hidden="true" />
          {title}
        </h2>
      </CardTitle>
      {description ? <CardDescription>{description}</CardDescription> : null}
    </CardHeader>
  );
}

function OutLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer noopener" className="inline-flex max-w-full items-center gap-1 text-brand hover:underline">
      <span className="min-w-0 [overflow-wrap:anywhere]">{children}</span>
      <ExternalLink className="size-3.5 shrink-0" aria-hidden="true" />
    </a>
  );
}

function Permissions({ plugin }: { plugin: MarketPluginDetail }) {
  const t = useTranslations("market.detail");
  const latest = plugin.latest;
  if (!latest) return <p className="text-sm text-muted-foreground">{t("noRelease")}</p>;
  if (latest.error) {
    return (
      <p className="flex items-start gap-2 text-sm text-attention" data-testid="market-release-error">
        <ShieldAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <span className="min-w-0 [overflow-wrap:anywhere]">{t("releaseError", { error: latest.error })}</span>
      </p>
    );
  }
  const permissions = latest.permissions ?? [];
  if (permissions.length === 0) return <p className="text-sm text-muted-foreground">{t("noPermissions")}</p>;
  return (
    <ul className="flex flex-col gap-2" data-testid="market-permissions">
      {permissions.map((permission) => (
        <li key={permission.name} className="flex min-w-0 flex-col gap-0.5 sm:flex-row sm:items-baseline sm:gap-3" data-permission={permission.name}>
          <code className="shrink-0 font-mono text-xs text-muted-foreground">{permission.name}</code>
          <span className="min-w-0 text-sm text-pretty">{permission.text}</span>
        </li>
      ))}
    </ul>
  );
}

function PublisherCard({ plugin }: { plugin: MarketPluginDetail }) {
  const t = useTranslations("market.detail");
  const publisher = plugin.publisher;
  return (
    <Card className="min-w-0" data-testid="market-publisher">
      <CardHead icon={BadgeCheck} title={t("publisherTitle")} />
      <Facts className="pt-0">
        <Fact label={t("publisher")}>
          <span className="inline-flex flex-wrap items-center gap-2">
            {publisher.name ?? publisher.id}
            <Identifier value={publisher.id} />
          </span>
        </Fact>
        <Fact label={t("identity")} testId="market-identity">
          <span className="font-mono text-xs">{publisher.identity}</span>
        </Fact>
        <Fact label={t("trust")} testId="market-trusted">
          {plugin.tier === "core" ? (
            <span className="text-muted-foreground">{t("trustImage")}</span>
          ) : plugin.trusted ? (
            <span className="inline-flex items-center gap-1.5">
              <ShieldCheck className="size-4 text-success" aria-hidden="true" />
              {t("trusted")}
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5">
              <ShieldAlert className="size-4 text-attention" aria-hidden="true" />
              {plugin.may_trust ? t("notTrusted") : t("locked")}
            </span>
          )}
        </Fact>
        <Fact label={t("repo")}>
          <OutLink href={`https://github.com/${plugin.repo}`}>{plugin.repo}</OutLink>
        </Fact>
      </Facts>
    </Card>
  );
}

function DetailBody({ plugin }: { plugin: MarketPluginDetail }) {
  const t = useTranslations("market.detail");
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const hubAdmin = me?.admin ?? false;
  const projects = requestableProjects(me?.grants ?? []);
  const wanted = plugin.status === "not_installed" || plugin.status === "upgrade";
  return (
    <div className="flex flex-col gap-6" data-testid="market-plugin">
      <PageHeader
        title={plugin.name}
        status={<MarketStatusBadge status={plugin.status} size="lg" />}
        tags={
          <>
            <TierTag tier={plugin.tier} />
            <PriceTag price={plugin.price} />
            <Identifier value={plugin.id} />
          </>
        }
        actions={hubAdmin ? <InstallActions plugin={plugin} /> : null}
        sub={t("from", { market: plugin.market, publisher: plugin.publisher.name ?? plugin.publisher.id })}
      />
      <div className="grid min-w-0 gap-6 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="flex min-w-0 flex-col gap-6">
          <Card className="min-w-0" data-testid="market-about">
            <CardHead icon={Info} title={t("aboutTitle")} />
            <CardContent className="flex min-w-0 flex-col gap-4">
              <p className="text-sm text-pretty">{plugin.description}</p>
              <NoInstallReason plugin={plugin} />
              <div className="flex flex-wrap gap-1.5">
                {[...plugin.industries, ...plugin.categories].map((label) => (
                  <Tag key={label}>{label}</Tag>
                ))}
              </div>
            </CardContent>
            <Facts className="border-t">
              <Fact label={t("latest")} testId="market-latest">
                {plugin.latest?.version ? <span className="font-mono text-xs">{plugin.latest.version}</span> : t("none")}
              </Fact>
              <Fact label={t("installedVersion")} testId="market-installed">
                {plugin.installed_version ? <span className="font-mono text-xs">{plugin.installed_version}</span> : t("none")}
              </Fact>
              {plugin.package ? (
                <Fact label={t("package")}>
                  <span className="font-mono text-xs">{plugin.package}</span>
                </Fact>
              ) : null}
              {plugin.license ? <Fact label={t("license")}>{plugin.license}</Fact> : null}
              {plugin.requires ? (
                <Fact label={t("requires")}>
                  <span className="font-mono text-xs">{plugin.requires}</span>
                </Fact>
              ) : null}
              {plugin.homepage ? (
                <Fact label={t("homepage")}>
                  <OutLink href={plugin.homepage}>{plugin.homepage}</OutLink>
                </Fact>
              ) : null}
            </Facts>
          </Card>
          <Card className="min-w-0">
            <CardHead icon={ListChecks} title={t("asksFor")} description={t("asksForDescription")} />
            <CardContent className="min-w-0">
              <Permissions plugin={plugin} />
            </CardContent>
          </Card>
        </div>
        <div className="flex min-w-0 flex-col gap-6">
          <PublisherCard plugin={plugin} />
          {plugin.price.kind !== "free" ? (
            <Card className="min-w-0" data-testid="market-license">
              <CardHead icon={KeyRound} title={t("licenseTitle")} description={t("licenseDescription")} />
            </Card>
          ) : null}
          {!hubAdmin && wanted && plugin.tier === "isolated" && plugin.price.kind === "free" ? (
            <Card className="min-w-0">
              <CardHead icon={Send} title={t("requestTitle")} />
              <CardContent className="min-w-0">
                <RequestInstall plugin={plugin} projects={projects} />
              </CardContent>
            </Card>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/**
 * /market/{source}/{plugin}, for every member (docs/market.md): the plugin's status on this hub, its tier, price and
 * id; what it is for, its latest release and what that release asks for; its publisher and the identity that signs its
 * releases, and whether the hub trusts it. A hub admin installs or upgrades it from here; a project's admin asks one to.
 */
export function MarketPluginPage({ source, plugin, initialError }: { source: number; plugin: string; initialError: ApiErrorInfo | null }) {
  const state = useHubQuery(marketPluginQuery(browserApi, source, plugin), initialError);
  return (
    <QueryView state={state} loading={<ListSkeleton rows={6} />}>
      {(found) => <DetailBody plugin={found} />}
    </QueryView>
  );
}
