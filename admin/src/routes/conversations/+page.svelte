<script>
  import { API_BASE } from '$lib/apiBase.js';
  import { onMount } from 'svelte';
  import { MessageSquare, RefreshCw } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';

  // Backend base. Fetched inline here per instructions (no shared-file edits).
  const base = API_BASE;

  let loading = $state(true);
  let error = $state(null);
  let rows = $state([]);

  // Filters
  let lang = $state('');
  let store = $state('');
  let limit = $state(25);
  let convOffset = $state(0);

  // Per-row expand state, keyed by conversation id.
  let expanded = $state({});

  async function load() {
    loading = true;
    error = null;
    try {
      const params = new URLSearchParams();
      params.set('limit', String(limit));
      if (lang) params.set('lang', lang);
      if (store.trim()) params.set('store', store.trim());
      params.set('offset', String(convOffset));

      const res = await fetch(`${base}/admin/conversations?${params.toString()}`);
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      const text = await res.text();
      const data = text ? JSON.parse(text) : [];
      rows = Array.isArray(data) ? data : [];
    } catch (e) {
      error = e.message || 'backend offline';
      rows = [];
    } finally {
      loading = false;
    }
  }

  onMount(load);

  // Filter/search change: reset to page 1 before reloading.
  function loadFiltered() {
    convOffset = 0;
    load();
  }

  function convPrev() {
    if (convOffset === 0) return;
    convOffset = Math.max(0, convOffset - limit);
    load();
  }

  function convNext() {
    convOffset += limit;
    load();
  }

  function toggle(id) {
    expanded = { ...expanded, [id]: !expanded[id] };
  }

  // Format a timestamp into a short relative / clock label.
  function fmtTime(ts) {
    if (!ts) return '–';
    const d = new Date(ts);
    if (isNaN(d.getTime())) return String(ts);
    const diff = Date.now() - d.getTime();
    const sec = Math.round(diff / 1000);
    if (sec < 60) return 'just now';
    const min = Math.round(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.round(min / 60);
    if (hr < 24) return `${hr}h ago`;
    const day = Math.round(hr / 24);
    if (day < 7) return `${day}d ago`;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function fmtLatency(ms) {
    if (ms === null || ms === undefined || isNaN(ms)) return '–';
    return `${Math.round(ms)} ms`;
  }
</script>

<PageHeader title="Conversations" subtitle="Chat logs from the assistant widget — questions and answers.">
  {#snippet actions()}
    <button
      onclick={load}
      disabled={loading}
      class="inline-flex items-center gap-2 rounded-lg border border-line bg-surface px-3.5 py-2 text-[13px] font-medium text-ink transition-colors hover:bg-surface-2 disabled:opacity-60"
    >
      <RefreshCw size={15} class={loading ? 'animate-spin' : ''} />
      Refresh
    </button>
  {/snippet}
</PageHeader>

<!-- Filter row -->
<div class="mb-6 flex flex-wrap items-end gap-3">
  <label class="flex flex-col gap-1.5">
    <span class="text-[12px] text-ink-2">Language</span>
    <select
      bind:value={lang}
      onchange={loadFiltered}
      class="rounded-lg border-[0.5px] border-line bg-surface px-3 py-2 text-[13px] text-ink outline-none focus:border-accent"
    >
      <option value="">All</option>
      <option value="EN">EN</option>
      <option value="MY">MY</option>
    </select>
  </label>

  <label class="flex flex-col gap-1.5">
    <span class="text-[12px] text-ink-2">Store</span>
    <input
      type="text"
      placeholder="Filter by store id…"
      bind:value={store}
      onchange={loadFiltered}
      class="w-52 rounded-lg border-[0.5px] border-line bg-surface px-3 py-2 text-[13px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
    />
  </label>

  <label class="flex flex-col gap-1.5">
    <span class="text-[12px] text-ink-2">Limit</span>
    <select
      bind:value={limit}
      onchange={loadFiltered}
      class="rounded-lg border-[0.5px] border-line bg-surface px-3 py-2 text-[13px] text-ink outline-none focus:border-accent"
    >
      <option value={25}>25</option>
      <option value={50}>50</option>
      <option value={100}>100</option>
    </select>
  </label>
</div>

{#if loading}
  <p class="text-[14px] text-ink-2">Loading conversations…</p>
{:else if error}
  <div class="rounded-lg border border-line bg-surface px-5 py-6 text-[14px] text-ink-2">
    <p class="font-medium text-ink">Backend offline</p>
    <p class="mt-1">
      Could not reach the agent at <span class="text-ink">{API_BASE}</span>.
      Start the backend and retry.
    </p>
    <button
      onclick={load}
      class="mt-4 rounded-lg border border-line px-3 py-1.5 text-[13px] font-medium text-ink hover:bg-surface-2"
    >
      Retry
    </button>
  </div>
{:else if rows.length === 0}
  <div
    class="flex flex-col items-center rounded-lg border border-line bg-surface px-6 py-14 text-center"
  >
    <MessageSquare size={26} class="text-ink-3" />
    <p class="mt-3 text-[14px] text-ink-2">
      No conversations yet — chat via the widget to populate this.
    </p>
  </div>
{:else}
  <div class="max-h-[560px] overflow-y-auto pr-1">
    {#each rows as row (row.id)}
      <article class="rounded-xl border-[0.5px] border-line bg-surface p-4 mb-2.5">
        <!-- Top row: lang badge + question + optional cached badge -->
        <div class="flex items-start gap-2">
          <Badge tone={row.lang === 'MY' ? 'my' : 'en'}>{row.lang || '–'}</Badge>
          <p class="min-w-0 flex-1 text-[14px] font-medium text-ink">{row.question}</p>
          {#if row.cached}
            <Badge tone="ok">cached</Badge>
          {/if}
        </div>

        <!-- Answer -->
        <p
          class="mt-2 text-[13px] leading-relaxed text-ink-2"
          class:clamp-3={!expanded[row.id]}
        >
          {row.answer}
        </p>
        {#if row.answer && row.answer.length > 160}
          <button
            onclick={() => toggle(row.id)}
            class="mt-1 text-[12px] font-medium text-accent hover:text-accent-hover"
          >
            {expanded[row.id] ? 'Show less' : 'Show more'}
          </button>
        {/if}

        <!-- Meta line -->
        <div class="mt-2.5 flex flex-wrap items-center gap-3.5 text-[12px] text-ink-3">
          <span>store: <span class="text-ink-2">{row.store_id || 'all'}</span></span>
          <span>{fmtLatency(row.latency_ms)}</span>
          <span>{fmtTime(row.ts)}</span>
        </div>
      </article>
    {/each}
  </div>
  <div class="mt-3 flex items-center justify-end gap-2">
    <button
      onclick={convPrev}
      disabled={convOffset === 0}
      class="cursor-pointer rounded-lg border border-line px-3 py-1.5 text-[13px] text-ink-2 hover:bg-surface-2 disabled:opacity-50"
    >
      Prev
    </button>
    <span class="text-[12px] text-ink-3">page {convOffset / limit + 1}</span>
    <button
      onclick={convNext}
      disabled={rows.length < limit}
      class="cursor-pointer rounded-lg border border-line px-3 py-1.5 text-[13px] text-ink-2 hover:bg-surface-2 disabled:opacity-50"
    >
      Next
    </button>
  </div>
{/if}

<style>
  .clamp-3 {
    display: -webkit-box;
    -webkit-line-clamp: 3;
    line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
</style>
