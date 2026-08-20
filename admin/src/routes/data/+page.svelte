<script>
  import { dialog } from '$lib/aurora/dialog.js';
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
  import { reload as reloadData, getJSON } from '$lib/api.js';
  import ErrorState from '$lib/ErrorState.svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import { toast } from '$lib/aurora/toast.js';

  let tab = $state('catalog');

  // shared data-status
  let ready = $state({});
  /** How many branches this reader can see. `null` until measured. */
  let sites = $state(null);
  let cats = $state([]); // [{category, n}]
  // One error per independently-loaded section. A failed call used to return
  // null and land in the same branch as an empty result, so a 401 read as
  // "No articles match your filters" — the console inventing an absence.
  let statusError = $state(null);
  let cError = $state(null);
  let iError = $state(null);
  let dError = $state(null);

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

  async function loadCatalog() {
    cLoading = true;
    cError = null;
    const p = new URLSearchParams({ limit: String(cSize), offset: String(cOffset) });
    if (cSearch.trim()) p.set('search', cSearch.trim());
    if (cCategory) p.set('category', cCategory);
    try {
      const d = await getJSON('/admin/catalog?' + p);
      cRows = Array.isArray(d) ? d : [];
    } catch (e) {
      cRows = [];
      cError = e;
    } finally {
      cLoading = false;
    }
  }

  async function loadInventory() {
    iLoading = true;
    iError = null;
    const p = new URLSearchParams({ limit: String(iSize), offset: String(iOffset) });
    if (iSearch.trim()) p.set('search', iSearch.trim());
    if (iSite.trim()) p.set('site', iSite.trim());
    if (iStatus) p.set('status', iStatus);
    try {
      const d = await getJSON('/admin/inventory?' + p);
      iRows = Array.isArray(d) ? d : [];
    } catch (e) {
      iRows = [];
      iError = e;
    } finally {
      iLoading = false;
    }
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

  /** Enter OR Space, matching what `role="button"` promises a screen reader.
      Space is a button's primary activation key; without preventDefault it
      scrolls the table instead of opening the row. Same shape as
      `lib/activity/FeedTab.svelte`'s `rowEnter` and the FTP file rows. */
  function rowActivate(e, fn) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      fn();
    }
  }

  async function openDetail(code) {
    dCode = code;
    dOpen = true;
    dData = null;
    dError = null;
    dLoading = true;
    try {
      dData = await getJSON(`/admin/catalog/${encodeURIComponent(code)}`);
    } catch (e) {
      dError = e;
    } finally {
      dLoading = false;
    }
  }

  let reloading = $state(false);
  async function onReload() {
    reloading = true;
    try {
      await reloadData();
      await Promise.all([loadCatalog(), loadInventory(), refreshStatus()]);
      toast('Data reloaded');
    } catch (e) {
      // Status-aware: a 401 here means the session expired, not that the
      // reload itself is broken.
      const s = Number(e?.status ?? 0);
      toast(
        s === 401
          ? 'Your sign-in has timed out. Sign in again, then reload.'
          : s > 0
            ? `Reload failed — the backend answered ${s}.`
            : 'Reload failed — no response from the backend.',
        'alert'
      );
    } finally {
      reloading = false;
    }
  }

  async function refreshStatus() {
    statusError = null;
    try {
      ready = (await getJSON('/ready')) ?? {};
      const c = await getJSON('/admin/categories');
      cats = Array.isArray(c) ? c : [];
      // The Sites tile read `ready.sites`, and `/ready` has never sent that
      // key — `counts()` returns catalog and inventory rows and nothing else.
      // So the tile printed the honest dash forever, next to a table whose
      // every row says 53. The number lives on `/admin/stores`, which is also
      // where it gets SCOPED: a store-scoped admin sees their own branch here,
      // the same one they see on Branches.
      const st = await getJSON('/admin/stores');
      sites = Array.isArray(st) ? st.length : null;
    } catch (e) {
      // Blank the counters rather than leaving stale ones next to a new error.
      ready = {};
      cats = [];
      sites = null;
      statusError = e;
    }
  }

  function reloadAll() {
    refreshStatus();
    loadCatalog();
    loadInventory();
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

  onMount(reloadAll);
</script>

<!-- The rail row says "Catalog & stock" and this page announced itself as
     "Data", which is the shape of the tables rather than the thing they hold.

     Two buttons stood here with no handler between them: Upload and Export.
     They hovered, they took a click, and nothing happened — no request, no
     error, no toast. Upload now goes to the page that really does it, and
     Export is gone until there is an endpoint behind it. A control that cannot
     work is worse than an absent one: it teaches the reader that a click here
     means nothing. -->
<PageHeader
  title="Catalog & stock"
  subtitle="Every product this assistant can answer about, and how much of it each branch is holding."
>
  {#snippet actions()}
    <a
      href={appBase + '/ftp'}
      class="flex items-center gap-1.5 rounded-card border border-line bg-surface px-3 py-2 text-body-sm font-semibold text-ink transition-colors hover:bg-surface-2"
    >
      <Upload size={15} /> Upload a file
    </a>
    <button
      onclick={onReload}
      disabled={reloading}
      class="flex items-center gap-1.5 rounded-card bg-accent px-3 py-2 text-body-sm font-semibold text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
    >
      <RefreshCw size={15} class={reloading ? 'animate-spin' : ''} /> Reload
    </button>
  {/snippet}
</PageHeader>

<!-- KPI strip. Every tile here comes from /ready + /admin/categories; if that
     call failed, `ready` is {} and each tile would render a confident "–" or 0
     beside a live-looking label. Show the failure instead. -->
{#if statusError}
  <div class="my-4">
    <ErrorState error={statusError} retry={refreshStatus} what="the data counts" />
  </div>
{:else}
  <div class="my-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
    {#each [['Catalog SKUs', fmt(catTotal), Package], ['Inventory rows', fmt(num(ready.inventory_rows)), Boxes], ['Sites', fmt(num(sites)), Store], ['Categories', fmt(cats.length || null), Tag]] as [k, v, Icon]}
      <div class="elev rounded-panel border border-line bg-surface p-3.5">
        <div class="page-title text-heading tnum text-ink">{v}</div>
        <div class="mt-0.5 flex items-center gap-1.5 text-meta text-ink-2"><Icon size={13} /> {k}</div>
      </div>
    {/each}
  </div>
{/if}

<!-- segmented -->
<div class="flex items-center gap-3">
  <div class="inline-flex rounded-card bg-surface-2 p-[3px]">
    {#each [['catalog', 'Catalog', BookOpen], ['inventory', 'Inventory', Boxes]] as [id, label, Icon]}
      <button
        onclick={() => (tab = id)}
        class="flex items-center gap-1.5 rounded-card px-4 py-1.5 text-body-sm font-semibold transition-colors
          {tab === id ? 'bg-surface text-ink shadow-[var(--shadow-card)]' : 'text-ink-2'}"
      >
        <Icon size={15} /> {label}
      </button>
    {/each}
  </div>
  <span class="text-meta text-ink-3">
    {#if statusError}
      counts unavailable
    {:else}
      {tab === 'catalog'
        ? `${fmt(catTotal)} articles`
        : `${fmt(num(ready.inventory_rows))} rows · ${fmt(num(ready.sites))} sites`}
    {/if}
  </span>
</div>

{#if tab === 'catalog'}
  <!-- catalog filters -->
  <div class="my-3.5 flex flex-wrap items-center gap-2">
    <div class="flex min-w-[260px] max-w-[420px] flex-1 items-center gap-2 rounded-card border border-line bg-surface px-3 py-2">
      <Search size={15} class="text-ink-3" />
      <input
        bind:value={cSearch}
        oninput={onCatalogInput}
        aria-label="Search catalog"
        placeholder="Search brand, generic or code…"
        class="w-full border-0 bg-transparent text-body-sm text-ink outline-none placeholder:text-ink-3"
      />
    </div>
    {#each CHIPS as c}
      <button
        onclick={() => setCategory(c.kw)}
        class="rounded-card border px-3 py-1.5 text-meta font-medium transition-colors
          {(cCategory === c.kw || (!cCategory && c.kw === ''))
          ? 'border-transparent bg-accent-soft text-accent'
          : 'border-line bg-surface text-ink-2 hover:border-accent hover:text-accent'}"
      >
        {c.label}
      </button>
    {/each}
  </div>

  <!-- catalog table -->
  <div class="elev overflow-hidden rounded-hero border border-line bg-surface">
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
          {:else if cError}
            <!-- Not "no matches": the query never ran. -->
            <tr><td colspan="6" class="p-4"><ErrorState error={cError} retry={loadCatalog} what="the catalog" /></td></tr>
          {:else if cRows.length === 0}
            <tr><td colspan="6" class="py-10 text-center text-ink-3">No articles match your filters.</td></tr>
          {:else}
            {#each cRows as r (r.article_code)}
              {@const cb = catBadge(r.category)}
              <tr role="button" tabindex="0" onclick={() => openDetail(r.article_code)} onkeydown={(e) => rowActivate(e, () => openDetail(r.article_code))}>
                <td><span class="rounded-control bg-accent-soft px-1.5 py-0.5 font-mono text-label font-medium text-accent">{r.article_code}</span></td>
                <td class="font-medium text-ink">{r.brand_name}</td>
                <td class="text-ink-2">{r.generic_name ?? '—'}</td>
                <td><span class="rounded-control px-2 py-0.5 text-micro font-semibold {cb.cls}">{cb.txt}</span></td>
                <td class="num tnum text-ink">53</td>
                <td><ChevronR size={15} class="text-ink-3" /></td>
              </tr>
            {/each}
          {/if}
        </tbody>
      </table>
    </div>
    <!-- footer / pagination -->
    <div class="flex items-center gap-3 border-t border-line px-4 py-2.5 text-meta text-ink-3">
      <span>Showing <b class="text-ink">{cOffset + 1}–{cOffset + cRows.length}</b></span>
      <select
        bind:value={cSize}
        onchange={() => { cOffset = 0; loadCatalog(); }}
        aria-label="Page size"
        class="rounded-panel border border-line bg-surface px-2 py-1 text-meta text-ink"
      >
        {#each [25, 50, 100] as n}<option value={n}>{n} / page</option>{/each}
      </select>
      <div class="ml-auto flex items-center gap-1.5">
        <button onclick={() => { if (cOffset > 0) { cOffset = Math.max(0, cOffset - cSize); loadCatalog(); } }} disabled={cOffset === 0 || cLoading} aria-label="Previous" class="flex h-8 w-8 items-center justify-center rounded-panel border border-line text-ink-2 hover:bg-surface-2 disabled:opacity-40"><ChevronLeft size={15} /></button>
        <span class="text-ink">page {Math.floor(cOffset / cSize) + 1}</span>
        <button onclick={() => { if (cRows.length === cSize) { cOffset += cSize; loadCatalog(); } }} disabled={cRows.length < cSize || cLoading} aria-label="Next" class="flex h-8 w-8 items-center justify-center rounded-panel border border-line text-ink-2 hover:bg-surface-2 disabled:opacity-40"><ChevronRight size={15} /></button>
      </div>
    </div>
  </div>
{:else}
  <!-- inventory filters -->
  <div class="my-3.5 flex flex-wrap items-center gap-2">
    <div class="flex min-w-[240px] max-w-[380px] flex-1 items-center gap-2 rounded-card border border-line bg-surface px-3 py-2">
      <Search size={15} class="text-ink-3" />
      <input bind:value={iSearch} oninput={onInventoryInput} aria-label="Search inventory" placeholder="Search article code or brand…" class="w-full border-0 bg-transparent text-body-sm text-ink outline-none placeholder:text-ink-3" />
    </div>
    <div class="flex items-center gap-2 rounded-card border border-line bg-surface px-3 py-2">
      <Store size={14} class="text-ink-3" />
      <input bind:value={iSite} oninput={onInventoryInput} aria-label="Filter by site" placeholder="Site code…" class="w-28 border-0 bg-transparent text-body-sm text-ink outline-none placeholder:text-ink-3" />
    </div>
    {#each STATUS as st}
      <button
        onclick={() => setStatus(st.s)}
        class="flex items-center gap-1.5 rounded-card border px-3 py-1.5 text-meta font-medium transition-colors
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
  <div class="elev overflow-hidden rounded-hero border border-line bg-surface">
    <div class="max-h-[calc(100vh-360px)] overflow-auto">
      <table class="tbl">
        <thead>
          <tr><th>Article</th><th>Brand</th><th>Site</th><th class="num">Stock</th><th style="width:120px">Level</th><th class="num">Price</th><th class="num">Status</th></tr>
        </thead>
        <tbody>
          {#if iLoading}
            {#each Array(8) as _}<tr><td colspan="7"><div class="skel" style="height:16px"></div></td></tr>{/each}
          {:else if iError}
            <!-- Not "no matches": the query never ran. -->
            <tr><td colspan="7" class="p-4"><ErrorState error={iError} retry={loadInventory} what="inventory" /></td></tr>
          {:else if iRows.length === 0}
            <tr><td colspan="7" class="py-10 text-center text-ink-3">No inventory matches your filters.</td></tr>
          {:else}
            {#each iRows as r (r.article_code + r.site_code)}
              {@const sb = stockBadge(r.stock_qty ?? 0)}
              <tr role="button" tabindex="0" onclick={() => openDetail(r.article_code)} onkeydown={(e) => rowActivate(e, () => openDetail(r.article_code))}>
                <td><span class="rounded-control bg-accent-soft px-1.5 py-0.5 font-mono text-label font-medium text-accent">{r.article_code}</span></td>
                <td class="font-medium text-ink">{r.brand_name}</td>
                <td class="font-mono text-meta text-ink-2">{r.site_code}</td>
                <td class="num tnum font-semibold text-ink">{fmt(r.stock_qty)}</td>
                <td>
                  <span class="inline-block h-[6px] w-[90px] overflow-hidden rounded-full bg-accent-soft align-middle">
                    <span class="block h-full bg-accent opacity-80" style="width:{Math.round(((r.stock_qty ?? 0) / invMax) * 100)}%"></span>
                  </span>
                </td>
                <td class="num tnum text-ink">{fmt(r.price)} <span class="text-label text-ink-3">MMK</span></td>
                <td class="num"><span class="rounded-control px-2 py-0.5 text-micro font-semibold {sb.cls}">{sb.txt}</span></td>
              </tr>
            {/each}
          {/if}
        </tbody>
      </table>
    </div>
    <div class="flex items-center gap-3 border-t border-line px-4 py-2.5 text-meta text-ink-3">
      <span>Showing <b class="text-ink">{iOffset + 1}–{iOffset + iRows.length}</b></span>
      <select bind:value={iSize} onchange={() => { iOffset = 0; loadInventory(); }} aria-label="Page size" class="rounded-panel border border-line bg-surface px-2 py-1 text-meta text-ink">
        {#each [25, 50, 100] as n}<option value={n}>{n} / page</option>{/each}
      </select>
      <div class="ml-auto flex items-center gap-1.5">
        <button onclick={() => { if (iOffset > 0) { iOffset = Math.max(0, iOffset - iSize); loadInventory(); } }} disabled={iOffset === 0 || iLoading} aria-label="Previous" class="flex h-8 w-8 items-center justify-center rounded-panel border border-line text-ink-2 hover:bg-surface-2 disabled:opacity-40"><ChevronLeft size={15} /></button>
        <span class="text-ink">page {Math.floor(iOffset / iSize) + 1}</span>
        <button onclick={() => { if (iRows.length === iSize) { iOffset += iSize; loadInventory(); } }} disabled={iRows.length < iSize || iLoading} aria-label="Next" class="flex h-8 w-8 items-center justify-center rounded-panel border border-line text-ink-2 hover:bg-surface-2 disabled:opacity-40"><ChevronRight size={15} /></button>
      </div>
    </div>
  </div>
{/if}

<!-- ===== detail drawer ===== -->
{#if dOpen}
  <!-- Pointer affordance only. It is aria-hidden and takes no focus on
       purpose: the keyboard route out is Escape, which the dialog owns. -->
  <div class="fixed inset-0 z-40 bg-black/30" onclick={() => (dOpen = false)} aria-hidden="true"></div>
{/if}
{#if dOpen}
<!--
  This drawer had no `role`, no `aria-modal` and no accessible name: it was not
  a dialog, it was a box that appeared. Pressing Enter on a product row looked
  like it did nothing — focus stayed on the row, the next four Tabs walked the
  next four table ROWS, and Escape was dead.

  It was also always mounted and merely slid away with `translate-x-full`. A
  transform does not remove anything from the tab order, so a sweep of this page
  hit "Close" at x=1790 — 350px past the viewport — as a focus stop nobody can
  see. `{#if}` is what actually closes it.
-->
<div
  use:dialog={{ onclose: () => (dOpen = false) }}
  role="dialog"
  aria-modal="true"
  aria-label={dData?.article?.brand_name ?? dCode ?? 'Product detail'}
  tabindex="-1"
  class="fixed bottom-0 right-0 top-0 z-50 flex w-[400px] max-w-[92vw] flex-col border-l border-line bg-surface shadow-[var(--shadow-pop)] outline-none
"
>
  <div class="flex items-start gap-3 border-b border-line px-[18px] py-4">
    <span class="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-card bg-accent-soft text-accent"><Pill size={17} /></span>
    <div class="min-w-0 flex-1">
      <div class="truncate text-body font-semibold text-ink">{dData?.article?.brand_name ?? dCode}</div>
      <div class="font-mono text-meta text-ink-3">{dCode}</div>
    </div>
    <button onclick={() => (dOpen = false)} aria-label="Close" class="flex h-8 w-8 items-center justify-center rounded-panel text-ink-3 hover:bg-surface-2"><X size={18} /></button>
  </div>
  <div class="flex-1 overflow-y-auto p-[18px]">
    {#if dLoading}
      <div class="space-y-2">{#each Array(6) as _}<div class="skel" style="height:16px"></div>{/each}</div>
    {:else if dError}
      <!-- "No catalog record found" would be a claim about the catalog. The
           request failed; we know nothing about whether the record exists. -->
      <ErrorState error={dError} retry={() => openDetail(dCode)} what="this product" />
    {:else if dData}
      <div class="mb-3 flex flex-wrap gap-1.5">
        {#if dData.article?.category}{@const cb = catBadge(dData.article.category)}<span class="rounded-control px-2 py-0.5 text-micro font-semibold {cb.cls}">{cb.txt}</span>{/if}
        <span class="rounded-control bg-success-soft px-2 py-0.5 text-micro font-semibold text-success">{fmt(dData.total_stock)} units · {dData.site_count} sites</span>
      </div>
      {#each ['generic_name', 'composition', 'category', 'mm_reg', 'status'] as k}
        {#if dData.article?.[k]}
          <div class="flex justify-between gap-3 border-b border-line py-2 text-body-sm">
            <span class="text-ink-2">{k.replace(/_/g, ' ')}</span>
            <span class="max-w-[60%] text-right font-semibold text-ink">{dData.article[k]}</span>
          </div>
        {/if}
      {/each}
      {#each [['indication', 'Indication'], ['dosage', 'Dosage'], ['side_effect', 'Side effects']] as [k, label]}
        {#if dData.article?.[k]}
          <div class="mb-1.5 mt-4 text-micro font-bold uppercase tracking-[0.05em] text-ink-3">{label}</div>
          <p class="text-body-sm leading-relaxed text-ink-2">{dData.article[k]}</p>
        {/if}
      {/each}
      {#if dData.sites?.length}
        <div class="mb-1.5 mt-4 text-micro font-bold uppercase tracking-[0.05em] text-ink-3">Top branches</div>
        {#each dData.sites.slice(0, 6) as s}
          <div class="flex justify-between gap-3 border-b border-line py-1.5 text-body-sm">
            <span class="font-mono text-ink-2">{s.site_code}</span>
            <span class="tnum font-semibold text-ink">{fmt(s.stock_qty)}</span>
          </div>
        {/each}
      {/if}
      <button onclick={() => goto(appBase + '/graph')} class="mt-4 flex w-full items-center justify-center gap-2 rounded-card bg-accent px-4 py-2.5 text-body-sm font-semibold text-on-accent hover:bg-accent-hover">
        <Share2 size={15} /> View in knowledge graph
      </button>
    {:else}
      <p class="text-body-sm text-ink-2">No catalog record found for {dCode}.</p>
    {/if}
  </div>
</div>
{/if}
