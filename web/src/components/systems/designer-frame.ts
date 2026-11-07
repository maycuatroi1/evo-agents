import type { SystemOut } from "@/lib/systems";

import type { DesignerSlots } from "./slots";

export type DesignerProps = { project: string; system: SystemOut; slots: DesignerSlots };

/** The frame of the designer, which its skeleton and its load error take too, so the page does not jump. */
export const DESIGNER_FRAME = "flex h-[calc(100dvh-13rem)] min-h-[560px] flex-col overflow-hidden rounded-md border bg-card shadow-raised";
