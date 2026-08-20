<script>
  import { API_BASE } from '$lib/apiBase.js';
  import { onMount } from 'svelte';
  import { Cpu, ShieldAlert, Network, Layers, Check } from '@lucide/svelte';
  import SettingRow from '$lib/aurora/SettingRow.svelte';
  import ErrorState from '$lib/ErrorState.svelte';
  import { getJSON } from '$lib/api.js';
  import { toast } from '$lib/aurora/toast.js';
  import PageHeader from '$lib/PageHeader.svelte';

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

  // ---- answer length (REAL, backend-backed) --------------------------------
  // Unlike the local-preference switches below, this changes the agent's system
  // prompt server-side and takes effect on the next question.
  let answerStyle = $state('standard');
  let styleBusy = $state(false);
  const STYLE_META = {
    crisp: { label: 'Crisp', desc: 'One short line — name, code, and the number asked. No tables unless comparing.' },
    standard: { label: 'Standard', desc: 'A lead sentence plus a table for multi-row results. The default.' },
    detailed: { label: 'Detailed', desc: 'Adds brief indication/dosage context and lists more results.' }
  };

  async function loadStyle() {
    try {
      const res = await fetch(base + '/admin/answer-style');
      if (!res.ok) return; // 403 for a non-super-admin — card just shows default
      const data = await res.json();
      answerStyle = data.style ?? 'standard';
    } catch {}
  }

  async function setStyle(s) {
    if (s === answerStyle || styleBusy) return;
    styleBusy = true;
    const prev = answerStyle;
    answerStyle = s; // optimistic
    try {
      const res = await fetch(base + '/admin/answer-style', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ style: s })
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `failed (${res.status})`);
      answerStyle = body.style;
      toast(`Answers set to ${STYLE_META[body.style].label} · live now`);
    } catch (e) {
      answerStyle = prev;
      toast(e.message || 'could not change answer length', 'shield-alert');
    } finally {
      styleBusy = false;
    }
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
      // The thrown ApiError carries the status; ErrorState needs the object.
      error = e;
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
    loadStyle();
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

<PageHeader
  level={2}
  subtitle="Per-deployment switches. Changes apply immediately — no redeploy — so one deployment can be tuned per client."
  title="Answer behaviour"
/>

<!-- Answer length (real, server-side) -->
<div class="mb-6 rounded-card border border-line bg-surface p-4">
  <div class="text-body-sm font-semibold text-ink">Answer length</div>
  <p class="mb-3 mt-0.5 text-body-sm text-ink-2">
    How long answers are, everywhere the agent replies — chat and the embedded widget. Applies to
    the next question; no redeploy.
  </p>
  <div class="grid gap-2 sm:grid-cols-3">
    {#each ['crisp', 'standard', 'detailed'] as s}
      <button
        onclick={() => setStyle(s)}
        disabled={styleBusy}
        class="rounded-card border p-3 text-left transition-colors disabled:opacity-60 {answerStyle === s
          ? 'border-accent bg-accent-soft'
          : 'border-line bg-surface-2 hover:bg-surface'}"
      >
        <div class="mb-0.5 flex items-center gap-1.5 text-body-sm font-medium text-ink">
          {#if answerStyle === s}<Check size={14} class="text-accent" />{/if}
          {STYLE_META[s].label}
          {#if s === 'standard'}<span class="text-label font-normal text-ink-3">default</span>{/if}
        </div>
        <div class="text-meta leading-snug text-ink-2">{STYLE_META[s].desc}</div>
      </button>
    {/each}
  </div>
</div>

<div class="my-6 h-px bg-line"></div>

<!-- Inline citations -->
<SettingRow title="Inline citations" bind:checked={prefs.citations} onchange={onToggle}>
  When ON, answers cite the exact source inline
  <span
    class="rounded-control border border-accent/30 bg-accent-soft px-1.5 py-px text-label font-semibold text-accent"
    >inventory</span
  > and show clickable source coins. When OFF, answers are clean prose with no markers — good for
  client-facing widgets where sources shouldn't show.
</SettingRow>

<!-- preview -->
<div class="mb-1.5 mt-5 text-micro font-bold uppercase tracking-[0.1em] text-ink-3">Preview</div>
<div class="rounded-panel border border-line bg-surface-2 px-[18px] py-4 text-body-sm leading-relaxed text-ink-2">
  PARACAP PARACETAMOL — total stock <b class="text-ink">14,963</b> units across 53 sites{#if prefs.citations}<sup
      class="text-micro font-semibold text-accent">[inventory]</sup
    >{/if}. Substitutes: BIOGESIC, PANADOL{#if prefs.citations}<sup
      class="text-micro font-semibold text-accent">[catalog]</sup
    >{/if}.
  {#if prefs.citations}
    <div class="mt-2.5 flex gap-2">
      <span
        class="rounded-control border border-accent/30 bg-accent-soft px-1.5 py-0.5 text-label font-semibold text-accent"
        >1 · inventory balance</span
      >
      <span
        class="rounded-control border border-accent/30 bg-accent-soft px-1.5 py-0.5 text-label font-semibold text-accent"
        >2 · catalog</span
      >
    </div>
  {/if}
</div>

<div class="my-7 h-px bg-line"></div>

<h2 class="page-title text-title text-ink">Bilingual &amp; safety</h2>
<p class="mb-4 mt-1.5 text-body-sm text-ink-2">How the agent handles language and medical guardrails.</p>

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

<h2 class="page-title text-title text-ink">Knowledge schema</h2>
<p class="mb-[18px] mt-1.5 text-body-sm text-ink-2">
  Conventions the agent uses to build the drug graph and detect contradictions.
</p>
<div class="grid gap-[18px] sm:grid-cols-2">
  <div>
    <label class="mb-1.5 block text-body-sm font-semibold text-ink" for="fresh">
      Freshness window <span class="font-normal text-ink-3">— stale past this (days)</span>
    </label>
    <input
      id="fresh"
      type="number"
      bind:value={freshnessDays}
      class="w-full rounded-card border border-line bg-surface px-3.5 py-2.5 text-body-sm tnum text-ink outline-none focus:border-accent"
    />
  </div>
  <div>
    <label class="mb-1.5 block text-body-sm font-semibold text-ink" for="stockout">
      Stock-out threshold <span class="font-normal text-ink-3">— flag SKU below this</span>
    </label>
    <input
      id="stockout"
      type="number"
      bind:value={stockoutThreshold}
      class="w-full rounded-card border border-line bg-surface px-3.5 py-2.5 text-body-sm tnum text-ink outline-none focus:border-accent"
    />
  </div>
</div>
<div class="mt-4">
  <label class="mb-1.5 block text-body-sm font-semibold text-ink" for="ent">
    Entity types <span class="font-normal text-ink-3">(comma-separated — graph nodes)</span>
  </label>
  <input
    id="ent"
    bind:value={entityTypes}
    class="w-full rounded-card border border-line bg-surface px-3.5 py-2.5 text-body-sm text-ink outline-none focus:border-accent"
  />
</div>
<div class="mt-4">
  <label class="mb-1.5 block text-body-sm font-semibold text-ink" for="con">
    Contradiction attributes <span class="font-normal text-ink-3">(compared across sources)</span>
  </label>
  <input
    id="con"
    bind:value={contradictionAttrs}
    class="w-full rounded-card border border-line bg-surface px-3.5 py-2.5 text-body-sm text-ink outline-none focus:border-accent"
  />
</div>

<button
  onclick={save}
  class="mt-5 inline-flex items-center gap-2 rounded-card bg-accent px-4 py-2.5 text-body-sm font-semibold text-on-accent hover:bg-accent-hover"
>
  <Check size={15} /> Save changes
</button>

<div class="my-7 h-px bg-line"></div>

<!-- ---- live system configuration (read-only, from backend) ---- -->
<h2 class="page-title text-title text-ink">System</h2>
<p class="mb-4 mt-1.5 text-body-sm text-ink-2">Live runtime configuration — read from the backend.</p>

{#if error}
  <ErrorState {error} retry={load} what="the runtime configuration" />
{:else}
  <div class="grid gap-3.5 lg:grid-cols-3">
    <section class="elev rounded-card border border-line bg-surface p-4">
      <div class="mb-2.5 flex items-center gap-2"><Cpu size={15} class="text-ink-2" /><h3 class="text-body-sm font-semibold text-ink">Models</h3></div>
      <dl class="space-y-2 text-body-sm">
        <div class="flex justify-between gap-3"><dt class="text-ink-2">Chat</dt><dd class="text-right text-ink">{model}</dd></div>
        <div class="flex justify-between gap-3"><dt class="text-ink-2">Embedding</dt><dd class="text-right text-ink">{embeddingModel}</dd></div>
        <div class="flex justify-between gap-3"><dt class="text-ink-2">Rate limit</dt><dd class="text-ink tnum">{fmt(rateLimit)}{rateLimit !== null ? ' / min' : ''}</dd></div>
        <div class="flex justify-between gap-3"><dt class="text-ink-2">Cache TTL</dt><dd class="text-ink tnum">{fmt(cacheTtl)}{cacheTtl !== null ? ' s' : ''}</dd></div>
      </dl>
    </section>

    <section class="elev rounded-card border border-line bg-surface p-4">
      <div class="mb-2.5 flex items-center justify-between gap-2">
        <div class="flex items-center gap-2"><Layers size={15} class="text-ink-2" /><h3 class="text-body-sm font-semibold text-ink">Materialized views</h3></div>
        <button onclick={refreshViews} disabled={refreshing} class="rounded-panel border border-line px-2.5 py-1 text-meta text-ink hover:bg-surface-2 disabled:opacity-60">{refreshing ? '…' : 'Refresh'}</button>
      </div>
      <dl class="space-y-2 text-body-sm">
        <div class="flex justify-between gap-3"><dt class="text-ink-2">mv_store_summary</dt><dd class="text-ink tnum">{fmt(num(views?.mv_store_summary))}</dd></div>
        <div class="flex justify-between gap-3"><dt class="text-ink-2">mv_article_summary</dt><dd class="text-ink tnum">{fmt(num(views?.mv_article_summary))}</dd></div>
      </dl>
    </section>

    <section class="rounded-card border border-warning bg-surface p-4">
      <div class="mb-2.5 flex items-center gap-2"><ShieldAlert size={15} class="text-warning" /><h3 class="text-body-sm font-semibold text-warning">Security checklist</h3></div>
      <ul class="list-disc space-y-1.5 pl-4 text-meta leading-relaxed text-ink-2 marker:text-warning">
        <li>Rotate the OpenRouter key if ever shared.</li>
        <li>32-byte SECRET_KEY matching Laravel.</li>
        <li>SFTP key-auth only in production.</li>
        <li>Tighten CORS before public deploy.</li>
      </ul>
    </section>
  </div>
{/if}
