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
  surface is the log's (`hub-terminal` in `globals.css`): JetBrains Mono 13 px, 16 ANSI colours readable on it, a 2 px
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
