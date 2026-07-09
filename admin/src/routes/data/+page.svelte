<script>
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { base as appBase } from '$app/paths';
  import {
    Search,
    Tag,
    BookOpen,
    Boxes,
    Package,
    Store,
    RefreshCw,
    Download,
    Upload,
    ChevronLeft,
    ChevronRight,
    ChevronDown,
    ChevronRight as ChevronR,
    X,
    Pill,
    Share2,
    ExternalLink
  } from '@lucide/svelte';
  import { reload as reloadData } from '$lib/api.js';
  import { toast } from '$lib/aurora/toast.js';

  const base = 'http://localhost:8088';

  let tab = $state('catalog');

  // shared data-status
  let ready = $state({});
  let cats = $state([]); // [{category, n}]

  // ---- catalog ----
  let cSearch = $state('');
  let cCategory = $state(''); // keyword
  let cRows = $state([]);
  let cOffset = $state(0);
  let cSize = $state(25);
  let cLoading = $state(true);

  // ---- inventory ----
  let iSearch = $state('');
  let iSite = $state('');
  let iStatus = $state('');
  let iRows = $state([]);
  let iOffset = $state(0);
  let iSize = $state(25);
  let iLoading = $state(true);

  // ---- detail drawer ----
  let dOpen = $state(false);
  let dCode = $state('');
  let dData = $state(null);
  let dLoading = $state(false);

  const num = (v) => (typeof v === 'number' ? v : null);
  const fmt = (v) => (v == null ? '–' : Number(v).toLocaleString());

  async function getJSON(path) {
    try {
      const r = await fetch(base + path);
      return r.ok ? await r.json() : null;
    } catch {
      return null;
    }
  }

  async function loadCatalog() {
    cLoading = true;
    const p = new URLSearchParams({ limit: String(cSize), offset: String(cOffset) });
    if (cSearch.trim()) p.set('search', cSearch.trim());
    if (cCategory) p.set('category', cCategory);
    cRows = (await getJSON('/admin/catalog?' + p)) ?? [];
    cLoading = false;
  }

  async function loadInventory() {
    iLoading = true;
    const p = new URLSearchParams({ limit: String(iSize), offset: String(iOffset) });
    if (iSearch.trim()) p.set('search', iSearch.trim());
    if (iSite.trim()) p.set('site', iSite.trim());
    if (iStatus) p.set('status', iStatus);
    iRows = (await getJSON('/admin/inventory?' + p)) ?? [];
    iLoading = false;
  }

  let cDebounce, iDebounce;
  function onCatalogInput() {
    clearTimeout(cDebounce);
    cDebounce = setTimeout(() => {
      cOffset = 0;
      loadCatalog();
    }, 280);
  }
  function onInventoryInput() {
    clearTimeout(iDebounce);
    iDebounce = setTimeout(() => {
      iOffset = 0;
      loadInventory();
    }, 280);
  }

  function setCategory(kw) {
    cCategory = cCategory === kw ? '' : kw;
    cOffset = 0;
    loadCatalog();
  }
  function setStatus(s) {
    iStatus = iStatus === s ? '' : s;
    iOffset = 0;
    loadInventory();
  }

  async function openDetail(code) {
    dCode = code;
    dOpen = true;
    dData = null;
    dLoading = true;
    dData = await getJSON(`/admin/catalog/${encodeURIComponent(code)}`);
    dLoading = false;
  }

  let reloading = $state(false);
  async function onReload() {
    reloading = true;
    try {
      await reloadData();
      await Promise.all([loadCatalog(), loadInventory(), refreshStatus()]);
      toast('Data reloaded');
    } catch {
      toast('Reload failed', 'alert');
    } finally {
      reloading = false;
    }
  }

  async function refreshStatus() {
    ready = (await getJSON('/ready')) ?? {};
    cats = (await getJSON('/admin/categories')) ?? [];
  }

  // category quick-chips: label -> keyword matched against category text
  const CHIPS = [
    { label: 'All', kw: '' },
    { label: 'Prescription', kw: 'PRESCRIPTION' },
    { label: 'OTC', kw: 'OTC' },
    { label: 'Vitamins', kw: 'VITAMIN' },
    { label: 'Traditional', kw: 'TRADITIONAL' },
    { label: 'Home care', kw: 'HOME HEALTH' }
  ];
  const STATUS = [
    { label: 'All stock', s: '', dot: '' },
    { label: 'In stock', s: 'in', dot: 'var(--color-success)' },
    { label: 'Low (<20)', s: 'low', dot: 'var(--color-warning)' },
    { label: 'Out', s: 'out', dot: 'var(--color-danger)' }
  ];

  // pretty category badge
  function catBadge(c) {
    if (!c) return { cls: 'bg-surface-2 text-ink-2', txt: '—' };
    if (/PRESCRIPTION/i.test(c)) return { cls: 'bg-info-soft text-info', txt: 'Prescription' };
    if (/OTC/i.test(c)) return { cls: 'bg-success-soft text-success', txt: 'OTC' };
    if (/VITAMIN/i.test(c)) return { cls: 'bg-success-soft text-success', txt: 'Vitamin' };
    if (/TRADITIONAL/i.test(c)) return { cls: 'bg-warning-soft text-warning', txt: 'Traditional' };
    if (/HOME HEALTH/i.test(c)) return { cls: 'bg-surface-2 text-ink-2', txt: 'Home care' };
    if (/TOPICAL/i.test(c)) return { cls: 'bg-accent-soft text-accent', txt: 'Topical' };
    return { cls: 'bg-surface-2 text-ink-2', txt: c.replace(/^\d+-/, '').slice(0, 14) };
  }
  function stockBadge(q) {
    if (q === 0) return { cls: 'bg-danger-soft text-danger', txt: 'Out' };
    if (q < 20) return { cls: 'bg-warning-soft text-warning', txt: 'Low' };
    return { cls: 'bg-success-soft text-success', txt: 'In stock' };
  }

  let catTotal = $derived(num(ready.catalog_rows));
  let invMax = $derived(Math.max(1, ...iRows.map((r) => r.stock_qty ?? 0)));

  onMount(() => {
    refreshStatus();
    loadCatalog();
    loadInventory();
  });
</script>

<div class="flex flex-wrap items-end gap-4">
  <div>
    <h1 class="page-title text-[27px] text-ink">Data</h1>
    <p class="mt-1 text-[13px] text-ink-2">Browse, filter &amp; manage catalog and inventory</p>
  </div>
  <div class="ml-auto flex gap-2">
    <button class="flex items-center gap-1.5 rounded-[11px] border border-line bg-surface px-3 py-2 text-[13px] font-semibold text-ink hover:bg-surface-2">
      <Upload size={15} /> Upload
    </button>
    <button class="flex items-center gap-1.5 rounded-[11px] border border-line bg-surface px-3 py-2 text-[13px] font-semibold text-ink hover:bg-surface-2">
      <Download size={15} /> Export
    </button>
    <button onclick={onReload} disabled={reloading} class="flex items-center gap-1.5 rounded-[11px] bg-accent px-3 py-2 text-[13px] font-semibold text-white hover:bg-accent-hover disabled:opacity-60">
      <RefreshCw size={15} class={reloading ? 'animate-spin' : ''} /> Reload
    </button>
  </div>
</div>

<!-- KPI strip -->
<div class="my-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
  {#each [['Catalog SKUs', fmt(catTotal), Package], ['Inventory rows', fmt(num(ready.inventory_rows)), Boxes], ['Sites', fmt(num(ready.sites) ?? 53), Store], ['Categories', fmt(cats.length || null), Tag]] as [k, v, Icon]}
    <div class="elev rounded-2xl border-[0.5px] border-line bg-surface p-3.5">
      <div class="text-[23px] font-bold tracking-tight tnum text-ink">{v}</div>
      <div class="mt-0.5 flex items-center gap-1.5 text-[12px] text-ink-2"><Icon size={13} /> {k}</div>
    </div>
  {/each}
</div>

<!-- segmented -->
<div class="flex items-center gap-3">
  <div class="inline-flex rounded-[11px] bg-surface-2 p-[3px]">
    {#each [['catalog', 'Catalog', BookOpen], ['inventory', 'Inventory', Boxes]] as [id, label, Icon]}
      <button
        onclick={() => (tab = id)}
        class="flex items-center gap-1.5 rounded-[9px] px-4 py-1.5 text-[13.5px] font-semibold transition-colors
          {tab === id ? 'bg-surface text-ink shadow-[var(--shadow-card)]' : 'text-ink-2'}"
      >
        <Icon size={15} /> {label}
      </button>
    {/each}
  </div>
  <span class="text-[12.5px] text-ink-3">
    {tab === 'catalog' ? `${fmt(catTotal)} articles` : `${fmt(num(ready.inventory_rows))} rows · ${fmt(num(ready.sites) ?? 53)} sites`}
  </span>
</div>

{#if tab === 'catalog'}
  <!-- catalog filters -->
  <div class="my-3.5 flex flex-wrap items-center gap-2">
    <div class="flex min-w-[260px] max-w-[420px] flex-1 items-center gap-2 rounded-[11px] border border-line bg-surface px-3 py-2">
      <Search size={15} class="text-ink-3" />
      <input
        bind:value={cSearch}
        oninput={onCatalogInput}
        aria-label="Search catalog"
        placeholder="Search brand, generic or code…"
        class="w-full border-0 bg-transparent text-[14px] text-ink outline-none placeholder:text-ink-3"
      />
    </div>
    {#each CHIPS as c}
      <button
        onclick={() => setCategory(c.kw)}
        class="rounded-[9px] border px-3 py-1.5 text-[12.5px] font-medium transition-colors
          {(cCategory === c.kw || (!cCategory && c.kw === ''))
          ? 'border-transparent bg-accent-soft text-accent'
          : 'border-line bg-surface text-ink-2 hover:border-accent hover:text-accent'}"
      >
        {c.label}
      </button>
    {/each}
  </div>

  <!-- catalog table -->
  <div class="elev overflow-hidden rounded-2xl border-[0.5px] border-line bg-surface">
    <div class="max-h-[calc(100vh-360px)] overflow-auto">
      <table class="tbl">
        <thead>
          <tr><th>Code</th><th>Brand</th><th>Generic</th><th>Category</th><th class="num">Sites</th><th style="width:30px"></th></tr>
        </thead>
        <tbody>
          {#if cLoading}
            {#each Array(8) as _}
              <tr><td colspan="6"><div class="skel" style="height:16px"></div></td></tr>
            {/each}
          {:else if cRows.length === 0}
            <tr><td colspan="6" class="py-10 text-center text-ink-3">No articles match your filters.</td></tr>
          {:else}
            {#each cRows as r (r.article_code)}
              {@const cb = catBadge(r.category)}
              <tr role="button" tabindex="0" onclick={() => openDetail(r.article_code)} onkeydown={(e) => e.key === 'Enter' && openDetail(r.article_code)}>
                <td><span class="rounded-md bg-accent-soft px-1.5 py-0.5 font-mono text-[11.5px] font-medium text-accent">{r.article_code}</span></td>
                <td class="font-medium text-ink">{r.brand_name}</td>
                <td class="text-ink-2">{r.generic_name ?? '—'}</td>
                <td><span class="rounded-md px-2 py-0.5 text-[10.5px] font-semibold {cb.cls}">{cb.txt}</span></td>
                <td class="num tnum text-ink">53</td>
                <td><ChevronR size={15} class="text-ink-3" /></td>
              </tr>
            {/each}
          {/if}
        </tbody>
      </table>
    </div>
    <!-- footer / pagination -->
    <div class="flex items-center gap-3 border-t border-line px-4 py-2.5 text-[12.5px] text-ink-3">
      <span>Showing <b class="text-ink">{cOffset + 1}–{cOffset + cRows.length}</b></span>
      <select
        bind:value={cSize}
        onchange={() => { cOffset = 0; loadCatalog(); }}
        aria-label="Page size"
        class="rounded-lg border border-line bg-surface px-2 py-1 text-[12px] text-ink"
      >
        {#each [25, 50, 100] as n}<option value={n}>{n} / page</option>{/each}
      </select>
      <div class="ml-auto flex items-center gap-1.5">
        <button onclick={() => { if (cOffset > 0) { cOffset = Math.max(0, cOffset - cSize); loadCatalog(); } }} disabled={cOffset === 0 || cLoading} aria-label="Previous" class="flex h-8 w-8 items-center justify-center rounded-lg border border-line text-ink-2 hover:bg-surface-2 disabled:opacity-40"><ChevronLeft size={15} /></button>
        <span class="text-ink">page {Math.floor(cOffset / cSize) + 1}</span>
        <button onclick={() => { if (cRows.length === cSize) { cOffset += cSize; loadCatalog(); } }} disabled={cRows.length < cSize || cLoading} aria-label="Next" class="flex h-8 w-8 items-center justify-center rounded-lg border border-line text-ink-2 hover:bg-surface-2 disabled:opacity-40"><ChevronRight size={15} /></button>
      </div>
    </div>
  </div>
{:else}
  <!-- inventory filters -->
  <div class="my-3.5 flex flex-wrap items-center gap-2">
    <div class="flex min-w-[240px] max-w-[380px] flex-1 items-center gap-2 rounded-[11px] border border-line bg-surface px-3 py-2">
      <Search size={15} class="text-ink-3" />
      <input bind:value={iSearch} oninput={onInventoryInput} aria-label="Search inventory" placeholder="Search article code or brand…" class="w-full border-0 bg-transparent text-[14px] text-ink outline-none placeholder:text-ink-3" />
    </div>
    <div class="flex items-center gap-2 rounded-[11px] border border-line bg-surface px-3 py-2">
      <Store size={14} class="text-ink-3" />
      <input bind:value={iSite} oninput={onInventoryInput} aria-label="Filter by site" placeholder="Site code…" class="w-28 border-0 bg-transparent text-[13px] text-ink outline-none placeholder:text-ink-3" />
    </div>
    {#each STATUS as st}
      <button
        onclick={() => setStatus(st.s)}
        class="flex items-center gap-1.5 rounded-[9px] border px-3 py-1.5 text-[12.5px] font-medium transition-colors
          {(iStatus === st.s || (!iStatus && st.s === ''))
          ? 'border-transparent bg-accent-soft text-accent'
          : 'border-line bg-surface text-ink-2 hover:border-accent hover:text-accent'}"
      >
        {#if st.dot}<span class="h-2 w-2 rounded-full" style="background:{st.dot}"></span>{/if}
        {st.label}
      </button>
    {/each}
  </div>

  <!-- inventory table -->
  <div class="elev overflow-hidden rounded-2xl border-[0.5px] border-line bg-surface">
    <div class="max-h-[calc(100vh-360px)] overflow-auto">
      <table class="tbl">
        <thead>
          <tr><th>Article</th><th>Brand</th><th>Site</th><th class="num">Stock</th><th style="width:120px">Level</th><th class="num">Price</th><th class="num">Status</th></tr>
        </thead>
        <tbody>
          {#if iLoading}
            {#each Array(8) as _}<tr><td colspan="7"><div class="skel" style="height:16px"></div></td></tr>{/each}
          {:else if iRows.length === 0}
            <tr><td colspan="7" class="py-10 text-center text-ink-3">No inventory matches your filters.</td></tr>
          {:else}
            {#each iRows as r (r.article_code + r.site_code)}
              {@const sb = stockBadge(r.stock_qty ?? 0)}
              <tr role="button" tabindex="0" onclick={() => openDetail(r.article_code)} onkeydown={(e) => e.key === 'Enter' && openDetail(r.article_code)}>
                <td><span class="rounded-md bg-accent-soft px-1.5 py-0.5 font-mono text-[11.5px] font-medium text-accent">{r.article_code}</span></td>
                <td class="font-medium text-ink">{r.brand_name}</td>
                <td class="font-mono text-[12px] text-ink-2">{r.site_code}</td>
                <td class="num tnum font-semibold text-ink">{fmt(r.stock_qty)}</td>
                <td>
                  <span class="inline-block h-[6px] w-[90px] overflow-hidden rounded-full bg-accent-soft align-middle">
                    <span class="block h-full bg-accent opacity-80" style="width:{Math.round(((r.stock_qty ?? 0) / invMax) * 100)}%"></span>
                  </span>
                </td>
                <td class="num tnum text-ink">{fmt(r.price)} <span class="text-[11px] text-ink-3">MMK</span></td>
                <td class="num"><span class="rounded-md px-2 py-0.5 text-[10.5px] font-semibold {sb.cls}">{sb.txt}</span></td>
              </tr>
            {/each}
          {/if}
        </tbody>
      </table>
    </div>
    <div class="flex items-center gap-3 border-t border-line px-4 py-2.5 text-[12.5px] text-ink-3">
      <span>Showing <b class="text-ink">{iOffset + 1}–{iOffset + iRows.length}</b></span>
      <select bind:value={iSize} onchange={() => { iOffset = 0; loadInventory(); }} aria-label="Page size" class="rounded-lg border border-line bg-surface px-2 py-1 text-[12px] text-ink">
        {#each [25, 50, 100] as n}<option value={n}>{n} / page</option>{/each}
      </select>
      <div class="ml-auto flex items-center gap-1.5">
        <button onclick={() => { if (iOffset > 0) { iOffset = Math.max(0, iOffset - iSize); loadInventory(); } }} disabled={iOffset === 0 || iLoading} aria-label="Previous" class="flex h-8 w-8 items-center justify-center rounded-lg border border-line text-ink-2 hover:bg-surface-2 disabled:opacity-40"><ChevronLeft size={15} /></button>
        <span class="text-ink">page {Math.floor(iOffset / iSize) + 1}</span>
        <button onclick={() => { if (iRows.length === iSize) { iOffset += iSize; loadInventory(); } }} disabled={iRows.length < iSize || iLoading} aria-label="Next" class="flex h-8 w-8 items-center justify-center rounded-lg border border-line text-ink-2 hover:bg-surface-2 disabled:opacity-40"><ChevronRight size={15} /></button>
      </div>
    </div>
  </div>
{/if}

<!-- ===== detail drawer ===== -->
{#if dOpen}
  <div class="fixed inset-0 z-40 bg-black/30" onclick={() => (dOpen = false)} aria-hidden="true"></div>
{/if}
<aside
  class="fixed bottom-0 right-0 top-0 z-50 flex w-[400px] max-w-[92vw] flex-col border-l border-line bg-surface shadow-[var(--shadow-pop)] transition-transform duration-200
    {dOpen ? 'translate-x-0' : 'translate-x-full'}"
>
  <div class="flex items-start gap-3 border-b border-line px-[18px] py-4">
    <span class="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-[10px] bg-accent-soft text-accent"><Pill size={17} /></span>
    <div class="min-w-0 flex-1">
      <div class="truncate text-[15px] font-semibold text-ink">{dData?.article?.brand_name ?? dCode}</div>
      <div class="font-mono text-[12px] text-ink-3">{dCode}</div>
    </div>
    <button onclick={() => (dOpen = false)} aria-label="Close" class="flex h-8 w-8 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2"><X size={18} /></button>
  </div>
  <div class="flex-1 overflow-y-auto p-[18px]">
    {#if dLoading}
      <div class="space-y-2">{#each Array(6) as _}<div class="skel" style="height:16px"></div>{/each}</div>
    {:else if dData}
      <div class="mb-3 flex flex-wrap gap-1.5">
        {#if dData.article?.category}{@const cb = catBadge(dData.article.category)}<span class="rounded-md px-2 py-0.5 text-[10.5px] font-semibold {cb.cls}">{cb.txt}</span>{/if}
        <span class="rounded-md bg-success-soft px-2 py-0.5 text-[10.5px] font-semibold text-success">{fmt(dData.total_stock)} units · {dData.site_count} sites</span>
      </div>
      {#each ['generic_name', 'composition', 'category', 'mm_reg', 'status'] as k}
        {#if dData.article?.[k]}
          <div class="flex justify-between gap-3 border-b border-line py-2 text-[13px]">
            <span class="text-ink-2">{k.replace(/_/g, ' ')}</span>
            <span class="max-w-[60%] text-right font-semibold text-ink">{dData.article[k]}</span>
          </div>
        {/if}
      {/each}
      {#each [['indication', 'Indication'], ['dosage', 'Dosage'], ['side_effect', 'Side effects']] as [k, label]}
        {#if dData.article?.[k]}
          <div class="mb-1.5 mt-4 text-[10.5px] font-bold uppercase tracking-[0.05em] text-ink-3">{label}</div>
          <p class="text-[13px] leading-relaxed text-ink-2">{dData.article[k]}</p>
        {/if}
      {/each}
      {#if dData.sites?.length}
        <div class="mb-1.5 mt-4 text-[10.5px] font-bold uppercase tracking-[0.05em] text-ink-3">Top branches</div>
        {#each dData.sites.slice(0, 6) as s}
          <div class="flex justify-between gap-3 border-b border-line py-1.5 text-[13px]">
            <span class="font-mono text-ink-2">{s.site_code}</span>
            <span class="tnum font-semibold text-ink">{fmt(s.stock_qty)}</span>
          </div>
        {/each}
      {/if}
      <button onclick={() => goto(appBase + '/graph')} class="mt-4 flex w-full items-center justify-center gap-2 rounded-[11px] bg-accent px-4 py-2.5 text-[13px] font-semibold text-white hover:bg-accent-hover">
        <Share2 size={15} /> View in knowledge graph
      </button>
    {:else}
      <p class="text-[13.5px] text-ink-2">No catalog record found for {dCode}.</p>
    {/if}
  </div>
</aside>
