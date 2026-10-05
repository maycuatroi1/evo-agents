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
