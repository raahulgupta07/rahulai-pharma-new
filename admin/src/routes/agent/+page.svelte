<script>
  import { onMount } from 'svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';

  const base = 'http://localhost:8088';

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
      const res = await fetch(base + '/admin/config');
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      const data = await res.json();
      config = data ?? {};
      promptOverridden = !!config.prompt_overridden;
      // Only seed the textarea once, so we never overwrite live user edits.
      if (!promptLoaded) {
        promptText = config.system_prompt ?? '';
        promptLoaded = true;
      }
    } catch (e) {
      error = e?.message === 'Failed to fetch' ? 'backend offline' : (e.message || 'backend offline');
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

<PageHeader title="Agent config" />

{#if loading}
  <p class="text-[14px] text-ink-2">Loading configuration…</p>
{:else if error}
  <div class="rounded-xl border border-line bg-surface px-5 py-6 text-[14px] text-ink-2">
    <p class="font-medium text-ink">Backend offline</p>
    <p class="mt-1">
      Could not reach the agent at <span class="text-ink">localhost:8088</span>.
      Start the backend and reload.
    </p>
    <button
      onclick={load}
      class="mt-4 rounded-lg border border-line px-3 py-1.5 text-[13px] font-medium text-ink hover:bg-surface-2"
    >
      Retry
    </button>
  </div>
{:else}
  <!-- Runtime settings (read-only, from .env) -->
  <div class="mb-4 grid grid-cols-2 gap-3">
    {#each envRows as row}
      <div class="rounded-xl border border-line bg-surface p-3.5">
        <div class="mb-1 text-[12px] text-ink-2">
          {row.label} <span class="text-ink-3">· .env</span>
        </div>
        <div class="text-[15px] font-medium tabular-nums">{dash(row.value)}</div>
      </div>
    {/each}
  </div>

  <!-- System prompt (editable) -->
  <div class="rounded-xl border border-line bg-surface p-4">
    <div class="mb-2.5 flex items-center gap-2">
      <span class="text-[14px] font-medium">System prompt</span>
      {#if promptOverridden}
        <Badge tone="warn">custom active</Badge>
      {/if}
    </div>

    <textarea
      bind:value={promptText}
      spellcheck="false"
      placeholder="Define how the agent should behave…"
      class="h-[150px] w-full resize-y rounded-lg border border-line bg-page px-3 py-2.5 font-mono text-[12.5px] leading-relaxed text-ink outline-none focus:border-accent"
    ></textarea>

    <div class="mt-2.5 flex items-center gap-2.5">
      <button
        onclick={savePrompt}
        disabled={saving}
        class="rounded-lg bg-accent px-3.5 py-2 text-[13px] font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-60"
      >
        {saving ? 'Saving' : 'Save prompt'}
      </button>
      <span class="text-[12px] text-ink-3">applied on next restart</span>

      {#if saveNote}
        <span class="text-[13px] text-success">Saved · {saveNote}</span>
      {:else if saveError}
        <span class="text-[13px] text-danger">{saveError}</span>
      {/if}
    </div>
  </div>
{/if}
