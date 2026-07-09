<script>
  import { onMount, onDestroy } from 'svelte';
  import { Upload, RefreshCw, Server, Check, AlertTriangle, Clock, FileSpreadsheet } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';

  const base = 'http://localhost:8088';

  let status = $state(null);
  let error = $state(null);
  let loading = $state(true);
  let uploading = $state(false);
  let ingesting = $state(false);
  let msg = $state(null);
  let fileInput;
  let timer;

  async function load() {
    error = null;
    try {
      const res = await fetch(base + '/admin/sftp');
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      status = await res.json();
    } catch (e) {
      error = 'backend offline';
    } finally {
      loading = false;
    }
  }

  async function onUpload(e) {
    const f = e.target.files?.[0];
    if (!f) return;
    uploading = true;
    msg = null;
    try {
      const fd = new FormData();
      fd.append('file', f);
      const res = await fetch(base + '/admin/upload', { method: 'POST', body: fd });
      if (!res.ok) throw new Error(`upload failed (${res.status})`);
      const j = await res.json();
      const done = (j.processed ?? []).map((x) => `${x.kind} ${Number(x.rows).toLocaleString()}`).join(', ');
      msg = `Uploaded ${j.file}${done ? ' → ' + done : ''}`;
      await load();
    } catch (err) {
      msg = err.message || 'upload failed';
    } finally {
      uploading = false;
      if (e.target) e.target.value = '';
    }
  }

  async function ingestNow() {
    ingesting = true;
    msg = null;
    try {
      const res = await fetch(base + '/api/embed/ingest', { method: 'POST' });
      if (!res.ok) throw new Error(`ingest failed (${res.status})`);
      const j = await res.json();
      msg = `Ingest run — ${(j.processed ?? []).length} processed, data v${j.data_version}`;
      await load();
    } catch (err) {
      msg = err.message || 'ingest failed';
    } finally {
      ingesting = false;
    }
  }

  onMount(() => {
    load();
    timer = setInterval(load, 10000);
  });
  onDestroy(() => clearInterval(timer));

  const size = (b) => (b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' KB');
  const when = (s) => new Date(s * 1000).toLocaleString();
</script>

<PageHeader
  title="SFTP uploads"
  subtitle="Drop article and balance-stock xlsx over SFTP, or upload manually. The worker ingests automatically and busts the cache."
>
  {#snippet actions()}
    <input type="file" accept=".xlsx" bind:this={fileInput} onchange={onUpload} class="hidden" />
    <button
      onclick={() => fileInput?.click()}
      disabled={uploading}
      class="inline-flex items-center gap-2 rounded-lg border border-line bg-surface px-3.5 py-2 text-[13px] font-medium text-ink transition-colors hover:bg-surface-2 disabled:opacity-60"
    >
      <Upload size={15} class={uploading ? 'animate-pulse' : ''} />
      {uploading ? 'Uploading' : 'Upload xlsx'}
    </button>
    <button
      onclick={ingestNow}
      disabled={ingesting}
      class="inline-flex items-center gap-2 rounded-lg bg-accent px-3.5 py-2 text-[13px] font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-60"
    >
      <RefreshCw size={15} class={ingesting ? 'animate-spin' : ''} />
      {ingesting ? 'Ingesting' : 'Ingest now'}
    </button>
  {/snippet}
</PageHeader>

{#if msg}
  <div class="mb-6 rounded-xl border border-line bg-surface px-4 py-3 text-[13px] text-ink-2">{msg}</div>
{/if}

{#if error}
  <div class="rounded-xl border border-line bg-surface px-5 py-4 text-[14px] text-ink-2">
    <p class="font-medium text-ink">Backend offline</p>
    <p class="mt-1">Could not reach the agent at localhost:8088.</p>
  </div>
{:else if loading}
  <p class="text-[14px] text-ink-2">Loading…</p>
{:else if status}
  <!-- connection -->
  <section class="mb-8 rounded-xl border border-line bg-surface px-5 py-4">
    <div class="mb-3 flex items-center gap-2 text-[14px] font-medium text-ink">
      <Server size={16} /> Connection
    </div>
    <div class="grid gap-x-8 gap-y-2 text-[13px] sm:grid-cols-2">
      <div class="flex justify-between gap-4"><span class="text-ink-2">Host</span><span class="text-ink">{status.connection.host}</span></div>
      <div class="flex justify-between gap-4"><span class="text-ink-2">Port</span><span class="text-ink tnum">{status.connection.port}</span></div>
      <div class="flex justify-between gap-4"><span class="text-ink-2">User</span><span class="text-ink">{status.connection.user}</span></div>
      <div class="flex justify-between gap-4"><span class="text-ink-2">Path</span><span class="text-ink">{status.connection.path}</span></div>
    </div>
    <p class="mt-3 border-t border-line pt-3 font-mono text-[12px] text-ink-2">
      sftp -P {status.connection.port} {status.connection.user}@{status.connection.host}
    </p>
    <p class="mt-1 text-[12px] text-ink-3">Auto-ingest polls every {status.poll_seconds}s · key or password auth.</p>
  </section>

  <!-- file lists -->
  {#each [['Pending', status.pending, Clock], ['Archived', status.archived, Check], ['Failed', status.failed, AlertTriangle]] as [title, rows, Icon]}
    <section class="mb-6">
      <div class="mb-2 flex items-center gap-2 text-[14px] font-medium text-ink">
        <Icon size={15} /> {title}
        <Badge>{rows.length}</Badge>
      </div>
      <div class="overflow-hidden rounded-xl border border-line bg-surface">
        {#if rows.length === 0}
          <p class="px-5 py-4 text-[13px] text-ink-3">None.</p>
        {:else}
          <div class="max-h-[240px] overflow-y-auto">
          <table class="tbl">
            <thead>
              <tr>
                <th>File</th>
                <th class="num">Size</th>
                <th class="num">Modified</th>
              </tr>
            </thead>
            <tbody>
              {#each rows as f (f.name)}
                <tr>
                  <td class="text-ink"><FileSpreadsheet size={14} class="mr-1.5 inline align-text-bottom text-ink-3" />{f.name}</td>
                  <td class="num text-ink-2">{size(f.size)}</td>
                  <td class="num text-ink-2">{when(f.mtime)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
          </div>
        {/if}
      </div>
    </section>
  {/each}
{/if}
