<!--
  Everything we know about one branch.

  The panel scrolls WITH the page, and is deliberately not a sticky box with its
  own scrollbar. Measured rather than assumed: the console's scroll container is
  the shell's `<main>`, not the window, so a `max-height: calc(100vh - 2rem)`
  resolves ~44px taller than the space `main` actually offers — and because the
  panel starts 247px down the page, its own scrollbar reached its end with the
  last 215px of the audit trail still below the fold and no page scroll left to
  reveal it. The refused attempts at the bottom of "Who changed what" were
  unreachable. Any fix that keeps the sticky box has to hardcode the shell
  header's height from a file this page does not own; the panel is 2187px tall
  for a busy branch anyway, so sticky only ever traded one scrollbar for a
  worse one.

  Presentational: the page owns the fetch, because the confirmation dialog needs
  the same payload (it quotes `pct_of_value` at the person about to hide the
  branch) and two components fetching the same URL is how those two numbers
  start disagreeing. The ONE thing this component fetches for itself is the
  embed snippet, which nothing else on the page reads and which must not be
  requested at all for a hidden branch.

  Two notes in here are the point of the whole panel rather than decoration, and
  their wording has been through several rounds with the owner:

    * "Before tracking" — 53 branches predate the registry and have a NULL
      `first_seen`. There is no honest date to show and today's date would be a
      lie, so the panel says so in words instead of rendering a blank.
    * "No chats yet" — a branch with no attributable conversations is not
      necessarily a branch nobody talked to. `chat_logs.store_id` is populated
      only for store-scoped embed sessions, so a customer who used the main site
      leaves a conversation carrying no branch at all. That is a real gap in
      what we can measure and the panel says which of the two it might be,
      rather than presenting a zero as a finding.
-->
<script>
  import {
    CircleCheck,
    CircleSlash,
    TriangleAlert,
    Info,
    Copy,
    Check,
    ExternalLink
  } from '@lucide/svelte';
  import { getJSON } from '$lib/api.js';
  import { toast } from '$lib/aurora/toast.js';
  import ErrorState from '$lib/ErrorState.svelte';
  import { int, ms, share, when, dayLabel, UNKNOWN, isNum } from '$lib/charts/format.js';
  import {
    EMBED_PATH,
    agentCost,
    mmk,
    estateShare,
    eventTone,
    hasChats,
    isSystemRecord,
    undated,
    TONE_WORD,
    OK,
    REFUSED,
    FAILED
  } from './detail.js';
  import StatusToggle from './StatusToggle.svelte';

  let {
    /** The registry row from the list — always present the moment a row is picked. */
    row = null,
    /** The `/detail` payload, or null while it loads. */
    detail = null,
    loading = false,
    error = null,
    retry = null,
    /** Only a super admin may change a branch's status. */
    isSuper = false,
    /** Busy while a status change is in flight. */
    saving = false,
    /** Called with the requested status. */
    onrequest,
    /**
     * "This branch's status changed underneath us — re-read it."
     *
     * Minting answers 404 for a branch that has been hidden since this panel
     * loaded, and that 404 is not a thing to show anybody; it is a signal that
     * the panel is stale.
     */
    onstale
  } = $props();

  // The header renders from the LIST row, which is already on screen, so
  // picking a branch does not blank the panel while `/detail` is in the air.
  // Everything below the header waits for the payload.
  let code = $derived(row?.site_code ?? '');
  let name = $derived(row?.site_name?.trim() || '');
  let hidden = $derived(row?.status === 'disabled');

  // ---- the embed snippet ---------------------------------------------------
  //
  // Not requested for a hidden branch. The API refuses one and it is right to:
  // a snippet outlives the request that minted it, sitting in a customer's HTML
  // with nothing to re-check it later, so there is no such thing as issuing one
  // "just to show it greyed out". Asking anyway would put a 403 in the audit
  // trail every time somebody clicked a hidden branch.
  let snippet = $state(null);
  let embedBody = $state(null); // the whole payload — Preview needs it back
  let snippetErr = $state(null);
  let snippetBusy = $state(false);
  let copied = $state(false);
  let previewBusy = $state(false);

  $effect(() => {
    const c = code;
    const canMint = !hidden;
    snippet = null;
    embedBody = null;
    snippetErr = null;
    copied = false;
    if (!c || !canMint) return;

    let live = true;
    snippetBusy = true;
    getJSON(EMBED_PATH(c))
      .then((body) => {
        if (!live) return;
        const s = body?.snippet;
        if (typeof s === 'string' && s.trim()) {
          snippet = s;
          embedBody = body;
        } else {
          // A body with no snippet in it is an ABSENT snippet, never an empty
          // code block presented as one — a customer pasting that gets silence.
          snippetErr = { status: 0, message: 'The backend returned no snippet.' };
        }
      })
      .catch((e) => {
        if (!live) return;
        // 404 here does NOT mean "no such branch". Minting reuses
        // `_validate_outlet_request`, which refuses a hidden branch with the
        // same 404 and the same "unknown store_id" wording it uses for a code
        // that never existed — deliberately, so nobody holding a credential can
        // discover which branches are offline. That string must never reach
        // this panel: on a branch the operator hid themselves, "unknown
        // store_id" reads as our bug.
        //
        // The panel already knows this branch's status, so a 404 here can only
        // mean it was hidden somewhere else since this panel loaded. Re-read
        // rather than render anything: the redraw comes back with `hidden`
        // true and shows the real reason.
        if (Number(e?.status) === 404) {
          onstale?.();
          return;
        }
        snippetErr = e;
      })
      .finally(() => {
        if (live) snippetBusy = false;
      });

    return () => {
      live = false;
    };
  });

  /**
   * Open the branch's embed on a demo page.
   *
   * The blank tab is opened SYNCHRONOUSLY, inside the click, and only then
   * pointed at the minted URL. Opening it after the await is what a popup
   * blocker stops — the gesture has expired by the time the round trip
   * returns, and the button would silently do nothing.
   */
  async function preview() {
    if (!embedBody || previewBusy) return;
    const tab = window.open('about:blank', '_blank');
    if (tab) tab.opener = null;
    previewBusy = true;
    try {
      const { url } = await getJSON('/admin/embed/preview-link', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(embedBody)
      });
      if (!url) throw new Error('no preview URL');
      if (tab) tab.location.replace(url);
      else toast('Your browser blocked the preview tab — allow pop-ups for this page.', 'alert-triangle');
    } catch (e) {
      tab?.close();
      toast(
        Number(e?.status) === 404
          ? 'This branch has just been hidden, so a preview cannot be opened.'
          : 'Could not open a preview for this branch.',
        'alert-triangle'
      );
    } finally {
      previewBusy = false;
    }
  }

  async function copySnippet() {
    if (!snippet) return;
    try {
      await navigator.clipboard.writeText(snippet);
      copied = true;
      setTimeout(() => (copied = false), 1400);
    } catch {
      // Clipboard access can be refused outright. The code is on screen and
      // selectable, so say that rather than failing silently.
      toast('Could not reach the clipboard — select the code and copy it by hand.', 'alert-triangle');
    }
  }

  const TONE_STYLE = {
    [OK]: { icon: CircleCheck, cls: 'bg-surface-2 text-ink-2' },
    [REFUSED]: { icon: CircleSlash, cls: 'bg-warning-soft text-warning' },
    [FAILED]: { icon: TriangleAlert, cls: 'bg-danger-soft text-danger' },
    unclear: { icon: Info, cls: 'bg-surface-2 text-ink-3' }
  };
</script>

<aside
  id="branch-detail"
  aria-labelledby="branch-detail-code"
  class="rounded-card border border-line bg-surface"
>
  {#if !row}
    <p class="px-4 py-6 text-body-sm text-ink-2">
      Pick a branch to see its stock, its conversations and who has changed it.
    </p>
  {:else}
    <!-- Header -->
    <div class="flex flex-wrap items-baseline gap-2 border-b border-line px-4 py-3">
      <b id="branch-detail-code" class="font-mono text-body-sm font-semibold text-ink tnum">
        {code}
      </b>
      {#if name}
        <span class="rounded-full bg-surface-2 px-2 py-0.5 text-micro uppercase tracking-[0.06em] text-ink-3">
          {name}
        </span>
      {/if}
      {#if isNum(detail?.rank) && isNum(detail?.of_branches)}
        <span
          class="rounded-full bg-accent-soft px-2 py-0.5 text-micro uppercase tracking-[0.06em] text-accent tnum"
        >
          rank {int(detail.rank)} of {int(detail.of_branches)}
        </span>
      {/if}
    </div>

    <!-- Shown to customers. Above the fetch, because it must work even if
         `/detail` is failing: hiding a branch that is answering wrongly is
         exactly the thing somebody would come here to do in a hurry. -->
    <section class="border-b border-line-2 px-4 py-3.5">
      <h3 class="mb-2 text-micro font-semibold uppercase tracking-[0.07em] text-ink-3">
        Shown to customers
      </h3>
      <div class="flex items-center justify-between gap-3">
        {#if isSuper}
          <StatusToggle status={row.status} {code} disabled={saving} onrequest={(n) => onrequest?.(n)} />
        {:else}
          <span class="inline-flex items-center gap-1.5 py-1 {hidden ? 'text-ink-2' : 'text-success'}">
            {#if hidden}
              <CircleSlash size={14} aria-hidden="true" />
            {:else}
              <CircleCheck size={14} aria-hidden="true" />
            {/if}
            <span class="text-body-sm font-medium">{hidden ? 'Hidden' : 'Shown'}</span>
          </span>
        {/if}
        <span class="max-w-[210px] text-right text-meta text-ink-3">
          {hidden ? 'answered as if it does not exist' : 'offered as a place that has stock'}
        </span>
      </div>
      {#if !isSuper}
        <p class="mt-2 text-meta text-ink-3">Only a super admin can change this.</p>
      {/if}
    </section>

    {#if loading}
      <p class="px-4 py-6 text-body-sm text-ink-2">Loading this branch…</p>
    {:else if error}
      <div class="px-4 py-4">
        <ErrorState {error} {retry} what="this branch" />
      </div>
    {:else if detail}
      <!-- Stock held -->
      <section class="border-b border-line-2 px-4 py-3.5">
        <h3 class="mb-2 text-micro font-semibold uppercase tracking-[0.07em] text-ink-3">
          Stock held
        </h3>
        <div class="grid grid-cols-2 gap-2.5">
          <div class="rounded-card bg-surface-2 px-3 py-2.5">
            <b class="block text-title font-semibold text-ink tnum">{int(detail.units)}</b>
            <span class="text-label text-ink-3">units</span>
          </div>
          <div class="rounded-card bg-surface-2 px-3 py-2.5">
            <b class="block text-title font-semibold text-ink tnum">{int(detail.skus)}</b>
            <span class="text-label text-ink-3">products</span>
          </div>
        </div>
        <dl class="mt-2.5">
          <div class="flex justify-between gap-3 py-[3px] text-body-sm">
            <dt class="text-ink-3">Stock value</dt>
            <dd class="text-ink tnum">{mmk(detail.value)}</dd>
          </div>
          <div class="flex justify-between gap-3 py-[3px] text-body-sm">
            <dt class="text-ink-3">Share of all branches</dt>
            <dd class="text-ink tnum">{estateShare(detail.pct_of_value)}</dd>
          </div>
          <div class="flex justify-between gap-3 py-[3px] text-body-sm">
            <dt class="text-ink-3">Average price</dt>
            <dd class="text-ink tnum">{mmk(detail.avg_price)}</dd>
          </div>
          <div class="flex justify-between gap-3 py-[3px] text-body-sm">
            <dt class="text-ink-3">Negative quantities</dt>
            <dd class="inline-flex items-center gap-1 tnum {detail.negatives > 0 ? 'text-warning' : 'text-ink'}">
              {int(detail.negatives)}
              {#if detail.negatives > 0}
                <TriangleAlert size={13} aria-hidden="true" />
                <span class="sr-only">— sent as negative by the file</span>
              {/if}
            </dd>
          </div>
        </dl>
      </section>

      <!-- Biggest holdings -->
      {#if detail.top_holdings?.length}
        <section class="border-b border-line-2 px-4 py-3.5">
          <h3 class="mb-2 text-micro font-semibold uppercase tracking-[0.07em] text-ink-3">
            Biggest holdings
          </h3>
          <ul class="flex flex-col gap-1.5 text-body-sm">
            {#each detail.top_holdings as t, i (t.product ?? i)}
              <li class="flex justify-between gap-2.5">
                <span class="text-ink">{t.product}</span>
                <span class="whitespace-nowrap text-ink-3 tnum">
                  {int(t.qty)}{isNum(t.price) ? ` · ${int(t.price)} MMK` : ''}
                </span>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      <!-- Record -->
      <section class="border-b border-line-2 px-4 py-3.5">
        <h3 class="mb-2 text-micro font-semibold uppercase tracking-[0.07em] text-ink-3">Record</h3>
        <dl>
          <div class="flex justify-between gap-3 py-[3px] text-body-sm">
            <dt class="text-ink-3">Opened</dt>
            <!-- Never today's date. See `undated()`. -->
            <dd class="text-ink">{undated(detail) ? 'Before tracking' : dayLabel(detail.first_seen)}</dd>
          </div>
          <div class="flex justify-between gap-3 py-[3px] text-body-sm">
            <dt class="text-ink-3">Last sent stock</dt>
            <dd class="text-ink tnum">{when(detail.last_seen_in_file)}</dd>
          </div>
          <div class="flex justify-between gap-3 py-[3px] text-body-sm">
            <dt class="text-ink-3">Missing from a file</dt>
            <dd class="text-ink">
              {detail.missing_since ? `Since ${dayLabel(detail.missing_since)}` : 'No'}
            </dd>
          </div>
        </dl>
        {#if undated(detail)}
          <p
            class="mt-2 rounded-r-card border-l-[3px] border-warning bg-warning-soft px-3 py-2.5 text-meta leading-relaxed text-ink-2"
          >
            <b class="text-ink">We do not know when this branch opened.</b>
            It existed before the branch list began, so there is no honest date to show. Branches
            that arrive from now on will have one.
          </p>
        {/if}
      </section>

      <!-- Customer conversations -->
      <section class="border-b border-line-2 px-4 py-3.5">
        <h3 class="mb-2 text-micro font-semibold uppercase tracking-[0.07em] text-ink-3">
          Customer conversations
        </h3>
        {#if hasChats(detail)}
          <div class="grid grid-cols-2 gap-2.5">
            <div class="rounded-card bg-surface-2 px-3 py-2.5">
              <b class="block text-title font-semibold text-ink tnum">{int(detail.chats.count)}</b>
              <span class="text-label text-ink-3">conversations</span>
            </div>
            <div class="rounded-card bg-surface-2 px-3 py-2.5">
              <b class="block text-title font-semibold text-ink tnum">{int(detail.chats.visitors)}</b>
              <!-- "visitors identified", not "visitors". This counts DISTINCT
                   non-null session ids, and the embed widget sends none — so a
                   branch whose customers all came through the widget shows real
                   conversations against zero. Under the bare word "visitors"
                   that reads as "19 questions, nobody asked them". -->
              <span class="text-label text-ink-3">visitors identified</span>
            </div>
          </div>
          {#if detail.chats.visitors === 0}
            <p class="mt-2 text-meta leading-relaxed text-ink-2">
              None of these conversations carried a visitor id, so we cannot say how many people
              they were. The embed widget does not send one — a turn was observed, a visitor was
              not.
            </p>
          {/if}
          <dl class="mt-2.5">
            <div class="flex justify-between gap-3 py-[3px] text-body-sm">
              <dt class="text-ink-3">Last one</dt>
              <dd class="text-ink tnum">{when(detail.chats.last)}</dd>
            </div>
            <div class="flex justify-between gap-3 py-[3px] text-body-sm">
              <dt class="text-ink-3">First one</dt>
              <dd class="text-ink tnum">{when(detail.chats.first)}</dd>
            </div>
            <div class="flex justify-between gap-3 py-[3px] text-body-sm">
              <dt class="text-ink-3">Answered instantly from cache</dt>
              <dd class="text-ink tnum">
                {int(detail.chats.cached)} ({share(detail.chats.cached, detail.chats.count)})
              </dd>
            </div>
            <div class="flex justify-between gap-3 py-[3px] text-body-sm">
              <dt class="text-ink-3">Typical wait</dt>
              <dd class="text-ink tnum">{ms(detail.chats.avg_ms)}</dd>
            </div>
            <div class="flex justify-between gap-3 py-[3px] text-body-sm">
              <dt class="text-ink-3">Could not answer</dt>
              <dd class="text-ink tnum">{int(detail.chats.gave_up)}</dd>
            </div>
            {#if detail.chats.langs?.length}
              <div class="flex justify-between gap-3 py-[3px] text-body-sm">
                <dt class="text-ink-3">Languages</dt>
                <dd class="text-ink tnum">
                  {detail.chats.langs.map((l) => `${l.lang} ${int(l.count)}`).join(' · ')}
                </dd>
              </div>
            {/if}
            <!-- Cost, always with its denominator.
                 `cost_usd` sums only the turns the provider actually priced —
                 14 of this branch's 19. A bare total presents itself as the
                 cost of the whole column, which is a different claim; §3's rule
                 for rates applies just as hard to a partial sum.

                 And `cost_known === 0` with real conversations is NOT $0.00.
                 Nothing on that branch was ever priced, so the honest answer is
                 "not configured" — a manager reading a zero concludes the
                 branch is free to run, and nobody notices for months. -->
            {#if detail.chats.cost_known === 0}
              <div class="flex justify-between gap-3 py-[3px] text-body-sm">
                <dt class="text-ink-3">Cost so far</dt>
                <dd class="text-ink">not configured</dd>
              </div>
              <p class="pt-0.5 text-label leading-relaxed text-ink-3">
                No price came back for any of these conversations, so there is no total to show —
                this is not a cost of zero.
              </p>
            {:else}
              <div class="flex justify-between gap-3 py-[3px] text-body-sm">
                <dt class="text-ink-3">Cost so far</dt>
                <dd class="text-ink tnum">
                  {agentCost(detail.chats.cost_usd) ?? UNKNOWN}
                  <span class="text-ink-3">
                    of {int(detail.chats.cost_known)} priced
                  </span>
                </dd>
              </div>
              {#if isNum(detail.chats.cost_known) && detail.chats.cost_known < detail.chats.count}
                <p class="pt-0.5 text-label leading-relaxed text-ink-3 tnum">
                  The other {int(detail.chats.count - detail.chats.cost_known)} came back with no price
                  from the provider and are not in that figure.
                </p>
              {/if}
            {/if}
          </dl>
        {:else}
          <p class="text-body-sm leading-relaxed text-ink-2">
            No conversations recorded for this branch. Either its embed is not installed yet, or
            its customers used the main site — chats there carry no branch.
          </p>
        {/if}
      </section>

      <!-- What customers asked. Questions only, never the answers. -->
      {#if detail.questions?.length}
        <section class="border-b border-line-2 px-4 py-3.5">
          <h3 class="mb-2 text-micro font-semibold uppercase tracking-[0.07em] text-ink-3">
            What customers asked
          </h3>
          <ul class="flex flex-col gap-2">
            {#each detail.questions as q, i (`${q.at ?? i}-${i}`)}
              <li class="border-l-2 border-line pl-2.5">
                <p class="text-body-sm text-ink">“{q.text}”</p>
                <p class="text-label text-ink-3 tnum">
                  {when(q.at)} · {q.lang ?? UNKNOWN} · {q.cached
                    ? 'instant, from cache'
                    : ms(q.ms)}
                </p>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      <!-- Website code -->
      <section class="border-b border-line-2 px-4 py-3.5">
        <h3 class="mb-2 text-micro font-semibold uppercase tracking-[0.07em] text-ink-3">
          Website code
        </h3>
        {#if hidden}
          <p class="text-body-sm leading-relaxed text-ink-2">
            A hidden branch cannot be given new code — a snippet keeps working on a customer's
            site with nothing to re-check it later.
          </p>
        {:else if snippetBusy}
          <p class="text-body-sm text-ink-2">Minting this branch's code…</p>
        {:else if snippet}
          <!-- WRAPPED, not scrolled.
               Measured in the panel: the snippet's longest line is 779px inside
               a 344px box, so with `white-space: pre` it scrolls correctly and
               yet 56% of every line sits off-screen at rest — and macOS draws
               overlay scrollbars, which appear only once you are already
               scrolling. The content was reachable and unreadable at the same
               time, which is the worst of both: this is the one block a person
               has to CHECK before pasting it onto a customer's website, and a
               horizontal scroll hides the left of the line as soon as you go
               looking at the right of it.

               `pre-wrap` keeps the newlines and indentation the backend sent,
               so every `data-*` attribute still starts on its own line and the
               shape stays eyeball-able; only an over-long value continues onto
               a wrapped line. `overflow-wrap: anywhere` is what actually breaks
               the signature — it is one unbroken token with no break
               opportunity in it, so `pre-wrap` alone would still overflow.
               `break-all` would do it too but would also chop the URL and the
               attribute names mid-word; `anywhere` breaks only what cannot fit.

               `overflow-x-auto` stays as a backstop for any future content that
               genuinely cannot be broken. It should now never engage. -->
          <pre
            class="overflow-x-auto whitespace-pre-wrap [overflow-wrap:anywhere] rounded-card border border-line bg-surface-2 p-2.5 font-mono text-label leading-relaxed text-ink-2">{snippet}</pre>
          <div class="mt-2 flex gap-2">
            <button
              type="button"
              onclick={copySnippet}
              class="inline-flex cursor-pointer items-center gap-1.5 rounded-card bg-accent px-3 py-1.5
                     text-body-sm font-semibold text-on-accent"
            >
              {#if copied}
                <Check size={14} aria-hidden="true" />Copied
              {:else}
                <Copy size={14} aria-hidden="true" />Copy
              {/if}
            </button>
            <button
              type="button"
              onclick={preview}
              disabled={previewBusy}
              class="inline-flex cursor-pointer items-center gap-1.5 rounded-card border border-line
                     bg-surface px-3 py-1.5 text-body-sm font-medium text-ink hover:bg-surface-2
                     disabled:cursor-default disabled:opacity-60"
            >
              <ExternalLink size={14} aria-hidden="true" />
              {previewBusy ? 'Opening…' : 'Preview'}
            </button>
          </div>
        {:else if snippetErr}
          <!-- Each of these says something different and only one of them is a
               fault. `ErrorState` is the fallback, not the default: its 5xx
               copy ("the server log has the traceback") is wrong for a 503 that
               means the registry cannot currently confirm which branches are
               visible, and its generic 400 copy would bury the one message
               here that tells the operator what to actually do. -->
          {#if Number(snippetErr.status) === 400}
            <p class="text-body-sm leading-relaxed text-ink-2">
              {snippetErr.message ||
                'No embed credential is registered on this deployment, so no code can be issued yet.'}
            </p>
            <p class="mt-1.5 text-meta text-ink-3">
              No code is shown rather than one with a placeholder in it — a placeholder passes here
              and then fails on the customer's own site.
            </p>
          {:else if Number(snippetErr.status) === 503}
            <p class="text-body-sm leading-relaxed text-ink-2">
              We cannot confirm which branches are visible to customers right now, so new code is
              not being issued. Try again shortly.
            </p>
          {:else}
            <ErrorState error={snippetErr} what="this branch's website code" />
          {/if}
        {/if}
      </section>

      <!-- Who changed what -->
      {#if detail.events?.length}
        <section class="px-4 py-3.5">
          <h3 class="mb-2 text-micro font-semibold uppercase tracking-[0.07em] text-ink-3">
            Who changed what
          </h3>
          <ul class="flex flex-col gap-1.5">
            {#each detail.events as e, i (`${e.at ?? i}-${i}`)}
              {@const tone = eventTone(e.status)}
              {@const look = TONE_STYLE[tone]}
              {@const ToneIcon = look.icon}
              {@const rawRecord = isSystemRecord(e)}
              <li class="flex gap-2">
                <!-- Three signals on a refused attempt, not one: the numeric
                     CODE (403 and 200 do not look alike whatever colour they
                     are), a glyph, and a word for the screen reader. The tint
                     is the redundant fourth. -->
                <span
                  class="inline-flex h-fit flex-none items-center gap-1 rounded-xs px-1.5 py-0.5 text-micro font-semibold tnum {look.cls}"
                >
                  <ToneIcon size={11} aria-hidden="true" />
                  {isNum(Number(e.status)) && Number(e.status) > 0 ? e.status : '···'}
                  <span class="sr-only">{TONE_WORD[tone]}</span>
                </span>
                <!-- A record with no written description is set in mono and
                     dimmed, so it reads at a glance as the system's own note
                     rather than as a sentence we wrote badly. It is still shown
                     VERBATIM — the contract says the console renders `summary`
                     as given, and quietly hiding an action nobody has named
                     would be worse than showing its slug. The screen-reader
                     line says which it is, because "admin dot stores dot status
                     dot create" read aloud among plain sentences is otherwise
                     just noise. -->
                <span class="leading-relaxed {rawRecord
                  ? 'font-mono text-label text-ink-3'
                  : 'text-meta text-ink-2'}">
                  {#if rawRecord}
                    <span class="sr-only">
                      System record — no description has been written for this action:
                    </span>
                  {/if}
                  {e.summary}
                  <span class="block font-sans text-label text-ink-3">
                    {e.actor ?? 'an unidentified caller'} · {when(e.at)}
                  </span>
                </span>
              </li>
            {/each}
          </ul>
          <p class="mt-2 text-label leading-relaxed text-ink-3">
            Refused attempts are kept too — a 403 is the record of someone who tried.
          </p>
        </section>
      {/if}
    {/if}
  {/if}
</aside>
