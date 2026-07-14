<script>
  import { API_BASE } from '$lib/apiBase.js';
  import { onMount } from 'svelte';
  import { Trash2, Plus, Info, Globe, Lock } from '@lucide/svelte';
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

  // ---- CORS allowed origins ------------------------------------------------
  // The other gate: a browser widget can only call the embed API if its page
  // origin is allowed here. Env origins are read-only (need a restart); runtime
  // origins are added below and take effect within seconds.
  let corsEnv = $state([]);
  let corsRuntime = $state([]);
  let originInput = $state('');
  let corsAdding = $state(false);
  let corsError = $state(null);

  async function loadCors() {
    corsError = null;
    try {
      const res = await fetch(`${BASE}/admin/cors-origins`);
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      const data = await res.json();
      corsEnv = data.env ?? [];
      corsRuntime = data.runtime ?? [];
    } catch (e) {
      corsError = e.message || 'backend offline';
    }
  }

  async function addOrigin(e) {
    e.preventDefault();
    corsError = null;
    const o = originInput.trim();
    if (!o) return;
    corsAdding = true;
    try {
      const res = await fetch(`${BASE}/admin/cors-origins`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ origin: o })
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `request failed (${res.status})`);
      toast(`Allowed ${body.origin}`);
      originInput = '';
      await loadCors();
    } catch (e) {
      corsError = e.message || 'could not add origin';
    } finally {
      corsAdding = false;
    }
  }

  async function removeOrigin(origin) {
    corsError = null;
    try {
      const res = await fetch(`${BASE}/admin/cors-origins?origin=${encodeURIComponent(origin)}`, {
        method: 'DELETE'
      });
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      toast(`Removed ${origin}`, 'trash-2');
      await loadCors();
    } catch (e) {
      corsError = e.message || 'could not remove origin';
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

  onMount(() => {
    load();
    loadCors();
  });
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
        No credentials registered — every embed request is rejected (fail-closed). Add one, or in
        dev the <code class="font-mono">web</code>/<code class="font-mono">web</code> credential is
        seeded automatically when the store is empty.
      </span>
    {:else}
      <span>
        With credentials registered, only matching embed_id + public_key are allowed.
      </span>
    {/if}
  </div>

  <!-- ============ CORS allowed origins ============ -->
  <div class="mt-8 mb-2 flex items-center gap-2">
    <Globe size={17} class="text-accent" />
    <h2 class="text-[17px] font-semibold text-ink">Allowed origins (CORS)</h2>
  </div>
  <p class="mb-4 max-w-[70ch] text-[13.5px] text-ink-2">
    A widget only loads if the <b class="text-ink">website it sits on</b> is listed here — this is
    separate from the credential above. Add the exact origin the browser sends:
    scheme + host + port, no path (e.g. <code class="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[12px] text-accent">http://localhost:8000</code>).
    Runtime origins take effect within seconds — no restart.
  </p>

  <div class="mb-4 rounded-[14px] border border-line bg-surface p-4">
    <div class="mb-2.5 text-[13px] font-medium text-ink">Add origin</div>
    <form onsubmit={addOrigin} class="flex flex-col gap-2.5 sm:flex-row sm:items-center">
      <input
        bind:value={originInput}
        aria-label="Origin"
        placeholder="https://shop.example.com"
        class="w-full flex-1 rounded-lg border border-line bg-page px-3 py-2 font-mono text-[13px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
      />
      <button
        type="submit"
        disabled={corsAdding}
        class="inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg bg-accent px-3.5 py-2 text-[13px] font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
      >
        <Plus size={15} />
        {corsAdding ? 'Adding' : 'Allow'}
      </button>
    </form>
    {#if corsError}
      <p class="mt-3 text-[13px] text-danger">{corsError}</p>
    {/if}
  </div>

  <div class="overflow-hidden rounded-[14px] border border-line bg-surface">
    <table class="tbl">
      <thead>
        <tr>
          <th>Origin</th>
          <th>Source</th>
          <th style="text-align:right">Actions</th>
        </tr>
      </thead>
      <tbody>
        {#each corsRuntime as o (o)}
          <tr>
            <td class="font-mono text-ink">{o}</td>
            <td><Badge>runtime</Badge></td>
            <td style="text-align:right">
              <button
                onclick={() => removeOrigin(o)}
                title="Remove origin"
                class="inline-flex items-center rounded-lg p-1.5 text-ink-3 transition-colors hover:text-danger"
              >
                <Trash2 size={15} />
              </button>
            </td>
          </tr>
        {/each}
        {#each corsEnv as o (o)}
          <tr>
            <td class="font-mono text-ink-2">{o}</td>
            <td>
              <span class="inline-flex items-center gap-1 text-[12px] text-ink-3">
                <Lock size={12} /> env
              </span>
            </td>
            <td style="text-align:right" class="text-[12px] text-ink-3">ALLOWED_ORIGINS</td>
          </tr>
        {/each}
        {#if corsRuntime.length === 0 && corsEnv.length === 0}
          <tr><td colspan="3" class="px-6 py-8 text-center text-[14px] text-ink-2">No origins allowed yet.</td></tr>
        {/if}
      </tbody>
    </table>
  </div>
  <div class="mt-3.5 flex items-start gap-2 rounded-lg bg-info-soft p-3 text-[13px] text-info">
    <Info size={15} class="mt-0.5 shrink-0" />
    <span>
      <b>env</b> origins come from <code class="font-mono">ALLOWED_ORIGINS</code> and need a restart
      to change. <b>runtime</b> origins are managed here and apply live. A CORS error in the
      browser console (“No 'Access-Control-Allow-Origin' header”) means the site's origin is not in
      this list.
    </span>
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
