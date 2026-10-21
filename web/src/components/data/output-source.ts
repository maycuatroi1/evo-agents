import { FolderOpen, type LucideIcon, Paperclip, Wrench } from "lucide-react";

import type { OutputSource } from "@/lib/outputs";

/** Each source's icon: a file the agent sent (attached), one a tool's result held, one of the run's output folder. */
export const SOURCE_ICON: Record<OutputSource, LucideIcon> = { sent: Paperclip, tool: Wrench, outputs: FolderOpen };
