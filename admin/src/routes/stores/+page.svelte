<script>
  import { onMount } from 'svelte';
  import { Search, RefreshCw, CircleCheck, Sparkles, TriangleAlert, CircleSlash } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import { getJSON } from '$lib/api.js';
  import { int, dayLabel } from '$lib/charts/format.js';
  import { toast } from '$lib/aurora/toast.js';
  import ErrorState from '$lib/ErrorState.svelte';
  import DetailPanel from './DetailPanel.svelte';
  import StatusDialog from './StatusDialog.svelte';
  import { DETAIL_PATH } from './detail.js';
  import {
    ACTIVE,
    NEW,
    NODATA,
    DISABLED,
    FILTERS,
    STATE_LABEL,
    at,
    derive,
    hasStock,
    tally
  } from './status.js';

  let loading = $state(true);
  let error = $state(null);
  let stores = $state([]);
  let query = $state('');
  let filter = $state('all');

  // `now` is captured once per load rather than read inside `derive`, so every
  // row on screen is judged against the same instant. A `Date.now()` inside a
  // `$derived` would also re-run on every keystroke in the search box.
  let now = $state(Date.now());

  /**
   * `quiet` re-reads without tearing the table down.
   *
   * A status change has to re-read — the POST echoes the registry row but not
   * the inventory aggregates, and a re-enabled branch puts its stock back into
   * the totals on this page. Doing that through the normal loading state
   * replaced the whole table with "Loading branches…" for a moment after every
   * toggle, which reads as the page having fallen over.
   */
  async function load({ quiet = false } = {}) {
    if (!quiet) loading = true;
    error = null;
    try {
      const data = await getJSON('/admin/stores');
      stores = Array.isArray(data) ? data : [];
      now = Date.now();
    } catch (e) {
      // Keep the error OBJECT: its status is the difference between "sign in
      // again" and "the server is down". Never collapse it to a message.
      //
      // A QUIET refresh never destroys the list. It runs after a status change
      // that the server already accepted, so throwing the table away over a
      // flaky re-read would hide a change that did happen. The rows on screen
      // stay — they are still what the server last told us — and the toast
      // below carries the failure.
      if (!quiet) {
        error = e;
        stores = [];
      } else {
        toast(reason(e, 'refresh the branch list'), 'alert-triangle');
      }
    } finally {
      loading = false;
    }
  }

  // ---- who is looking ------------------------------------------------------
  //
  // The console has no shared store for the signed-in user; the layout keeps
  // `me` to itself. Every page that needs the role reads /auth/me the same way
  // the layout does, so this one does too — the role is NEVER decoded out of
  // the JWT (the chat page decodes `sub` for a storage key and says why that
  // is not a security decision; a permission is).
  //
  // `me` stays null until it answers, and null means no ⋮ at all. Failing
  // closed is the right way round here: the backend enforces super_admin on
  // POST /admin/stores/{code}/status regardless, so the worst a wrong guess
  // could do is offer a control that 403s — but offering it and then refusing
  // is a worse experience than never showing it.
  let me = $state(null);
  async function loadMe() {
    try {
      me = await getJSON('/auth/me');
    } catch {
      /* offline, expired, or an older backend — no control is offered */
    }
  }

  let isSuper = $derived(me?.role === 'super_admin');

  onMount(() => {
    load();
    loadMe();
  });

  // ---- counts and filtering ------------------------------------------------

  let counts = $derived(tally(stores, now));

  let filtered = $derived.by(() => {
    const f = FILTERS.find((x) => x.id === filter) ?? FILTERS[0];
    const q = query.trim().toLowerCase();
    return stores.filter((r) => {
      if (!f.test(r, now)) return false;
      if (!q) return true;
      return (
        (r.site_code ?? '').toLowerCase().includes(q) ||
        (r.site_name ?? '').toLowerCase().includes(q)
      );
    });
  });

  // Only buckets that actually hold something. A "0 disabled" reads as a fact
  // nobody asked for and pushes the numbers that matter along the line.
  let countLine = $derived(
    [
      `${int(filtered.length)} shown`,
      ...[ACTIVE, NEW, NODATA, DISABLED]
        .filter((s) => counts[s] > 0)
        .map((s) => `${int(counts[s])} ${STATE_LABEL[s].toLowerCase()}`)
    ].join(' · ')
  );

  // ---- per-state presentation ---------------------------------------------
  //
  // Every state carries a GLYPH and a WORD. Colour is the third signal, never
  // the only one — two of these four (New and Active) sit close together in
  // hue once the accent is re-themed, and a colour-blind reader would have no
  // way to tell them apart.
  const STATE_STYLE = {
    [ACTIVE]: { icon: CircleCheck, cls: 'text-success' },
    [NEW]: { icon: Sparkles, cls: 'text-accent' },
    [NODATA]: { icon: TriangleAlert, cls: 'text-warning' },
    [DISABLED]: { icon: CircleSlash, cls: 'text-ink-2' }
  };

  /**
   * The line under a row, or '' for a row that needs none.
   *
   * Every clause here is built from a field the backend actually sent. Where a
   * timestamp is missing the clause is dropped rather than guessed — the
   * registry is populated at boot and this console can be pointed at a backend
   * that has not restarted yet, so null dates are a real state, not a bug.
   */
  function subline(row) {
    const state = derive(row, now);

    if (state === DISABLED) {
      const parts = [];
      const who = row.disabled_by ? `hidden by ${row.disabled_by}` : 'hidden by an admin';
      const on = at(row.disabled_at) ? ` on ${dayLabel(row.disabled_at)}` : '';
      parts.push(who + on);
      if (row.note) parts.push(`“${row.note}”`);
      return parts.join(' · ');
    }

    if (state === NODATA) {
      const parts = [];
      if (at(row.last_seen_in_file)) {
        parts.push(`last in a file ${dayLabel(row.last_seen_in_file)}`);
      }
      if (at(row.missing_since)) {
        parts.push(`missing from the file since ${dayLabel(row.missing_since)}`);
      } else if (!hasStock(row)) {
        parts.push('no stock rows in the current file');
      }
      return parts.join(' · ');
    }

    if (state === NEW && at(row.first_seen)) {
      const d = new Date(row.first_seen);
      const sameDay = new Date(now).toDateString() === d.toDateString();
      return sameDay
        ? `first seen in today's file, ${dayLabel(row.first_seen)}`
        : `first seen ${dayLabel(row.first_seen)}`;
    }

    return '';
  }

  /**
   * A measured number, or `—` when there is nothing behind it.
   *
   * A branch with no inventory rows has no stock FIGURE, and `0` would be a
   * claim that it holds nothing. The console's rule everywhere else is that
   * `—` means we do not know and `0` means we measured zero; a branch dropped
   * from the export is squarely the first.
   */
  const cell = (row, v) => (hasStock(row) ? int(v) : '—');

  // ---- the selected branch and its detail ---------------------------------
  //
  // The page owns this fetch rather than the panel, because the confirmation
  // dialog quotes the same payload back at the person about to hide the branch
  // ("that is 4.4% of everything"). Two components fetching one URL is how
  // those two numbers start disagreeing.

  let selected = $state(null); // site_code, or null for nothing picked
  let detail = $state(null);
  let detailLoading = $state(false);
  let detailError = $state(null);

  let selectedRow = $derived(stores.find((r) => r.site_code === selected) ?? null);

  // The payload is only usable while it still describes the branch on screen.
  // Without this guard a slow response for the previously selected branch
  // would land under the new branch's header and read as its stock.
  let shownDetail = $derived(detail?.site_code === selected ? detail : null);

  async function loadDetail(code, { quiet = false } = {}) {
    if (!code) return;
    if (!quiet) {
      detail = null;
      detailLoading = true;
    }
    detailError = null;
    try {
      const body = await getJSON(DETAIL_PATH(code));
      // Ignore a response that arrived after the user moved on.
      if (selected !== code) return;
      detail = body && typeof body === 'object' ? { site_code: code, ...body } : null;
    } catch (e) {
      if (selected !== code) return;
      detailError = e;
      detail = null;
    } finally {
      if (selected === code) detailLoading = false;
    }
  }

  // Keyed on `selected` alone. Reading anything else reactive in here would
  // re-fetch the branch on every keystroke in the filter box.
  $effect(() => {
    const code = selected;
    if (code) loadDetail(code);
    else {
      detail = null;
      detailError = null;
      detailLoading = false;
    }
  });

  /** Enter OR Space, matching what `role="button"` promises a screen reader.
      Space is a button's primary activation key; without preventDefault it
      scrolls the table instead of picking the row. Same shape as
      `routes/data/+page.svelte` and the FTP file rows. */
  function rowActivate(e, fn) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      fn();
    }
  }

  // ---- changing a branch's status -----------------------------------------

  let dialogRow = $state(null);
  let dialogNext = $state('disabled');
  let dialogOpen = $state(false);
  let saving = $state(false);

  function ask(row, next) {
    dialogRow = row;
    dialogNext = next;
    dialogOpen = true;
  }

  /** Status-aware reason an ACTION failed. Same shape as the Users page. */
  function reason(e, verb) {
    const s = Number(e?.status ?? 0);
    if (s === 401) return `Your sign-in has timed out. Sign in again, then ${verb}.`;
    if (s === 403) return `Your account is not permitted to ${verb}.`;
    if (s === 404) return `That branch is no longer in the registry — could not ${verb}.`;
    if (s > 0) return e?.message || `The backend answered ${s}.`;
    return `No response from the backend — could not ${verb}.`;
  }

  async function apply(note) {
    const row = dialogRow;
    if (!row) return;
    const next = dialogNext;
    const verb = next === 'disabled' ? 'hide this branch' : 'show this branch';
    saving = true;
    try {
      const body = await getJSON(
        `/admin/stores/${encodeURIComponent(row.site_code)}/status`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: next, note: note || null })
        }
      );
      // Prefer the server's echo of the row — it carries disabled_by and
      // disabled_at, which only the server knows. Fall back to patching the
      // status alone rather than leaving the row showing its old state.
      const i = stores.findIndex((x) => x.site_code === row.site_code);
      if (i >= 0) {
        stores[i] =
          body && typeof body === 'object' && body.site_code
            ? { ...stores[i], ...body }
            : { ...stores[i], status: next };
      }
      const name = row.site_name?.trim() || row.site_code;
      toast(
        next === 'disabled'
          ? `${name} is hidden from customers`
          : `${name} is back in customer answers`
      );
      dialogOpen = false;
      // The echo does not carry the aggregates, and a re-enabled branch brings
      // its stock back into every total on this page. Re-read rather than
      // leaving the summary line describing the state before the change.
      load({ quiet: true });
      // And re-read the panel: the change just added a row to the audit trail
      // this branch is showing, so leaving it would present a record of who
      // changed what that is missing the change made ten seconds ago. Quiet,
      // for the same reason `load` is — the panel must not blank out under a
      // change the server already accepted.
      if (selected === row.site_code) loadDetail(row.site_code, { quiet: true });
    } catch (e) {
      toast(reason(e, verb), 'alert-triangle');
    } finally {
      saving = false;
    }
  }

  /**
   * The row the dialog quotes figures from.
   *
   * The registry row carries `units` and `value`; only `/detail` knows what
   * share of the estate that is. Merged, and only when the payload is for the
   * same branch — quoting one branch's share over another branch's units is
   * the worst possible sentence to put in front of a destructive confirm.
   */
  let dialogSubject = $derived(
    dialogRow && shownDetail?.site_code === dialogRow.site_code
      ? // Detail first, registry row last: the list row is the freshest thing
        // we have for `status`, `units` and `value`, and detail is only here to
        // fill in what the list does not carry (`pct_of_value`).
        { ...shownDetail, ...dialogRow }
      : dialogRow
  );
</script>

<!-- The rail row says "Branches". A row is a promise about where it goes,
     and this page announced itself as "Stores" — the word the database uses,
     not the word the person clicked. -->
<PageHeader
  title="Branches"
  subtitle="Every branch we know about, whether or not it sent stock today. A hidden branch stays here; it just stops existing for customers."
>
  {#snippet meta()}
    <!-- Only counts we actually loaded. Before that, `stores` is [] and this
         chip would announce "0 shown" over a failure — a number nobody sent. -->
    {#if !loading && !error}
      <span class="text-meta text-ink-2 tnum">{countLine}</span>
    {/if}
  {/snippet}
  {#snippet actions()}
    <!-- `motion-safe:transition-[background-color]`, NOT `transition-colors`.
         Tailwind v4's `transition-colors` includes `outline-color`, so on a
         focusable control it fades the global focus ring in over 150ms instead
         of showing it — the ring being the only thing that tells a keyboard
         user where they are. Only the property that actually moves is named. -->
    <button
      onclick={() => load()}
      disabled={loading}
      class="inline-flex cursor-pointer items-center gap-2 rounded-panel border border-line px-3.5 py-2 text-body-sm font-medium text-ink duration-150 motion-safe:transition-[background-color] hover:bg-surface-2 disabled:cursor-default disabled:opacity-60"
    >
      <RefreshCw size={15} class={loading ? 'animate-spin' : ''} />
      Refresh
    </button>
  {/snippet}
</PageHeader>

{#if loading}
  <div class="rounded-card border border-line bg-surface-2 px-5 py-6 text-body-sm text-ink-2">
    Loading branches…
  </div>
{:else if error}
  <ErrorState {error} retry={() => load()} what="branches" />
{:else if stores.length === 0}
  <div class="rounded-card border border-line bg-surface-2 px-5 py-6 text-body-sm text-ink-2">
    <p class="font-medium text-ink">No branches</p>
    <p class="mt-1">
      The registry is empty. It fills itself from the inventory file, so this normally means no
      file has been loaded yet.
    </p>
  </div>
{:else}
  <!-- Filter chips.
       `aria-pressed` toggle buttons in a named group, NOT a tablist: these
       filter one list in place, they do not switch between panels, and a
       screen reader announcing "tab 2 of 4" would promise a panel change that
       never comes. -->
  <div
    role="group"
    aria-label="Filter branches by status"
    class="mb-3 flex flex-wrap items-center gap-2"
  >
    {#each FILTERS as f (f.id)}
      {@const on = filter === f.id}
      <button
        type="button"
        aria-pressed={on}
        onclick={() => (filter = f.id)}
        class="rounded-panel border px-3 py-1.5 text-body-sm font-medium
               {on
          ? 'border-accent bg-accent-soft text-accent-hover'
          : 'border-line bg-surface text-ink-2 hover:bg-surface-2 hover:text-ink'}"
      >
        {f.label}
      </button>
    {/each}

    <!-- Search / filter -->
    <div class="relative ml-auto w-full max-w-xs">
      <Search
        size={15}
        class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-3"
      />
      <input
        type="text"
        bind:value={query}
        aria-label="Filter by branch code or name"
        placeholder="Filter code or name…"
        class="w-full rounded-card border border-line bg-surface py-2 pl-9 pr-3 text-body-sm text-ink placeholder:text-ink-3 focus:border-accent"
      />
    </div>
  </div>

  <!-- Table and panel. Side by side where there is room; the panel drops BELOW
       the table on a narrow viewport rather than squeezing five numeric columns
       into a third of the width.

       `items-start` so the panel can be its own height and stick, instead of
       being stretched to the height of a 53-row table. -->
  <div class="grid grid-cols-1 items-start gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
    <!-- The card is deliberately NOT `overflow-hidden` and the body does not
         scroll inside it: the page scrolls, and the sticky `thead` from app.css
         keeps the column names in view while it does. -->
    <section class="rounded-card border border-line bg-surface">
      <table class="tbl">
        <thead>
          <tr>
            <!-- Code and name are ONE column, not two.
               `inventory.site_name` is empty for all 53 branches on dev and
               stays empty until a file carrying that column loads, so a
               separate Name column would be a header standing over 53 blanks —
               which reads as data that failed to arrive, not as a branch that
               has no name yet. The code is the identity here; the name is an
               enhancement that sits beside it when there is one.

                 `min-width` reserves the name's room NOW, so a name appearing
                 later widens nothing and the columns to the right do not
                 move. -->
            <th style="min-width:220px">Branch</th>
            <th class="num">SKUs</th>
            <th class="num">Units</th>
            <th class="num">Stock value</th>
            <th style="width:120px">Status</th>
          </tr>
        </thead>
        <tbody>
          {#each filtered as row (row.site_code)}
            {@const state = derive(row, now)}
            {@const look = STATE_STYLE[state]}
            {@const Icon = look.icon}
            {@const note = subline(row)}
            {@const picked = selected === row.site_code}
            <!-- A hidden branch is tinted, never dimmed. This is the admin view;
                 the hiding is for customers, and an opacity that made the row
                 "look off" would take its text below AA at the same time.

                 Selection is an INLINE style, not a utility class. `app.css` is
                 unlayered and Tailwind's utilities live in `@layer utilities`,
                 so `.tbl tbody tr:hover { background: … }` beats any
                 `bg-accent-soft` written as a class — the selected row would
                 lose its tint the moment the pointer crossed it. Same trap as
                 the 48 inert `outline-none`s.

                 The 3px inset bar is not decoration either: it is the signal
                 that does not depend on colour, alongside `aria-current` for a
                 reader. -->
            <tr
              role="button"
              tabindex="0"
              aria-current={picked ? 'true' : undefined}
              aria-controls="branch-detail"
              onclick={() => (selected = row.site_code)}
              onkeydown={(e) => rowActivate(e, () => (selected = row.site_code))}
              class="cursor-pointer {row.status === 'disabled' ? 'bg-surface-2' : ''}"
              style={picked
                ? 'background:var(--color-accent-soft); box-shadow: inset 3px 0 0 var(--color-accent);'
                : ''}
            >
              <!-- No placeholder where the name would go. A row that is just its
                   code is complete, because the code IS how this branch is
                   identified everywhere else in the product; an em-dash there
                   would say something is missing when nothing is. -->
              <td>
                <span class="font-medium text-ink tnum">{row.site_code}</span>
                {#if row.site_name?.trim()}
                  <span class="ml-2 text-ink-2">{row.site_name.trim()}</span>
                {/if}
              </td>
              <td class="num text-ink-2">{cell(row, row.skus)}</td>
              <td class="num text-ink-2">{cell(row, row.units)}</td>
              <td class="num whitespace-nowrap text-ink-2">
                {hasStock(row) ? `${int(row.value)} MMK` : '—'}
              </td>
              <td>
                <span class="inline-flex items-center gap-1.5 whitespace-nowrap {look.cls}">
                  <Icon size={14} aria-hidden="true" />
                  <span class="text-body-sm font-medium">{STATE_LABEL[state]}</span>
                </span>
              </td>
            </tr>
            {#if note}
              <!-- The explanation for the row above. `border-top:0` and no top
                   padding keep it attached to its row instead of reading as a
                   sixth branch.

                   Deliberately NOT clickable and not a tab stop. It is a
                   continuation of the row above, and making it a second
                   `role="button"` would put two tab stops on one branch and
                   announce the same branch twice. It carries the selected tint
                   so the selection still reads as one block. -->
              <tr
                class={row.status === 'disabled' ? 'bg-surface-2' : ''}
                style={picked
                  ? 'background:var(--color-accent-soft); box-shadow: inset 3px 0 0 var(--color-accent);'
                  : ''}
              >
                <td colspan="5" class="text-meta text-ink-3" style="border-top:0; padding-top:0;">
                  {note}
                </td>
              </tr>
            {/if}
          {/each}
          {#if filtered.length === 0}
            <tr>
              <td colspan="5" class="text-center text-body-sm text-ink-2" style="padding:24px 16px;">
                {query.trim()
                  ? `No branches match “${query.trim()}”.`
                  : 'No branches in this view.'}
              </td>
            </tr>
          {/if}
        </tbody>
      </table>
    </section>

    <DetailPanel
      row={selectedRow}
      detail={shownDetail}
      loading={detailLoading}
      error={detailError}
      retry={() => loadDetail(selected)}
      {isSuper}
      {saving}
      onrequest={(next) => ask(selectedRow, next)}
      onstale={() => {
        // Minting refused because the branch was hidden somewhere else since
        // this panel loaded. Re-read BOTH: the list, so the row's status and
        // the count line stop contradicting the panel, and the panel itself.
        // Quiet, because nothing failed — we are behind, not broken.
        load({ quiet: true });
        if (selected) loadDetail(selected, { quiet: true });
      }}
    />
  </div>
{/if}

<StatusDialog
  open={dialogOpen}
  row={dialogSubject}
  next={dialogNext}
  busy={saving}
  onconfirm={apply}
  onclose={() => (dialogOpen = false)}
/>
