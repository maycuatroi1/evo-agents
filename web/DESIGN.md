# Design system of the hub web

The hub web is an internal, read-mostly dashboard for a small team: projects, members, plans, memories,
skills and the knowledge graph. People read it in English first, with Vietnamese one pick away in the user
menu, and on laptops first, but it has to work on a 375 px phone. The choices below came from the
`ui-ux-pro-max` skill (design-system queries for an admin dashboard and a data-dense developer tool, the
`typography` domain for Vietnamese, the `shadcn` stack and the `web` accessibility guidelines). The tokens live
in `src/app/globals.css`; components use the Tailwind names (`bg-background`, `text-muted-foreground`, ...) and
never raw colours.

## Style

The skill's "Data-Dense Dashboard" style: tables and compact cards, small padding, no decoration, WCAG AA.
Its anti-patterns apply too: no ornate effects, and any list long enough to scan gets sorting or filtering.
Icons come from Lucide only, no emoji. The GitHub mark is the Simple Icons path.

## Colour

Slate neutrals, a blue primary for links and the selected state, amber kept for the hub admin role and
warnings. Ratios are WCAG contrast against the surface named; the e2e axe run checks the rendered pages.

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `background` | `#f8fafc` | `#020617` | page canvas |
| `card`, `popover`, `sidebar` | `#ffffff` | `#0f172a` | surfaces |
| `foreground` | `#0f172a` (17.9:1 on card) | `#f8fafc` (17.1:1 on card) | text |
| `muted-foreground` | `#475569` (7.6:1 on card) | `#94a3b8` (7.0:1 on card) | secondary text, labels |
| `primary` / `primary-foreground` | `#1e40af` / `#ffffff` (8.7:1) | `#60a5fa` / `#020617` (7.9:1) | links, buttons, brand |
| `accent` / `accent-foreground` | `#eff6ff` / `#1e3a8a` (9.5:1) | `#172554` / `#dbeafe` (12.0:1) | hover, active nav, visible levels |
| `info` | `#dbeafe` / `#1e3a8a` (8.5:1) | `#172554` / `#bfdbfe` (10.3:1) | writer role |
| `success` | `#dcfce7` / `#14532d` (8.3:1) | `#052e16` / `#bbf7d0` (12.3:1) | done states (later steps) |
| `warning` | `#fef3c7` / `#78350f` (8.2:1) | `#422006` / `#fde68a` (11.7:1) | admin role, no-access state |
| `destructive` | `#b91c1c` (6.5:1 on card) | `#f87171` (6.5:1 on card) | errors |
| `border` / `input` | `#e2e8f0` / `#cbd5e1` | `#1e293b` / `#334155` | dividers, fields |
| `ring` | `#2563eb` (5.2:1 on card) | `#60a5fa` (7.9:1 on canvas) | focus outline |
| `log` / `log-foreground` | `#0f172a` / `#e2e8f0` (14.5:1) | `#020617` / `#e2e8f0` (16.4:1) | a run's log, a terminal surface in both themes |
| `log-muted`, `log-agent`, `log-tool`, `log-system`, `log-user`, `log-ok`, `log-error` | slate-400, blue-300, amber-300, violet-300, pink-300, green-300, red-300 (7.0:1 to 12.7:1 on `log`) | the same (7.9:1 to 14.4:1) | time, kinds and tones of log lines |

Charts (steps 26 to 28) use `chart-1` to `chart-5`: blue, amber, emerald, violet, slate. A role or state is
never told by colour alone: badges carry an icon and a word (`Admin`, `Writer`, `Reader`; `Quản trị`, `Ghi`,
`Đọc` in Vietnamese).

## Type

- Be Vietnam Pro, weights 400, 500, 600 and 700, Latin and Vietnamese subsets, for all interface text. The
  skill's "Vietnamese Friendly" pairing; it draws Vietnamese diacritics cleanly at small sizes. It stays with
  English first: the Vietnamese interface and the Vietnamese prose people write (plans, memories, evidence)
  render in the same face as the English copy around them.
- JetBrains Mono for identifiers: project names on their own page, levels, branches, paths, request ids and
  table counts. It has a Vietnamese subset, so mixed text never falls back to another font.
- Both load through `next/font/google`, which serves the files from the app's own origin (`font-src 'self'`).
- Scale: page title 24 px semibold, card title 16 px medium, body and table text 14 px, captions 12 px; line
  height 1.5 for prose. Counts and dates use `tabular-nums`.

## Spacing and layout

- A 4 px grid (Tailwind spacing). Page padding 16, 24 and 32 px from phone to desktop, content at most
  1280 px wide (`max-w-7xl`), 24 px between sections, 16 px inside cards, 12 by 10 px in table cells.
- Radius 8 px (`--radius: 0.5rem`), smaller than shadcn's default to suit dense screens.
- The sidebar is 16 rem wide, folds to 3 rem icons on desktop (Ctrl or Cmd + B, or the header button) and
  becomes a sheet below 768 px. The header is 56 px and sticks to the top.
- z-index: header 10, sidebar rail 20, menus, sheets and tooltips 50.
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
- `badge.tsx`: `info`, `success` and `warning` variants.
- `hooks/use-mobile.ts`: `useSyncExternalStore` instead of state set inside an effect.
- `tabs.tsx` (Radix tabs): a line style, the selected tab underlined in `primary` and set in a heavier weight.
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
- `components/runs`: the runs pages ask the hub every 5 seconds while the project (or the step, or the worker) has an
  active run and every 30 seconds otherwise; the list's facets (active, review, done, failed or cancelled) and search
  are the API's own filters, so a page of 50 runs comes back with the count of each state. A run's state is a badge
  with an icon and a word. The Dispatch dialog lists every pending step of a plan in plan order and folds the done,
  in progress and blocked ones away; a step that is not ready keeps a disabled checkbox and says why. Its footer says
  which of the visitor's own workers could take the runs now, from what their heartbeats report, the way the hub
  matches them at claim time. The Dispatch and Run this step buttons show only for a writer of the project (whoami's
  grants); the API decides again on every dispatch.
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

## Motion

Colour and opacity transitions of 150 to 200 ms; hover never moves layout. Menus and sheets fade and slide
in through `tw-animate-css`. `prefers-reduced-motion: reduce` turns every animation and transition off.

## Accessibility

- Text contrast at least 4.5:1 in both themes (table above); a 2 px focus outline in `ring` with a 2 px
  offset on every focusable element.
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
