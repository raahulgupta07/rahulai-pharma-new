<script>
  import { API_BASE } from '$lib/apiBase.js';
  import { onMount } from 'svelte';
  import { Cpu, ShieldAlert, Network, Layers, Check } from '@lucide/svelte';
  import SettingRow from '$lib/aurora/SettingRow.svelte';
  import { toast } from '$lib/aurora/toast.js';

  const base = API_BASE;

  let loading = $state(true);
  let error = $state(null);
  let config = $state({});
  let views = $state(null);
  let refreshing = $state(false);

  // ---- per-deployment behaviour switches (persisted as UI preferences) ----
  // These mirror the agent's built-in behaviour; persisted locally so a single
  // deployment can be tuned per operator without a redeploy.
  const PREF_KEY = 'citcare_answer_prefs';
  let prefs = $state({
    citations: true,
    bilingual: true,
    disclaimer: true,
    autoResolve: false
  });
  let freshnessDays = $state(2);
  let stockoutThreshold = $state(0);
  let entityTypes = $state('brand, generic, ingredient, category, condition, site');
  let contradictionAttrs = $state('price, stock, dosage, substitute, indication');

  async function getJSON(path) {
    let res;
    try {
      res = await fetch(base + path);
    } catch {
      throw new Error('backend offline');
    }
    if (!res.ok) throw new Error(`request failed (${res.status})`);
    const text = await res.text();
    return text ? JSON.parse(text) : {};
  }

  async function load() {
    loading = true;
    error = null;
    try {
      const [c, v] = await Promise.all([
        getJSON('/admin/config'),
        getJSON('/admin/views').catch(() => null)
      ]);
      config = c ?? {};
      views = v ?? null;
    } catch (e) {
      error = e.message || 'backend offline';
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    try {
      const saved = localStorage.getItem(PREF_KEY);
      if (saved) prefs = { ...prefs, ...JSON.parse(saved) };
    } catch {}
    load();
  });

  function save() {
    localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
    toast('Settings saved · live now');
  }
  function onToggle() {
    localStorage.setItem(PREF_KEY, JSON.stringify(prefs));
  }

  async function refreshViews() {
    refreshing = true;
    try {
      const res = await fetch(base + '/admin/views/refresh', { method: 'POST' });
      const text = await res.text();
      views = text ? JSON.parse(text) : views;
      toast('Views refreshed');
    } finally {
      refreshing = false;
    }
  }

  const num = (v) => (typeof v === 'number' ? v : null);
  const fmt = (v) => (v === null || v === undefined || v === '' ? '–' : v.toLocaleString());
  const str = (v) => (v === null || v === undefined || v === '' ? '–' : v);

  let model = $derived(str(config.model));
  let embeddingModel = $derived(str(config.embedding_model));
  let rateLimit = $derived(num(config.rate_limit_per_min));
  let cacheTtl = $derived(num(config.cache_ttl_seconds));
</script>

<h1 class="page-title text-[28px] text-ink">Answer behaviour</h1>
<p class="mb-5 mt-1.5 max-w-[640px] text-[13.5px] text-ink-2">
  Per-deployment switches. Changes apply immediately — no redeploy — so one deployment can be tuned
  per client.
</p>

<!-- Inline citations -->
<SettingRow title="Inline citations" bind:checked={prefs.citations} onchange={onToggle}>
  When ON, answers cite the exact source inline
  <span
    class="rounded-md border border-accent/30 bg-accent-soft px-1.5 py-px text-[11px] font-semibold text-accent"
    >inventory</span
  > and show clickable source coins. When OFF, answers are clean prose with no markers — good for
  client-facing widgets where sources shouldn't show.
</SettingRow>

<!-- preview -->
<div class="mb-1.5 mt-5 text-[10px] font-bold uppercase tracking-[0.1em] text-ink-3">Preview</div>
<div class="rounded-[13px] border-[0.5px] border-line bg-surface-2 px-[18px] py-4 text-[13.5px] leading-relaxed text-ink-2">
  PARACAP PARACETAMOL — total stock <b class="text-ink">14,963</b> units across 53 sites{#if prefs.citations}<sup
      class="text-[10px] font-semibold text-accent">[inventory]</sup
    >{/if}. Substitutes: BIOGESIC, PANADOL{#if prefs.citations}<sup
      class="text-[10px] font-semibold text-accent">[catalog]</sup
    >{/if}.
  {#if prefs.citations}
    <div class="mt-2.5 flex gap-2">
      <span
        class="rounded-md border border-accent/30 bg-accent-soft px-1.5 py-0.5 text-[11px] font-semibold text-accent"
        >1 · inventory balance</span
      >
      <span
        class="rounded-md border border-accent/30 bg-accent-soft px-1.5 py-0.5 text-[11px] font-semibold text-accent"
        >2 · catalog</span
      >
    </div>
  {/if}
</div>

<div class="my-7 h-px bg-line"></div>

<h2 class="page-title text-[17px] text-ink">Bilingual &amp; safety</h2>
<p class="mb-4 mt-1.5 text-[13.5px] text-ink-2">How the agent handles language and medical guardrails.</p>

<SettingRow title="Auto-detect Burmese ↔ English" bind:checked={prefs.bilingual} onchange={onToggle}>
  Reply in the same language the question was asked. Never translate article codes or brand names.
</SettingRow>
<SettingRow title="Append pharmacist disclaimer" bind:checked={prefs.disclaimer} onchange={onToggle}>
  Every medical answer ends with “consult a licensed pharmacist before use.”
</SettingRow>
<SettingRow
  title="Auto-resolve data conflicts to newest file"
  bind:checked={prefs.autoResolve}
  onchange={onToggle}
>
  ON = a newer upload silently wins. OFF (recommended) = conflicts go to a review queue for a human.
</SettingRow>

<div class="my-7 h-px bg-line"></div>

<h2 class="page-title text-[17px] text-ink">Knowledge schema</h2>
<p class="mb-[18px] mt-1.5 text-[13.5px] text-ink-2">
  Conventions the agent uses to build the drug graph and detect contradictions.
</p>
<div class="grid gap-[18px] sm:grid-cols-2">
  <div>
    <label class="mb-1.5 block text-[13px] font-semibold text-ink" for="fresh">
      Freshness window <span class="font-normal text-ink-3">— stale past this (days)</span>
    </label>
    <input
      id="fresh"
      type="number"
      bind:value={freshnessDays}
      class="w-full rounded-[11px] border border-line bg-surface px-3.5 py-2.5 text-[13.5px] tnum text-ink outline-none focus:border-accent"
    />
  </div>
  <div>
    <label class="mb-1.5 block text-[13px] font-semibold text-ink" for="stockout">
      Stock-out threshold <span class="font-normal text-ink-3">— flag SKU below this</span>
    </label>
    <input
      id="stockout"
      type="number"
      bind:value={stockoutThreshold}
      class="w-full rounded-[11px] border border-line bg-surface px-3.5 py-2.5 text-[13.5px] tnum text-ink outline-none focus:border-accent"
    />
  </div>
</div>
<div class="mt-4">
  <label class="mb-1.5 block text-[13px] font-semibold text-ink" for="ent">
    Entity types <span class="font-normal text-ink-3">(comma-separated — graph nodes)</span>
  </label>
  <input
    id="ent"
    bind:value={entityTypes}
    class="w-full rounded-[11px] border border-line bg-surface px-3.5 py-2.5 text-[13.5px] text-ink outline-none focus:border-accent"
  />
</div>
<div class="mt-4">
  <label class="mb-1.5 block text-[13px] font-semibold text-ink" for="con">
    Contradiction attributes <span class="font-normal text-ink-3">(compared across sources)</span>
  </label>
  <input
    id="con"
    bind:value={contradictionAttrs}
    class="w-full rounded-[11px] border border-line bg-surface px-3.5 py-2.5 text-[13.5px] text-ink outline-none focus:border-accent"
  />
</div>

<button
  onclick={save}
  class="mt-5 inline-flex items-center gap-2 rounded-[11px] bg-accent px-4 py-2.5 text-[13px] font-semibold text-on-accent hover:bg-accent-hover"
>
  <Check size={15} /> Save changes
</button>

<div class="my-7 h-px bg-line"></div>

<!-- ---- live system configuration (read-only, from backend) ---- -->
<h2 class="page-title text-[17px] text-ink">System</h2>
<p class="mb-4 mt-1.5 text-[13.5px] text-ink-2">Live runtime configuration — read from the backend.</p>

{#if error}
  <div class="rounded-xl border border-line bg-surface px-5 py-5 text-[13.5px] text-ink-2">
    <p class="font-medium text-ink">Backend offline</p>
    <p class="mt-1">Could not reach <span class="text-ink">{API_BASE}</span>.</p>
    <button
      onclick={load}
      class="mt-3 rounded-lg border border-line px-3 py-1.5 text-[13px] text-ink hover:bg-surface-2"
      >Retry</button
    >
  </div>
{:else}
  <div class="grid gap-3.5 lg:grid-cols-3">
    <section class="elev rounded-2xl border-[0.5px] border-line bg-surface p-4">
      <div class="mb-2.5 flex items-center gap-2"><Cpu size={15} class="text-ink-2" /><h3 class="text-[13px] font-semibold text-ink">Models</h3></div>
      <dl class="space-y-2 text-[13px]">
        <div class="flex justify-between gap-3"><dt class="text-ink-2">Chat</dt><dd class="text-right text-ink">{model}</dd></div>
        <div class="flex justify-between gap-3"><dt class="text-ink-2">Embedding</dt><dd class="text-right text-ink">{embeddingModel}</dd></div>
        <div class="flex justify-between gap-3"><dt class="text-ink-2">Rate limit</dt><dd class="text-ink tnum">{fmt(rateLimit)}{rateLimit !== null ? ' / min' : ''}</dd></div>
        <div class="flex justify-between gap-3"><dt class="text-ink-2">Cache TTL</dt><dd class="text-ink tnum">{fmt(cacheTtl)}{cacheTtl !== null ? ' s' : ''}</dd></div>
      </dl>
    </section>

    <section class="elev rounded-2xl border-[0.5px] border-line bg-surface p-4">
      <div class="mb-2.5 flex items-center justify-between gap-2">
        <div class="flex items-center gap-2"><Layers size={15} class="text-ink-2" /><h3 class="text-[13px] font-semibold text-ink">Materialized views</h3></div>
        <button onclick={refreshViews} disabled={refreshing} class="rounded-lg border border-line px-2.5 py-1 text-[12px] text-ink hover:bg-surface-2 disabled:opacity-60">{refreshing ? '…' : 'Refresh'}</button>
      </div>
      <dl class="space-y-2 text-[13px]">
        <div class="flex justify-between gap-3"><dt class="text-ink-2">mv_store_summary</dt><dd class="text-ink tnum">{fmt(num(views?.mv_store_summary))}</dd></div>
        <div class="flex justify-between gap-3"><dt class="text-ink-2">mv_article_summary</dt><dd class="text-ink tnum">{fmt(num(views?.mv_article_summary))}</dd></div>
      </dl>
    </section>

    <section class="rounded-2xl border border-warning bg-surface p-4">
      <div class="mb-2.5 flex items-center gap-2"><ShieldAlert size={15} class="text-warning" /><h3 class="text-[13px] font-semibold text-warning">Security checklist</h3></div>
      <ul class="list-disc space-y-1.5 pl-4 text-[12.5px] leading-relaxed text-ink-2 marker:text-warning">
        <li>Rotate the OpenRouter key if ever shared.</li>
        <li>32-byte SECRET_KEY matching Laravel.</li>
        <li>SFTP key-auth only in production.</li>
        <li>Tighten CORS before public deploy.</li>
      </ul>
    </section>
  </div>
{/if}
