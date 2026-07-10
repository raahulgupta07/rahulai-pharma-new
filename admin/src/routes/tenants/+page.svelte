<script>
  import { API_BASE } from '$lib/apiBase.js';
  import { onMount } from 'svelte';
  import { Trash2, Plus, Info } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';
  import Modal from '$lib/aurora/Modal.svelte';
  import { toast } from '$lib/aurora/toast.js';

  const BASE = API_BASE;

  let delId = $state(null);
  let delOpen = $state(false);

  let loading = $state(true);
  let error = $state(null);
  let creds = $state([]);

  let embedId = $state('');
  let publicKey = $state('');
  let adding = $state(false);
  let formError = $state(null);

  async function load() {
    loading = true;
    error = null;
    try {
      const res = await fetch(`${BASE}/admin/credentials`);
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      const data = await res.json();
      creds = Array.isArray(data) ? data : [];
    } catch (e) {
      error = e.message || 'backend offline';
    } finally {
      loading = false;
    }
  }

  async function add(e) {
    e.preventDefault();
    formError = null;
    const id = embedId.trim();
    const key = publicKey.trim();
    if (!id || !key) {
      formError = 'Both fields are required.';
      return;
    }
    adding = true;
    try {
      const res = await fetch(`${BASE}/admin/credentials`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ embed_id: id, public_key: key })
      });
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      toast(`Added ${id}`);
      embedId = '';
      publicKey = '';
      await load();
    } catch (e) {
      formError = e.message || 'could not add credential';
    } finally {
      adding = false;
    }
  }

  function remove(id) {
    delId = id;
    delOpen = true;
  }

  async function doRemove() {
    const id = delId;
    if (!id) return;
    try {
      const res = await fetch(`${BASE}/admin/credentials/${encodeURIComponent(id)}`, {
        method: 'DELETE'
      });
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      toast(`Deleted ${id}`, 'trash-2');
      await load();
    } catch (e) {
      error = e.message || 'could not delete credential';
    }
  }

  onMount(load);
</script>

<PageHeader
  title="Tenants"
  subtitle="Embed credentials for client sites. Point the widget's CITYAGENT_BASE_URL here."
/>

{#if loading}
  <p class="text-[14px] text-ink-2">Loading credentials…</p>
{:else if error}
  <div class="rounded-xl border border-line bg-surface px-5 py-6 text-[14px] text-ink-2">
    <p class="font-medium text-ink">Backend offline</p>
    <p class="mt-1">
      Could not reach the agent at <span class="text-ink">{API_BASE}</span>. Start the
      backend and reload.
    </p>
    <button
      onclick={load}
      class="mt-4 rounded-lg border border-line px-3 py-1.5 text-[13px] font-medium text-ink hover:bg-surface-2"
    >
      Retry
    </button>
  </div>
{:else}
  <!-- Add credential -->
  <div class="mb-4 rounded-[14px] border border-line bg-surface p-4">
    <div class="mb-2.5 text-[13px] font-medium text-ink">Add credential</div>
    <form onsubmit={add} class="flex flex-col gap-2.5 sm:flex-row sm:items-center">
      <input
        bind:value={embedId}
        aria-label="Embed ID"
        placeholder="embed_id"
        class="w-full flex-1 rounded-lg border border-line bg-page px-3 py-2 text-[14px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
      />
      <input
        bind:value={publicKey}
        aria-label="Public key"
        placeholder="public_key"
        class="w-full flex-1 rounded-lg border border-line bg-page px-3 py-2 font-mono text-[13px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
      />
      <button
        type="submit"
        disabled={adding}
        class="inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg bg-accent px-3.5 py-2 text-[13px] font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
      >
        <Plus size={15} />
        {adding ? 'Adding' : 'Add'}
      </button>
    </form>
    {#if formError}
      <p class="mt-3 text-[13px] text-danger">{formError}</p>
    {/if}
  </div>

  <!-- Credentials table -->
  <div class="overflow-hidden rounded-[14px] border border-line bg-surface">
    {#if creds.length === 0}
      <div class="px-6 py-10 text-center text-[14px] text-ink-2">
        No credentials yet. Add one above to start gating embed access.
      </div>
    {:else}
      <table class="tbl">
        <thead>
          <tr>
            <th>embed_id</th>
            <th>public_key</th>
            <th style="text-align:right">Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each creds as c (c.embed_id)}
            <tr>
              <td class="text-ink">{c.embed_id}</td>
              <td class="font-mono text-ink-2">{c.public_key}</td>
              <td style="text-align:right">
                <button
                  onclick={() => remove(c.embed_id)}
                  title="Delete credential"
                  class="inline-flex items-center rounded-lg p-1.5 text-ink-3 transition-colors hover:text-danger"
                >
                  <Trash2 size={15} />
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>

  <!-- Auth mode note -->
  <div class="mt-3.5 flex items-center gap-2 rounded-lg bg-info-soft p-3 text-[13px] text-info">
    <Info size={15} class="shrink-0" />
    {#if creds.length === 0}
      <span>
        Open mode (dev). No credentials exist, so every embed request is allowed. Add a
        credential to lock access down.
      </span>
    {:else}
      <span>
        With credentials registered, only matching embed_id + public_key are allowed.
      </span>
    {/if}
  </div>
{/if}

<Modal
  bind:open={delOpen}
  title="Delete credential"
  confirmLabel="Delete"
  tone="danger"
  onconfirm={doRemove}
>
  Delete <b class="text-ink">{delId}</b>? Client sites using this embed_id will immediately lose
  access.
</Modal>
