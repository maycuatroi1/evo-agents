"use client";

import { Package } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { Tag } from "@/components/data/identifier";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import type { AgentPack } from "@/lib/agents";
import { MAX_PACKS } from "@/lib/agent-rules";
import { cn } from "@/lib/utils";

import { FieldError } from "./form-parts";
import { type PackPin, packKey, withoutPack, withPack } from "./model";

/** A row of the list: a pack the project offers, or one the agent attaches that the project no longer offers. */
type Row = { key: string; plugin: string; name: string; detail: string | null; versions: string[]; installed: string | null };

/** The packs offered, in the hub's order, then those attached that it no longer offers, in the agent's order. */
export function packRows(offered: readonly AgentPack[], packs: readonly PackPin[]): Row[] {
  const rows: Row[] = offered.map((pack) => ({
    key: pack.pack,
    plugin: pack.plugin,
    name: pack.name,
    detail: [pack.title, pack.description].filter(Boolean).join(": ") || null,
    versions: pack.versions,
    installed: pack.installed_version,
  }));
  const known = new Set(rows.map((row) => row.key));
  const gone = packs.filter((pin) => !known.has(packKey(pin)));
  return [...rows, ...gone.map((pin) => ({ key: packKey(pin), plugin: pin.plugin, name: pin.name, detail: null, versions: [], installed: null }))];
}

/**
 * The agent packs the agent attaches, in its form (web/DESIGN.md, Agents): the packs of the plugins the project
 * enabled, each picked with a box and pinned at one of the versions the hub holds, the one installed now when picked.
 * A pack the agent attaches that the project no longer offers (its plugin turned off, its version gone) stays listed
 * with a tag, so the admin can remove it; the hub refuses the packs whole while one is.
 */
export function PacksField({
  project,
  value,
  onChange,
  offered,
  loading,
  failed,
  problem,
  disabled = false,
}: {
  project: string;
  value: PackPin[];
  onChange: (value: PackPin[]) => void;
  offered: AgentPack[];
  loading: boolean;
  failed: boolean;
  problem: "tooMany" | undefined;
  disabled?: boolean;
}) {
  const t = useTranslations("agents.form.packs");
  const ids = useId();
  const rows = packRows(offered, value);
  const pinned = new Map(value.map((pin) => [packKey(pin), pin]));

  return (
    <section className="flex min-w-0 flex-col gap-2.5 rounded-md border px-3 py-3 sm:px-4" aria-labelledby={`${ids}-title`} aria-describedby={`${ids}-hint`} data-testid="agent-packs-field">
      <h3 id={`${ids}-title`} className="flex items-center gap-2 text-sm font-medium">
        <Package className="size-4 text-muted-foreground" aria-hidden="true" />
        {t("title")}
      </h3>
      <p id={`${ids}-hint`} className="text-xs text-pretty text-muted-foreground">
        {t("hint")}
      </p>
      {failed ? <p className="text-xs text-attention">{t("failed")}</p> : null}
      {loading ? (
        <Skeleton className="h-16 w-full" />
      ) : rows.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground" data-testid="agent-packs-none">
          {failed ? t("noneRead") : t("none", { project })}
        </p>
      ) : (
        <ul className="flex max-h-72 flex-col overflow-y-auto rounded-md border" aria-label={t("choices")} data-testid="agent-packs-options">
          {rows.map((row) => {
            const pin = pinned.get(row.key);
            const versions = pin && !row.versions.includes(pin.version) ? [pin.version, ...row.versions] : row.versions;
            const pick = (version: string) => onChange(withPack(value, { plugin: row.plugin, name: row.name, version }));
            return (
              <li key={row.key} className={cn("flex min-h-9 flex-wrap items-center gap-x-3 gap-y-1.5 border-t px-3 py-1.5 first:border-t-0 max-md:min-h-11", pin && "bg-surface-selected")} data-testid="agent-pack-option" data-value={row.key}>
                <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-2.5">
                  <input
                    type="checkbox"
                    checked={Boolean(pin)}
                    disabled={disabled || (!pin && row.versions.length === 0)}
                    onChange={(event) => (event.target.checked ? pick(row.installed ?? row.versions[0]) : onChange(withoutPack(value, row.key)))}
                    className="size-4 shrink-0 cursor-pointer accent-primary"
                    data-testid="agent-pack-check"
                  />
                  <span className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2">
                    <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{row.key}</span>
                    {row.detail ? <span className="text-xs text-fg-subtle [overflow-wrap:anywhere]">{row.detail}</span> : null}
                    {row.installed === null ? <Tag className="text-attention">{t("gone")}</Tag> : null}
                  </span>
                </label>
                {pin ? (
                  <NativeSelect
                    size="sm"
                    value={pin.version}
                    onChange={(event) => pick(event.target.value)}
                    disabled={disabled}
                    aria-label={t("version", { pack: row.key })}
                    className="max-w-48"
                    data-testid="agent-pack-version"
                  >
                    {versions.map((version) => (
                      <NativeSelectOption key={version} value={version}>
                        {version === row.installed ? t("installed", { version }) : row.versions.includes(version) ? version : t("goneVersion", { version })}
                      </NativeSelectOption>
                    ))}
                  </NativeSelect>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      <p className="text-xs text-fg-subtle" aria-live="polite" data-testid="agent-packs-count">
        {value.length === 0 ? t("noneAttached") : t("count", { count: value.length, max: MAX_PACKS })}
      </p>
      {problem ? <FieldError text={t("tooMany", { max: MAX_PACKS })} /> : null}
    </section>
  );
}
