"use client";

import { ArrowUpCircle, Download, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { ConfirmAction } from "@/components/feedback/confirm-action";
import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { type Plugin } from "@/lib/plugin-queries";

import { adminPluginHref } from "./queries";
import { type StoreAction, usePluginFailure, useStoreAction } from "./data";
import { ImageTag } from "./parts";

/**
 * Whether `plugin` is a core plugin of the hub's image, which the store neither installs, upgrades nor removes. A
 * version of signature kind image says so; in the list, which has no versions, a core plugin installed by nobody.
 */
export function isFromImage(plugin: Plugin, versions?: readonly { signature_kind: string }[]): boolean {
  if (versions) return versions.some((version) => version.signature_kind === "image");
  return plugin.tier === "core" && plugin.state !== "available" && plugin.installed_by === null;
}

type Asked = { action: StoreAction; version: string | null };

const DONE = { install: "installed", upgrade: "upgraded", remove: "removed" } as const satisfies Record<StoreAction, string>;

/**
 * Install, upgrade and remove a plugin of the store, for hub admins (web/DESIGN.md, Plugins). Install goes at once:
 * nothing runs until a project enables the plugin. Upgrade asks first, saying that projects keep what they granted and
 * that a permission the new version adds waits for each project's admin. Remove asks as every destructive action does:
 * projects lose the plugin's pages, routes and tools, and keep their grants and config for when it comes back. A core
 * plugin of the hub's image offers none of them; the image installs it. Each result is a toast.
 */
export function StoreActions({
  plugin,
  fromImage,
  size = "default",
  version = null,
}: {
  plugin: Plugin;
  fromImage: boolean;
  size?: "sm" | "default";
  version?: string | null;
}) {
  const t = useTranslations("plugins.actions");
  const action = useStoreAction();
  const failure = usePluginFailure();
  const [asked, setAsked] = useState<Asked | null>(null);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const link = { label: t("openPlugin"), href: adminPluginHref(plugin.name) };

  const run = async ({ action: verb, version: wanted }: Asked) => {
    setRefused(null);
    try {
      const done = await action.mutateAsync({ name: plugin.name, action: verb, version: wanted });
      const shown = done.installed_version ?? plugin.installed_version ?? "";
      notify({ tone: "success", text: t(DONE[verb], { name: plugin.title, version: shown }), link });
      setAsked(null);
    } catch (error) {
      const found = failure(error);
      if (verb === "install") {
        notify({ tone: "error", text: t("installFailed", { name: plugin.title }), description: found.text, detail: found.detail, requestId: found.requestId });
      } else setRefused(found);
    }
  };

  const busy = (verb: StoreAction) => action.isPending && action.variables?.action === verb;
  const close = (open: boolean) => {
    if (!open && !action.isPending) {
      setAsked(null);
      setRefused(null);
    }
  };

  if (fromImage) return <ImageTag />;
  return (
    <>
      {plugin.state === "available" ? (
        <Button
          size={size}
          variant="outline"
          busy={busy("install")}
          onClick={() => void run({ action: "install", version })}
          data-testid="plugin-install"
        >
          <Download aria-hidden="true" />
          {busy("install") ? t("installing") : version ? t("installVersion", { version }) : t("install")}
        </Button>
      ) : null}
      {plugin.upgrade_to ? (
        <Button size={size} variant="outline" onClick={() => setAsked({ action: "upgrade", version: plugin.upgrade_to })} data-testid="plugin-upgrade-open">
          <ArrowUpCircle aria-hidden="true" />
          {t("upgradeTo", { version: plugin.upgrade_to })}
        </Button>
      ) : null}
      {plugin.state !== "available" && size === "default" ? (
        <Button size={size} variant="quiet-danger" onClick={() => setAsked({ action: "remove", version: null })} data-testid="plugin-remove-open">
          <Trash2 aria-hidden="true" />
          {t("remove")}
        </Button>
      ) : null}

      <Dialog open={asked?.action === "upgrade"} onOpenChange={close}>
        <DialogContent className="sm:max-w-md" data-testid="plugin-upgrade-dialog">
          <DialogHeader>
            <DialogTitle className="[overflow-wrap:anywhere]">
              {t("upgradeTitle", { name: plugin.title, version: asked?.version ?? "" })}
            </DialogTitle>
            <DialogDescription>
              {t("upgradeDescription", { from: plugin.installed_version ?? "", to: asked?.version ?? "" })}
            </DialogDescription>
          </DialogHeader>
          {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
          <DialogFooter>
            <Button variant="outline" size="lg" onClick={() => close(false)} disabled={action.isPending}>
              {t("cancel")}
            </Button>
            <Button size="lg" busy={busy("upgrade")} onClick={() => asked && void run(asked)} data-testid="plugin-upgrade-confirm">
              {busy("upgrade") ? t("upgrading") : t("upgradeConfirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmAction
        open={asked?.action === "remove"}
        onOpenChange={close}
        title={t("removeTitle", { name: plugin.title })}
        description={<p>{t("removeDescription")}</p>}
        confirmLabel={t("removeConfirm")}
        pendingLabel={t("removing")}
        cancelLabel={t("cancel")}
        pending={busy("remove")}
        error={refused}
        onConfirm={() => void run({ action: "remove", version: null })}
        testId="plugin-remove"
      />
    </>
  );
}
