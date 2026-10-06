import * as React from "react"
import { cn } from "cn"

/** A keyboard key of the kit: mono 11 px on `card` inside a `border-strong` edge, its bottom edge doubled. */
function Kbd({ className, ...props }: React.ComponentProps<"kbd">) {
  return (
    <kbd
      data-slot="kbd"
      className={cn(
        "inline-flex h-[18px] min-w-[18px] shrink-0 items-center justify-center rounded-xs border border-b-2 border-border-strong bg-card px-1 font-mono text-[11px] leading-none font-medium text-muted-foreground",
        className
      )}
      {...props}
    />
  )
}

export { Kbd }
