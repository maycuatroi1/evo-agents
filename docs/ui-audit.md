# UI audit of the demo pages

The images `pnpm screenshots` writes to `docs/images/` (see `web/scripts/screenshots.mjs`), reviewed against
`web/DESIGN.md` and the ui-ux-pro-max checklist: 11 pages, light and dark, 1440 and 375 px wide, 44 images. The
pages are the sign-in page (`login`), an invite link signed out (`invite`), the Get started list once the invite is
accepted (`get-started`), Home, the Inbox with a decision waiting (`inbox`), a plan's graph (`plan-graph`), a plan
run waiting for its decision (`plan-run`), the Monitor, a run's Trace with an output the hub's hook cut
(`run-spool`), and the knowledge graph's search and a node (`kg-search`, `kg-graph`). Touch targets were measured
in the browser at 375 px: the box of every link, button, field and tab on the page.

Severity:

- **high**: fails WCAG 2.2 AA, scrolls the page sideways, or leaves content or an action out of reach.
- **medium**: content cut or hard to read where it stands, or a control under the 44 px DESIGN.md asks for under
  768 px.
- **low**: a design-system detail (a type role, a spacing), or how the image is framed.

Every high and medium row is fixed; the images in `docs/images/` are the ones taken after the fixes. The pages also
pass `web/e2e/a11y.spec.ts` (axe, WCAG 2.2 AA, both themes) and `web/e2e/no-sideways-scroll.spec.ts` (375, 768, 1024
and 1440 px).

| page | theme | width | rule or visible defect | severity | status | reason when open |
| --- | --- | --- | --- | --- | --- | --- |
| get-started | light, dark | 375, 1440 | A long command (the install line, `hub login` with its URL) ran past its box and showed cut; the rest scrolled sideways inside a one-line box (ui-ux-pro-max: overflow hidden, truncation) | medium | fixed | |
| run-spool | light, dark | 375, 1440 | The hub's `[evo-hub: ...]` note in a cut output ran off the block's right edge, hiding the path to the whole output (whitespace-pre) | medium | fixed | |
| plan-run | light, dark | 1440 | The decision's context read at about 170 characters a line (ui-ux-pro-max line length: 65 to 75) | medium | fixed | |
| inbox | light, dark | 375 | "Open run #4" under a notification was 28 px tall beside the 44 px "Mark as read" (DESIGN.md: 44 px under 768 px) | medium | fixed | |
| plan-run | light, dark | 375 | "Open in the Inbox" beside "Waiting for your decision" was 28 px tall | medium | fixed | |
| kg-graph | light, dark | 375 | The neighbourhood's "1 step" and "2 steps" toggle and the relation filters were 32 px tall | medium | fixed | |
| kg-graph | light, dark | 375 | "Open the page of ..." beside each neighbour was a 28 by 28 px icon link | medium | fixed | |
| invite | light, dark | 375, 1440 | The h1 was 24 px; DESIGN.md makes every h1 the page-title role (20 px semibold) | low | fixed | |
| login | light, dark | 375, 1440 | The h1 was 24 px, not the page-title role | low | fixed | |
| plan-graph | light, dark | 375 | The image put the search field half under the sticky top bar | low | fixed | |
| get-started | light, dark | 375, 1440 | The list's h2 is 18 px, not the section-title role (15/22) | low | open | It heads steps whose own titles are 15 px; at 15 px it would read as one of them. |
| get-started | light, dark | 375, 1440 | The `hub login` command shows the demo stack's origin, `http://localhost:3344` | low | open | The page shows the hub's own URL, and the demo's hub runs on this machine. |
| invite | light, dark | 375, 1440 | "Expires" counts from today, not from the demo's story time | low | open | The demo moves the hub's stamps to story time but keeps every expiry on the real clock (`_kept` in `web/e2e/demo/stack.py`). |
| plan-graph | light, dark | 375 | At zoom 0.6 the side columns of the graph fall outside the canvas, and a step box is 158 by 31 px on screen | low | open | DESIGN.md's plan graph opens a plan that does not read whole at 0.6 at 0.6, its first row at the top; Fit shows the whole plan, the canvas pans and the zoom buttons are 44 px. |
| plan-run | light, dark | 1440 | A cut line of the trace shows under the sticky tabs, and the decision's heading sits a few pixels under the composer | low | open | The image scrolls the page to the decision, the trace's own scroll region at its end. |
| run-spool | light, dark | 375, 1440 | The log's long lines scroll sideways inside the output block | low | open | DESIGN.md: wide content scrolls in its own region; a log keeps its columns. |
| run-spool | light, dark | 375 | "Jump to the latest" floats over the log's last lines | low | open | It shows while the trace is scrolled up from its end, which the image needs to show the hub's note. |
| monitor | light, dark | 375 | The first log line of a tile is cut at the tile's top | low | open | A tile follows the tail of its run's log. |
| monitor | light, dark | 375 | A tile's run link ("#3") is 16 px wide and 44 px tall | low | open | A text link; DESIGN.md's 44 px rule is for controls and buttons other than `link`. |
| home | light, dark | 375 | Run titles and worker names are text links 18 to 20 px tall | low | open | Text links in a list, as on the Monitor. |
| kg-search | light, dark | 375 | Long ids break in the middle of a word | low | open | Breaking anywhere keeps the table within the phone's width. |
| kg-graph | light, dark | 375, 1440 | A neighbour's id breaks in the middle of a word | low | open | Breaking anywhere keeps the table within the phone's width. |
| kg-graph | light, dark | 1440 | Edge labels overlap node labels where edges cross | low | open | The graph library lays them out; the neighbour table under the graph lists every edge. |

The fixes:

- `CommandLine` (`web/src/components/data/command-line.tsx`) wraps at spaces and anywhere in a long word, in place
  of one line that scrolls. Get started, the admin's invite dialog, the register-a-worker dialog and the run page's
  actions use it.
- `TermBlock` (`web/src/components/runs/trace-term.tsx`) wraps the line of a `[evo-hub: ...]` note, at the block's
  width; the lines around it keep their columns and scroll.
- The decision's context (`web/src/components/inbox/decision-view.tsx`) is at most `max-w-prose` wide.
- Under 768 px the Inbox's links under a notification, the run page's "Open in the Inbox", the knowledge graph's
  hop toggle and relation filters are 44 px tall, and each neighbour's open link has a 44 px hit area.
- The h1 of the sign-in page and of the invite page take the page-title role.
- `web/e2e/demo/screenshots.shots.ts` shows a plan's graph from the top of its card on a phone.
