"use client";

import { ArrowUpCircle, Download, Send, ShieldQuestion } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { isApiError } from "@/lib/api/errors";

import { useMarketFailure, useMarketInstall, useMarketRequest } from "./data";
import { isUntrusted, storeAction } from "./model";
import { storePluginHref, type MarketPluginDetail } from "./queries";

/**
 * Install or upgrade a free isolated plugin of a market, for hub admins (docs/market.md). When the hub does not trust
 * the signer the market names for the publisher yet, a dialog asks first: it names the publisher and the identity that
 * signs its releases, and installing from there trusts that signer from now on. Where the operator locked the
 * publishers, or the publisher is a core one, nothing is offered but why. Each result is a toast.
 */
export function InstallActions({ plugin }: { plugin: MarketPluginDetail }) {
  const t = useTranslations("market.install");
  const install = useMarketInstall();
  const failure = useMarketFailure();
  const [asking, setAsking] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const action = storeAction(plugin);
  if (action === null) return null;

  const run = async (trust: boolean) => {
    setRefused(null);
    try {
      const done = await install.mutateAsync({ source: plugin.source, plugin: plugin.id, trust });
      const version = done.plugin.installed_version ?? "";
      const text = done.upgraded ? t("upgraded", { name: plugin.name, version }) : t("installed", { name: plugin.name, version });
      notify({
        tone: "success",
        text,
        description: done.trusted_publisher ? t("trustedNow", { publisher: plugin.publisher.name ?? plugin.publisher.id }) : null,
        link: { label: t("openStore"), href: storePluginHref(plugin.id) },
      });
      setAsking(false);
    } catch (error) {
      if (!trust && isApiError(error) && error.info.status === 409 && isUntrusted(error.info.message) && plugin.may_trust) {
        setAsking(true);
        return;
      }
      const found = failure(error);
      if (asking) setRefused(found);
      else notifyFailure(t("failed", { name: plugin.name }), found);
    }
  };

  const blocked = !plugin.trusted && !plugin.may_trust;
  const start = () => (plugin.trusted ? void run(false) : setAsking(true));
  const label = action === "upgrade" ? t("upgrade", { version: plugin.latest?.version ?? "" }) : t("install");
  return (
    <>
      <Button
        busy={install.isPending && !asking}
        disabled={blocked || install.isPending}
        onClick={start}
        data-testid="market-install"
      >
        {action === "upgrade" ? <ArrowUpCircle aria-hidden="true" /> : <Download aria-hidden="true" />}
        {install.isPending && !asking ? t("installing") : label}
      </Button>
      <Dialog
        open={asking}
        onOpenChange={(open) => {
          if (!open && !install.isPending) {
            setAsking(false);
            setRefused(null);
          }
        }}
      >
        <DialogContent className="sm:max-w-md" data-testid="market-trust-dialog">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 [overflow-wrap:anywhere]">
              <ShieldQuestion className="size-4 shrink-0 text-attention" aria-hidden="true" />
              {t("trustTitle", { publisher: plugin.publisher.name ?? plugin.publisher.id })}
            </DialogTitle>
            <DialogDescription>{t("trustDescription", { name: plugin.name })}</DialogDescription>
          </DialogHeader>
          <dl className="grid min-w-0 gap-x-4 gap-y-2 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
            <dt className="text-[13px] text-muted-foreground">{t("publisher")}</dt>
            <dd className="min-w-0 font-mono text-xs [overflow-wrap:anywhere]">{plugin.publisher.id}</dd>
            <dt className="text-[13px] text-muted-foreground">{t("signer")}</dt>
            <dd className="min-w-0 font-mono text-xs [overflow-wrap:anywhere]" data-testid="market-trust-identity">
              {plugin.publisher.identity}
            </dd>
          </dl>
          {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
          <DialogFooter>
            <Button variant="outline" size="lg" onClick={() => setAsking(false)} disabled={install.isPending}>
              {t("cancel")}
            </Button>
            <Button size="lg" busy={install.isPending} onClick={() => void run(true)} data-testid="market-trust-confirm">
              {install.isPending ? t("installing") : t("trustConfirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/**
 * A project admin's request that a hub admin install a plugin, for a member who is no hub admin: the hub's admins get
 * an Inbox notice naming the project, and asking again while one is open says so instead of asking twice.
 */
export function RequestInstall({ plugin, projects }: { plugin: MarketPluginDetail; projects: string[] }) {
  const t = useTranslations("market.request");
  const id = useId();
  const request = useMarketRequest();
  const failure = useMarketFailure();
  const [project, setProject] = useState(projects[0] ?? "");
  if (projects.length === 0) return <p className="text-sm text-muted-foreground">{t("noProjects")}</p>;

  const send = async () => {
    try {
      const done = await request.mutateAsync({ source: plugin.source, plugin: plugin.id, project });
      notify({ tone: "success", text: done.created ? t("sent", { project }) : t("already", { project }) });
    } catch (error) {
      notifyFailure(t("failed"), failure(error));
    }
  };

  return (
    <form
      className="flex min-w-0 flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        void send();
      }}
      data-testid="market-request"
    >
      <p className="text-sm text-pretty text-muted-foreground">{t("description")}</p>
      <div className="flex min-w-0 flex-col gap-1.5">
        <Label htmlFor={id}>{t("project")}</Label>
        <NativeSelect id={id} value={project} onChange={(event) => setProject(event.target.value)} data-testid="market-request-project">
          {projects.map((name) => (
            <NativeSelectOption key={name} value={name}>
              {name}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </div>
      <Button type="submit" variant="outline" busy={request.isPending} className="self-start" data-testid="market-request-send">
        <Send aria-hidden="true" />
        {request.isPending ? t("sending") : t("send")}
      </Button>
    </form>
  );
}
