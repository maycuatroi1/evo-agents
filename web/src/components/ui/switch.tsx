"use client"

import * as React from "react"
import { cn } from "cn"
import { Switch as SwitchPrimitive } from "radix-ui"

/**
 * Radix switch (`role="switch"`, `aria-checked`) in the kit's control look: an on/off setting that applies as soon as
 * it is flipped. The track is ink (`primary`, the action colour) when on and `border-control` (`input`, 3:1 on a card)
 * when off, the thumb on `on-action` or `surface`, and the thumb moves, so its state is not told by colour alone. The
 * focus outline is the global 2 px `ring`; the hit area reaches 44 by 44 px. Name it with a `<label htmlFor>`.
 */
function Switch({
  className,
  ...props
}: React.ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      className={cn(
        "peer relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-transparent transition-colors after:absolute after:-inset-x-1 after:-inset-y-3 disabled:cursor-not-allowed disabled:opacity-45 aria-busy:cursor-progress data-[state=checked]:bg-primary data-[state=unchecked]:bg-input",
        className
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        data-slot="switch-thumb"
        className="pointer-events-none block size-4 rounded-full shadow-raised ring-0 transition-transform data-[state=checked]:translate-x-4 data-[state=checked]:bg-primary-foreground data-[state=unchecked]:translate-x-0.5 data-[state=unchecked]:bg-card"
      />
    </SwitchPrimitive.Root>
  )
}

export { Switch }
