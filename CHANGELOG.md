# Release notes

Written for the people who use the console, not for engineers. One entry per
release, newest first. The top entry's version must match `VERSION` in
`app/version.py` — `tests/test_version.py` enforces it.

Format: `## <version> — <YYYY-MM-DD>`, then any of `### Fixed`, `### Added`,
`### Changed`, `### Security`. `app/release_notes.py` parses exactly that, and
the admin console renders it, so a heading it does not recognise is dropped.

## 1.7.1 — 2026-08-21

### Security

- The reason a branch was hidden, and the name of the administrator who hid it,
  are no longer shown to branch staff. Those notes are written on an
  administrator-only screen and can say things like "licence suspended" or
  "pharmacist under review", so they are now visible only to administrators.
  Branch staff still see that a branch is hidden, which is the part they need.

### Fixed

- A page that cannot load its data now says so, instead of showing an empty
  list. Conversations and the daily overview could both report "nothing here"
  when the real answer was "something is broken" — which on the Conversations
  page meant it appeared no one had asked anything at all.
- The website code offered for a branch is now always signed with the real
  credential. In one place it could be built with the development one, which
  works here and then silently fails on the customer's own website.
- The Embed page now shows an error if it cannot load, rather than looking as
  though no credentials are set up.

### Changed

- If a stock or product spreadsheet contains more than one sheet, the load now
  records which sheet was read and warns that it may be the wrong one. Only the
  first sheet is ever read, as before.
- The stock loader now reports its own health, so a loader that has stopped
  working is visible instead of appearing to run normally while files pile up.

## 1.7.0 — 2026-08-21

### Added

- Branches now have their own page entry that stays put. Until now a branch
  existed only while it appeared in the daily stock file, so a branch left out
  of one export disappeared from the console and from every answer the
  assistant gave. A branch you have seen once is now remembered, and a branch
  missing from the latest file is flagged on the Branches page rather than
  removed — nothing is hidden from customers because an export went wrong.
- A branch can be hidden from customers. Use the menu at the end of its row on
  the Branches page and give a short reason. A hidden branch is answered as
  though it does not exist: it is never offered as a place that has a medicine,
  and it cannot be given a website chat window. It stays on the Branches page
  with its stock and the reason it was hidden, and you can switch it back on at
  any time. Only a super admin can do this.
- The Branches page shows what happened to each branch: newly arrived, hidden,
  or missing from the latest file, and it can be filtered by any of those.
- The stock file's history now says when a branch sent stock for the first
  time, and when a branch we have seen before is absent from that file.

### Changed

- Hiding a branch removes its stock from company-wide totals as well, so the
  figures on the console will fall by that branch's share on the day you hide
  it. The confirmation says so, with that branch's actual numbers, before you
  confirm.
- Branch names are read from the stock file when it carries them, under any of
  the usual spellings. No file we receive today has a name column, so branches
  continue to show as codes until one does.

## 1.6.4 — 2026-08-21

### Security

- The file-drop server now accepts registered keys only; the shared password
  no longer opens it. Every partner has their own key, generated on the Data
  file transfer page, so one partner can be removed without disturbing the
  rest. Set SFTP_ALLOW_PASSWORD=true to allow passwords again on a development
  machine.

### Fixed

- The Data file transfer page told partners to connect on the wrong port. The
  port it displays and the port the server actually opens are now the same
  setting, so they cannot disagree again.

## 1.6.3 — 2026-08-21

### Added

- Branch staff can now be given a console account that shows them only what
  they need: Today, Chat and the knowledge graph. Everything else — the
  catalog, branches, the data pipeline, people, settings, the security log —
  is neither shown to them nor reachable by typing its address.

### Changed

- Chat history now belongs to the person, not the computer. On a shared branch
  machine, signing in no longer shows you the questions the last person asked.
  Anyone who had a conversation list before this release starts with an empty
  one; nothing they asked was lost, and administrators can still read every
  console conversation under Conversations.
- New accounts are created as either a user or a super admin. The middle
  "admin" role is no longer offered, because there is no one who should see
  the console but not its settings. Existing admin accounts keep working
  exactly as they did.

### Security

- A signed-in user with no admin rights is refused every administrative
  address, including the ones that hand back the SFTP password, the directory
  settings and the audit trail. An account that is waiting for approval, or
  that has been switched off, is still refused everything regardless.

## 1.6.2 — 2026-08-21

### Added

- The sign-in screen now has a Continue with LDAP button. Directory sign-in
  was already switched on and working, but the screen only ever asked for an
  email address — and most directory accounts are a plain username, which there
  was no way to type. The button swaps the form to a Username field; Continue
  with Email switches back, and the choice is remembered next time.
- Sending files by SFTP is now a page you can follow instead of a set of
  instructions you have to be told. Adding a partner is four numbered steps, the
  console makes the key itself rather than asking someone to produce one, and a
  replayed terminal shows exactly what a partner will type and see.
- A partner can be handed a single file that already contains their key and
  address, so there is nothing for them to fill in, rename or copy. It is also
  available as a downloadable pack.
- Clicking a partner opens a panel from the right with their key and the exact
  commands for that partner, ready to copy.
- A partner's key can be replaced in place — the old one stops working, the
  new one is issued, and the partner keeps their name, settings and history.
  Previously the only way was to delete them and start again.
- Staff who sign in through the directory or single sign-on, and whose account
  is not approved yet, now get a screen that thanks them by name of product,
  says an administrator will review the request, and shows which sign-in they
  used. It still tells them plainly that nobody is alerted automatically and
  they need to ask an administrator, because that remains true.

### Fixed

- The company name in the footer was wrong on every screen. It read City
  Medical Health & Logistics; the company is City Mart Holding Co., Ltd.
- The CMHL logo was almost invisible in dark mode — it sat on a dark tile, and
  the purple half of the mark disappeared into it.
- The waiting-for-approval screen sent people to a "Users page" that has not
  been called that for some time. It now points at People & access.

- The Branch assistant opened on an empty screen with nothing to click.
- The example questions on that screen looked like ordinary text, so nobody
  pressed them. They now look like the buttons they always were.
- The catalog was a wall of text rather than a table, and every row claimed the
  product was held at 53 sites — the same number on all 5,292 rows, because it
  was never read from anything. The column is gone.
- The chat button on customer sites squashed the logo into a circle and cropped
  its corners. It is now a square tile the right way up.
- Suggested follow-up questions appeared inside the assistant's answer, as if it
  had said them. They now sit below it, where they read as offers.
- Uploaded files needed a Save nobody could see. They are picked up on their own.
- Buttons across the console had stopped showing the hand cursor, so nothing
  looked pressable.

### Changed

- The assistant introduces itself as City Care Agent on customer sites; one
  place still said Stock assistant.

## 1.6.1 — 2026-08-20

### Fixed

- The chat assistant did not appear at all on the live site. The page is served
  over a secure connection, but the assistant's script was being requested over
  an insecure one, so the browser refused to run it and showed the page with no
  chat button on it. Nothing was wrong with the assistant itself.
- On a phone, pressing Tab moved through the whole of the closed navigation menu
  before reaching the page — twenty-one invisible steps. The menu is hidden off
  the side of the screen, but it was still in the keyboard's path. Opening the
  menu had the opposite fault: the keyboard could wander out of it onto the page
  behind. Neither happens now.
- Both the message box and the search box drew two rings when selected, one
  inside the other, which read as a rendering fault.
- "Skip to content" did nothing when the phone menu was open.

### Changed

- The product is now called City Care Agent throughout — the sign-in screen, the
  browser tab, the assistant's own name, and the line under the chat window on
  customer sites. A few places still said City Pharma.
- The sign-in headline no longer breaks the product name across two lines.
- The email box on the sign-in screen shows a neutral example address instead of
  one built from the old product name.

## 1.6.0 — 2026-08-20

### Fixed

- The chat button on your website was invisible. It carried the CityCare mark on
  an indigo circle, and the mark is itself a navy tile, so there was nothing to
  see but a blob. It now sits on white inside a coloured ring — and that ring is
  what keeps the button visible on a customer site with a dark page, where it
  used to disappear almost completely.
- The Knowledge graph drew nothing at all, while the counts above it reported
  16,021 connections. It was only ever willing to draw the "treats" layer, which
  is read out of indication text by the model and has not been built here. It now
  draws the connections that do exist — ingredients, shared generics and
  categories — and says in words which layer you are looking at and which one is
  missing.
- Opening the live preview on Embed and integration left a second chat button
  stuck to the console. It followed you onto other pages, and every visit that
  used the preview added another one.

### Changed

- The chat box is quieter. The four example questions were filled pills that read
  as the main thing to press when they are only suggestions; they are now plain
  text, and there are three. The branch and language pickers lost their boxes,
  the read-only marker no longer looks like a button you can press, and the send
  button is the one thing on the row with any weight.
- Branch assistant shows the preview inside a browser frame, with the real
  address it loads from. Without it the panel was a white rectangle that read as
  a page that had failed to load.
- There is one live widget in the console, not two. Embed and integration links
  to Branch assistant instead of running its own copy.
- "Live preview" is now "What a branch sees", and "Live test" is now "Test on a
  customer domain". The two pages looked like duplicates; the names now say which
  question each one answers.

## 1.5.0 — 2026-08-20

### Added

- Governance is a real page. It was a placeholder; it now shows how a change
  reaches this system — the five steps from someone asking for it to it being
  live — a log of the decisions already taken and what each one settled, and who
  supports the thing day to day. The support tiers drawn there are a proposal,
  not an agreement, and the page says so on itself rather than letting you
  assume otherwise.
- Foundations is a reference page for anyone checking this console's own
  work: every text colour measured against every background it is used on, the
  full type scale with the real fonts, and the five states a panel can be in
  (loading, empty, refused, failed, and the answer) shown side by side.
- Skip to content. Press Tab on any page and the first thing you get is a
  link that jumps straight past the sidebar. There are nineteen rows in that
  sidebar and they came before the page on every screen, so reaching the content
  from the keyboard used to mean nineteen presses, every time.

### Fixed

- A failed action showed a green tick. "Could not save branding", "backend
  offline — nothing was saved", "could not delete this user" — eleven different
  failure messages appeared with a success mark beside them. They now carry a
  warning mark and a red edge.
- Nothing was announced to screen readers when an action finished. Deleting
  a user, approving one, saving settings — the little message at the bottom of
  the screen is the only confirmation this console gives for those, and it was
  silent. Successes are now announced quietly, failures immediately.
- Confirmation dialogs did not take the keyboard. "Delete user" and "Delete
  credential" appeared while your typing cursor was still on the page behind
  them, Escape did nothing, and cancelling left you at the top of the document.
  Every dialog and side panel in the console now takes focus when it opens,
  keeps Tab inside itself, closes on Escape, and puts you back on the control
  you started from.
- Opening a product on Catalog & stock appeared to do nothing from the
  keyboard: the panel opened to one side, focus stayed on the table, and Escape
  was dead. Same for a file on Data pipeline.
- The keyboard focus outline was invisible on the sidebar. It was drawn in
  the page's indigo, on near-black — you could not see which row you were on for
  the first nineteen presses of every page. It is now drawn in a colour taken
  from the sidebar's own palette.
- Two side panels stayed reachable by keyboard while closed, so one press per
  page landed on an invisible "Close" button off the edge of the screen.
- Nine full-screen click-to-dismiss layers were keyboard stops of their own —
  four of them invisible. They are now click targets only, and Escape closes
  what they cover.
- Rows that behave like buttons now respond to the space bar as well as Enter.
  Space used to scroll the page instead.
- Ten buttons, menus and fields had no name a screen reader could read, or were
  named only by their tooltip. Per-row controls now say which row they act on —
  "Delete admin@citcare.local", not "Delete user" repeated forty times.
- Burmese text was clipped. Stacked marks above and below the line had
  between 0.4 and 3.8 pixels of room on five different surfaces; they now have
  5.4 to 6.5.
- Sidebar rows and the pages they open now share a name. Eight of them did
  not, so a row was a promise the page did not keep. Every page also has exactly
  one heading at the top level; four had as many as four.
- The page header used to eat the first line under a sticky tab strip, cutting a
  sentence in half on the Embed page.
- "Last signed in" on People & access printed a raw machine timestamp, and
  showed the same dash for "never signed in" as for "we could not read it".

### Changed

- Charts no longer paint two different things the same colour. On Activity,
  two of the three event sources were one colour. On Analytics, the two lines of
  the same chart — "Questions" and "From cache" — were one colour. A ten-category
  chart was drawn with five colours, so five pairs of categories were
  indistinguishable. Charts now carry six distinct colours; past six, the
  smallest categories are grouped into one band labelled "Other", with the count
  in the label, and the table beside the chart still lists every one of them by
  name.
- Segments stacked directly on top of each other are now separated by a hairline
  in the background colour, so the boundary is visible even where two colours sit
  close together.
- Response classes on Activity are five colours rather than three: 4xx and 5xx
  were both red and could not be told apart, which is the difference between
  "someone is probing us" and "we are broken".
- Faint chart colours that were close to invisible — the "Miss" bar, the
  "Target" line, the muted legend entries — are legible in both light and dark.

## 1.4.0 — 2026-08-18

### Changed

- Analytics and Activity are one page. There were fourteen tabs across two
  pages, all answering the same question — what has this system been doing? —
  and you had to know which of the two pages held the number you wanted. There
  are now six: Overview, Conversations, Speed & cost, Delivery, Activity and
  Explore. Nothing was removed; the panels that used to be their own tab are
  now sections you can jump to from a row of buttons under the filters.
- Old links still work. A bookmark to the Activity page, or to a particular
  Activity tab, opens the same view on the new page with its filters intact and
  scrolled to the section it named.
- The tab row now stays put while you scroll, so you can move between groups
  without going back to the top.

### Fixed

- The last date on a chart's bottom axis was cut off — "18 Aug" printed as
  "18 Au", which reads as a truncated date rather than a label out of room.
- Chart gridlines could sit at half-steps: a chart topping out around 41 drew
  its lines at 12.5 and 37.5 and labelled them "13" and "38", so a bar measured
  against the axis read half a step out.

## 1.3.0 — 2026-08-18

### Changed

- The charts on Analytics and Activity are half the height and no longer grow
  with the window. On a wide monitor a chart used to be 423 pixels tall with
  axis labels bigger than the page headings, because the whole drawing — text
  included — was being stretched to fit the width.
- Move the cursor across any chart and it now shows a marker line and a panel
  with every figure for that day at once. Previously you had to hover exactly on
  a dot, wait about a second for the browser's own tooltip, and it gave one
  figure at a time and nothing at all on a touchscreen.
- Chart scales now step in whole numbers. A chart whose largest value was 1 used
  to label its gridlines "1, 1, 1, 0, 0".
- Charts can be read from the keyboard: tab to one, then use the arrow keys to
  step through days, Home and End to jump, Enter to open that day's turns.

## 1.2.1 — 2026-08-18

### Fixed

- On the Settings, Embed and Quality pages, text slid visibly through the row
  of section links while you scrolled, and appeared to sit behind them. The bar
  stays put as intended; it now covers the whole strip of the page it sits on.

## 1.2.0 — 2026-08-18

### Added

- The SFTP uploads page now says which files the agent is actually answering
  from. Every upload is kept, so the same filename can appear many times; the
  live one — the most recent that successfully loaded, one for products and one
  for stock — is marked Live, and the page opens on that view. Older copies
  are labelled "superseded" and stay available to download or retry.

### Fixed

- Each upload now shows its own row count and its own history. Previously every
  stored copy of a file displayed whatever happened to the *most recent* upload
  of the same name, so five copies of one stock file all showed the same
  figures — and when the newest attempt had been rejected, files that had loaded
  correctly showed no row count at all.

## 1.1.3 — 2026-08-18

### Fixed

- Product files were refused if they arrived without the four title rows above
  the column headings. The export CMHL sent on 18 August had no title rows, so
  the file was rejected as "missing Article Code, Brand Name" while the stock
  file beside it loaded — leaving stock with no product names against it. Both
  layouts now load.

## 1.1.2 — 2026-08-18

### Fixed

- An empty or half-written stock file could delete every stock row. Uploads
  through the console were already protected, but the reload endpoint used by
  the deployment scripts was not — it emptied the table and reported success.
  A stock file with nothing usable in it now leaves the existing stock alone.

## 1.1.1 — 2026-08-18

### Fixed

- After an update, the console could keep showing the previous version's look
  and behaviour until you emptied the browser cache. The browser was allowed
  to reuse the console's start page without checking with the server first, so
  a deployed update could sit on the server unseen. The start page is now
  re-checked on every load.

## 1.1.0 — 2026-08-18

The console has been redesigned. Nothing was removed: every page, setting,
report and permission works exactly as it did — this release changes only how
it all looks.

### Changed

- The console has a new look throughout: a calmer page background, a clearer
  sidebar, one consistent shape for cards and buttons, and titles that are the
  same size on every page instead of five different sizes.
- Text is easier to read. Small grey labels, the amber "warning" colour and
  the green "success" colour were all too faint against a white page; each has
  been darkened to meet the accessibility standard for readable text. The
  warning colour was the worst offender and is now well clear of it.
- Tables across the console now use the same row height and type size, so a
  figure on the analytics page looks the same as the same figure on a
  product page.
- Hairline borders have been replaced with real one-pixel ones. On a standard
  (non-Retina) monitor the old hairlines rounded down to nothing, so some
  cards had no visible edge at all.
- Charts that show two things at once — English against Burmese, cached
  answers against fresh ones, tokens in against tokens out — now use two
  colours that are genuinely different. The second colour used to be the
  CityCare logo blue, which is too pale to read against white.
- One colour now means one thing across every chart: anything about the
  answer cache is green wherever it appears.
- The sign-in page has been rebuilt to match, and the panel beside the form
  is easier to read. Several words in it were previously a shade of blue that
  was too dark against its background to read comfortably.
- Sidebar and menu rows are now large enough to tap reliably on a phone or
  tablet.

## 1.0.0 — 2026-08-13

First numbered release. Everything before this shipped unversioned, so the
entries below cover the fixes made while adding versioning rather than a full
history.

### Fixed

- Answers to "do you have X" and "which branch has X" were being cut off
  mid-sentence, in both English and Burmese — the two questions asked most
  often. They now complete.
- Long answers — a full substitute list, or a price search — were cut off part
  way through the table.
- Answers arrive much faster: common stock questions now take about 5 seconds
  instead of 15, and long lists about 15 seconds instead of 35.
- The catalog page failed to open unless you typed a search term first.
- Refreshing the browser on the Users, Stores, Graph, Conversations or
  Learning page showed an error instead of the page.

### Security

- A staff account limited to one branch could see other branches' stock,
  prices and stock value on the inventory and stores pages. It now sees only
  its own branch.

### Changed

- The console now shows which version it is running, and these notes are
  readable in the console under Version.
