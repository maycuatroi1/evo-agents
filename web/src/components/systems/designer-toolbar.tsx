"use client";

import { FlaskConical, LayoutGrid, Plus, Redo2, Save, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { canAdd, NODE_KINDS, type NodeKind } from "@/lib/systems";

import { KIND_ICONS } from "./designer-nodes";
import type { Designer } from "./use-designer";

type Props = {
  designer: Designer;
  canChange: boolean;
  errors: number;
  onAdd: (kind: NodeKind) => void;
  onLayout: () => void;
  onTest: () => void;
  /** Why Test cannot start, or null when it can. */
  testBlocked: string | null;
};

function IconButton({ label, onClick, disabled, testId, children }: { label: string; onClick: () => void; disabled: boolean; testId: string; children: React.ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button type="button" variant="ghost" size="icon" aria-label={label} onClick={onClick} disabled={disabled} data-testid={testId}>
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

/**
 * The bar above the canvas: Add (the palette of node kinds; one entry, curator and orchestrator a system), undo and
 * redo, auto layout, then what the draft is (saved at revision N, unsaved changes, the errors to fix), Test and Save.
 */
export function DesignerToolbar({ designer, canChange, errors, onAdd, onLayout, onTest, testBlocked }: Props) {
  const t = useTranslations("systems.designer");
  const { spec, dirty, save, revision, checking } = designer;
  const saving = save.status === "saving";
  const state = saving ? t("saving") : dirty ? t("unsaved") : t("savedAt", { revision });
  return (
    <div className="flex flex-wrap items-center gap-1.5 border-b bg-card px-2 py-1.5" role="toolbar" aria-label={t("toolbar")} data-testid="system-toolbar">
      {canChange ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="outline" size="sm" data-testid="system-add">
              <Plus aria-hidden="true" />
              {t("add")}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-64" data-testid="system-palette">
            <DropdownMenuLabel>{t("palette")}</DropdownMenuLabel>
            {NODE_KINDS.map((kind) => {
              const Icon = KIND_ICONS[kind];
              const allowed = canAdd(spec, kind);
              return (
                <DropdownMenuItem key={kind} disabled={!allowed} onSelect={() => onAdd(kind)} data-testid={`system-add-${kind}`}>
                  <Icon aria-hidden="true" />
                  <span className="flex min-w-0 flex-col">
                    <span>{t(`kinds.${kind}`)}</span>
                    <span className="text-xs text-muted-foreground">{allowed ? t(`kindHint.${kind}`) : t("onlyOne")}</span>
                  </span>
                </DropdownMenuItem>
              );
            })}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}
      {canChange ? (
        <>
          <IconButton label={t("undo")} onClick={designer.undo} disabled={!designer.canUndo || saving} testId="system-undo">
            <Undo2 aria-hidden="true" />
          </IconButton>
          <IconButton label={t("redo")} onClick={designer.redo} disabled={!designer.canRedo || saving} testId="system-redo">
            <Redo2 aria-hidden="true" />
          </IconButton>
          <IconButton label={t("layout")} onClick={onLayout} disabled={saving || spec.nodes.length < 2} testId="system-layout">
            <LayoutGrid aria-hidden="true" />
          </IconButton>
        </>
      ) : null}
      <div className="ml-auto flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground" aria-live="polite" data-testid="system-state" data-dirty={dirty ? "true" : "false"}>
          {state}
          {checking ? null : errors > 0 ? <span className="text-danger">{`, ${t("errorCount", { count: errors })}`}</span> : null}
        </span>
        <Button type="button" variant="outline" size="sm" onClick={onTest} disabled={testBlocked !== null} title={testBlocked ?? undefined} data-testid="system-test">
          <FlaskConical aria-hidden="true" />
          {t("test")}
        </Button>
        {canChange ? (
          <Button type="button" size="sm" busy={saving} onClick={() => void designer.saveDraft()} disabled={!dirty || errors > 0 || designer.conflict !== null} data-testid="system-save">
            <Save aria-hidden="true" />
            {saving ? t("saving") : t("save")}
          </Button>
        ) : null}
      </div>
    </div>
  );
}
