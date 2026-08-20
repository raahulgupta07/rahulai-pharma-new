<script>
  // ONE filter bar drives every chart on every tab.
  //
  // The control names are the API's parameter names (contract §4) — start, end,
  // store, lang, path, embed, model, actor, cached, rated — and the state lives
  // in the URL, so a filtered view is a link you can paste to someone else.
  // Anything narrower (per-tab filters, or state held only in memory) produces
  // charts on the same screen answering different questions.
  //
  // `store` is offered here as a NARROWING control only. The caller's own store
  // scope is enforced server-side and cannot be widened from this bar.
  import { X } from '@lucide/svelte';

  let {
    f = {},
    options = {},
    range = '30',
    onrange,
    onset,
    onclear,
    dayFilter = null,
    onclearday = null
  } = $props();

  const RANGES = [
    { id: '1', label: '24h' },
    { id: '7', label: '7d' },
    { id: '30', label: '30d' },
    { id: '90', label: '90d' },
    { id: 'all', label: 'All' }
  ];

  // Each select is (key, label, options). `embed` carries the reserved literal
  // `none`, which the API reads as "turns with no embed_id" — a real selection,
  // not an absence of one.
  let selects = $derived([
    { key: 'store', label: 'Store', any: 'All stores', opts: options.stores ?? [] },
    { key: 'lang', label: 'Language', any: 'Any language', opts: options.langs ?? [] },
    { key: 'path', label: 'Path', any: 'Any path', opts: options.paths ?? [] },
    // The reserved `none` value (unattributed turns) is offered by the caller
    // only if the endpoint actually implements it — it is NOT hardcoded here.
    { key: 'embed', label: 'Embed', any: 'Any embed', opts: options.embeds ?? [] },
    { key: 'model', label: 'Model', any: 'Any model', opts: options.models ?? [] },
    { key: 'actor', label: 'Asked by', any: 'Anyone', opts: options.actors ?? [] }
  ]);

  const norm = (o) => (typeof o === 'string' ? { value: o, label: o } : o);

  let active = $derived(
    [
      ...selects
        .filter((s) => f[s.key])
        .map((s) => ({ key: s.key, text: `${s.label}: ${f[s.key]}` })),
      ...(f.cached ? [{ key: 'cached', text: f.cached === 'true' ? 'Cache hits only' : 'Cache misses only' }] : []),
      ...(f.rated ? [{ key: 'rated', text: `Rated: ${f.rated}` }] : []),
      ...(f.tool ? [{ key: 'tool', text: `Tool: ${f.tool}` }] : []),
      ...(f.q ? [{ key: 'q', text: `Search: ${f.q}` }] : [])
    ]
  );
</script>

<div class="elev rounded-card border border-line bg-surface p-3">
  <div class="flex flex-wrap items-center gap-2">
    <div class="flex overflow-hidden rounded-panel border border-line">
      {#each RANGES as r (r.id)}
        <button
          onclick={() => onrange(r.id)}
          class="min-h-[36px] cursor-pointer px-3 text-meta font-semibold
                 {range === r.id ? 'bg-accent text-on-accent' : 'bg-surface text-ink-2 hover:bg-surface-2'}"
          aria-pressed={range === r.id}
        >{r.label}</button>
      {/each}
    </div>

    {#each selects as s (s.key)}
      <label class="sr-only" for="flt-{s.key}">{s.label}</label>
      <select
        id="flt-{s.key}"
        value={f[s.key] ?? ''}
        onchange={(e) => onset(s.key, e.currentTarget.value)}
        class="min-h-[36px] cursor-pointer rounded-panel border border-line bg-surface px-2.5 text-meta text-ink-2"
      >
        <option value="">{s.any}</option>
        {#each s.opts.map(norm) as o (o.value)}
          <option value={o.value}>{o.label}</option>
        {/each}
      </select>
    {/each}

    <select
      aria-label="Cached"
      value={f.cached ?? ''}
      onchange={(e) => onset('cached', e.currentTarget.value)}
      class="min-h-[36px] cursor-pointer rounded-panel border border-line bg-surface px-2.5 text-meta text-ink-2"
    >
      <option value="">Cached: any</option>
      <option value="true">Cache hits</option>
      <option value="false">Cache misses</option>
    </select>

    <select
      aria-label="Rated"
      value={f.rated ?? ''}
      onchange={(e) => onset('rated', e.currentTarget.value)}
      class="min-h-[36px] cursor-pointer rounded-panel border border-line bg-surface px-2.5 text-meta text-ink-2"
    >
      <option value="">Rated: any</option>
      <option value="up">Thumbs up</option>
      <option value="down">Thumbs down</option>
      <option value="any">Rated at all</option>
    </select>

    <input
      type="search"
      placeholder="Search questions…"
      value={f.q ?? ''}
      oninput={(e) => onset('q', e.currentTarget.value)}
      class="min-h-[36px] min-w-[180px] flex-1 rounded-panel border border-line bg-surface px-3 text-meta text-ink"
      aria-label="Search questions"
    />
  </div>

  {#if active.length || dayFilter}
    <div class="mt-2.5 flex flex-wrap items-center gap-2 border-t border-line pt-2.5">
      {#if dayFilter}
        <button
          onclick={onclearday}
          class="inline-flex min-h-[30px] cursor-pointer items-center gap-1.5 rounded-full bg-accent px-3 text-meta font-medium text-on-accent"
        >
          Day: {dayFilter}<X size="12" />
        </button>
      {/if}
      {#each active as a (a.key)}
        <button
          onclick={() => onset(a.key, '')}
          class="inline-flex min-h-[30px] cursor-pointer items-center gap-1.5 rounded-full border border-line bg-surface px-3 text-meta text-ink-2 hover:border-accent hover:text-accent"
        >
          {a.text}<X size="12" />
        </button>
      {/each}
      <button
        onclick={onclear}
        class="min-h-[30px] cursor-pointer rounded-full border border-dashed border-line px-3 text-meta text-ink-3 hover:border-accent hover:text-accent"
      >Clear all</button>
    </div>
  {/if}
</div>
