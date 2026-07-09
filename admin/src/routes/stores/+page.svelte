<script>
  import { onMount } from 'svelte';
  import { Search, RefreshCw } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import StatCard from '$lib/StatCard.svelte';

  const base = 'http://localhost:8088';

  let loading = $state(true);
  let error = $state(null);
  let stores = $state([]);
  let query = $state('');

  async function load() {
    loading = true;
    error = null;
    try {
      const res = await fetch(base + '/admin/stores');
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      const data = await res.json();
      stores = Array.isArray(data) ? data : [];
    } catch (e) {
      error = e.message || 'backend offline';
    } finally {
      loading = false;
    }
  }

  onMount(load);

  // Summary metrics.
  let totalSites = $derived(stores.length);
  let totalUnits = $derived(stores.reduce((s, r) => s + (r.units ?? 0), 0));
  let totalValue = $derived(stores.reduce((s, r) => s + (r.value ?? 0), 0));
  let maxValue = $derived(stores.reduce((m, r) => Math.max(m, r.value ?? 0), 0));

  // Filtered; backend already sorts by value desc.
  let filtered = $derived(
    query.trim()
      ? stores.filter((r) =>
          (r.site_code ?? '').toLowerCase().includes(query.trim().toLowerCase())
        )
      : stores
  );

  const fmt = (v) =>
    v === null || v === undefined ? '–' : Math.round(v).toLocaleString();
  const mmk = (v) =>
    v === null || v === undefined ? '–' : `${Math.round(v).toLocaleString()} MMK`;
  const barPct = (v) =>
    maxValue > 0 ? Math.max(2, ((v ?? 0) / maxValue) * 100) : 0;

  // Compact display formatters for the metric cards / value labels.
  const compactNum = (v) => {
    if (v === null || v === undefined) return '–';
    if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
    if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
    if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
    return Math.round(v).toLocaleString();
  };
  const compactValue = (v) => {
    if (v === null || v === undefined) return '–';
    if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B MMK`;
    if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M MMK`;
    return `${Math.round(v).toLocaleString()} MMK`;
  };
  const valueLabel = (v) => `${Math.round((v ?? 0) / 1e6)}M`;
</script>

<PageHeader title="Stores">
  {#snippet meta()}
    <span
      class="inline-flex items-center rounded-lg bg-surface-2 px-2 py-0.5 text-[12px] text-ink-2"
    >
      {totalSites} {totalSites === 1 ? 'site' : 'sites'}
    </span>
  {/snippet}
  {#snippet actions()}
    <button
      onclick={load}
      disabled={loading}
      class="inline-flex cursor-pointer items-center gap-2 rounded-lg border-[0.5px] border-line px-3.5 py-2 text-[13px] font-medium text-ink transition-colors hover:bg-surface-2 disabled:cursor-default disabled:opacity-60"
    >
      <RefreshCw size={15} class={loading ? 'animate-spin' : ''} />
      Refresh
    </button>
  {/snippet}
</PageHeader>

{#if loading}
  <div class="rounded-xl border-[0.5px] border-line bg-surface-2 px-5 py-6 text-[14px] text-ink-2">
    Loading stores…
  </div>
{:else if error}
  <div class="rounded-xl border-[0.5px] border-line bg-surface-2 px-5 py-6 text-[14px] text-ink-2">
    <p class="font-medium text-ink">Backend offline</p>
    <p class="mt-1">
      Could not reach the agent at <span class="text-ink">localhost:8088</span>.
      Start the backend and reload.
    </p>
    <button
      onclick={load}
      class="mt-4 cursor-pointer rounded-lg border-[0.5px] border-line px-3 py-1.5 text-[13px] font-medium text-ink transition-colors hover:bg-surface"
    >
      Retry
    </button>
  </div>
{:else if stores.length === 0}
  <div class="rounded-xl border-[0.5px] border-line bg-surface-2 px-5 py-6 text-[14px] text-ink-2">
    <p class="font-medium text-ink">No stores</p>
    <p class="mt-1">No inventory was returned for any site.</p>
  </div>
{:else}
  <!-- Summary metric cards -->
  <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
    <StatCard label="Sites" value={fmt(totalSites)} />
    <StatCard label="Total units" value={compactNum(totalUnits)} />
    <StatCard label="Stock value" value={compactValue(totalValue)} />
  </div>

  <!-- Search / filter -->
  <div class="relative mt-4 max-w-sm">
    <Search
      size={15}
      class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-3"
    />
    <input
      type="text"
      bind:value={query}
      aria-label="Filter by site code"
      placeholder="Filter site code…"
      class="w-full rounded-[10px] border-[0.5px] border-line bg-surface py-2 pl-9 pr-3 text-[14px] text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none focus:ring-[3px] focus:ring-accent-soft"
    />
  </div>

  <!-- Table -->
  <section class="mt-4 overflow-hidden rounded-xl border-[0.5px] border-line bg-surface">
    <div class="max-h-[440px] overflow-y-auto">
    <table class="tbl">
      <thead>
        <tr>
          <th>Site</th>
          <th class="num">SKUs</th>
          <th class="num">Units</th>
          <th style="width:180px">Stock value</th>
        </tr>
      </thead>
      <tbody>
        {#each filtered as row (row.site_code)}
          <tr>
            <td class="font-medium text-ink">{row.site_code}</td>
            <td class="num text-ink-2">{fmt(row.skus)}</td>
            <td class="num text-ink-2">{fmt(row.units)}</td>
            <td>
              <div class="flex items-center gap-2.5">
                <div
                  class="h-1.5 flex-1 overflow-hidden rounded-[4px]"
                  style="background-color: var(--color-accent-soft);"
                >
                  <span
                    class="block h-full rounded-[4px]"
                    style="width: {barPct(row.value)}%; background-color: var(--color-accent); opacity: .55;"
                  ></span>
                </div>
                <span class="tnum whitespace-nowrap text-[12px] text-ink-2">
                  {valueLabel(row.value)}
                </span>
              </div>
            </td>
          </tr>
        {/each}
        {#if filtered.length === 0}
          <tr>
            <td colspan="4" class="text-center text-[14px] text-ink-2" style="padding:24px 16px;">
              No sites match “{query}”.
            </td>
          </tr>
        {/if}
      </tbody>
    </table>
    </div>
  </section>
{/if}
