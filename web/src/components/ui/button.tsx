import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "cn"
import { LoaderCircle } from "lucide-react"
import { Slot } from "radix-ui"

/** The kit's secondary: a surface fill inside a border-strong edge. `outline` is the same button under shadcn's name. */
const SECONDARY =
  "border-border-strong bg-card text-foreground hover:bg-accent hover:text-foreground aria-expanded:bg-accent aria-expanded:text-foreground"

/** Every variant but `link` takes the 44 px touch target under 768 px; a link sits inside text and keeps its line. */
const CONTROLS = ["default", "secondary", "outline", "ghost", "quiet-danger", "destructive"] as const

/**
 * Buttons of the evo-agents hub UI kit (web/DESIGN.md): ink for the one main action of a view, colour kept for state.
 * The focus outline is the global 2 px `ring` of globals.css. A busy button (`busy`) says so with `aria-busy`, swaps
 * its icon for a turning loader-circle and ignores clicks; the caller gives it the -ing label ("Dispatching").
 */
const buttonVariants = cva(
  "group/button inline-flex shrink-0 items-center justify-center rounded-sm border border-transparent bg-clip-padding font-medium whitespace-nowrap transition-colors select-none disabled:pointer-events-none disabled:opacity-45 aria-disabled:cursor-not-allowed aria-busy:cursor-progress aria-busy:[&>svg:not([data-slot=button-spinner])]:hidden aria-invalid:border-danger aria-invalid:ring-3 aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        /** The primary action, one per view: ink, darkest in light and lightest in dark. */
        default: "bg-primary text-primary-foreground hover:bg-action-hover",
        /** Actions beside the primary: View diff, Rerun, Take over. */
        secondary: SECONDARY,
        outline: SECONDARY,
        /** Toolbar and row actions, and every icon-only button. */
        ghost:
          "text-muted-foreground hover:bg-accent hover:text-foreground aria-expanded:bg-accent aria-expanded:text-foreground",
        /** A reversible stop, such as Cancel run. */
        "quiet-danger": "text-danger hover:bg-danger-soft aria-expanded:bg-danger-soft",
        /** Only inside the confirm step of something permanent; darkens on hover so the label keeps its ratio. */
        destructive: "bg-danger-solid text-on-danger hover:brightness-94",
        link: "text-brand underline-offset-4 hover:text-brand-hover hover:underline",
      },
      size: {
        /** 32 px, the control height on desktop. */
        default:
          "h-8 gap-1.5 px-3 text-sm has-data-[icon=inline-end]:pr-2.5 has-data-[icon=inline-start]:pl-2.5",
        /** 28 px, in dense toolbars and table rows. */
        sm: "h-7 gap-1 px-2.5 text-[13px] has-data-[icon=inline-end]:pr-2 has-data-[icon=inline-start]:pl-2 [&_svg:not([class*='size-'])]:size-3.5",
        /** 40 px, in dialog footers and beside the composer. */
        lg: "h-10 gap-2 px-4 text-sm has-data-[icon=inline-end]:pr-3.5 has-data-[icon=inline-start]:pl-3.5",
        icon: "size-8",
        "icon-sm": "size-7 [&_svg:not([class*='size-'])]:size-3.5",
        "icon-lg": "size-10",
      },
    },
    compoundVariants: [
      { variant: [...CONTROLS], class: "max-md:min-h-11" },
      { variant: [...CONTROLS], size: ["icon", "icon-sm", "icon-lg"], class: "max-md:min-w-11" },
    ],
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  busy = false,
  children,
  onClick,
  "aria-disabled": ariaDisabled,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
    /** Work this button started is under way: aria-busy, a turning loader-circle in place of its icon, clicks ignored. */
    busy?: boolean
  }) {
  const Comp = asChild ? Slot.Root : "button"

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      aria-busy={busy || undefined}
      aria-disabled={busy || ariaDisabled || undefined}
      onClick={busy ? (event: React.MouseEvent<HTMLButtonElement>) => event.preventDefault() : onClick}
      {...props}
    >
      {busy && !asChild ? (
        <>
          <LoaderCircle
            data-slot="button-spinner"
            className="animate-spin motion-reduce:animate-none"
            aria-hidden="true"
          />
          {children}
        </>
      ) : (
        children
      )}
    </Comp>
  )
}

export { Button, buttonVariants }
