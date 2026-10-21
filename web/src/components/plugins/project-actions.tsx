"use client";

import { KeyRound, LayoutPanelLeft, Plug, PlugZap, Unplug } from "lucide-react";
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
import { pendingPermissions, pluginOn } from "@/lib/plugin-pages";
import { type ProjectPlugin, projectPluginHref } from "@/lib/plugin-queries";

import { useDisablePlugin, useEnablePlugin, usePluginFailure } from "./data";
import { PermissionList } from "./parts";

/**
 * The consent (web/DESIGN.md, Plugins): before an admin of the project enables a plugin, or grants what an upgrade
 * added, the dialog lists every permission the installed version asks for in words, the new ones marked, and the pages
 * it adds to the project's menu. Granting names back exactly that list, as the hub requires, so nobody grants what they
 * did not read. A refusal stays in the dialog; the result is a toast.
 */
function ConsentDialog({
  project,
  plugin,
  open,
  onOpenChange,
}: {
  project: string;
  plugin: ProjectPlugin;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations("plugins.consent");
  const enable = useEnablePlugin(project);
  const failure = usePluginFailure();
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const regrant = plugin.enabled;
  const pending = pendingPermissions(plugin);
  const menu = plugin.pages.filter((page) => page.menu);

  const grant = async () => {
    setRefused(null);
    try {
      await enable.mutateAsync({ name: plugin.name, permissions: plugin.permissions.map((item) => item.name) });
      notify({
        tone: "success",
        text: regrant ? t("granted", { name: plugin.title }) : t("enabled", { name: plugin.title, project }),
        link: { label: t("openPlugin"), href: projectPluginHref(project, plugin.name) },
      });
      onOpenChange(false);
    } catch (error) {
      setRefused(failure(error, { 409: t("turnedOff") }));
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => (enable.isPending ? undefined : onOpenChange(next))}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg" data-testid="plugin-consent">
        <DialogHeader>
          <DialogTitle className="[overflow-wrap:anywhere]">
            {regrant ? t("regrantTitle", { name: plugin.title }) : t("title", { name: plugin.title, project })}
          </DialogTitle>
          <DialogDescription>
            {t("description", { name: plugin.title, version: plugin.version, publisher: plugin.publisher })}
          </DialogDescription>
        </DialogHeader>
        <section className="flex flex-col gap-2.5" aria-labelledby="consent-asks">
          <h3 id="consent-asks" className="flex items-center gap-1.5 text-[13px] font-medium text-muted-foreground">
            <KeyRound className="size-3.5" aria-hidden="true" />
            {t("asks")}
          </h3>
          <PermissionList permissions={plugin.permissions} pending={pending} testId="consent-permissions" />
        </section>
        {menu.length > 0 ? (
          <section className="flex flex-col gap-2" aria-labelledby="consent-pages">
            <h3 id="consent-pages" className="flex items-center gap-1.5 text-[13px] font-medium text-muted-foreground">
              <LayoutPanelLeft className="size-3.5" aria-hidden="true" />
              {t("pages")}
            </h3>
            <p className="text-sm">{menu.map((page) => page.title).join(", ")}</p>
          </section>
        ) : null}
        <p className="text-[13px] text-muted-foreground">{t("note")}</p>
        {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
        <DialogFooter>
          <Button variant="outline" size="lg" onClick={() => onOpenChange(false)} disabled={enable.isPending}>
            {t("cancel")}
          </Button>
          <Button size="lg" busy={enable.isPending} onClick={() => void grant()} data-testid="plugin-consent-grant">
            <PlugZap aria-hidden="true" />
            {enable.isPending ? t("granting") : regrant ? t("regrant") : t("grant")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Enable (through the consent), Review what an upgrade asks for, and Disable a plugin in a project, for an admin of the
 * project. Disabling asks first: the plugin's pages, routes and tools stop answering in the project and its grants go,
 * while its config and data stay for when it is enabled again. A plugin the hub turned off offers neither.
 */
export function ProjectActions({ project, plugin, size = "default" }: { project: string; plugin: ProjectPlugin; size?: "sm" | "default" }) {
  const t = useTranslations("plugins.project");
  const disable = useDisablePlugin(project);
  const failure = usePluginFailure();
  const [consent, setConsent] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  if (plugin.state === "disabled") return null;
  const pending = pendingPermissions(plugin);

  const turnOff = async () => {
    setRefused(null);
    try {
      await disable.mutateAsync(plugin.name);
      notify({ tone: "success", text: t("disabled", { name: plugin.title, project }) });
      setConfirming(false);
    } catch (error) {
      setRefused(failure(error));
    }
  };

  return (
    <>
      {!plugin.enabled ? (
        <Button size={size} variant={size === "sm" ? "outline" : "default"} onClick={() => setConsent(true)} data-testid="plugin-enable">
          <Plug aria-hidden="true" />
          {t("enable")}
        </Button>
      ) : null}
      {pending.length > 0 ? (
        <Button size={size} variant="outline" onClick={() => setConsent(true)} data-testid="plugin-regrant">
          <KeyRound aria-hidden="true" />
          {t("review", { count: pending.length })}
        </Button>
      ) : null}
      {pluginOn(plugin) && size === "default" ? (
        <Button size={size} variant="quiet-danger" onClick={() => setConfirming(true)} data-testid="plugin-disable">
          <Unplug aria-hidden="true" />
          {t("disable")}
        </Button>
      ) : null}
      {consent ? <ConsentDialog project={project} plugin={plugin} open={consent} onOpenChange={setConsent} /> : null}
      <ConfirmAction
        open={confirming}
        onOpenChange={(open) => {
          setConfirming(open);
          if (!open) setRefused(null);
        }}
        title={t("disableTitle", { name: plugin.title, project })}
        description={<p>{t("disableDescription")}</p>}
        confirmLabel={t("disableConfirm")}
        pendingLabel={t("disabling")}
        cancelLabel={t("cancel")}
        pending={disable.isPending}
        error={refused}
        onConfirm={() => void turnOff()}
        testId="plugin-disable-confirm"
      />
    </>
  );
}
