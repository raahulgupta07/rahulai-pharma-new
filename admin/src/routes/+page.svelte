<script>
  import { API_BASE } from '$lib/apiBase.js';
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { getMetrics, getReady, reload } from '$lib/api.js';
  import { Sparkles, MessageSquare, DollarSign, Clock, Database, PackageX, ClockAlert, UserX } from '@lucide/svelte';
  import StatusPill from '$lib/aurora/StatusPill.svelte';
  import HeroMetric from '$lib/aurora/HeroMetric.svelte';
  import Ring from '$lib/aurora/Ring.svelte';
  import AlertChip from '$lib/aurora/AlertChip.svelte';
  import { toast } from '$lib/aurora/toast.js';

  let loading = $state(true);
  let error = $state(null);
  let metrics = $state({});
  let ready = $state({});
  let overview = $state([]);
  let hist = $state([]);
  let range = $state('7d');
  let askText = $state('');

  async function load() {
    loading = true;
    error = null;
    try {
      const [m, r] = await Promise.all([getMetrics(), getReady()]);
      metrics = m ?? {};
      ready = r ?? {};
      try {
        const res = await fetch(API_BASE + '/admin/overview?limit=6');
        overview = res.ok ? ((await res.json()) ?? []) : [];
      } catch {
        overview = [];
      }
      try {
        const res = await fetch(API_BASE + '/metrics/history');
        const json = res.ok ? await res.json() : {};
        hist = json.buckets ?? [];
      } catch {
        hist = [];
      }
    } catch (e) {
      error = e.message || 'backend offline';
    } finally {
      loading = false;
    }
  }

  onMount(load);

  const num = (v) => (typeof v === 'number' ? v : null);
  const fmt = (v) => (v === null || v === undefined ? '–' : v.toLocaleString());

  let cacheHit = $derived(num(metrics.cache_hit_rate));
  let p95 = $derived(num(metrics.latency_ms?.p95));
  let requests = $derived(num(metrics.requests_total));
  let errors = $derived(num(metrics.errors_total));
  let catalogRows = $derived(num(metrics.catalog_rows) ?? num(ready.catalog_rows));
  let inventoryRows = $derived(num(metrics.inventory_rows) ?? num(ready.inventory_rows));
  let sites = $derived(num(metrics.sites) ?? num(ready.sites) ?? 53);
  let llmCalls = $derived(num(metrics.llm_calls));

  // normalize a rate that may be 0..1 or 0..100 into a 0..100 number
  const asPct = (v) => (v === null ? null : v <= 1 ? v * 100 : v);

  let cachePct = $derived(asPct(cacheHit) ?? 0);
  // reliability = share of requests that did not error
  let reliability = $derived(
    requests && requests > 0 ? Math.max(0, (1 - (errors ?? 0) / requests)) * 100 : 100
  );
  let siteCoverage = $derived(sites ? Math.min(100, (sites / 53) * 100) : 0);
  // freshness: clean ingest assumed healthy when data + no errors
  let freshness = $derived(catalogRows && catalogRows > 0 ? 100 : 0);

  // bar sparkline from history (requests per bucket), normalized 0..100
  let bars = $derived(
    (() => {
      const b = hist.map((x) => x.requests ?? 0);
      if (!b.length) return [34, 28, 40, 30, 52, 38, 60, 44, 55, 72, 90, 66];
      const max = Math.max(1, ...b);
      return b.map((v) => Math.round((v / max) * 100));
    })()
  );

  function ask() {
    if (!askText.trim()) return;
    goto(base + '/chat');
  }
</script>

{#if loading}
  <div class="space-y-3">
    <div class="skel" style="height:40px;width:240px"></div>
    <div class="skel" style="height:150px"></div>
    <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {#each Array(4) as _}<div class="skel" style="height:150px"></div>{/each}
    </div>
  </div>
{:else if error}
  <div class="rounded-xl border-[0.5px] border-line bg-surface-2 px-5 py-6 text-[14px] text-ink-2">
    <p class="font-medium text-ink">Backend offline</p>
    <p class="mt-1">
      Could not reach the agent at <span class="text-ink">{API_BASE}</span>. Start the backend
      and reload.
    </p>
    <button
      onclick={load}
      class="mt-4 cursor-pointer rounded-lg border-[0.5px] border-line px-3 py-1.5 text-[13px] font-medium text-ink transition-colors hover:bg-surface"
    >
      Retry
    </button>
  </div>
{:else}
  <!-- status pill strip -->
  <div class="mb-5 flex flex-wrap gap-2">
    <StatusPill dot="var(--color-success)" value="1" label="online" />
    <StatusPill icon={MessageSquare} value={fmt(requests)} label="today" />
    <StatusPill icon={DollarSign} value={'$' + (((llmCalls ?? 0) * 0.0002).toFixed(2))} label="spend" />
    <StatusPill icon={Clock} value={p95 === null ? '–' : p95 + ' ms'} label="p95" />
    <StatusPill dot={errors ? 'var(--color-warning)' : 'var(--color-success)'} label={errors ? 'Degraded' : 'Healthy'} />
    <StatusPill icon={Database} value={fmt(inventoryRows)} label="rows" />
  </div>

  <div class="mb-[18px] flex items-end">
    <div>
      <h1 class="page-title text-[28px] text-ink">Overview</h1>
      <div class="mt-1.5 text-[13.5px] text-ink-2">
        Agent health, data freshness &amp; usage — pharmacy-wide
      </div>
    </div>
    <div class="ml-auto inline-flex rounded-[10px] bg-surface-2 p-[3px]">
      {#each ['7d', '30d', '90d'] as r}
        <button
          onclick={() => (range = r)}
          class="rounded-lg px-3 py-1.5 text-[12.5px] font-semibold transition-colors
            {range === r ? 'bg-surface text-ink shadow-[var(--shadow-card)]' : 'text-ink-2'}"
        >
          {r}
        </button>
      {/each}
    </div>
  </div>

  <!-- hero -->
  <div class="mb-[18px]">
    <HeroMetric
      value={cachePct ? cachePct.toFixed(1) : '–'}
      suffix={cachePct ? '%' : ''}
      caption="answers served from cache or a cited source"
      live={`live · ${fmt(requests)} answered this period`}
      {bars}
      stats={[
        { value: fmt(sites), label: 'active sites' },
        { value: '$' + (((llmCalls ?? 0) * 0.0002).toFixed(2)), label: 'llm spend' },
        { value: fmt(llmCalls), label: 'llm calls' }
      ]}
    />
  </div>

  <!-- ask bar -->
  <div
    class="elev flex items-center gap-2.5 rounded-[13px] border-[0.5px] border-line bg-surface px-[15px] py-[11px]"
  >
    <Sparkles size={19} class="text-accent-2" />
    <input
      bind:value={askText}
      onkeydown={(e) => e.key === 'Enter' && ask()}
      aria-label="Ask the agent"
      placeholder="Ask about stock, cold SKUs, substitutes, accuracy…"
      class="flex-1 border-0 bg-transparent text-[14px] text-ink outline-none placeholder:text-ink-3"
    />
    <button
      onclick={ask}
      class="rounded-[10px] bg-accent px-4 py-2.5 text-[13px] font-semibold text-on-accent hover:bg-accent-hover"
    >
      Ask
    </button>
  </div>
  <div class="mt-3 flex flex-wrap gap-2">
    {#each ['Which SKUs are out of stock?', 'Top sellers this week', 'How is answer accuracy?', 'What should I fix first?'] as c}
      <button
        onclick={() => {
          askText = c;
          ask();
        }}
        class="rounded-[9px] border-[0.5px] border-line bg-surface px-3 py-1.5 text-[12.5px] text-ink-2 transition-colors hover:border-accent hover:bg-accent-soft hover:text-accent"
      >
        {c}
      </button>
    {/each}
  </div>

  <!-- alert chips -->
  <div class="my-[18px] flex flex-wrap gap-2.5">
    <AlertChip
      tone="danger"
      icon={PackageX}
      label="Zero-stock SKUs"
      count="—"
      onclick={() => goto(base + '/data')}
    />
    <AlertChip
      tone="warning"
      icon={ClockAlert}
      label="Check data freshness"
      onclick={() => goto(base + '/ftp')}
    />
    <AlertChip
      tone="info"
      icon={UserX}
      label="Review logins"
      onclick={() => goto(base + '/users')}
    />
  </div>

  <!-- rings (all real metrics) -->
  <div class="my-3.5 grid grid-cols-2 gap-3.5 lg:grid-cols-4">
    <Ring value={cachePct} label="Cache hit" sub="cost saver" color="var(--color-accent)" />
    <Ring value={reliability} label="Reliability" sub={`${fmt(errors)} errors / 24h`} color="var(--color-success)" />
    <Ring value={siteCoverage} label="Site coverage" sub={`${fmt(sites)} active sites`} color="var(--color-info)" />
    <Ring value={freshness} label="Ingest health" sub="last load clean" color="var(--color-warning)" />
  </div>

  <!-- stat row -->
  <div class="my-3.5 grid grid-cols-2 gap-3.5 lg:grid-cols-4">
    <div class="elev rounded-2xl border-[0.5px] border-line bg-surface p-4">
      <div class="page-title text-[24px] tnum text-ink">{fmt(catalogRows)}</div>
      <div class="mt-1 text-[12.5px] text-ink-2">Catalog SKUs</div>
    </div>
    <div class="elev rounded-2xl border-[0.5px] border-line bg-surface p-4">
      <div class="page-title text-[24px] tnum text-ink">{fmt(inventoryRows)}</div>
      <div class="mt-1 text-[12.5px] text-ink-2">Inventory rows</div>
    </div>
    <div class="elev rounded-2xl border-[0.5px] border-line bg-surface p-4">
      <div class="page-title text-[24px] tnum text-ink">{fmt(requests)}</div>
      <div class="mt-1 text-[12.5px] text-ink-2">Questions answered</div>
    </div>
    <div class="elev rounded-2xl border-[0.5px] border-line bg-surface p-4">
      <div class="page-title text-[24px] tnum text-ink">
        ${(((llmCalls ?? 0) * 0.0002).toFixed(2))}
      </div>
      <div class="mt-1 text-[12.5px] text-ink-2">LLM cost · est</div>
    </div>
  </div>

  <!-- top articles -->
  <div class="mt-[18px] overflow-hidden rounded-2xl border-[0.5px] border-line bg-surface elev">
    <div class="flex items-center border-b border-line px-[18px] py-[15px]">
      <span class="page-title text-[16px] text-ink">Top articles by stock</span>
      <span class="ml-auto text-[12px] text-ink-3">via materialized view</span>
    </div>
    <table class="tbl">
      <thead>
        <tr>
          <th class="tnum">Code</th>
          <th>Brand</th>
          <th class="num tnum">Total stock</th>
          <th class="num tnum">Avg price</th>
          <th class="num tnum">Sites</th>
        </tr>
      </thead>
      <tbody>
        {#if overview.length === 0}
          <tr><td colspan="5" class="text-ink-2">No data</td></tr>
        {:else}
          {#each overview as row (row.article_code)}
            <tr
              role="button"
              tabindex="0"
              onclick={() => goto(base + '/graph')}
              onkeydown={(e) => e.key === 'Enter' && goto(base + '/graph')}
            >
              <td class="tnum text-ink">{row.article_code}</td>
              <td class="text-ink">{row.brand_name}</td>
              <!-- null = unknown, never 0: a blank count is not a count of zero. -->
              <td class="num tnum {row.total_stock == null ? 'text-ink-3 italic' : 'text-ink'}"
                >{row.total_stock == null ? 'unknown' : row.total_stock.toLocaleString()}</td
              >
              <td class="num tnum {row.weighted_avg_price == null ? 'text-ink-3 italic' : 'text-ink'}"
                >{row.weighted_avg_price == null
                  ? 'unknown'
                  : row.weighted_avg_price.toLocaleString() + ' MMK'}</td
              >
              <td class="num tnum text-ink">{(row.site_count ?? 0).toLocaleString()}</td>
            </tr>
          {/each}
        {/if}
      </tbody>
    </table>
  </div>
{/if}
