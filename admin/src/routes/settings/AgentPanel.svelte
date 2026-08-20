<script>
  import { API_BASE } from '$lib/apiBase.js';
  import { onMount } from 'svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';
  import ErrorState from '$lib/ErrorState.svelte';
  import { getJSON } from '$lib/api.js';

  const base = API_BASE;

  let loading = $state(true);
  let error = $state(null);
  let config = $state({});

  // Editable system prompt. Kept separate so re-renders never clobber edits.
  let promptText = $state('');
  let promptLoaded = $state(false);
  let promptOverridden = $state(false);

  let saving = $state(false);
  let saveNote = $state(null);
  let saveError = $state(null);

  async function load() {
    loading = true;
    error = null;
    try {
      const data = await getJSON('/admin/config');
      config = data ?? {};
      promptOverridden = !!config.prompt_overridden;
      // Only seed the textarea once, so we never overwrite live user edits.
      if (!promptLoaded) {
        promptText = config.system_prompt ?? '';
        promptLoaded = true;
      }
    } catch (e) {
      // Keep the ApiError itself: its status is what tells an expired session
      // apart from a backend that is genuinely not there.
      error = e;
    } finally {
      loading = false;
    }
  }

  async function savePrompt() {
    saving = true;
    saveNote = null;
    saveError = null;
    try {
      const res = await fetch(base + '/admin/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ system_prompt: promptText })
      });
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      const data = await res.json();
      saveNote = data?.note || 'Saved.';
      promptOverridden = true;
    } catch (e) {
      saveError = e?.message === 'Failed to fetch' ? 'backend offline' : (e.message || 'save failed');
    } finally {
      saving = false;
    }
  }

  onMount(load);

  const dash = (v) => (v === null || v === undefined || v === '' ? '–' : v);

  // Env-sourced, read-only config cards.
  let envRows = $derived([
    { label: 'Model', value: config.model },
    { label: 'Embedding model', value: config.embedding_model },
    { label: 'Rate limit', value: config.rate_limit_per_min == null ? null : `${config.rate_limit_per_min} / min` },
    { label: 'Cache TTL', value: config.cache_ttl_seconds == null ? null : `${config.cache_ttl_seconds} s` }
  ]);
</script>

<PageHeader
  level={2}
  title="Agent config"
  subtitle="Which model answers, and the knobs that change how it reasons."
/>

{#if loading}
  <p class="text-body-sm text-ink-2">Loading configuration…</p>
{:else if error}
  <ErrorState {error} retry={load} what="the agent configuration" />
{:else}
  <!-- Runtime settings (read-only, from .env) -->
  <div class="mb-4 grid grid-cols-2 gap-3">
    {#each envRows as row}
      <div class="rounded-panel border border-line bg-surface p-3.5">
        <div class="mb-1 text-meta text-ink-2">
          {row.label} <span class="text-ink-3">· .env</span>
        </div>
        <div class="text-body font-medium tabular-nums">{dash(row.value)}</div>
      </div>
    {/each}
  </div>

  <!-- System prompt (editable) -->
  <div class="rounded-panel border border-line bg-surface p-4">
    <div class="mb-2.5 flex items-center gap-2">
      <span id="agent-system-prompt" class="text-body-sm font-medium">System prompt</span>
      {#if promptOverridden}
        <Badge tone="warn">custom active</Badge>
      {/if}
    </div>

    <textarea
      bind:value={promptText}
      aria-labelledby="agent-system-prompt"
      spellcheck="false"
      placeholder="Define how the agent should behave…"
      class="h-[150px] w-full resize-y rounded-panel border border-line bg-page px-3 py-2.5 font-mono text-meta leading-relaxed text-ink outline-none focus:border-accent"
    ></textarea>

    <div class="mt-2.5 flex items-center gap-2.5">
      <button
        onclick={savePrompt}
        disabled={saving}
        class="rounded-panel bg-accent px-3.5 py-2 text-body-sm font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
      >
        {saving ? 'Saving' : 'Save prompt'}
      </button>
      <span class="text-meta text-ink-3">applied on next restart</span>

      {#if saveNote}
        <span class="text-body-sm text-success">Saved · {saveNote}</span>
      {:else if saveError}
        <span class="text-body-sm text-danger">{saveError}</span>
      {/if}
    </div>
  </div>
{/if}
