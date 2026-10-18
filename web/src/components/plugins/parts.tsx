"use client";

import {
  ArrowUpCircle,
  Ban,
  Box,
  CircleCheck,
  CircleDashed,
  Cpu,
  KeyRound,
  PackageCheck,
  Plug,
  ShieldCheck,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { type StatusLook, StatusPill, type StatusPillProps } from "@/components/status/status-badge";
import type { Plugin, PluginPermission, ProjectPlugin } from "@/lib/plugin-queries";
import { cn } from "@/lib/utils";

/**
 * The small parts the plugin pages share (web/DESIGN.md, Plugins): the tier as a tag, the state in the store and in a
 * project as a pill (its looks live here, as `change-badge.tsx` keeps a proposal's change), an upgrade waiting, and
 * the list of permissions with what each lets the plugin do.
 */

type StoreState = Plugin["state"];
type ProjectState = "enabled" | "notEnabled" | "disabled";

export const STORE_LOOKS = {
  available: { tone: "neutral", icon: CircleDashed },
  installed: { tone: "success", icon: CircleCheck },
  disabled: { tone: "danger", icon: Ban },
} as const satisfies Record<StoreState, StatusLook>;

export const PROJECT_LOOKS = {
  enabled: { tone: "success", icon: Plug },
  notEnabled: { tone: "neutral", icon: CircleDashed },
  disabled: { tone: "danger", icon: Ban },
} as const satisfies Record<ProjectState, StatusLook>;

export function StoreStateBadge({ state, ...props }: StatusPillProps & { state: StoreState }) {
  const t = useTranslations("plugins.state");
  return (
    <StatusPill look={STORE_LOOKS[state]} label={t(state)} data-status={state} data-testid="plugin-state" {...props} />
  );
}

/** Where a plugin stands in a project: enabled there, not enabled, or turned off on the hub, which wins. */
export function projectState(plugin: ProjectPlugin): ProjectState {
  if (plugin.state === "disabled") return "disabled";
  return plugin.enabled ? "enabled" : "notEnabled";
}

export function ProjectStateBadge({ plugin, ...props }: StatusPillProps & { plugin: ProjectPlugin }) {
  const t = useTranslations("plugins.state");
  const state = projectState(plugin);
  return (
    <StatusPill
      look={PROJECT_LOOKS[state]}
      label={t(state)}
      data-status={state}
      data-testid="plugin-project-state"
      {...props}
    />
  );
}

/** The tier as a tag, what it means as its tooltip and, for screen readers, in words. */
export function TierTag({ tier }: { tier: Plugin["tier"] }) {
  const t = useTranslations("plugins.tier");
  const Icon = tier === "core" ? Cpu : Box;
  const hint = t(tier === "core" ? "coreHint" : "isolatedHint");
  return (
    <Tag title={hint} data-tier={tier} data-testid="plugin-tier">
      <Icon aria-hidden="true" />
      {t(tier)}
      <span className="sr-only">: {hint}</span>
    </Tag>
  );
}

/** The newest version above the installed one that runs on this hub, as an attention tag. */
export function UpgradeTag({ version }: { version: string }) {
  const t = useTranslations("plugins");
  return (
    <Tag className="bg-attention-soft text-attention" data-testid="plugin-upgrade">
      <ArrowUpCircle aria-hidden="true" />
      {t("upgrade", { version })}
    </Tag>
  );
}

/** A core plugin of the hub's image: installed, upgraded and removed with the image, never through the store. */
export function ImageTag() {
  const t = useTranslations("plugins");
  return (
    <Tag data-testid="plugin-from-image">
      <PackageCheck aria-hidden="true" />
      {t("fromImage")}
    </Tag>
  );
}

/** A page's frame is a sandbox: said beside its title, what it means as its tooltip and in words. */
export function SandboxTag() {
  const t = useTranslations("plugins.page");
  return (
    <Tag title={t("sandboxedHint")} data-testid="plugin-sandboxed">
      <ShieldCheck aria-hidden="true" />
      {t("sandboxed")}
      <span className="sr-only">: {t("sandboxedHint")}</span>
    </Tag>
  );
}

/** The permissions a plugin may ask for (evo_agents.plugins.manifest.PERMISSIONS), whose words the web has. */
export const PERMISSION_CODES = [
  "runs:read",
  "runs:dispatch",
  "chats:read",
  "chats:write",
  "plans:read",
  "memories:read",
  "notifications:send",
] as const;
type PermissionCode = (typeof PERMISSION_CODES)[number];

function isPermissionCode(name: string): name is PermissionCode {
  return (PERMISSION_CODES as readonly string[]).includes(name);
}

/**
 * What a plugin asks for: each permission's code and what it lets the plugin do in the project, in the viewer's
 * language (the hub's words for a code this web does not know). `pending` marks those an upgrade added that the
 * project has not granted yet.
 */
export function PermissionList({
  permissions,
  pending = [],
  className,
  testId,
}: {
  permissions: readonly (string | PluginPermission)[];
  pending?: readonly string[];
  className?: string;
  testId?: string;
}) {
  const t = useTranslations("plugins.permissions");
  if (permissions.length === 0) {
    return (
      <p className={cn("text-sm text-muted-foreground", className)} data-testid={testId}>
        {t("none")}
      </p>
    );
  }
  return (
    <ul className={cn("flex flex-col gap-2.5", className)} data-testid={testId}>
      {permissions.map((item) => {
        const name = typeof item === "string" ? item : item.name;
        const text = isPermissionCode(name) ? t(`text.${name}`) : typeof item === "string" ? name : item.text;
        return (
          <li key={name} className="flex items-start gap-2.5" data-permission={name}>
            <KeyRound className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <div className="flex min-w-0 flex-col gap-0.5">
              <span className="flex flex-wrap items-center gap-2">
                <span className="text-sm text-foreground">{text}</span>
                {pending.includes(name) ? <Tag className="bg-attention-soft text-attention">{t("new")}</Tag> : null}
              </span>
              <code className="font-mono text-xs text-fg-subtle">{name}</code>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
