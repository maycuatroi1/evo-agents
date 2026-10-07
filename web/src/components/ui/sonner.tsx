"use client"

import { CircleCheck, CircleX, Info, LoaderCircle, TriangleAlert } from "lucide-react"
import { useTheme } from "next-themes"
import type { CSSProperties } from "react"
import { Toaster as Sonner, type ToasterProps } from "sonner"

/**
 * shadcn/ui's Toaster (Sonner) on the kit's tokens (web/DESIGN.md, Toasts): bottom right, 24 px from the edges (16 on
 * phones), above dialogs and sheets at z-index 60, toasts on `surface-raised` inside a `border` edge with
 * `shadow-popover` and container corners, Lucide icons in the tone's text colour. The hub's own toasts are drawn by
 * `components/feedback/toast.tsx`; these defaults keep any plain `toast()` in the same look.
 */
const Toaster = ({ ...props }: ToasterProps) => {
  const { resolvedTheme } = useTheme()

  return (
    <Sonner
      theme={resolvedTheme === "dark" ? "dark" : "light"}
      position="bottom-right"
      offset={24}
      mobileOffset={16}
      gap={8}
      visibleToasts={4}
      className="toaster group"
      icons={{
        success: <CircleCheck className="size-4 text-success" />,
        info: <Info className="size-4 text-muted-foreground" />,
        warning: <TriangleAlert className="size-4 text-attention" />,
        error: <CircleX className="size-4 text-danger" />,
        loading: <LoaderCircle className="size-4 animate-spin text-running" />,
      }}
      style={
        {
          zIndex: 60,
          "--width": "380px",
          "--normal-bg": "var(--surface-raised)",
          "--normal-text": "var(--foreground)",
          "--normal-border": "var(--border)",
          "--border-radius": "var(--radius-md)",
        } as CSSProperties
      }
      toastOptions={{
        classNames: {
          toast: "font-sans text-[13px] leading-[18px] shadow-popover",
          title: "font-medium text-foreground",
          description: "text-muted-foreground",
        },
      }}
      {...props}
    />
  )
}

export { Toaster }
