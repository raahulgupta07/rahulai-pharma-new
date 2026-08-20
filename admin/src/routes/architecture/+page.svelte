<script>
  import { onMount } from 'svelte';
  import { getJSON } from '$lib/api.js';
  import PageHeader from '$lib/PageHeader.svelte';
  import { RefreshCw } from '@lucide/svelte';
  import { pct as fmtPct, int as fmtInt, UNKNOWN } from '$lib/charts/format.js';

  let health = $state(null);
  let healthError = $state(null);
  let path = $state(null);
  let pathError = $state(null);
  let obs = $state(null);
  let obsError = $state(null);
  let checkedAt = $state(null);

  onMount(load);

  async function load() {
    healthError = null;
    pathError = null;
    obsError = null;
    const [h, p, o] = await Promise.allSettled([
      getJSON('/admin/architecture/health'),
      getJSON('/admin/architecture/question-path'),
      getJSON('/admin/architecture/observability')
    ]);
    if (o.status === 'fulfilled') obs = o.value;
    else {
      obs = null;
      obsError = o.reason;
    }
    if (h.status === 'fulfilled') {
      health = h.value;
      checkedAt = new Date();
    } else {
      health = null;
      healthError = h.reason;
    }
    if (p.status === 'fulfilled') path = p.value;
    else {
      path = null;
      pathError = p.reason;
    }
  }

  /** Seconds, or milliseconds when that is the honest unit.
   *  A 1 ms cache hit printed as "0.0 s" is a measurement rounded into a lie. */
  const dur = (ms) =>
    typeof ms !== 'number' ? UNKNOWN : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;

  /** How each claim is drawn.
   *
   *  `partial` exists for one specific case: the code records the signal and
   *  the table is empty. That is what a capture layer wired into one of the
   *  two places it needed looks like — it shipped here once, nothing errored,
   *  and 804 tests passed. A tick would have hidden it. */
  const OBS = {
    in_place: { chip: 'bg-success-soft text-success', word: 'In place' },
    partial: { chip: 'bg-warning-soft text-warning', word: 'Nothing recorded yet' },
    unknown: { chip: 'bg-surface-2 text-ink-3', word: 'Could not check' },
    none: { chip: 'bg-danger-soft text-danger', word: 'Not watched' }
  };
  const obsTone = (s) => OBS[s] ?? OBS.unknown;

  let signals = $derived(Array.isArray(obs?.signals) ? obs.signals : []);

  let routes = $derived(Array.isArray(path?.routes) ? path.routes : []);
  let slowest = $derived(
    Math.max(...routes.map((r) => (typeof r.p50_ms === 'number' ? r.p50_ms : 0)), 1)
  );

  /** How the state is drawn.
   *
   *  `unknown` is grey and says "not known" in words. It must never take the
   *  colour of `ok`: a part nobody checked, painted green, is the failure this
   *  whole page is supposed to make impossible — and it is invisible, because
   *  a green board is exactly what a working system looks like. */
  const TONE = {
    ok: { dot: 'bg-success', chip: 'bg-success-soft text-success', word: 'Working' },
    watch: { dot: 'bg-warning', chip: 'bg-warning-soft text-warning', word: 'Worth a look' },
    down: { dot: 'bg-danger', chip: 'bg-danger-soft text-danger', word: 'Not answering' },
    unknown: { dot: 'bg-ink-3', chip: 'bg-surface-2 text-ink-3', word: 'Not known' }
  };
  const tone = (s) => TONE[s] ?? TONE.unknown;

  /** The claim each row is entitled to make. */
  const HOW = {
    probed: { label: 'asked just now', title: 'We called this dependency while building this page and timed the reply.' },
    observed: { label: 'from its record', title: 'We did not call it. We read something it wrote down.' },
    not_checked: { label: 'not checked', title: 'Nothing here was established. The state is unknown, not healthy.' }
  };
  const how = (h) => HOW[h] ?? HOW.not_checked;

  const KIND = {
    client: 'Runs on your sites',
    service: 'Runs here',
    worker: 'Its own container',
    external: 'Someone else runs it',
    store: 'Holds the data',
    edge: 'Where files arrive'
  };

  let parts = $derived(Array.isArray(health?.parts) ? health.parts : []);
  let counts = $derived(health?.counts ?? {});
  let notOk = $derived(parts.filter((p) => p.state !== 'ok'));

  const clock = (d) =>
    d ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : null;
</script>

<PageHeader
  title="Architecture & health"
  subtitle="What this system is made of, what each part is doing right now, and which questions about it we cannot answer yet."
/>

<section aria-labelledby="parts-heading">
  <div class="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1.5">
    <h2 id="parts-heading" class="text-title font-semibold tracking-[-0.014em] text-ink">
      The parts
    </h2>
    {#if parts.length}
      <span class="text-meta text-ink-3">
        {#if notOk.length}
          {notOk.length} of {parts.length} {notOk.length === 1 ? 'is' : 'are'} not plainly working
        {:else}
          all {parts.length} answering
        {/if}
        {#if counts.unknown}
          · {counts.unknown} not established
        {/if}
      </span>
    {/if}
    {#if checkedAt}
      <span class="font-mono text-label text-ink-3">checked {clock(checkedAt)}</span>
    {/if}
    <button
      onclick={load}
      class="ml-auto inline-flex items-center gap-1.5 rounded-control border border-line bg-surface px-3 py-1.5 text-meta font-semibold text-ink-2 hover:border-accent hover:text-accent"
    >
      <RefreshCw size={13} /> Check again
    </button>
  </div>

  {#if healthError}
    <div class="rounded-panel border border-line bg-surface p-[15px]">
      <p class="text-body font-semibold text-ink">The health check did not run.</p>
      <p class="mt-1.5 text-body-sm leading-[1.5] text-ink-2">
        The console could not reach its own health endpoint{healthError.status
          ? ` (HTTP ${healthError.status})`
          : ''}. That says nothing about the parts below — none of them was asked.
      </p>
    </div>
  {:else if !health}
    <div class="space-y-2">
      {#each Array(8) as _}<div class="skel" style="height:62px"></div>{/each}
    </div>
  {:else}
    <div class="overflow-hidden rounded-panel border border-line bg-surface">
      {#each parts as p, i (p.id)}
        <div
          class="flex flex-wrap items-start gap-x-3 gap-y-2 px-[18px] py-3.5 {i
            ? 'border-t border-line-2'
            : ''}"
        >
          <span class="mt-1.5 size-2 shrink-0 rounded-full {tone(p.state).dot}"></span>
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
              <span class="text-body font-semibold text-ink">{p.name}</span>
              <span class="text-meta text-ink-3">{KIND[p.kind] ?? p.kind}</span>
            </div>
            <p class="mt-1 max-w-[720px] text-body-sm leading-[1.5] text-ink-2">{p.detail}</p>
          </div>
          <div class="flex shrink-0 flex-col items-end gap-1">
            <span
              class="inline-flex items-center rounded-control px-2 py-[3px] text-micro font-semibold uppercase tracking-[0.06em] {tone(
                p.state
              ).chip}"
            >
              {tone(p.state).word}
            </span>
            <!-- The evidence, next to the verdict it justifies. A row that says
                 "Working · not checked" would be a contradiction the reader can
                 see, which is the point. -->
            <span class="font-mono text-label text-ink-3" title={how(p.how).title}>
              {#if p.metric}{p.metric}&nbsp;·&nbsp;{/if}{how(p.how).label}
            </span>
          </div>
        </div>
      {/each}
    </div>

    <p class="mt-2.5 max-w-[720px] text-meta leading-[1.5] text-ink-3">
      Two of these are read rather than called. The model provider is deliberately never probed — a
      health check there would spend money and add about five seconds to every load of this page —
      so it is reported from what real questions already paid for. The ingest worker runs in another
      container that nothing here can see; its row is the trail it leaves in the pipeline log, which
      is why it says "last seen" and not "healthy".
    </p>
  {/if}
</section>

<!-- THE PATH OF ONE QUESTION -->
<section class="mt-7" aria-labelledby="path-heading">
  <div class="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
    <h2 id="path-heading" class="text-title font-semibold tracking-[-0.014em] text-ink">
      The path of one question
    </h2>
    <span class="text-meta text-ink-3">
      three routes, measured over the last {path?.window_days ?? 30} days
    </span>
  </div>

  {#if pathError}
    <div class="rounded-panel border border-line bg-surface p-[15px]">
      <p class="text-body font-semibold text-ink">The routes could not be read.</p>
      <p class="mt-1.5 text-body-sm leading-[1.5] text-ink-2">
        The turn log did not answer{pathError.status ? ` (HTTP ${pathError.status})` : ''}, so which
        route real questions took is unknown — not that they all took the slow one.
      </p>
    </div>
  {:else if routes.length}
    <div class="flex flex-col gap-3">
      {#each routes as r (r.id)}
        <article class="elev rounded-panel border border-line bg-surface p-[15px]">
          <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <h3 class="text-body font-semibold text-ink">{r.name}</h3>
            {#if r.turns === null}
              <!-- Never 0: no question took this route in the window, which is
                   not the same as a route measured at zero. -->
              <span class="text-meta text-ink-3">no question took this route</span>
            {:else}
              <span class="font-mono text-label text-ink-2">
                {fmtInt(r.turns)} question{r.turns === 1 ? '' : 's'}
                {#if r.share !== null}· {fmtPct(r.share)}{/if}
              </span>
            {/if}
            <span class="ml-auto font-mono text-body font-semibold text-ink">{dur(r.p50_ms)}</span>
            <span class="font-mono text-label text-ink-3">median</span>
          </div>

          {#if typeof r.p50_ms === 'number'}
            <!-- Scaled to the slowest route, so the bar shows the gap between
                 them. That gap IS the finding: a share-of-anything bar would
                 flatten a 1 ms hit and a 15 s run into neighbours. -->
            <span class="mt-2.5 block h-1.5 overflow-hidden rounded-full bg-surface-2">
              <span
                class="block h-full rounded-full {r.id === 'agent'
                  ? 'bg-warning'
                  : r.id === 'fast_path'
                    ? 'bg-info'
                    : 'bg-success'}"
                style="width:{Math.max(1, Math.round((r.p50_ms / slowest) * 100))}%"
              ></span>
            </span>
          {/if}

          <dl class="mt-2.5 grid gap-x-5 gap-y-1.5 text-body-sm leading-[1.5] sm:grid-cols-[86px_minmax(0,1fr)]">
            <dt class="text-meta font-semibold uppercase tracking-[0.06em] text-ink-3">Taken when</dt>
            <dd class="text-ink-2">{r.when}</dd>
            <dt class="text-meta font-semibold uppercase tracking-[0.06em] text-ink-3">What runs</dt>
            <dd class="text-ink-2">{r.does}</dd>
            <dt class="text-meta font-semibold uppercase tracking-[0.06em] text-ink-3">Never</dt>
            <dd class="text-ink-2">{r.skips}</dd>
          </dl>
        </article>
      {/each}
    </div>

    {#if path?.unknown_paths?.length}
      <!-- A route the console has no description for is named rather than
           folded into one it does know, or a new branch in the code would
           silently inflate an existing row. -->
      <p class="mt-2.5 text-meta text-ink-3">
        {path.unknown_paths.length} route{path.unknown_paths.length === 1 ? '' : 's'} the console has
        no description for: {path.unknown_paths.map((u) => `${u.id} (${u.turns})`).join(', ')}.
      </p>
    {/if}

    {#if typeof path?.model_call_p50_ms === 'number'}
      <p class="mt-2.5 max-w-[720px] text-meta leading-[1.5] text-ink-3">
        One provider call takes {dur(path.model_call_p50_ms)} on this stack, measured. That is the
        floor: the fast path is faster because it makes one call instead of several, not because the
        call is quicker, and the cache is fast because it makes none. Anything that makes this feel
        quicker has to delete a call.
      </p>
    {/if}
  {:else if path}
    <div class="rounded-panel border border-line bg-surface p-[15px]">
      <p class="text-body-sm leading-[1.5] text-ink-2">
        No question has been asked in the last {path.window_days} days, so no route has been taken.
      </p>
    </div>
  {/if}
</section>

<!-- WHAT WE CAN SEE, AND WHAT WE CANNOT -->
<section class="mt-7" aria-labelledby="obs-heading">
  <div class="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
    <h2 id="obs-heading" class="text-title font-semibold tracking-[-0.014em] text-ink">
      What we can see, and what we cannot
    </h2>
    {#if signals.length}
      <span class="text-meta text-ink-3">
        {obs.counts.none} of {signals.length} not watched at all
        {#if obs.counts.partial}· {obs.counts.partial} recording nothing{/if}
      </span>
    {/if}
  </div>

  {#if obsError}
    <div class="rounded-panel border border-line bg-surface p-[15px]">
      <p class="text-body-sm leading-[1.5] text-ink-2">
        The signal list could not be read{obsError.status ? ` (HTTP ${obsError.status})` : ''}, so
        what is and is not being watched is unknown from here.
      </p>
    </div>
  {:else if signals.length}
    <div class="overflow-hidden rounded-panel border border-line bg-surface">
      {#each signals as g, i (g.id)}
        <div
          class="flex flex-wrap items-start gap-x-3 gap-y-2 px-[18px] py-3.5 {i
            ? 'border-t border-line-2'
            : ''}"
        >
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
              <span class="text-body font-semibold text-ink">{g.signal}</span>
              {#if g.where}<span class="text-meta text-ink-3">{g.where}</span>{/if}
            </div>
            {#if g.note}
              <p class="mt-1 max-w-[720px] text-body-sm leading-[1.5] text-ink-2">{g.note}</p>
            {/if}
          </div>
          <div class="flex shrink-0 flex-col items-end gap-1">
            <span
              class="inline-flex items-center rounded-control px-2 py-[3px] text-micro font-semibold uppercase tracking-[0.06em] {obsTone(
                g.state
              ).chip}"
            >
              {obsTone(g.state).word}
            </span>
            <!-- The count IS the claim. "In place" with nothing behind it is
                 the failure this column exists to make visible. -->
            {#if typeof g.rows === 'number'}
              <span class="font-mono text-label text-ink-3">{fmtInt(g.rows)} recorded</span>
            {/if}
          </div>
        </div>
      {/each}
    </div>
    <p class="mt-2.5 max-w-[720px] text-meta leading-[1.5] text-ink-3">
      Every signal that claims to be in place names a table and is answered by counting it. A
      capture layer that is written but wired to nothing reads as working in the code and records
      nothing in the database — that has happened here once, and no test caught it. A row with a
      zero beside it is not called in place, whatever the code does.
    </p>
  {/if}
</section>
