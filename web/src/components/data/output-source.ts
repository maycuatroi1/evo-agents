import { FolderOpen, type LucideIcon, Paperclip, UserRound, Wrench } from "lucide-react";

import type { FileSource } from "@/lib/outputs";

/** Each source's icon: a file the agent sent, one a tool's result held, one of the run's output folder, one a person attached. */
export const SOURCE_ICON: Record<FileSource, LucideIcon> = { sent: Paperclip, tool: Wrench, outputs: FolderOpen, attached: UserRound };
