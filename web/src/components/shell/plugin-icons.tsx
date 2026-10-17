"use client";

import {
  BookOpen,
  Boxes,
  Bug,
  Calendar,
  ChartColumn,
  ClipboardList,
  FileText,
  FolderKanban,
  Gauge,
  Kanban,
  LayoutDashboard,
  ListChecks,
  type LucideIcon,
  MessageSquare,
  NotebookPen,
  Puzzle,
  ShieldCheck,
  Table,
  Workflow,
} from "lucide-react";

/**
 * The icons a plugin's page may name for its menu item (plugin.schema.json, $defs.page.icon): a fixed set, so a plugin
 * never brings an icon of its own into the shell; a name this web does not know is the puzzle.
 */
export const PLUGIN_ICONS: Record<string, LucideIcon> = {
  puzzle: Puzzle,
  "file-text": FileText,
  "list-checks": ListChecks,
  "layout-dashboard": LayoutDashboard,
  table: Table,
  kanban: Kanban,
  "book-open": BookOpen,
  "chart-column": ChartColumn,
  calendar: Calendar,
  "clipboard-list": ClipboardList,
  "notebook-pen": NotebookPen,
  bug: Bug,
  "folder-kanban": FolderKanban,
  "message-square": MessageSquare,
  gauge: Gauge,
  "shield-check": ShieldCheck,
  workflow: Workflow,
  boxes: Boxes,
};

/** The icon of a plugin's menu item, decorative beside its title. */
export default function PluginIconOf({ name }: { name: string }) {
  const Icon = PLUGIN_ICONS[name] ?? Puzzle;
  return <Icon aria-hidden="true" />;
}
