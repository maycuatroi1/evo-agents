"use client"

import * as React from "react"
import { Command as CommandPrimitive } from "cmdk"
import { cn } from "cn"
import { SearchIcon } from "lucide-react"

/**
 * shadcn's Command (cmdk) in the radix-nova layout, written by hand from upstream because the registry could not be
 * reached, and set in the kit's CommandPalette look: a 52 px input row over a hairline, overline group headings in
 * `fg-subtle`, 36 px items (44 px under 768 px) with 6 px corners whose icon is `fg-muted` and turns `brand` on the
 * selected item, which sits on `surface-selected`. The palette puts it in a Dialog itself (command-palette.tsx), so
 * the upstream CommandDialog is left out.
 */
function Command({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive>) {
  return (
    <CommandPrimitive
      data-slot="command"
      className={cn(
        "flex h-full w-full flex-col overflow-hidden rounded-[inherit] bg-popover text-popover-foreground",
        className
      )}
      {...props}
    />
  )
}

function CommandInput({
  className,
  wrapperClassName,
  children,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Input> & {
  /** Classes of the row around the field. */
  wrapperClassName?: string
}) {
  return (
    <div
      data-slot="command-input-wrapper"
      className={cn(
        "flex h-13 shrink-0 items-center gap-3 border-b px-4 text-fg-subtle",
        wrapperClassName
      )}
    >
      <SearchIcon className="size-5 shrink-0" aria-hidden="true" />
      <CommandPrimitive.Input
        data-slot="command-input"
        className={cn(
          "h-full min-w-0 flex-1 bg-transparent text-base text-foreground outline-hidden placeholder:text-fg-subtle disabled:cursor-not-allowed disabled:opacity-50 md:text-[15px]",
          className
        )}
        {...props}
      />
      {children}
    </div>
  )
}

function CommandList({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.List>) {
  return (
    <CommandPrimitive.List
      data-slot="command-list"
      className={cn(
        "max-h-95 scroll-py-2 overflow-x-hidden overflow-y-auto overscroll-contain p-2 outline-none",
        className
      )}
      {...props}
    />
  )
}

function CommandEmpty({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Empty>) {
  return (
    <CommandPrimitive.Empty
      data-slot="command-empty"
      className={cn("px-2 py-6 text-center text-[13px] text-muted-foreground", className)}
      {...props}
    />
  )
}

function CommandGroup({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Group>) {
  return (
    <CommandPrimitive.Group
      data-slot="command-group"
      className={cn(
        "overflow-hidden text-foreground [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-[11px] [&_[cmdk-group-heading]]:leading-4 [&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:tracking-[0.06em] [&_[cmdk-group-heading]]:text-fg-subtle [&_[cmdk-group-heading]]:uppercase",
        className
      )}
      {...props}
    />
  )
}

function CommandSeparator({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Separator>) {
  return (
    <CommandPrimitive.Separator
      data-slot="command-separator"
      className={cn("-mx-2 my-1 h-px bg-border", className)}
      {...props}
    />
  )
}

function CommandItem({
  className,
  ...props
}: React.ComponentProps<typeof CommandPrimitive.Item>) {
  return (
    <CommandPrimitive.Item
      data-slot="command-item"
      className={cn(
        "group/command-item relative flex min-h-9 cursor-pointer items-center gap-3 rounded-sm px-2 py-1.5 text-[13px] leading-[18px] text-foreground outline-hidden select-none data-[disabled=true]:pointer-events-none data-[disabled=true]:opacity-45 data-[selected=true]:bg-surface-selected max-md:min-h-11 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4 [&>svg]:text-muted-foreground data-[selected=true]:[&>svg]:text-brand",
        className
      )}
      {...props}
    />
  )
}

function CommandShortcut({
  className,
  ...props
}: React.ComponentProps<"span">) {
  return (
    <span
      data-slot="command-shortcut"
      className={cn(
        "ml-auto flex shrink-0 items-center gap-2 text-xs whitespace-nowrap text-fg-subtle",
        className
      )}
      {...props}
    />
  )
}

export {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
}
