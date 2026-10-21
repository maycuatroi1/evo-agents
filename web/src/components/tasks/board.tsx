"use client";

import { useTranslations } from "next-intl";
import { type DragEvent, useId, useState } from "react";

import { TONE_TEXT } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { useIsMobile } from "@/hooks/use-mobile";
import { cn } from "@/lib/utils";

import type { Task, TaskPerson, TasksSettings } from "./api";
import { groupLook } from "./badges";
import type { BoardColumn } from "./model";
import { TaskCard } from "./task-card";
import type { EditTask } from "./task-controls";

/** A column shows this many cards before "Show all", so a long one does not bury the others; fewer on a phone. */
const COLUMN_LIMIT = 12;
const PHONE_COLUMN_LIMIT = 6;

type Shared = { project: string; settings: TasksSettings; people: readonly TaskPerson[]; writer: boolean; edit: EditTask };

function Column({
  column,
  shared,
  dragged,
  setDragged,
}: {
  column: BoardColumn;
  shared: Shared;
  dragged: Task | null;
  setDragged: (task: Task | null) => void;
}) {
  const t = useTranslations("tasks.board");
  const tControls = useTranslations("tasks.controls");
  const id = useId();
  const [expanded, setExpanded] = useState(false);
  const [over, setOver] = useState(false);
  const limit = useIsMobile() ? PHONE_COLUMN_LIMIT : COLUMN_LIMIT;
  const shown = expanded ? column.tasks : column.tasks.slice(0, limit);
  const look = groupLook(column.group);
  const name = column.key === null ? t("otherColumn") : column.name;
  const accepts = shared.writer && dragged !== null && column.key !== null && dragged.status !== column.key;
  const drop = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    setOver(false);
    if (!accepts || !dragged || column.key === null) return;
    const task = dragged;
    setDragged(null);
    void shared.edit(task, { status: column.key }, tControls("moved", { ref: task.ref, status: column.name }));
  };
  return (
    <section
      aria-labelledby={`${id}-heading`}
      className={cn(
        "flex min-w-0 snap-start flex-col gap-2 rounded-md border bg-muted/30 p-2.5 transition-colors",
        over && "border-brand bg-surface-selected",
      )}
      onDragOver={(event) => {
        if (!accepts) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        setOver(true);
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOver(false);
      }}
      onDrop={drop}
      data-testid="board-column"
      data-status={column.key ?? "other"}
    >
      <h2 id={`${id}-heading`} className="flex items-center gap-2 px-1 text-sm font-medium">
        <look.icon className={cn("size-4", TONE_TEXT[look.tone])} aria-hidden="true" />
        <span className="min-w-0 truncate">{name}</span>
        <span className="ml-auto font-mono text-xs text-muted-foreground tabular-nums" data-testid="column-count">
          {column.tasks.length}
        </span>
      </h2>
      {column.tasks.length === 0 ? (
        <p className="rounded-sm border border-dashed px-1 py-4 text-center text-xs text-muted-foreground">{t("emptyColumn")}</p>
      ) : (
        <ol id={`${id}-list`} className="flex flex-col gap-2" aria-labelledby={`${id}-heading`}>
          {shown.map((task) => (
            <TaskCard
              key={task.number}
              task={task}
              {...shared}
              dragging={dragged?.number === task.number}
              onDragStart={setDragged}
              onDragEnd={() => setDragged(null)}
            />
          ))}
        </ol>
      )}
      {column.tasks.length > limit ? (
        <Button
          variant="ghost"
          className="w-full text-muted-foreground"
          aria-expanded={expanded}
          aria-controls={`${id}-list`}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? t("showFewer") : t("showAll", { count: column.tasks.length })}
        </Button>
      ) : null}
    </section>
  );
}

/**
 * The columns of a board, one per status (model.boardColumns), in a region that scrolls sideways on its own when they
 * do not fit, so the page never does; on a phone each column takes most of the width and snaps into place. The region
 * is positioned, so the words kept for screen readers (`sr-only`, absolute) inside a column out of view are clipped
 * with it rather than widening the page. A writer drops a card on another column to move it there.
 */
export function Board({ columns, ...shared }: Shared & { columns: BoardColumn[] }) {
  const t = useTranslations("tasks.board");
  const [dragged, setDragged] = useState<Task | null>(null);
  return (
    <div
      role="region"
      aria-label={t("label")}
      tabIndex={0}
      className="relative grid snap-x snap-mandatory auto-cols-[85%] grid-flow-col gap-3 overflow-x-auto pb-2 md:snap-none md:auto-cols-[minmax(13.5rem,1fr)]"
      data-testid="task-board"
    >
      {columns.map((column) => (
        <Column key={column.key ?? "other"} column={column} shared={shared} dragged={dragged} setDragged={setDragged} />
      ))}
    </div>
  );
}
