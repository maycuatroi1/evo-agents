"use client";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import type { Agent } from "@/lib/agents";
import { agentInitial, inkOn } from "@/lib/agent-rules";
import { cn } from "@/lib/utils";

type Size = "sm" | "default" | "lg" | "xl";

const SIZES: Record<Size, { root: string; text: string }> = {
  sm: { root: "size-6", text: "text-xs" },
  default: { root: "size-8", text: "text-sm" },
  lg: { root: "size-10", text: "text-base" },
  xl: { root: "size-16", text: "text-2xl" },
};

/**
 * An agent's face (web/DESIGN.md, Agents): its avatar image, or while there is none (or it has not loaded) the first
 * letter of its name on its colour, in white or ink, whichever reads better on that colour. Round, as avatars are.
 * It is decorative next to the agent's name; give `label` where it stands alone. `src` shows an image picked but not
 * saved yet (a preview in the form).
 */
export function AgentAvatar({
  agent,
  size = "default",
  src,
  label,
  className,
}: {
  agent: Pick<Agent, "name" | "handle" | "color" | "icon_url">;
  size?: Size;
  src?: string | null;
  label?: string;
  className?: string;
}) {
  const image = src ?? agent.icon_url;
  const look = SIZES[size];
  return (
    <Avatar
      className={cn(look.root, className)}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      data-testid="agent-avatar"
      data-avatar={image ? "image" : "initial"}
    >
      {image ? <AvatarImage src={image} alt="" /> : null}
      <AvatarFallback
        className={cn("font-semibold select-none", look.text)}
        style={{ backgroundColor: agent.color, color: inkOn(agent.color) }}
      >
        {agentInitial(agent)}
      </AvatarFallback>
    </Avatar>
  );
}
