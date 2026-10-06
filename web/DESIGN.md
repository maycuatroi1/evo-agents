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
- Inputs use 16 px below 768 px (`text-base md:text-sm`), so phones do not zoom. The kit also steps body text up to
  15 px under 640 px; the app does not do that yet.
- `PageHeader` still sets the h1 at 24 px (`text-2xl`); it moves to `page-title` with the one-row page head.

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
| Plans | `ListChecks` |
| Workers | `Server` |
| Knowledge graph | `Network` |
| Memories | `BookOpen` |
| Skills | `Sparkles` |
| Inbox; the bell in the top bar | `Inbox`; `Bell` |
| Done, failed, lost, cancelled | `CircleCheck`, `CircleX`, `Unplug`, `Ban` |

The mark (`components/brand.tsx`, `src/app/icon.svg`) is three linked nodes on a rounded tile: the tile in `brand`
(`#2f4bd8` in the favicon), two nodes and the edges in `on-brand`, the third node amber `#fbbf24` in both themes. It
shows at 20 px or larger beside the name in Plex Sans 600, and is never recoloured.

## Spacing and layout

- A 4 px grid (Tailwind spacing). Page padding 16, 24 and 32 px from phone to desktop, content at most
  1280 px wide (`max-w-7xl`), 24 px between sections, 16 px inside cards, 12 by 10 px in table cells.
- The sidebar is 16 rem wide, folds to 3 rem icons on desktop (Ctrl or Cmd + B, or the header button) and
  becomes a sheet below 768 px. The header is 56 px and sticks to the top.
- z-index: header 10, sidebar rail 20, menus, sheets and tooltips 50.
- The kit's sizes: controls 32 px (28 in dense toolbars, 40 in dialog footers), every control at least 44 px under
  768 px, table rows 44 px (36 compact), a 52 px top bar, a 240 px sidebar folding to 56 px, content at most 1280 px
  with a 24 px gutter (16 on phones). The shell above keeps its own sizes until it is rebuilt on the kit's AppShell.
- Checked widths: 375, 768, 1024 and 1440 px. At 375 px secondary table columns hide, wide tables scroll
  inside their own focusable region, and the page itself never scrolls sideways (`e2e/shell.spec.ts`;
  `e2e/no-sideways-scroll.spec.ts` checks a page of each area at 375, 768 and 1024 px). The shell's inset is
  `min-w-0`: as a flex item beside the sidebar it would otherwise grow to the natural width of the widest table.

## Components

shadcn/ui in the `radix-nova` style on Radix primitives, generated into `src/components/ui` and then
changed where the defaults fell short:

- `sidebar.tsx`: the sheet title and trigger label take translated text; `SidebarInset` is a `div`, so each
  page has exactly one `<main>`; group labels use `muted-foreground` (the 70 % opacity default fails AA in dark).
- `table.tsx`: `scrollLabel` makes a table's scroll container a named, focusable region.
- `badge.tsx`: `info`, `success` and `warning` variants on the kit's tones (`brand-soft` with `brand`, `success-soft`
  with `success`, `attention-soft` with `attention`); `destructive` is `danger-soft` with `danger`; the shape is
  `rounded-full`, a state.
- `button.tsx`: `default` is the ink primary (`primary`, `action-hover` on hover), `outline` the kit's secondary
  (`card` with a `border-strong` edge), `ghost` hovers `accent`; every size has the control radius.
- `input.tsx`, `textarea.tsx`, `native-select.tsx`: a `card` fill inside an `input` (`border-control`) edge,
  `fg-subtle` placeholders, `muted` when disabled.
- `card.tsx`, `dialog.tsx`, `alert-dialog.tsx`, `sheet.tsx`, `dropdown-menu.tsx`: the kit's radius and elevation by
  role (Shape), with `duration-slow` for dialogs and sheets and `duration-base` for menus.
- `hooks/use-mobile.ts`: `useSyncExternalStore` instead of state set inside an effect.
- `tabs.tsx` (Radix tabs): a line style, the selected tab underlined in `brand` and set in a heavier weight.
- Menus (`DropdownMenu`) are not modal, so the page behind stays readable by assistive technology.

Shared pieces built on them:

- `components/states`: every data view goes through `useHubQuery` and `QueryView`. Loading shows a skeleton
  inside `role="status"`. A 401 sends the visitor to `/login`; 403 shows the no-access state, 404 the
  not-found state, 5xx or no answer the error state with the request id and a retry button. Empty lists show
  `EmptyState` with what to do next.
- `components/data/data-table.tsx`: TanStack Table v9 with sorting, `aria-sort`, a caption, and columns that
  hide on narrow screens.
- `components/shell`: sidebar, project picker, user menu (login, hub role, role in the current project,
  theme, language, sign out), header with breadcrumbs, page header.
- `components/data/search-field.tsx` and `facet-group.tsx`: a search box in a search landmark (committed after a
  300 ms pause or on Enter) and a facet as a labelled group of `aria-pressed` toggles with counts. Filters live in
  the URL and change it through `window.history.replaceState`, which Next.js syncs with `useSearchParams` without
  rendering the page on the server again.
- `components/workers`: the workers pages poll the hub every 10 seconds (`refetchInterval`), the Register dialog
  every 2 seconds while its pairing code waits. Draining or revoking a worker asks for its name, typed out
  (`confirm-by-name.tsx`). The hub keeps only a worker's latest heartbeat, so the 60-minute heartbeat strip is built
  from what the tab has read (`heartbeats.ts`): a received minute is a full bar, a missed one a short red bar, a
  minute nobody watched a dot, with the counts written out beside it.
- `components/runs`: a run is in one of the API's states, waiting (a plan run's agent asked its owner a decision) and
  parked (nobody answered for 24 hours) included; both show in the running phase of a run's stepper, under their own
  name and icon. The runs pages ask the hub every 5 seconds while the project (or the step, or the worker) has an
  active run and every 30 seconds otherwise; the list's facets (active, review, done, failed or cancelled) and search
  are the API's own filters, so a page of 50 runs comes back with the count of each state. A run's state is a badge
  with an icon and a word. The Dispatch dialog lists every pending step of a plan in plan order and folds the done,
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
  to the Inbox with the number of unread notifications (99+ past 99), read from `GET /v1/me/notifications/count` every
  10 seconds; its accessible name says that number and the decisions waiting for the visitor's answer, and a polite live
  region says how many arrived when the number grows. The Inbox (`/inbox`, also in the sidebar's hub section) lists the
  member's notifications 50 a page, refreshed every 10 seconds: the decisions still open first under "Waiting for your
  answer", then the notices and the decisions answered, expired or cancelled, newest first. Facets for unread, kind and
  project live in the URL like the runs page's; Mark as read on each unread one, and Mark all as read in the header (Mark
  these as read, by id, while a filter is on). An unread notification says Unread in words beside a dot and has a
  heavier title. A notice of a push or merge into a default branch names its repo, branch and commits (seven digits,
  the full name for screen readers, five shown and the rest a click away); its body folds to three lines. A decision
  opens beside the list at `/inbox?decision=ID`, the link every decision notification carries (the History API pushes
  it, so Back closes it; opened from the list, the question's heading takes focus, and Close gives it back to the
  list); below the lg breakpoint it replaces the list, with a way back. A link that names only the decision asks each project of the visitor's grants for it at once. Opening it
  marks its notification read. The decision (`decision-view.tsx`) shows its number, state and category (badges with an
  icon and a word), the question, where it comes from (project, run with its state, plan, step, when, of whom), the
  agent's context as Markdown through the memories' `SafeMarkdown` (no raw HTML, no images loaded), then for the run's
  owner while it is open the answer form: the options as radio cards with the recommended one badged Recommended (a
  thumbs-up icon and the word) and none picked for them, a box for their own words (4 KiB of UTF-8, counted past 75%),
  and Send answer. Nothing chosen or written, or too many bytes, is refused beside the field; the hub's answer shows
  above the form (403, 404 and 409 in the decision's own words through `useWriteFailure`, the hub's message as a
  detail). Once answered the form gives way to who answered, when, the option and words, whether the worker has handed
  it to the agent yet (read every 10 seconds until it has), and the run that resumed a parked one; the options list
  marks the one chosen. Anyone else reads the options and why they cannot answer. A plan run's page shows its open
  decisions in a "Waiting for your decision" banner above the stepper (`runs/run-decisions.tsx`) with the same view and
  form, read every 5 seconds while the run is active; a decision answered there stays with its answer until the visitor
  leaves.
- `components/runs`, a run's page (`/p/{project}/runs/{id}`): a stepper of its states (the current one
  `aria-current="step"`, a run that ended badly marked where it stopped), the log, the details and the result, and the
  owner's controls in the header, each shown only when the state and the visitor's rights allow it (`run-model.ts`,
  `runControls`): Cancel (confirmed in a dialog), Take over (a dialog with `evo-agents worker attach N` and, for Claude
  Code, the Remote Control session `evo-run-N`), Hand back, Approve and Rerun, plus the message box under the log. Anyone
  but the run's owner reads only. The header puts these actions under the title until the xl breakpoint
  (`PageHeader`'s `metaBelow`), so they wrap instead of pushing the page sideways.
- The log (`use-run-log.ts`, `run-log.tsx`) follows the run's server-sent events with an EventSource; the browser
  reconnects by itself with `Last-Event-ID`, every event is kept once by its seq, and the stream's `end` closes it for
  good. When the stream fails (closed by the browser, three errors without opening, or 10 seconds behind the run's
  `last_seq`), the page reads `events?after=` every 1.5 seconds and tries the stream again every 30. The lines sit in a
  `role="log"` region (polite), with filters by group, a search that highlights, Follow (scrolling up turns it off) and
  Pause; past 2,000 lines shown only the rows in view render (`@tanstack/react-virtual`). Where the web forwards `/v1`
  itself, `proxy.ts` asks for the stream unencoded: Next.js would gzip it and hold the events back.
- The Terminal tab (`run-terminal.tsx`, `use-run-terminal.ts`, `terminal-model.ts`) shows beside the Log tab in the log
  card only for the run's owner on a worker of theirs registered with `--allow-web-terminal` (`terminalAccess`), while
  the run is leased, running or interactive, and stays for the rest of the visit once shown. Both tabs stay mounted
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
- `e2e/a11y.spec.ts` runs axe (WCAG 2.2 A and AA rules) on every page and open menu in light and dark and
  fails on any serious or critical violation.

## Adding a page (steps 25 to 28)

1. Add the query to `src/lib/queries.ts`, or to `queries.ts` in the section's own components folder (memories,
   skills); it takes `() => ApiClient`, so the server and the browser share it.
2. Make the route a server component that calls `prefetch` and wraps a client component in
   `HydrationBoundary`; the client component reads with `useHubQuery` and renders through `QueryView`.
3. Add the sidebar entry to `PROJECT_NAV` or `HUB_NAV` in `components/shell/nav.ts`, and its label under
   `nav` in both `messages/vi.json` and `messages/en.json` (a unit test keeps the two files in step).
4. Add the page to `PAGES` in `e2e/a11y.spec.ts`. Seed data with the `admin`, `member` and `signInAs`
   fixtures from `e2e/support/fixtures.ts`. Specs see the English default; a spec that matches Vietnamese
   copy says so with `test.use({ uiLocale: "vi" })`, which sets the locale cookie on its browser context
   (`e2e/locale.spec.ts` checks both).
