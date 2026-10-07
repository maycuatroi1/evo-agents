# Design system of the hub web

The hub web is the operator console for coding agents that run on the team's own machines. A person opens it to
answer three questions: what needs me, what is running, and what finished. It is read in English first, with
Vietnamese one pick away in the user menu, on laptops first, and it has to work on a 375 px phone.

The design follows the evo-agents hub UI kit ([kit](https://claude.ai/artifact/H8SfKSwspNPCrGtdmX8gdV): README,
`tokens.json`, a README and preview per component), which answers the
[review of the 0.3.0 interface](https://claude.ai/artifact/YHprjCQ9SjqmxSnDM9qXni) (findings F1 to F13). This file is
the source of truth inside the repo; the kit is the design reference behind it. Every UI change loads the
`ui-ux-pro-max` skill. The tokens live in `src/app/globals.css`; components use the Tailwind names (`bg-background`,
`text-muted-foreground`, `bg-running-soft`, `text-term-agent`) and never raw colours.

## Style

Calm graphite surfaces, one cobalt for live agent work, amber only when a person is needed, and IBM Plex for an
engineered voice that reads the same in English and Vietnamese.

1. **Live state first.** A page that shows work in progress says whether it is current, and running work carries a
   pulsing dot.
2. **Colour is spent on state.** The primary button is ink (`action`, the `primary` of shadcn), never a colour.
   Cobalt (`brand`) means an agent is working right now; it also marks links, the focus ring, the selected tab and
   the selected nav icon. Amber (`attention`) means a person is needed. Green, red and violet mean done, failed and
   waiting for review. Everything else is neutral.
3. **Product words, not system words.** "Visibility: Internal", not "Max label level".
4. **Dense, not cramped.** 14 px text, 44 px table rows and 32 px controls on desktop; 44 px touch targets under
   768 px. Tables over cards for lists; one container for a row of numbers.
5. **Every wait says what it waits for.** A queued run names the worker it waits for, a locked button the run that
   holds it.

No gradients, no glass, no coloured left borders on cards, no emoji: state lives in the pill and the dot. Icons are
Lucide only; the GitHub mark is the Simple Icons path.

## Colour

Light and dark are both first-class; the hub follows the system until a person picks one in the user menu
(`next-themes`, class strategy). Ratios are WCAG contrast; the e2e axe run checks the rendered pages in both themes.

**Surfaces and lines**

| Kit token | Tailwind | Light | Dark | Use |
| --- | --- | --- | --- | --- |
| `canvas` | `background` | `#f5f6f8` | `#0a0c10` | the page behind the content |
| `surface` | `card`, `sidebar` | `#ffffff` | `#111419` | cards, tables, the sidebar, the top bar, inputs |
| `surface-sunken` | `muted`, `surface-sunken` | `#eef0f3` | `#0c0e12` | wells: table header, identifier chips, skeletons, disabled fields |
| `surface-raised` | `popover`, `surface-raised` | `#ffffff` | `#181b22` | menus, dialogs, sheets, toasts; lighter than `surface` in dark |
| `surface-hover` | `accent` | `#f0f2f5` | `#1a1e26` | hover of rows, nav items, ghost and outline buttons |
| `surface-selected` | `sidebar-accent`, `surface-selected` | `#e9edfd` | `#1a2246` | the selected nav item, row, pressed chip, checked card |
| `border` | `border`, `chart-grid` | `#e2e5ea` | `#22262f` | hairlines; never the only cue that something is a control |
| `border-strong` | `border-strong` | `#d3d7de` | `#2e333d` | outline (secondary) buttons, segmented controls |
| `border-control` | `input` | `#858c99` (3.4:1 on surface, 3.1:1 on canvas) | `#626a7a` (3.4:1, 3.2:1 on surface-raised) | edges of inputs, selects, radios and checkboxes; under 3:1 on surface-sunken, so an enabled control never sits in a well |

**Text**

| Kit token | Tailwind | Light | Dark | Use |
| --- | --- | --- | --- | --- |
| `fg` | `foreground` | `#0e1116` (18.9:1 on surface, 16.2:1 at the least, on surface-selected) | `#e9ecf1` (15.6:1, 13.0:1 at the least) | primary text and icons |
| `fg-muted` | `muted-foreground` | `#4b5362` (7.7:1, 6.6:1 at the least) | `#a4acba` (8.1:1, 6.8:1 at the least) | secondary text, labels, inactive nav |
| `fg-subtle` | `fg-subtle` | `#646c7b` (5.3:1, 4.5:1 at the least) | `#868f9e` (5.7:1, 4.7:1 at the least) | timestamps, placeholders, helper text; nothing lighter is text |

**Action and brand**

| Kit token | Tailwind | Light | Dark | Use |
| --- | --- | --- | --- | --- |
| `action` / `on-action` | `primary` / `primary-foreground` | `#0e1116` / `#ffffff` (18.9:1) | `#e9ecf1` / `#0a0c10` (16.5:1) | the primary button, one per view |
| `action-hover` | `action-hover` | `#2a303a` (13.3:1 with on-action) | `#c9cfd9` (12.5:1) | primary button hover |
| `brand` | `brand` | `#2f4bd8` (6.7:1 on surface, 5.8:1 on brand-soft and surface-selected) | `#8c9dff` (7.4:1, 6.2:1) | links, the selected tab underline, the selected nav icon, running |
| `brand-hover` | `brand-hover` | `#263fba` (8.4:1) | `#a3b0ff` (9.0:1) | link hover |
| `brand-soft` | `brand-soft` | `#ebeefd` | `#19204a` | ground of the running pill and brand tags |
| `on-brand` | `on-brand` | `#ffffff` (6.7:1 on brand) | `#0a0c10` (7.8:1) | glyphs on a brand fill: the logo tile |
| `focus-ring` | `ring` | = `brand` (6.2:1 on canvas) | = `brand` (7.8:1) | the 2 px keyboard focus outline |

Cobalt is never a large fill; the logo tile is the one exception.

**States.** Each tone has a text token, a soft ground and a solid mark. Text and icons on the soft ground use the text
token; dots, bars and progress segments use the solid one. Ratios are text on the soft ground; marks are at least
3:1 on surface.

| Tone | Text / ground / mark (Tailwind) | Light | Dark | Means |
| --- | --- | --- | --- | --- |
| Running | `running` / `running-soft` / `running` | `#2f4bd8` / `#ebeefd` (5.8:1) | `#8c9dff` / `#19204a` (6.2:1) | an agent is working now: leased, running, busy, plan run active |
| Attention | `attention` / `attention-soft` / `attention-solid` | `#8a4b00` / `#fdf0da` (6.0:1) / `#bc7800` | `#f2b54a` / `#2d2210` (8.5:1) / `#e9a23b` | a person is needed: waiting for you, parked, draining, blocked |
| Review | `review` / `review-soft` / `review-solid` | `#6136c2` / `#f1ecfd` (6.5:1) / `#7c4de8` | `#b9a0ff` / `#241b3e` (7.3:1) / `#9d7df6` | done, waiting to be checked: verifying, review |
| Success | `success` / `success-soft` / `success-solid` | `#11713d` / `#e4f4ea` (5.3:1) / `#1e9e58` | `#5fd394` / `#0f2a1c` (8.2:1) / `#34b86e` | finished well, healthy: done, idle, received heartbeat |
| Danger | `danger` / `danger-soft` / `danger-solid` (`destructive`) | `#b3261e` / `#fcebea` (5.7:1) / `#d93a2f` | `#ff8178` / `#33161a` (6.8:1) / `#ef5a4e` | failed or unreachable: failed, offline, errors, destructive buttons |
| Neutral | `muted-foreground` / `muted` / `neutral-solid` | `#4b5362` / `#eef0f3` (6.8:1) / `#858c99` | `#a4acba` / `#0c0e12` (8.5:1) / `#6b7383` | no tone: queued, lost, cancelled, pending |

`on-danger` (`#ffffff` light, 4.6:1; `#0a0c10` dark, 5.8:1) is the label on a `danger-solid` fill, which darkens on
hover (`hover:brightness-94`) and never lightens, so the label keeps its ratio. `danger-solid` is
a mark and a fill, not text: it falls to 4.2:1 on canvas, so error text is `text-danger`. Amber is the same idea as
the mark's third node, the person in the loop; it is not a warning colour for anything else.

**Terminal.** The log, tool output and the web terminal sit on `term-bg` in both themes. Every colour below is
measured on `term-bg` and holds at least 5.9:1 on the hovered row (`term-row`, `#141820`).

| Token | Value | Ratio | Use |
| --- | --- | --- | --- |
| `term-bg` / `term-border` | `#0b0d11` / `#1e222a` | | ground and dividers |
| `term-fg` | `#d8dde5` | 14.3:1 | log text |
| `term-muted` | `#8c95a4` | 6.4:1 | timestamps, thoughts, output |
| `term-agent`, `term-tool`, `term-system`, `term-user` | `#9fb0ff`, `#f2c46d`, `#bba6ff`, `#f29bc4` | 9.4:1, 11.9:1, 9.3:1, 9.5:1 | log kinds |
| `term-ok`, `term-error` | `#7adfa4`, `#ff8f86` | 12.0:1, 8.8:1 | log tones |
| `term-mark` | `#f2c46d`, with `term-bg` text | 11.9:1 | search matches |

**Charts.** Series use `chart-1` to `chart-5` in order: `brand`, `attention-solid`, teal (`#0f8c80` light, `#2dc2b0`
dark), `review-solid`, `neutral-solid`; `chart-grid` (`border`) draws the rules. The first series is always the main
measure. The knowledge graph's node kinds use the same five.

**shadcn's variables** map onto the kit, so the generated primitives follow it: `--background` canvas, `--card` and
`--sidebar` surface, `--popover` surface-raised, `--foreground` fg, `--muted` surface-sunken, `--muted-foreground`
fg-muted, `--primary` action, `--primary-foreground` on-action, `--secondary` surface-sunken, `--accent`
surface-hover, `--border` border, `--input` border-control, `--ring` focus-ring, `--destructive` danger-solid,
`--sidebar-accent` surface-selected. A role or state is never told by colour alone: badges carry an icon and a word
(`Admin`, `Writer`, `Reader`; `Quản trị`, `Ghi`, `Đọc` in Vietnamese).

## Type

IBM Plex Sans for the interface and IBM Plex Mono for data, logs and the terminal, both through `next/font/google`
with the latin, latin-ext and vietnamese subsets, so the files come from the app's own origin (`font-src 'self'`) and
the Vietnamese people write in plans, evidence and memories renders in the same faces as the English around it.
Sans loads 400, 500 and 600; Mono loads 400 and 500, so mono text is never set heavier than `font-medium`. The faces
reach Tailwind as `--font-plex-sans` and `--font-plex-mono` behind `font-sans`, `font-heading` and `font-mono`.

| Role | Size / line | Weight | Tailwind | Use |
| --- | --- | --- | --- | --- |
| `display` | 28 / 34 px, -0.015em | 600 | `text-[28px] leading-[34px] font-semibold tracking-tight` | Home's greeting, large empty states; once a page |
| `page-title` | 20 / 28 px, -0.01em | 600 | `text-xl font-semibold tracking-tight` | every h1, beside its status pill |
| `section-title` | 15 / 22 px | 600 | `text-[15px] leading-[22px] font-semibold` | h2, card titles |
| `body` | 14 / 20 px | 400 | `text-sm` | interface text, menus, dialogs, the trace |
| `body-strong` | 14 / 20 px | 500 | `text-sm font-medium` | buttons, the primary cell of a row |
| `small` | 13 / 18 px | 400 | `text-[13px] leading-[18px]` | secondary lines, descriptions, chip labels |
| `caption` | 12 / 16 px | 400 | `text-xs` | timestamps, column headers, helper text |
| `overline` | 11 / 16 px, 0.06em, uppercase | 600 | `text-[11px] leading-4 font-semibold tracking-[0.06em] uppercase` | sidebar and palette group labels only |
| `metric` | 24 / 30 px, -0.01em | 600 | `text-2xl leading-[30px] font-semibold tabular-nums` | the metric strip, progress headers |
| `code` | 13 / 20 px | 400 | `font-mono text-[13px] leading-5` | log lines, commands, inline code, the terminal |
| `code-small` | 12 / 16 px | 400 | `font-mono text-xs` | identifiers in chips and cells |

- Mono is for values, never for titles: a plan's title is `page-title`; its id sits in the breadcrumb and details.
- A number that lines up in a column, a count or a duration uses `tabular-nums`.
- Inputs, text areas and selects use 16 px below 768 px (`text-base md:text-sm`, `max-md:text-base` on
  `NativeSelect`), so phones do not zoom. The kit also steps body text up to 15 px under 640 px; the app does not do
  that yet.
- Every h1 is `page-title` in Plex Sans, set by `PageHeader`; a plan's, a project's, a worker's or a member's name is
  the title in the interface face, and the id it goes by sits in a chip beside it.

## Shape

| Radius | Value | Tailwind | For |
| --- | --- | --- | --- |
| `radius-xs` | 4 px | `rounded-xs`, `rounded` | names: identifier chips, tags, kbd keys |
| `radius-sm` | 6 px | `rounded-sm` | controls: buttons, inputs, selects, menu items, nav items |
| `radius-md` | 8 px | `rounded-md` | containers: cards, tables, menus, notices, toasts |
| `radius-lg` | 12 px | `rounded-lg` | overlays: dialogs, sheets, the command palette |
| `radius-full` | 999 px | `rounded-full` | states and counts only: status pills, live dots, avatars |

A fully round shape always means a state or a count; a square-ish one means a name.

Borders before shadows. Cards take `border` and `shadow-raised`; menus, popovers and toasts `shadow-popover`; dialogs,
sheets and the palette `shadow-dialog`. In dark, elevation comes mostly from the lighter `surface-raised`.

| Shadow | Light | Dark |
| --- | --- | --- |
| `shadow-raised` | `0 1px 2px rgba(14,17,22,0.05)` | `0 1px 2px rgba(0,0,0,0.4)` |
| `shadow-popover` | `0 1px 3px rgba(14,17,22,0.08), 0 8px 24px rgba(14,17,22,0.10)` | `0 0 0 1px rgba(255,255,255,0.04), 0 8px 24px rgba(0,0,0,0.55)` |
| `shadow-dialog` | `0 2px 6px rgba(14,17,22,0.08), 0 24px 48px rgba(14,17,22,0.18)` | `0 0 0 1px rgba(255,255,255,0.05), 0 24px 64px rgba(0,0,0,0.7)` |

One container for a row of numbers, never four separate cards.

## Motion

| Token | Value | Tailwind | For |
| --- | --- | --- | --- |
| `duration-fast` | 120 ms | `duration-fast`, and the default of every `transition-*` | hover and press |
| `duration-base` | 180 ms | `duration-base` | menus, popovers, toasts entering |
| `duration-slow` | 240 ms | `duration-slow` | sheets, dialogs, expanding a trace step |
| `ease-standard` | `cubic-bezier(0.2, 0, 0, 1)` | `ease-standard`, and the default easing | every transition |
| `pulse-period` | 1600 ms | `animate-live-ping` | one cycle of the live dot |

- Hover changes colour, never layout. Menus and sheets fade and slide in through `tw-animate-css`.
- The live dot pulses only for live work: a running run, a busy worker, the live connection. At most one pulsing dot
  per row. A ring grows and fades over a solid dot:

  ```tsx
  <span className="relative inline-flex size-2 rounded-full bg-running">
    <span className="absolute inset-0 animate-live-ping rounded-full bg-running" aria-hidden="true" />
  </span>
  ```

- Streamed agent text appends as it arrives; finished text never animates in.
- Under `prefers-reduced-motion: reduce` every animation and transition ends at once and `animate-live-ping` is off,
  so the dot stays solid. Playwright runs with reduced motion, so menus and sheets are settled before axe looks.

## Iconography

Lucide (`lucide-react`), outline, stroke 1.75 (`svg.lucide` in `globals.css`). 16 px in the interface, 14 px inside pills, chips and small buttons, 20 px in empty
states and the phone top bar. Icons sit before their label and inherit its colour; icon-only buttons carry an
`aria-label` that names the object. One icon per concept:

| Concept | Icon |
| --- | --- |
| Dispatch | `Send` |
| Run plan | `Play` |
| Rerun | `RotateCcw` |
| Take over, terminal | `Terminal` |
| Diff | `FileDiff` |
| Decision, waiting for you | `MessageSquare` |
| Runs | `Activity` |
| Insights | `ChartColumn` |
| Plans | `ListChecks` |
| Workers | `Server` |
| Secrets | `LockKeyhole` |
| Knowledge graph | `Network` |
| Memories | `BookOpen` |
| Skills | `Sparkles` |
| Inbox; the bell in the top bar | `Inbox`; `Bell` |
| Home; a project's overview | `LayoutDashboard`; `FolderKanban` |
| My memories; administration | `Brain`; `Shield` |
| Sidebar toggle | `PanelLeft` |
| Done, failed, lost, cancelled | `CircleCheck`, `CircleX`, `Unplug`, `Ban` |

The mark (`components/brand.tsx`, `src/app/icon.svg`) is three linked nodes on a rounded tile: the tile in `brand`
(`#2f4bd8` in the favicon), two nodes and the edges in `on-brand`, the third node amber `#fbbf24` in both themes. It
shows at 20 px or larger beside the name in Plex Sans 600, and is never recoloured.

## Spacing and layout

- A 4 px grid (Tailwind spacing). Page padding 16 px on phones and 24 px from 768 px, content at most 1280 px wide
  (`max-w-7xl`), 24 px between sections, 16 px inside cards, 12 by 10 px in table cells.
- The shell is the kit's AppShell: the sidebar is 240 px wide (`15rem`), folds to 56 px of icons on desktop (Ctrl or
  Cmd + B, or the top bar's toggle) and becomes a sheet below 768 px; the top bar is 52 px on `surface` and sticks to
  the top.
- z-index: header 10, sidebar rail 20, menus, sheets and tooltips 50, toasts 60 (above an open dialog).
- The kit's sizes: controls 32 px (28 in dense toolbars, 40 in dialog footers), every control at least 44 px under
  768 px, table rows 44 px (36 compact), a 52 px top bar, a 240 px sidebar folding to 56 px, content at most 1280 px
  with a 24 px gutter (16 on phones).
- Checked widths: 375, 768, 1024 and 1440 px. Under 768 px a list's table becomes a list of rows (`DataList`) and its
  filters fold into a "Filters (n)" sheet; a table that stays a table (the knowledge graph's, a skill's versions)
  hides its secondary columns. Wide tables scroll inside their own focusable region, and the page itself never
  scrolls sideways (`e2e/shell.spec.ts`; `e2e/no-sideways-scroll.spec.ts` checks a page of each area at 375, 768 and
  1024 px; `e2e/mobile.spec.ts` the phone layout at 375 by 812 with a phone's user agent and touch). The shell's
  inset is `min-w-0`: as a flex item beside the sidebar it would otherwise grow to the natural width of the widest
  table.
- The server renders the phone layout on a guess: `lib/mobile-hint.ts` reads `Sec-CH-UA-Mobile` and the user agent
  (a phone, not a tablet), the root layout passes it to `Providers`, and `useIsMobile` (`hooks/use-mobile.ts`) returns
  it on the server and while the page hydrates, then the browser's own answer. A phone gets the list in the HTML
  itself, so nothing swaps once the scripts run; a wrong guess (a narrow desktop window) is corrected right after
  hydration.

## Components

shadcn/ui in the `radix-nova` style on Radix primitives, generated into `src/components/ui` and then
changed where the defaults fell short:

- `sidebar.tsx`: the sheet title and trigger label take translated text; `SidebarInset` is a `div`, so each
  page has exactly one `<main>`; 240 px wide, 56 px folded. Group labels are the `overline` in `fg-subtle` (the 70 %
  opacity default fails AA in dark). A menu button is the kit's nav item: 32 px (44 px in the sheet), 13 px `fg-muted`
  text, `accent` (surface-hover) under the pointer, `sidebar-accent` (surface-selected) with a `brand` icon when it is
  the page shown, and the global focus outline instead of the default inset ring.
- `table.tsx`: `scrollLabel` makes a table's scroll container a named, focusable region, its focus outline drawn
  inside its edge; a row is `accent` (surface-hover) under the pointer and `surface-selected` when selected.
- `kbd.tsx`: a key of the kit (`data-slot="kbd"`), mono 11 px on `card` inside a `border-strong` edge whose bottom
  is doubled, with the key-chip radius; it stands beside the action a shortcut triggers.
- `badge.tsx`: `info`, `success` and `warning` variants on the kit's tones (`brand-soft` with `brand`, `success-soft`
  with `success`, `attention-soft` with `attention`); `destructive` is `danger-soft` with `danger`; the shape is
  `rounded-full`, a state or a count. A state of a run, worker, plan, step, repo or decision is a `StatusBadge`, not a
  `Badge`.
- `button.tsx`: `default` is the ink primary (`primary`, `action-hover` on hover), one per view; `secondary` (and
  `outline`, the same button under shadcn's name) is `card` inside a `border-strong` edge; `ghost` is
  `muted-foreground` until it hovers `accent`, for toolbars, rows and icon-only buttons; `quiet-danger` is `danger`
  text on no fill, for a reversible stop (Cancel run); `destructive` is `danger-solid` with `on-danger`, darker on
  hover (`brightness-94`), only inside the confirm step of something permanent; `link` is `brand`. Sizes: `sm` 28 px
  (dense toolbars, table rows), `default` 32 px (page and card actions), `lg` 40 px (dialog footers, the composer's
  Send), `icon`, `icon-sm` and `icon-lg` square at 32, 28 and 40 px; every size has the control radius. Under 768 px
  every button but `link` is at least 44 px tall, and icon buttons 44 px wide too. `busy` sets `aria-busy` and
  `aria-disabled`, hides the button's own icon behind a turning `LoaderCircle` (still under reduced motion) and
  ignores clicks, so a busy submit button does not submit; the caller passes the -ing label ("Dispatching"). The
  focus outline is the global 2 px `ring`.
- `input.tsx`, `textarea.tsx`, `native-select.tsx`: a `card` fill inside an `input` (`border-control`) edge,
  `fg-subtle` placeholders, `muted` when disabled; an input or select is 44 px with 16 px text under 768 px (a
  call site that sets a height on desktop keeps it from md: `md:[&_select]:h-9`).
- `card.tsx`, `dialog.tsx`, `alert-dialog.tsx`, `sheet.tsx`, `dropdown-menu.tsx`: the kit's radius and elevation by
  role (Shape), with `duration-slow` for dialogs and sheets and `duration-base` for menus.
- `hooks/use-mobile.ts`: `useSyncExternalStore` instead of state set inside an effect, with the server's guess
  (`MobileHintProvider`) as its server snapshot; false where the browser cannot match media (unit tests).
- `tabs.tsx` (Radix tabs): a line style, the selected tab underlined in `brand` and set in a heavier weight; a tab is
  44 px under 768 px.
- `switch.tsx` (Radix switch): for a setting that applies once flipped. The track is ink (`primary`) when on and
  `border-control` (`input`, 3:1 on a card) when off, the thumb on `on-action` or `surface`, and the thumb moves, so
  the state is not told by colour alone; the focus outline is the global `ring` and the hit area 44 by 44 px. A
  `<label htmlFor>` names it.
- Menus (`DropdownMenu`) are not modal, so the page behind stays readable by assistive technology. Their items are
  44 px under 768 px.
- `command.tsx`: shadcn's Command (cmdk 1.1.1), written by hand from upstream since the registry could not be reached:
  a 52 px input row over a hairline, `overline` group headings in `fg-subtle`, 36 px items (44 px under 768 px) with
  the control radius, the icon in `fg-muted` turning `brand` and the row `surface-selected` when selected. The
  palette puts it in a Dialog itself, so upstream's CommandDialog is left out.

Shared pieces built on them:

- `components/states`: every data view goes through `useHubQuery` and `QueryView`. Loading shows a skeleton in the
  shape of the content inside `role="status"`, faded in 300 ms after loading starts (`animate-appear-late`) so a quick
  answer never flashes it: `TableSkeleton` is the header on `surface-sunken` and 44 px rows of a short reference, a
  title over its secondary line, a pill and a figure (`toolbar` adds the toolbar's search and chips),
  `ListSkeleton` puts the metric strip's 78 px cells above it. A 401 sends the visitor to `/login`; 403 shows the no-access
  state, 404 the not-found state, 5xx or no answer the error state with the request id and a retry button. Every
  state is the kit's EmptyState: the icon in a 40 px `surface-sunken` square (`danger-soft` or `attention-soft` for
  errors and no access), a one-line title in 15 px 600, one sentence in `fg-muted` and the actions, the primary one
  first, centred with 48 px above and below; alone it is a card, inside a `DataCard` it draws no frame. First use
  (`EmptyState`) says what the page is for, the explanation its head no longer carries, and offers the one thing
  to do next; no results (`NoResults`) lists the filters in use as tags ("State: Done", "Search: deploy") and
  offers Clear filters.
- `components/data/data-card.tsx` and `data-table.tsx`: a list is one card (`DataCard`, `border`, `shadow-raised`):
  the toolbar on top, then the table, or the no-results, empty or loading state in its place, then the pager as its
  footer. `DataToolbar` is the list's search landmark, named after the list: the search field (256 px, the full
  width on phones), the filter chips with their counts, then the number of results on the right in 12 px
  `fg-subtle` tabular figures, said again in a polite live region. The table (TanStack Table v9, sorting with
  `aria-sort`, a caption) has its header on `surface-sunken` in 12 px `fg-muted`, 36 px, and rows of 44 px, 36 px
  with `density="compact"` (the audit trail), cells in `fg-muted`. A column's `meta` says what it holds: `primary`
  is the row's title (`CellMain`: the name in `body-strong` and at most one line under it in `caption`, `fg-subtle`,
  or `danger` for a failure; both end in an ellipsis and the column takes the width the others leave, so a row never
  grows past two lines), `numeric` sets a number, size, duration or time on the right in tabular figures, header
  too, and `actions` holds the row's buttons, which show while the row is hovered or holds focus (`.row-actions` in
  `globals.css`; always shown where nothing can hover and under 768 px). Sortable headers show their direction with
  a chevron, and the up-down chevron only under the pointer or focus (a 44 px target under 768 px). Columns listed in
  `columnClassNames` hide on narrow screens. The admin lists paged by the API keep their filters as a form
  (`FilterBar`, submitted with Filter so a screen reader moving through a select does not reload the list), drawn as
  the toolbar: labels in `caption` over 32 px fields.
- `components/data/data-list.tsx`, the kit's DataTable under 768 px: a table given `mobile` (a row as a phone lists
  it, `DataListRow`) renders, below 768 px, a named list of its rows in the table's order instead, under the same test
  id with `data-layout="list"` (`"table"` otherwise). A row is at least 60 px (52 px `compact`): the title in
  `body-strong` on one line with its tags, the state at the end of the line, and one meta line in `caption`
  `fg-subtle` (`danger` for a failure), both cut with an ellipsis and whole in their tooltips. The title links to the
  object's page and its link covers the row (an `::after` over the row; the focus ring is drawn on that cover), so a
  tap anywhere opens it, and the link is described by the state and the meta line; any other link or button in the
  row sits above the cover. The other columns are on that page: runs (the step's or plan's title, the state, "#12,
  plan, step 2" or the failure), plans (the plan run's badge, "id, 3/12 steps done"), the plan's steps, workers
  (slots in use and the machine), memories (the type, what it is about), skills (the version chip, the description),
  members (the admin and not signed in tags, the first grant and how many more). Rows without a page of their own keep
  their actions at the end as 44 px icon buttons named for what they act on: tokens (state, user and machine, Revoke,
  the user's page as the row's link), secrets (kind, what it sets, Expired, Replace and Delete) and the audit trail
  (the time at the end of the line, who did it to what). Sorting stays the table's; a phone keeps the order the table
  had.
- `components/data/filter-sheet.tsx`, the kit's filters under 768 px: `ToolbarFilters` keeps a toolbar's filters
  inline from 768 px and, below, puts them behind one 44 px "Filters (n)" button beside the search (pressed, on
  `surface-selected` inside a `brand` edge, while a filter is in force); the toolbar's result count moves to a line of
  its own under them, so the toolbar keeps to one row and a list's first row is on the first screen. The button opens
  `FilterSheet`, a dialog named "Filters" that slides up from the bottom (12 px top corners, at most 85 % of the
  height, its foot above the safe area): a 52 px head with the title and a close button, every group with its label
  above its 44 px chips (`useInFilterSheet`; a select's label through `ToolbarField`), then the result count (polite),
  Clear filters and Show results. Chips apply as they are pressed; Show results, Esc or a tap outside close the sheet
  and give focus back to the button. The admin lists' form (`FilterBar`) moves into the sheet whole, rows per page and
  the time zone note included, and applies with Filter, which closes the sheet unless a field is wrong (its error
  shows in the sheet).
- `components/status/status-badge.tsx`: one `StatusBadge` (`kind` and `status`) and one map per kind, run, worker,
  plan, step, repo and decision; each state has a tone, a Lucide icon and its words under `status.<kind>` in
  `messages/en.json` and `vi.json`. The maps are `satisfies Record<...>` on the API's own types in
  `src/lib/api/schema.d.ts` (`Run["state"]`; `Worker["status"]` with online split into idle and busy;
  `PlanSummary["area"]` with pending and blocked; `StepReport["status"]` with blocked; `Decision["state"]`), and
  `useStatusText` reads the words through typed keys, so a state the API adds fails typecheck until it has a look
  and words. The pill is 22 px (26 px with `size="lg"`, beside an h1), the icon 14 px and `aria-hidden`; leased
  shows a still dot, and only a running run, a busy worker and an active plan run show the pulsing one. A plan's
  state comes from `planState` in `lib/plans.ts`: completed by its area, active while a plan run holds it, blocked
  when every step not done is blocked, pending otherwise. A status the hub does not know (a plan written by hand) is
  `OtherStatusBadge`, kept as written. `StatusIcon` is the icon alone with its word for screen readers, and the
  test ids stay those the e2e specs read (`run-state`, `worker-status`, `decision-state`).
- `components/data/identifier.tsx`: names. `Identifier` is a mono chip on `surface-sunken` with 4 px corners for a
  worker, branch, revision, session or hash, a link in text colour turning `brand` on hover when it has a page, with
  an optional copy button (24 px, a 44 px hit area under 768 px) that says what it copied in a toast and
  selects the text where the clipboard is refused (`useClipboard`, which every copy button of the web goes through). `RunRef` writes a run as `#N` in mono with tabular figures.
  `Tag` is a kind or a role (Admin, Plan run, Deploy, Blocking): an icon and a word on `surface-sunken` with 4 px
  corners, never round. `NAME_LINK` is the name in a table's first column or a list's primary cell: text colour,
  `brand` on hover.
- `components/shell`: the kit's AppShell. The sidebar (`app-sidebar.tsx`, links in `nav.ts`) carries the mark and
  "evo-agents hub" (Plex Sans 600, the mark at 24 px), the project switcher, then Home and Inbox, the current project
  (Overview, Plans, Runs, Insights, Memories, Skills, Knowledge graph), the hub (Workers, My memories, Shared skills,
  Administration for a hub admin), and at the foot the fleet line and the account. Inbox counts the decisions waiting
  for the visitor's answer in `attention`, Runs the project's active runs in `running` (`nav-counts.ts`, through the
  same queries as the bell and the runs pages); a count is a round pill drawn for the eye, said in words to screen
  readers after the label, hidden at zero, and a dot on the icon when the sidebar is folded. The fleet line
  (`fleet-line.tsx`) reads `GET /v1/workers` like the Workers page ("2 workers online, 1 busy"; a `success` dot while
  any is online, `danger` when every one is offline or draining, `neutral` before the first) and links there. The
  project switcher and the account menu (login, hub role, role and visibility in the current project, theme,
  language, sign out) are the kit's switch: a bordered `card` control, the name over an `fg-subtle` line. The top bar
  (`site-header.tsx`) holds the sidebar toggle, the breadcrumb (13 px, slashes, the trail starts at the project; under
  768 px only its first crumb and the page itself, the page keeping up to three quarters of the trail while the crumbs
  before it truncate), the command palette's field, the page's LiveIndicator and the inbox bell. Page header below.
- `components/shell/project-switcher.tsx`: the switcher opens a popover (`ui/popover.tsx`, a dialog named "Choose a
  project", to the right of the sidebar, below the trigger in the phone's sheet) with a search field on top, focused
  as it opens, that keeps the projects whose name holds every word typed, the count said in a polite live region. The
  projects are a navigation of links, exactly those `GET /v1/projects` returns (the grants; every project for a hub
  admin): the tile, the name, then the role, "Visibility: Internal", the repos and the active plans in `caption`
  `fg-subtle`, and the open decisions as an `attention` count said in words. The counts come from
  `GET /v1/me/overview`, read when the switcher opens; a hub admin's project without a grant shows "No role" and its
  repos. The current project is `surface-selected` and `aria-current`. Down from the field enters the list, the arrows,
  Home and End move in it, up from the first goes back to the field; the first Escape empties the field, the next
  closes. Home sits at its foot.
- `components/palette`, the kit's CommandPalette. The top bar's field (`palette-trigger.tsx`) reads "Search or jump
  to" with its key (⌘K on Apple devices, Ctrl K elsewhere, once the page has hydrated) on `background` inside a
  `border-strong` edge, 280 px from xl and 224 px at lg; below lg it folds to its magnifier, a 32 px square (44 px under
  768 px), the words kept as its name. It, Cmd K and Ctrl K open the palette from anywhere but the web terminal (and
  Ctrl K in a text field on an Apple keyboard, which cuts the line there), unless another dialog is open; the shortcut
  closes it again. `palette-context.tsx` holds only that state in the shell; the palette, cmdk and the dialogs it opens
  are a chunk of their own, fetched when the pointer or focus reaches the field or at the first shortcut. The palette
  (`command-palette.tsx`, `model.ts`) is a dialog on `surface-raised` with `shadow-dialog` and the overlay radius,
  640 px wide at 12 % of the height (the full width less 16 px gutters, 16 px from the top, on a phone): the combobox
  in 15 px (16 px on phones) with the busy loader and Esc (an X under 768 px), then the listbox in groups. Actions come
  first, only those whoami's grants allow: Run plan on each active plan with steps left where the visitor writes ("4
  steps left"; three while nothing is typed), Dispatch a step in each such project, Rerun on the visitor's latest failed
  or lost run of one step that no later run of the step replaced (Home's rule, "failed 5 minutes ago"), and Register
  worker with the writer role anywhere. Runs follow: those of the overview, in flight first, while nothing is typed or
  when looking in every project; the hub's search of the project (the runs list's `q`, which takes `#N`) once
  something is typed, asked 150 ms after typing stops, the request of the last query cancelled. Then Plans (active
  only until something is typed), Workers (the visitor's, those serving the project) and Go to (the project's pages,
  the hub's pages, and the projects while looking in every project or typing). An item is an icon, its name and a
  meta in `caption` `fg-subtle` at its end (a state, steps left, progress; the project first when looking in every
  project), with ↵ on the selected one. A query keeps what holds every word typed, case and Vietnamese diacritics
  folded; five items a group while nothing is typed, eight after. The palette starts in the page's project when the
  visitor holds a grant on it, else in every project; Tab and Shift Tab, or the scope at the foot, move through the
  projects of the grants and every project. The first item stays selected while groups load above it, until the arrows,
  Home, End or the pointer move the selection. Choosing an item opens its page, or the dialog its page opens (Dispatch,
  Run plan, Register worker) once the palette has closed; Rerun reruns as Home's does and says so in a toast. Nothing
  in it deletes, cancels, drains or revokes. Esc closes it and gives focus back to what held it, and a dialog it
  opened gives focus back there too. The foot says the keys (↑ ↓ move, ↵ open, Tab switch project; hidden under 768
  px) and the scope; a polite live region says how many results the scope holds; no match names the query and the
  scope, and a read that failed says some results could not be read.
- `components/shell/page-header.tsx`: the page head of the kit, one row. `title` is the one h1 (`page-title`, Plex
  Sans, wrapping anywhere for a long unbroken name), `status` the state of what it names (a `StatusBadge` at `lg`),
  `tags` its kind, role, counts and identifier chips, `actions` the page's buttons on the right (`ml-auto`), and `sub`
  one optional line under the row (`small`, `fg-muted`) that links the parent objects: a run's plan and step, a step's
  plan, or where the object stands (who changed a plan last, a worker's machine, a member's first and last visit).
  There is no eyebrow, no description paragraph and no rule under the head; what a page is for is said once, in its
  `EmptyState`, and parent pages are reached from the breadcrumb. The row wraps instead of squeezing: actions that do
  not fit beside the title move to their own line, still on the right, so a run's five controls never push the page
  sideways at 375 px. A plan's pages add their tabs under the head (`PlanHeader`).
- `components/live`: the kit's LiveIndicator, in the top bar beside the bell. A page registers what keeps it current:
  its main query through `useHubQuery(..., { live: true })` (or `usePagedQuery`, `useLiveQuery`), a stream through
  `useLiveSignal` (the run page's event stream, with the log's Pause); a page that registered nothing shows none, and a
  query that does not poll counts for nothing. `live-model.ts` is the state machine: Live while updates arrive (a
  `success-solid` dot that pulses, "updated 3s ago"); Reconnecting from the first failed read (TanStack's
  `fetchFailureCount`, or an error newer than the data) or while the log reads events instead of its stream (an
  `attention-solid` dot, "retry in 4s, polling every 5s"; "stream retry in 25s, polling every 1.5s meanwhile");
  Offline after 15 seconds of failures, or at once when the browser has no network (`danger-solid`, "last update
  2 min ago", Retry now), and it stays Offline until data arrives again; Paused while the person holds the log
  (`neutral-solid`, "by you", Resume). The clock is `useNow`, one ticker for every caller. The state's name is a
  `role="status"` region ("Updates: Offline") and the ticking words sit outside it, so a screen reader hears each
  change of state and not each second. Under 768 px the words fold away and the buttons keep their names for screen
  readers only. A live query keeps what it last read on screen through a failed refetch (no answer or a 5xx,
  `keepsStaleData`): the indicator, not an error state, says the page is not current; a 403 or 404 shows as before.
- `components/data/metric-strip.tsx`: the kit's MetricStrip, the counts above the runs and workers lists. One `border`
  container whose cells are divided by 1 px rules (a 1 px grid gap over `border`), four in a row from the lg
  breakpoint and two below; each cell a `caption` label with its icon, the `metric` figure and one `fg-subtle` line
  naming what is behind it. A zero is `fg-subtle`, a cell that needs a person (Review) is `attention` and the agents
  at work (Running, Busy) `running` with the live dot in place of the icon while above zero. A cell can carry a
  sparkline (`sparkline.tsx`, Recharts through shadcn's `ui/chart.tsx`: `chart-1` over a `brand-soft` area, a
  `chart-grid` baseline, a dot on the latest value); the cell is a `role="img"` named with every value, the chart
  itself is hidden from assistive technology, and it is loaded with `next/dynamic` behind a skeleton of the same 22 px,
  so Recharts is not in any page's first load. With `quiet`, a strip whose in-flight cells are all zero gives way to
  one line on `surface-sunken` ("All quiet. No run is in progress, queued or waiting for review." and Dispatch for a
  writer): the runs page passes it while no plan run is parked; the workers page does not, since its counts are the
  fleet's state and an offline machine must stay in view.
- `components/home`, Home (`/`), the kit's MissionControl: one read of `GET /v1/me/overview` (prefetched on the
  server with the workers list, then asked every 5 seconds while a run is in flight or a decision is open and every 30
  otherwise; the page's LiveIndicator query) and the workers list for the Fleet card. The page head is "Home"; under
  it the MetricStrip of five cells: Waiting on you (`attention`, the oldest of the visitor's open decisions named),
  Running (`running` with the live dot, the run and its worker), Queued (the run that waited longest), Done in 7 days
  with the sparkline of `done_by_day` (UTC days, every value in its label) and Failed in 7 days with "and N lost" and
  the last failure's reason. With no run in flight and no decision open it gives way to the quiet line, which still says
  the week's figures. Then two columns from the lg breakpoint (300 px on the right, 340 px from xl), one below, in the
  order Needs you, In flight, Recent, Fleet, Projects; between md and lg Fleet and Projects sit side by side. Each card
  has a 48 px head (the h2, a count pill said in words, a link on the right) over rows of a 20 px mark centred on the
  text, the title over one `caption` line in `fg-subtle`, and what ends the row; in a card narrower than 576 px (a
  container query) the end moves under the line. Needs you lists only the open decisions the visitor answers (`yours`) and disappears when there
  is none: the question links to `/inbox?decision=ID`, a plain click and the primary Answer (named "Answer decision
  #7") open the DecisionSheet over Home, loaded on demand (`next/dynamic`, asked for when the pointer or focus reaches
  Answer), so Answer then Send answer answers with the agent's pick. In flight lists the active runs (the agent at work
  first with the live dot, waiting and parked, queued): a plan run carries its tag and its steps done of the total as a
  bar; a waiting run says whose answer it waits for. Recent lists the runs that ended last, a failure's reason in
  `danger`, and Rerun (ghost, `RotateCcw`) on the visitor's own failed or lost run of one step where they write. Fleet
  lists the visitor's own workers with `HeartbeatBars`; Projects each project of the grants with the role, active plans,
  repos and open decisions. A member without a grant sees the EmptyState that says a hub administrator grants roles,
  with their login to copy; a hub admin without one is sent to Administration. The pure parts are `home/model.ts`.
- `components/feedback/toast.tsx` and `ui/sonner.tsx`: the kit's Toast through Sonner, mounted once in `providers.tsx`.
  Bottom right, 24 px from the edges (16 on phones), 380 px wide, z-index 60, on `surface-raised` inside a `border` edge
  with `shadow-popover` and 8 px corners: the tone's Lucide icon, a past-tense title in `body-strong` ("Run #13
  dispatched", "Access granted to octo"), one sentence in `fg-muted`, and a `brand` link back to what changed ("Open
  run", "Open member"), left out while that page is the one shown; a Dismiss button on every toast. A success closes
  after 5 seconds (Sonner pauses the timer under the pointer and while the tab is hidden); a failure stays until it is
  dismissed, titled with what could not be done ("Couldn't rerun #4"), then the words `useWriteFailure` chose, the
  hub's message and the request id in `code-small`. Toasts sit in Sonner's polite region ("Notifications"), which a
  modal dialog leaves readable; a failure is also `role="alert"`. Every write reports this way: dispatch, Run plan,
  rerun, cancel, take over, hand back, approve, drain, resume and revoke a worker, register a worker, grant and revoke
  a grant, revoke a token, answer a decision, mark as read, and copy. A failure inside a dialog that stays open is said
  in the dialog instead (`InlineError`), next to the button that failed; no page keeps an inline notice that only says
  something worked.
- `components/data/visibility.tsx`: Visibility is the web's word for a label level, the reach of a grant (`max_level`)
  or of a memory's label. `VisibilityLevel` shows the default ladder's levels as Public, Internal, Customer and Secret
  (the same words in both languages, as the CLI's terms are English) with the code in the tooltip and in `data-level`;
  a level a project named itself shows as written. `VisibilityTag` is "Visibility: Internal" beside a project's title.
  API fields, URL parameters, form values (the grant dialog's options) and test ids keep the codes.
- `plans/plan-header.tsx`, `ReadOnlyNotice`: every plan page carries the kit's info banner on `surface-sunken`, a lock,
  the one sentence "Read-only. Plans change from the CLI with `evo harness step`; each change adds a revision." and a
  ghost Copy button that copies the command (`useClipboard` of `identifier.tsx`: it selects the command where the
  clipboard is refused, and says which in a toast). It is a `note` named "Read-only".
- `components/data/search-field.tsx` and `facet-group.tsx`: the kit's search input, a magnifier, the field and the
  `/` key while it is empty, a clear button once it holds text, committed after a pause or on Enter; and a facet as
  the kit's filter chips, a labelled group of `aria-pressed` toggles, 28 px with 6 px corners, the count after the
  label in `fg-subtle` tabular figures and in words for screen readers, the pressed one on `surface-selected` inside
  a `brand` edge in a heavier weight. Under 768 px the field, the chips and the admin filters are 44 px, with 16 px
  text in fields so phones do not zoom, and the field takes the toolbar's row beside "Filters (n)". `/` focuses the page's search field (`search-shortcut.ts`, the first
  registered one in document order; the knowledge graph's query field too) unless focus is in a field that takes
  text or a dialog is open. Filters live in the URL and change it through `window.history.replaceState`, which
  Next.js syncs with `useSearchParams` without rendering the page on the server again.
- `components/data/segmented.tsx`: the kit's segmented control, for two or three ways to show the same thing (a
  plan's board or list, a diff unified or split, a memory rendered or raw). A labelled group of `aria-pressed`
  buttons on `surface-sunken` with 6 px corners, 24 px with 12 px text (44 px under 768 px); the pressed one sits on
  `surface` inside a `border-strong` ring. A select beside it is `ui/native-select.tsx` in Plex Sans, 32 px.
- `components/workers`: the workers pages poll the hub every 10 seconds (`refetchInterval`), the Register dialog
  every 2 seconds while its pairing code waits. Draining or revoking a worker asks for its name, typed out
  (`confirm-by-name.tsx`). The hub keeps only a worker's latest heartbeat, so the 60-minute heartbeat strip is built
  from what the tab has read (`heartbeats.ts`): a received minute is a full bar, a missed one a short red bar, a
  minute nobody watched a dot, with the counts written out beside it. `HeartbeatBars` is the kit's compact strip of
  the same cells for Home's Fleet card: 60 bars 18 px high (a full `success-solid` bar, an 8 px `danger-solid` one, a
  3 px `neutral-solid` stub), one image named with the counts. On a worker's page its owner flips "Only runs
  dispatched from the web" (`dispatch_from`) with a switch in the Scope card; the switch moves once the hub answered
  and the worker was read again, the result is a toast, and nobody else sees the switch, only the value in "Who
  dispatches".
- `components/secrets`, the Secrets page (`/secrets`, in the sidebar's hub section after Workers): the head is the
  title, the count as a tag and Add secret; its sub line says the secrets are the visitor's alone and no value comes
  back. The visitor's own secrets sit in a `DataCard` table sorted by name: the name in mono with the variable it sets
  or the https prefix and the user git sends as its second line, the kind as a tag (`Variable` or `GitBranch` and a
  word), the projects, the workers or "Any of yours", the end with an Expired pill once past, and when the value was
  last written; Replace (ghost) and Delete (`quiet-danger`) are the row's actions. With none, the empty state says what
  a secret is and offers Add secret and `evo-agents hub secret set`. Add and Replace open one form
  (`secret-dialog.tsx`): the name (adding only), the kind as two radio cards, the variable or the URL prefix and
  username, the projects the visitor writes to, their own workers that are not revoked, an optional end day (00:00
  UTC, as the CLI's `--expires`) and the value. The value is write-only: a password field without a `name`, never
  filled from the hub (a replace asks for it again, since the hub keeps no copy it could show), read when the form is
  sent and emptied at that moment whatever the hub answers. The PUT goes through plain state rather than a TanStack
  mutation, whose cache would keep the body (`hooks.ts`). The form repeats the hub's checks (`model.ts`), so nothing
  goes out while a field is wrong, and adding a name the visitor uses already is refused rather than replacing it; a
  refusal of the hub stays in the dialog, the saved secret is a toast. Delete asks first (`ConfirmAction`) and says
  that the leases still out are revoked and that the token should be revoked where it was made too; the result is a
  toast.
- `components/runs`: a run is in one of the API's states, waiting (a plan run's agent asked its owner a decision) and
  parked (nobody answered for 24 hours) included; both show in the running phase of a run's timeline, under their own
  name, on an `attention` node. The runs pages ask the hub every 5 seconds while the project (or the step, or the worker) has an
  active run and every 30 seconds otherwise; the list's facets (active, review, done, failed or cancelled) and search
  are the API's own filters, so a page of 50 runs comes back with the count of each state. A run's state is a
  `StatusBadge` with an icon and a word. The Dispatch dialog lists every pending step of a plan in plan order and folds the done,
  in progress and blocked ones away; a step that is not ready keeps a disabled checkbox and says why. Its footer says
  which of the visitor's own workers could take the runs now, from what their heartbeats report, the way the hub
  matches them at claim time. The Dispatch and Run this step buttons show only for a writer of the project (whoami's
  grants); the API decides again on every dispatch.
- `components/runs`, plan runs (`plan-run.tsx`, `plan-run-dialog.tsx`, shared fields in `dispatch-fields.tsx`): Run plan
  sits in the plan page's header (`PlanHeader`'s `actions`) and in an action column on the rows of the plans list's
  active area, only for a writer. It is shown but locked, with the reason in words beside it, while the plan has an
  active run of any kind or no pending step (`planRunLock`; the list only knows steps done of the total). Its dialog
  first says what the run will do (`planRunScope`): the steps not done, by status; each repo with the branch the plan
  names, a repo on its default branch (main, master or the one registered for the repo) badged, a repo without a
  branch blocking; the checkpoint steps; and, in a warning box when a default branch is named, that the agent will push
  and merge into it there. Then runtime, model, mode, worker and timeout (2, 4, 8 or 24 hours of agent time). The model
  is free text with the models the visitor's workers list for the picked runtime as a datalist; a model typed with Any
  runtime is refused in the field, since a model name means something only to its runtime. The footer is the Dispatch
  dialog's outlook, for one plan run and a worker with a checkout of every repo. The plan page shows the active plan
  run in a banner between the read-only notice and the goal: run #N on its worker, its state as Queued, Running,
  Waiting for your decision or Parked (a badge with an icon and a word, and the banner's tone), the steps done of the
  total with a bar, the steps in progress, and links to the run and to the open decision (`/inbox?decision=ID`). The
  plan's active runs are read as the runs pages read theirs (every 5 seconds while one is active), and the plan itself
  as often while its plan run is. The plans list badges a plan whose plan run is active, linked to the run. While a
  plan run holds a plan, Run this step on its steps is locked and names the run. A plan run's page is titled Plan run,
  carries a Plan run badge, lists the plan's steps with their status (the one in progress `aria-current="step"`), its
  repos and branches, its model and the agent time used, and the open decisions it waits on (below); it offers no Rerun.
- `components/inbox`, notifications and decisions (docs/notifications.md): a bell in the top bar (`inbox-bell.tsx`) links
  to the Inbox with the number of unread notifications (99+ past 99) in an `attention` pill raised beside the glyph, never
  over it, read from `GET /v1/me/notifications/count` every 10 seconds; its accessible name says that number and the decisions waiting for the visitor's answer, and a polite live
  region says how many arrived when the number grows. The Inbox (`/inbox`, also at the top of the sidebar) lists the
  member's notifications 50 a page, refreshed every 10 seconds: the decisions still open first under "Waiting for your
  answer", then the notices and the decisions answered, expired or cancelled, newest first. Facets for unread, kind and
  project live in the URL like the runs page's; Mark as read on each unread one, and Mark all as read in the header (Mark
  these as read, by id, while a filter is on). An unread notification says Unread in words beside a dot and has a
  heavier title. A notice of a push or merge into a default branch names its repo, branch and commits (seven digits,
  the full name for screen readers, five shown and the rest a click away); its body folds to three lines; its kind is
  a tag (an icon and a word, square corners), not a pill.
- `components/inbox/decision-sheet.tsx`: a decision is answered in a sheet, without leaving the page: from the Inbox's
  list at `/inbox?decision=ID`, the link every decision notification carries (the History API pushes it, so Back closes
  it), and from Home's Needs you, which passes the decision's project and when its run parks. It slides in from the
  right over the page, the whole width of a phone and 576 px from the sm breakpoint, on `surface-raised` with
  `shadow-dialog`: a 52 px top bar names the decision ("Decision #7", the dialog's name) beside a close button, then
  the card edge to edge. The question's heading takes focus once the decision is read; closing (the button, Esc, a
  click outside or Back) gives focus back to what opened the sheet, or, for a sheet opened by its URL, to the
  decision's link in the list. A link that names only the decision asks each project of the visitor's grants for it at
  once. Opening it from the Inbox marks its notification read.
- `components/inbox/mobile-decision.tsx`, the kit's MobileDecision: under 768 px the Inbox's sheet is a screen of its
  own instead (Home's stays a sheet), the phone's whole width and `100dvh` tall on `canvas`. A 52 px top bar on
  `surface`: Back to Inbox (a 44 px ghost icon button that closes it, like Esc and Back), "Run #12" in 16 px 600 as a
  link to the run, then the LiveIndicator (the decision is read every 10 seconds while it waits) and the bell. Then,
  the only part that scrolls: the state pill (`lg`) and the category, the project, plan and step on one 14 px line, when
  it was asked and when the run parks, the question as the screen's h1 in 20/28 px 600 (the dialog's name, focused
  once read), the context at 15 px, the options as radio cards with 14 px of padding (15 px titles, 14 px
  descriptions), the note in 16 px text so the phone does not zoom into it, and "What the agent did so far" folded:
  opened, it reads the run and its last 200 events and lists the last five things the Trace shows (`agent-digest.ts`
  over `buildTrace`; no moves between states, no runtime events shown raw, not the decision on screen), oldest first,
  one line each with its icon and time, then a link to the run's trace. A bar at the foot on `surface`, above the
  safe area (`env(safe-area-inset-bottom)`), holds Take over (when offered) and Send answer side by side, 44 px and
  15 px, with no `kbd`; the button submits the form above it (`form=`). Once answered the bar goes and focus comes back
  to the question. The card's `kbd` hides under 768 px everywhere else too (the run's page, Home's sheet).
- `components/inbox/decision-view.tsx`: the kit's DecisionCard, the same in the sheet and in the side column of a plan
  run's page. The head on `attention-soft` while the decision is open: the `StatusBadge` "Waiting for you" (for the
  run's owner; "Waiting for octo" for anyone else), the category as a tag, the run, project, plan and step as links
  with a quiet underline (on the run's own page only the step), then "asked 6 minutes ago, parks in 23 h 59 min", or
  "parked 3 hours ago" once the run has parked. When a run parks is the hub's to say (its decision wait is a setting),
  so the card reads it from `GET /v1/me/overview` while the run waits, or takes it from Home. The question in 15/22 px
  500; the agent's context as Markdown through the memories' `SafeMarkdown` (no raw HTML, no images loaded), clamped
  to four lines with Show more, and unfolded when focus moves into it. For the run's owner the answer form: the options
  as the kit's radio cards (`border-strong`, `surface-selected` inside a `brand` edge when checked, 44 px under 768 px),
  the label and what the option does next, the agent's pick tagged "Agent's pick" (`brand` on `brand-soft`) and
  checked, so one click on Send answer answers with it; Clear the choice leaves the owner's words alone as the answer.
  Then a note for the agent (the answer itself once the choice is cleared; 4 KiB of UTF-8, counted past 75%), and the
  footer: "The agent continues as soon as you answer." (or that the parked run resumes), Take over, and Send answer
  with its `kbd` from 768 px (⌘↵ on Apple devices, Ctrl ↵ elsewhere, shown once the page has hydrated; Cmd or Ctrl with Enter sends
  from anywhere in the form, `aria-keyshortcuts` on the button). Take over shows while the owner may open the run's
  terminal (their run on a worker of theirs registered with `--allow-web-terminal`, in a state the hub opens a
  terminal in): it leads to the run's page on its Terminal tab (`?view=terminal`), or on that page shows the tab.
  Nothing chosen or written, or too many bytes, is refused beside the field; the hub's answer is a toast: "Answer sent
  to run #N" with a link to the run, or the refusal (403, 404 and 409 in the decision's own words through
  `useWriteFailure`, the hub's message as a detail), which stays until dismissed. After a 409 the card reads the
  decision again and shows it as the hub holds it. Once answered the card is one compact block: the head on
  `surface-sunken` with the Answered pill and "by octo, 2 minutes ago", the question in `fg-muted`, the option chosen,
  the owner's words, whether the worker has handed it to the agent yet (read every 10 seconds until it has) and the
  run that resumed a parked one; an expired or cancelled one says what happened. Anyone but the owner reads the
  context, the options (the agent's pick tagged) and why they cannot answer. A plan run's page shows its open decisions
  at the top of the side column from the xl breakpoint, above the log below it (`runs/run-decisions.tsx`, "Waiting for
  your decision" and a link to the Inbox), read every 5 seconds while the run is active; a decision answered there, or
  found answered after a 409, stays with its answer until the visitor leaves.
- `components/runs`, a run's page (`/p/{project}/runs/{id}`), the kit's RunScreen: the page head, the `RunTimeline`,
  then the session card (Trace, Raw log, Terminal) on the left and, in a 23 rem side column from xl, the decision while
  a plan run waits, Details, Usage, the plan's steps, the Result and, for the run's owner, Credentials. Below xl the
  side column moves under the timeline, the decision first, then the side cards (two by two from md), then the
  session card; the page reads in that order at every width (`run-side`). The head
  is "Run #12" for every run, a plan run saying so in its Plan run tag, over the plan (in the body font) and step as
  `brand` links with a quiet underline, so colour is not all that sets them apart from the line. The owner's controls sit in the header, each shown only when the state and the visitor's rights allow it
  (`run-model.ts`, `runControls`), in the kit's order: Take over (a dialog with `evo-agents worker attach N` and, for
  Claude Code, the Remote Control session `evo-run-N`), the diff with its `+12 −3` in mono `success` and `danger` once
  the worker reported a diffstat, Rerun, Hand back, Approve, and Cancel (confirmed in a dialog) last, plus the composer
  under the trace. Anyone but the run's owner reads only. The header puts these actions under the title until the xl
  breakpoint (`PageHeader`'s `actions`, which take a line of their own when the title leaves no room), so they wrap
  instead of pushing the page sideways.
- `run-credentials.tsx`, the owner's Credentials card in the side column, drawn as the other side cards (`surface`,
  `shadow-raised`, a `section-title` head with one `caption` line in `fg-subtle`): each lease the run got, its name in
  mono, its provider as a tag (Your secret with `KeyRound`, or GitHub App with the GitHub mark), the variable or the
  origins it answered for, when and to which worker it was issued, when it ends and when it was revoked, and its state
  as a pill (Out in `brand`, Expired neutral, Revoked outlined with `Ban`); read every 5 seconds while the run is
  active and again when it moves, and never a value. Nobody else gets the card, as the API answers them 403; its foot
  links to the Secrets page.
- `run-timeline.tsx`, the kit's RunTimeline (`timelineModel` in `run-model.ts`): the phases left to right (top to
  bottom under 768 px), the time from each phase to the next on the line between them, mono 11 px in `fg-muted` on
  `card`. Done phases are `fg-muted` nodes on a solid `fg-subtle` line; the current one is a `running` node with the
  live ping and a dashed line ahead, its gap counting up with `useNow`; a queued run's node is neutral and a run in
  review is `review-solid`, since neither is agent work. A waiting or parked run shows its state on the running phase
  in `attention`, "since 10:22:41", the gap "waiting 6m". A failed run stops on a `danger-solid` node at the time it
  ended (lost and cancelled on a `neutral-solid` one), the gap before it on the line, the later phases "skipped"; only a
  run that is done ends on `success-solid`. Times are local in `code-small`; each is focusable and its tooltip says
  the full date and time zone. An ordered list labelled "Phases of run #N", the current phase `aria-current="step"`,
  each phase's status in words for screen readers.
- The session card (`run-log.tsx`) holds the tabs, all kept mounted (Radix `forceMount`) and kept in the URL
  (`?view=log`, `?view=terminal`; the Trace has none), changed with `history.replaceState` so the page does not
  navigate. What the stream is doing sits at the right end of the tab bar as the kit's live state (a dot in its tone,
  pulsing while live, and the word in `caption` `fg-subtle`); the Raw log's line count sits in its tab and, on that tab,
  before the stream's state. The composer shows under the Trace and the Raw log, not under the Terminal.
- The Trace (`agent-trace.tsx`, `trace-model.ts`), the kit's AgentTrace and the first tab, reads the same events as
  the Raw log (`useRunLog` keeps them beside its lines): consecutive `agent_message_chunk` events are one message
  (SafeMarkdown), a turn ending at anything else shown or at a `usage_update`; `agent_thought_chunk` events fold into
  "Thought for 3.2s" (from the event before them) over the words that follow; a `tool_call` and its
  `tool_call_update` events, matched by `toolCallId` in seq order whatever order they arrived in, are one row: the
  tool's name (or its kind's for a title that is a command or a path, as Codex and opencode give), what the agent said
  it is for, the main argument in mono (a Codex command without its `/bin/zsh -lc` wrapper), an edit's `+3 −1`, the
  exit code (Codex `exitCode`, opencode `exit`, Claude Code's "Exit code 1"), the duration from the events' times, and
  a spinner with the time so far while it runs. Rows are `details`, folded, a failed one open; the output (the
  update's `content`, else its `rawOutput`) sits on `term-bg`, cut at 20 lines with "Show the full output (45 lines)",
  an edit's change as `- old` and `+ new` lines in `term-error` and `term-ok`. A `plan` is a checklist, a
  `user_message` "You" (or "You answered decision #7"), a `state` a caption line, a `system` line in `caption` with
  its tone and its output folded, and the hub's "decision #7 asked" an `attention` row "Asked you" with the question
  and Answer, a link to the decision's card on the page (`#run-decision-7`) while it is open, to the Inbox otherwise.
  `output` events and kinds the page does not know fold together, one caption line per run of them, with their JSON.
  A running run ends with the typing dots (`animate-typing`, solid under reduced motion). The trace is a `role="log"`
  region (polite) that follows the newest item unless the person scrolled up, when "Jump to the latest" shows; past
  500 items only the ones in view render (`@tanstack/react-virtual`, measured rows).
- `usage-meter.tsx`, the kit's UsageMeter, and `usage-model.ts`: the tokens the runtime reported as one total, a bar of
  cache read, input, output and reasoning in `chart-5`, `chart-1`, `chart-3` and `chart-4` (each part at least 3 px),
  a row per part with its share ("under 0.1%" for a part that rounds to nothing), cache writes said under it, and the
  cost "as reported" or "Cost not reported"; nothing is priced by the page. Once the worker reported the run's end it
  reads `run.usage`; while it runs it adds up the `usage_update` events, as each runtime reports them: Claude Code per
  turn with the session's cost so far, opencode per step with the step's cost, Codex the thread's running total (its
  cached input inside its input, reasoning inside output). The Result card no longer lists raw usage keys.
- The Raw log (`use-run-log.ts`, `run-log.tsx`) follows the run's server-sent events with an EventSource; the browser
  reconnects by itself with `Last-Event-ID`, every event is kept once by its seq, and the stream's `end` closes it for
  good. When the stream fails (closed by the browser, three errors without opening, or 10 seconds behind the run's
  `last_seq`), the page reads `events?after=` every 1.5 seconds and tries the stream again every 30. The lines sit in a
  `role="log"` region (polite), with filters by group (chips; under 768 px one 44 px menu button with the group's icon
  and name, "Show: Tools" to a screen reader, its items the groups with their line counts, under the search field
  and beside Follow and Pause), a search that highlights, Follow (scrolling up turns it off) and
  Pause (held by the page, so the top bar says Paused and resumes it); past 2,000 lines shown only the rows in view render (`@tanstack/react-virtual`). Where the web forwards `/v1`
  itself, `proxy.ts` asks for the stream unencoded: Next.js would gzip it and hold the events back.
- The composer (`run-composer.tsx`), the kit's Composer: one bordered box with the focus ring on the box, the textarea
  without an edge of its own, and a bar that says what the run is ("Headless run on laptop.") beside Send and its key
  (Cmd or Ctrl with Enter, as the DecisionCard's).
- The Terminal tab (`run-terminal.tsx`, `use-run-terminal.ts`, `terminal-model.ts`) shows after the Raw log tab in the
  session card only for the run's owner on a worker of theirs registered with `--allow-web-terminal` (`terminalAccess`), while
  the run is leased, running or interactive, and stays for the rest of the visit once shown. Every tab stays mounted
  (Radix tabs with `forceMount`), so switching keeps the terminal's session and the log's place. Connect loads the
  terminal (wterm's DOM renderer over libghostty's VT core, WebAssembly from the web's own origin, `terminal-view.tsx`
  through `next/dynamic`, so the page carries none of it until then), then opens a websocket on the page's own origin:
  a text hello with the session's CSRF value and the terminal's size, then binary frames (input, output, resize as in
  ttyd). Keys go out only once the worker's end has printed something, since the hub drops earlier input. The status
  badge reads Not connected, Loading terminal, Connecting, Waiting for the worker, Connected or Closed; every close of
  the hub (4401, 4403, 4408, 4409, 4426, 1011, a lost connection) says what happened and what to do, with the hub's own
  reason under it, and a sign-in older than 12 hours (whoami's `token.created_at`) asks to sign in again before
  anything is tried. On a headless run the intro and the waiting message say that connecting takes the run over. The
  surface is the log's (`term-*` tokens, `hub-terminal` in `globals.css`): IBM Plex Mono 13 px, 16 ANSI colours drawn
  from the `term-*` tokens, a 2 px
  `ring` outline inside its edge while focused; Esc then Tab leaves it, as the footer says. The CSP allows
  `'wasm-unsafe-eval'` (WebAssembly only, no JavaScript eval) and keeps connect-src `'self'`, which covers a websocket
  to the page's own host in Chromium and Firefox (`e2e/terminal.spec.ts` runs in both).
- The diff page (`/p/{project}/runs/{id}/diff`) reads the run's diff on the web's server through the presigned GET
  the API signs (`diff-blob.ts`), so the blob store needs no CORS rule and connect-src stays `'self'`; the download is
  a navigation to the presigned URL, as for skill bundles. It renders up to 20,000 lines, file by file.
- `components/insights`, a project's Insights (`/p/{project}/insights`, in the sidebar's project group after Runs): the
  runs that ended on each UTC day of the last 7, 30 or 90 days, from `GET /v1/projects/{p}/runs/stats` (prefetched on the
  server for the range the URL names). The range is the kit's segmented control in one row under the page head, with
  the days it spans ("Oct 1 to Oct 7, UTC days"), kept in the URL (`?days=7`, `?days=90`, none for 30) through
  `history.replaceState`; while the next range loads the cards keep the last one at 60 % opacity. Four cards, two
  columns from lg: Runs by outcome (columns stacked done, failed, and lost or cancelled in `success-solid`,
  `danger-solid` and `neutral-solid`, the two neutral states sharing one segment), Failure rate (failed or lost of the
  runs that ended done, failed or lost, a `danger-solid` line, cancelled runs left out), Run duration (the median and
  90th percentile from start to end, `chart-1` and `chart-2` lines on round duration ticks), and Tokens by type
  (stacked cache read, input, output and reasoning in the UsageMeter's `chart-5`, `chart-1`, `chart-3`, `chart-4`).
  Each card has its title, one line of what the range adds up to, Chart or Table (a segmented control), a legend for
  two series or more (a swatch for bars, a line key for lines), and a 224 px plot. Marks follow the dataviz rules:
  columns at most 24 px wide with a 4 px rounded end on the top segment of the day and square at the baseline, 2 px of
  the card between stacked segments, 2 px lines with a gap where a day has no value (a lone day gets an 8 px dot ringed
  in `card`), hairline `chart-grid` rules, no axis line, ticks in `fg-muted`. The tooltip, on `popover` with
  `shadow-popover`, names the day and every series of it (each keyed by a short mark in its colour, the value in
  tabular figures, the total last); it is a polite status region. The plot is Recharts' keyboard surface: Tab reaches
  it (the 2 px `ring` outline, put back on the surface shadcn's container clears), the left and right arrow keys move
  the tooltip from day to day. Every chart keeps its table in the document for screen readers (`sr-only`), and Table
  shows it instead: a row per day, the newest first, the whole range in its foot, a day without a value saying None,
  inside its own focusable region of at most 352 px whose head stays in view. The charts are one chunk loaded with
  `next/dynamic` behind skeletons of the plot's height, so Recharts is in no page's first load. With no run ended in the
  range the cards give way to the EmptyState, with the runs page and, under 90 days, Show 90 days.
- `components/memories/markdown.tsx`: Markdown written by people (memory bodies) through react-markdown without
  raw HTML: tags show as text, only listed elements render, links keep http(s), mailto and anchors, images are
  never loaded, headings move under the page's h1 and the card's h2.

Themes come from `next-themes` with the `class` strategy and follow the system until the person picks light
or dark in the user menu. The language is the `NEXT_LOCALE` cookie set from the same menu: English without it
(or with a value that is not a locale), Vietnamese when it says `vi`. The browser's Accept-Language is not read,
so the server renders a page the same way for everyone who has not picked a language.

## Accessibility

- Text contrast at least 4.5:1 in both themes (tables above); a 2 px focus outline in `ring` (`focus-ring`, the
  brand cobalt) with a 2 px offset on every focusable element.
- A skip link to `#main`, one `<main>`, a labelled `<nav>` and breadcrumb, an `h1` on every page.
- Keys: Cmd K or Ctrl K opens the command palette, `/` focuses the page's search, Cmd or Ctrl with Enter sends an
  answer or a message, Ctrl or Cmd with B folds the sidebar, Esc closes and gives focus back; each is shown in a `kbd`
  beside what it does or in the palette's foot.
- `e2e/a11y.spec.ts` runs axe (WCAG 2.2 A and AA rules) on every page and open menu in light and dark and
  fails on any serious or critical violation.

## Adding a page (steps 25 to 28)

1. Add the query to `src/lib/queries.ts`, or to `queries.ts` in the section's own components folder (memories,
   skills); it takes `() => ApiClient`, so the server and the browser share it.
2. Make the route a server component that calls `prefetch` and wraps a client component in
   `HydrationBoundary` from `src/lib/api/hydration-boundary.tsx` (ESLint refuses TanStack's own: the shell reads
   some of the page's queries before the page renders, and TanStack's would hold those back, so the server would
   send a skeleton; `e2e/server-render.spec.ts` checks Home, Workers and Runs); the client component reads with
   `useHubQuery` and renders through `QueryView`.
3. Add the sidebar entry to `PROJECT_NAV` or `HUB_NAV` in `components/shell/nav.ts`, and its label under
   `nav` in both `messages/vi.json` and `messages/en.json` (a unit test keeps the two files in step).
4. Add the page to `PAGES` in `e2e/a11y.spec.ts`. Seed data with the `admin`, `member` and `signInAs`
   fixtures from `e2e/support/fixtures.ts`. Specs see the English default; a spec that matches Vietnamese
   copy says so with `test.use({ uiLocale: "vi" })`, which sets the locale cookie on its browser context
   (`e2e/locale.spec.ts` checks both).
