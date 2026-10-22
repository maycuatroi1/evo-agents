import { FolderOpen, type LucideIcon, Paperclip, Wrench } from "lucide-react";

import type { FileSource } from "@/lib/outputs";

/**
 * Each source's icon: a file the agent sent (attached), one a tool's result held, one of the run's output folder, and
 * one a person attached to a chat message.
 */
export const SOURCE_ICON: Record<FileSource, LucideIcon> = { sent: Paperclip, tool: Wrench, outputs: FolderOpen, attached: Paperclip };
