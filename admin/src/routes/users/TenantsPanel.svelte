<script>
  import { onMount } from 'svelte';
  import { Trash2, Plus, Info, Globe, Lock } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';
  import Modal from '$lib/aurora/Modal.svelte';
  import { toast } from '$lib/aurora/toast.js';
  import { getJSON } from '$lib/api.js';
  import ErrorState from '$lib/ErrorState.svelte';

  let delId = $state(null);
  let delOpen = $state(false);

  let loading = $state(true);
  let error = $state(null);
  let creds = $state([]);

  /**
   * A one-line, status-aware reason an ACTION failed, for the inline messages
   * next to a form (a whole ErrorState panel would be too heavy there).
   * Like ErrorState, it never blames a stopped backend for an answered request.
   */
  function reason(e, verb) {
    const s = Number(e?.status ?? 0);
    if (s === 401) return `Your sign-in has timed out. Sign in again, then ${verb}.`;
    if (s === 403) return `Your account is not permitted to ${verb}.`;
    if (s > 0) return e?.message || `The backend answered ${s}.`;
    return `No response from the backend — could not ${verb}.`;
  }

  let embedId = $state('');
  let publicKey = $state('');
  let adding = $state(false);
  let formError = $state(null);

  async function load() {
    loading = true;
    error = null;
    try {
      const data = await getJSON('/admin/credentials');
      creds = Array.isArray(data) ? data : [];
    } catch (e) {
      // The object, not the message: 401 and a dead backend are different pages.
      error = e;
      creds = [];
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
      await getJSON('/admin/credentials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ embed_id: id, public_key: key })
      });
      toast(`Added ${id}`);
      embedId = '';
      publicKey = '';
      await load();
    } catch (e) {
      formError = reason(e, 'add this credential');
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

  // `corsLoadError` is the LIST failing to load — the table must not then claim
  // "No origins allowed yet", which reads as "nothing is allowed" when in fact
  // nothing was read. `corsError` stays the inline message for the add/remove
  // form actions.
  let corsLoadError = $state(null);

  async function loadCors() {
    corsError = null;
    corsLoadError = null;
    try {
      const data = await getJSON('/admin/cors-origins');
      corsEnv = data.env ?? [];
      corsRuntime = data.runtime ?? [];
    } catch (e) {
      corsEnv = [];
      corsRuntime = [];
      corsLoadError = e;
    }
  }

  async function addOrigin(e) {
    e.preventDefault();
    corsError = null;
    const o = originInput.trim();
    if (!o) return;
    corsAdding = true;
    try {
      const body = await getJSON('/admin/cors-origins', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ origin: o })
      });
      toast(`Allowed ${body.origin}`);
      originInput = '';
      await loadCors();
    } catch (e) {
      corsError = reason(e, 'allow this origin');
    } finally {
      corsAdding = false;
    }
  }

  async function removeOrigin(origin) {
    corsError = null;
    try {
      await getJSON(`/admin/cors-origins?origin=${encodeURIComponent(origin)}`, {
        method: 'DELETE'
      });
      toast(`Removed ${origin}`, 'trash-2');
      await loadCors();
    } catch (e) {
      corsError = reason(e, 'remove this origin');
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
      await getJSON(`/admin/credentials/${encodeURIComponent(id)}`, { method: 'DELETE' });
      toast(`Deleted ${id}`, 'trash-2');
      await load();
    } catch (e) {
      // A failed delete is an action failure, not a broken page — say so in a
      // toast and leave the (still valid) list on screen.
      toast(reason(e, 'delete this credential'), 'alert-triangle');
    }
  }

  onMount(() => {
    load();
    loadCors();
  });
</script>

<PageHeader
  level={2}
  title="Tenants"
  subtitle="Embed credentials for client sites. Point the widget's CITYAGENT_BASE_URL here."
/>

{#if loading}
  <p class="text-body-sm text-ink-2">Loading credentials…</p>
{:else if error}
  <ErrorState {error} retry={load} what="embed credentials" />
{:else}
  <!-- Add credential -->
  <div class="mb-4 rounded-panel border border-line bg-surface p-4">
    <div class="mb-2.5 text-body-sm font-medium text-ink">Add credential</div>
    <form onsubmit={add} class="flex flex-col gap-2.5 sm:flex-row sm:items-center">
      <input
        bind:value={embedId}
        aria-label="Embed ID"
        placeholder="embed_id"
        class="w-full flex-1 rounded-panel border border-line bg-page px-3 py-2 text-body-sm text-ink outline-none placeholder:text-ink-3 focus:border-accent"
      />
      <input
        bind:value={publicKey}
        aria-label="Public key"
        placeholder="public_key"
        class="w-full flex-1 rounded-panel border border-line bg-page px-3 py-2 font-mono text-body-sm text-ink outline-none placeholder:text-ink-3 focus:border-accent"
      />
      <button
        type="submit"
        disabled={adding}
        class="inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-panel bg-accent px-3.5 py-2 text-body-sm font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
      >
        <Plus size={15} />
        {adding ? 'Adding' : 'Add'}
      </button>
    </form>
    {#if formError}
      <p class="mt-3 text-body-sm text-danger">{formError}</p>
    {/if}
  </div>

  <!-- Credentials table -->
  <div class="overflow-hidden rounded-panel border border-line bg-surface">
    {#if creds.length === 0}
      <div class="px-6 py-10 text-center text-body-sm text-ink-2">
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
                  aria-label={`Delete credential ${c.embed_id}`}
                  title="Delete credential"
                  class="inline-flex items-center rounded-panel p-1.5 text-ink-3 transition-colors hover:text-danger"
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
  <div class="mt-3.5 flex items-center gap-2 rounded-panel bg-info-soft p-3 text-body-sm text-info">
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
    <h2 class="text-title font-semibold text-ink">Allowed origins (CORS)</h2>
  </div>
  <p class="mb-4 max-w-[70ch] text-body-sm text-ink-2">
    A widget only loads if the <b class="text-ink">website it sits on</b> is listed here — this is
    separate from the credential above. Add the exact origin the browser sends:
    scheme + host + port, no path (e.g. <code class="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-meta text-accent">http://localhost:8000</code>).
    Runtime origins take effect within seconds — no restart.
  </p>

  <div class="mb-4 rounded-panel border border-line bg-surface p-4">
    <div class="mb-2.5 text-body-sm font-medium text-ink">Add origin</div>
    <form onsubmit={addOrigin} class="flex flex-col gap-2.5 sm:flex-row sm:items-center">
      <input
        bind:value={originInput}
        aria-label="Origin"
        placeholder="https://shop.example.com"
        class="w-full flex-1 rounded-panel border border-line bg-page px-3 py-2 font-mono text-body-sm text-ink outline-none placeholder:text-ink-3 focus:border-accent"
      />
      <button
        type="submit"
        disabled={corsAdding}
        class="inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-panel bg-accent px-3.5 py-2 text-body-sm font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
      >
        <Plus size={15} />
        {corsAdding ? 'Adding' : 'Allow'}
      </button>
    </form>
    {#if corsError}
      <p class="mt-3 text-body-sm text-danger">{corsError}</p>
    {/if}
  </div>

  {#if corsLoadError}
    <!-- The list never loaded. Rendering the empty table here would read as
         "no origins are allowed", which is a claim about the CORS policy we
         cannot make. -->
    <ErrorState error={corsLoadError} retry={loadCors} what="the allowed origins" />
  {:else}
  <div class="overflow-hidden rounded-panel border border-line bg-surface">
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
                aria-label={`Remove allowed origin ${o}`}
                title="Remove origin"
                class="inline-flex items-center rounded-panel p-1.5 text-ink-3 transition-colors hover:text-danger"
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
              <span class="inline-flex items-center gap-1 text-meta text-ink-3">
                <Lock size={12} /> env
              </span>
            </td>
            <td style="text-align:right" class="text-meta text-ink-3">ALLOWED_ORIGINS</td>
          </tr>
        {/each}
        {#if corsRuntime.length === 0 && corsEnv.length === 0}
          <tr><td colspan="3" class="px-6 py-8 text-center text-body-sm text-ink-2">No origins allowed yet.</td></tr>
        {/if}
      </tbody>
    </table>
  </div>
  {/if}
  <div class="mt-3.5 flex items-start gap-2 rounded-panel bg-info-soft p-3 text-body-sm text-info">
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
