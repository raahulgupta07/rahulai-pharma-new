<script>
  import { API_BASE } from '$lib/apiBase.js';
  import { onMount, onDestroy } from 'svelte';
  import {
    Upload,
    RefreshCw,
    Server,
    Check,
    AlertTriangle,
    Clock,
    FileSpreadsheet,
    FolderOpen,
    Copy,
    Eye,
    EyeOff,
    KeyRound,
    FileCheck2,
    Lock,
    Plus,
    Trash2
  } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';

  const base = API_BASE;

  // SFTP_PUBLIC_HOST is the authoritative answer. Without it the backend falls
  // back to the hostname THIS request arrived on (host_source="detected") — a
  // starting point, not a fact: behind a proxy, or when the sftp port is not
  // published on the same name as the console, it is simply wrong. So a detected
  // host is shown pre-filled and asks to be confirmed, and an operator override
  // beats it and persists per browser (same pattern as the embed page's
  // embed_public_base). We never invent a `<server>` placeholder.
  const HOST_KEY = 'sftp_public_host';

  let status = $state(null);
  let conn = $state(null);
  let connError = $state(null); // 403 => not a super_admin
  let error = $state(null);
  let loading = $state(true);
  let uploading = $state(false);
  let ingesting = $state(false);
  let msg = $state(null);
  let fileInput;
  let timer;

  let hostInput = $state('');
  let hostTouched = $state(false);
  let revealed = $state(false);
  let copied = $state('');

  // ---- partner keys --------------------------------------------------------
  let keys = $state([]);
  let keysError = $state(null); // e.g. the volume is not mounted (503)
  let keyLabel = $state('');
  let keyMaterial = $state('');
  let keyBusy = $state(false);
  let keyMsg = $state(null);
  let keyErr = $state(null);
  let confirmDelete = $state(null); // label pending confirmation

  async function loadKeys() {
    keysError = null;
    try {
      const res = await fetch(base + '/admin/sftp/keys');
      if (res.status === 403) return; // handled by connError
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        keysError = body.detail || `request failed (${res.status})`;
        keys = [];
        return;
      }
      keys = body;
    } catch (e) {
      keysError = 'backend offline';
    }
  }

  async function addKey() {
    keyBusy = true;
    keyErr = null;
    keyMsg = null;
    try {
      const res = await fetch(base + '/admin/sftp/keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: keyLabel.trim(), public_key: keyMaterial })
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `failed (${res.status})`);
      keyMsg = `${body.label} registered — ${body.fingerprint}. Live on their next connection.`;
      keyLabel = '';
      keyMaterial = '';
      await loadKeys();
    } catch (e) {
      keyErr = e.message || 'could not register the key';
    } finally {
      keyBusy = false;
    }
  }

  async function removeKey(label) {
    keyBusy = true;
    keyErr = null;
    keyMsg = null;
    try {
      const res = await fetch(base + '/admin/sftp/keys/' + encodeURIComponent(label), {
        method: 'DELETE'
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `failed (${res.status})`);
      keyMsg = `${label} revoked — that key can no longer connect.`;
      await loadKeys();
    } catch (e) {
      keyErr = e.message || 'could not revoke the key';
    } finally {
      keyBusy = false;
      confirmDelete = null;
    }
  }

  const addedOn = (secs) => new Date(secs * 1000).toLocaleDateString();

  // ---- ingest settings (poll cadence + catalog mode + stale purge) ---------
  // Both the api and the worker container read this from Redis, so a change here
  // takes effect on the worker's next scan with no restart. super_admin only:
  // the /admin/ingest/* endpoints all require it, so the card only renders when
  // the connection card loaded (i.e. the caller is a super_admin).
  let ingestCfg = $state(null); // { poll_seconds, catalog_mode }
  let pollInput = $state('');
  let savingCfg = $state(false);
  let cfgMsg = $state(null);
  let cfgErr = $state(null);

  let staleDays = $state(90);
  let stalePreview = $state(null); // { count, legacy_count, cutoff }
  let previewing = $state(false);
  let purging = $state(false);
  let purgeConfirm = $state(false);
  let purgeMsg = $state(null);
  let purgeErr = $state(null);

  async function loadIngestConfig() {
    cfgErr = null;
    try {
      const res = await fetch(base + '/admin/ingest/config');
      if (!res.ok) return; // 403 for a plain admin — card stays hidden
      ingestCfg = await res.json();
      pollInput = String(ingestCfg.poll_seconds);
    } catch (e) {
      cfgErr = 'backend offline';
    }
  }

  async function saveIngestConfig(updates, note) {
    savingCfg = true;
    cfgErr = null;
    cfgMsg = null;
    try {
      const res = await fetch(base + '/admin/ingest/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates)
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `failed (${res.status})`);
      ingestCfg = body;
      pollInput = String(body.poll_seconds);
      cfgMsg = note;
      await load(); // the Files card echoes the poll interval
    } catch (e) {
      cfgErr = e.message || 'could not save';
    } finally {
      savingCfg = false;
    }
  }

  const savePoll = () => saveIngestConfig({ poll_seconds: Number(pollInput) }, 'Poll interval saved.');
  const setMode = (m) => {
    if (m === ingestCfg?.catalog_mode) return;
    saveIngestConfig(
      { catalog_mode: m },
      m === 'full_sync'
        ? 'Catalog mode set to Full sync — article files now delete rows they omit.'
        : 'Catalog mode set to Merge — nothing is auto-deleted.'
    );
  };

  async function previewStale() {
    previewing = true;
    purgeErr = null;
    purgeMsg = null;
    purgeConfirm = false;
    stalePreview = null;
    try {
      const res = await fetch(base + '/admin/ingest/stale?days=' + encodeURIComponent(staleDays));
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `failed (${res.status})`);
      stalePreview = body;
    } catch (e) {
      purgeErr = e.message || 'could not preview';
    } finally {
      previewing = false;
    }
  }

  async function purgeStale() {
    purging = true;
    purgeErr = null;
    purgeMsg = null;
    try {
      const res = await fetch(base + '/admin/ingest/purge-stale', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ days: Number(staleDays) })
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `failed (${res.status})`);
      purgeMsg = `Removed ${Number(body.deleted).toLocaleString()} stale ${body.deleted === 1 ? 'row' : 'rows'}.`;
      stalePreview = null;
      purgeConfirm = false;
      await load();
    } catch (e) {
      purgeErr = e.message || 'could not purge';
    } finally {
      purging = false;
    }
  }

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

  async function loadConn() {
    connError = null;
    try {
      const res = await fetch(base + '/admin/sftp/connection');
      if (res.status === 403) {
        connError = 'super_admin';
        return;
      }
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      conn = await res.json();
    } catch (e) {
      connError = e.message || 'unavailable';
    }
  }

  function saveHost(v) {
    hostInput = v;
    hostTouched = true;
    localStorage.setItem(HOST_KEY, v);
  }

  // Accept the detected host as-is: persist it so it survives a reload and is no
  // longer merely a guess this browser happens to be echoing.
  function confirmDetected() {
    saveHost(detectedHost);
  }

  // Precedence: the env-configured host (authoritative) > what the operator
  // typed (they know better than we do) > what we detected off the request
  // (a guess). A detected host must never outrank a human.
  const envHost = $derived(conn?.host_source === 'env' ? (conn.host || '').trim() : '');
  const detectedHost = $derived(conn?.host_source === 'detected' ? (conn.host || '').trim() : '');
  const host = $derived(envHost || (hostInput || '').trim() || detectedHost);
  const hostKnown = $derived(host !== '');
  // True while the operator is going along with the detected value — the state
  // the "confirm this" prompt is for.
  const usingDetected = $derived(!envHost && !!detectedHost && host === detectedHost);
  const isLocal = $derived(/^(localhost|127\.|0\.0\.0\.0|\[::1\]|::1)/i.test(host));
  const port = $derived(conn?.port ?? 2222);
  const user = $derived(conn?.username ?? 'pharma');
  const path = $derived(conn?.upload_path ?? 'upload/');
  // Shown inside snippets before a host is known — shouted, so nobody pastes it.
  const h = $derived(hostKnown ? host : 'SFTP_HOST_NOT_SET');

  const sftpSnippet = $derived(`sftp -P ${port} ${user}@${h}
# password: the shared credential shown above
sftp> cd ${path.replace(/\/$/, '')}
sftp> put articles-export-2026-07-13.csv
sftp> put balance_stock_20260713.xlsx
sftp> bye`);

  const scpSnippet = $derived(
    `scp -P ${port} balance_stock_20260713.xlsx ${user}@${h}:${path}`
  );

  const cronSnippet = $derived(`# /etc/cron.d/pharma-export — nightly push at 01:15
# sshpass keeps the shared password out of the command line; better still,
# switch to key auth (below) and drop the SSHPASS env entirely.
15 1 * * *  pharma  SSHPASS="$SFTP_PASSWORD" sshpass -e \\
  sftp -oBatchMode=no -oStrictHostKeyChecking=accept-new -P ${port} \\
  -b - ${user}@${h} <<< $'cd ${path.replace(/\/$/, '')}\\nput /exports/balance_stock_$(date +%Y%m%d).xlsx'`);

  const pythonSnippet = $derived(`# pip install paramiko
import os
from datetime import date

import paramiko

HOST, PORT = "${h}", ${port}
USER = "${user}"
PASSWORD = os.environ["PHARMA_SFTP_PASSWORD"]   # never hardcode it

# The name is the contract: it must contain one of the keywords below, or the
# file lands in failed/ and nothing is ingested.
local = f"/exports/balance_stock_{date.today():%Y%m%d}.xlsx"
remote = f"${path}{os.path.basename(local)}"

transport = paramiko.Transport((HOST, PORT))
transport.connect(username=USER, password=PASSWORD)
try:
    sftp = paramiko.SFTPClient.from_transport(transport)
    # Upload to a temp name, then rename: the watcher only ingests a file whose
    # size has stopped changing, and a rename is atomic — so a half-written file
    # is never picked up mid-flight.
    sftp.put(local, remote + ".part")
    sftp.rename(remote + ".part", remote)
finally:
    transport.close()`);

  const winscpSnippet = $derived(`File protocol:  SFTP
Host name:      ${h}
Port number:    ${port}
User name:      ${user}
Password:       <shared password from this page>
Remote directory: /${path.replace(/\/$/, '')}

Transfer settings -> Transfer mode: Binary
Endurance / resume: ON  (WinSCP writes a .filepart then renames — the
watcher ignores the partial and ingests only the final name)`);

  // What the PARTNER runs. They send back the .pub line and read you the
  // fingerprint; you paste the line here and check the two match.
  const keygenSnippet = `# on the PARTNER's machine — generate a key pair
ssh-keygen -t ed25519 -f ~/.ssh/pharma_sftp -C "acme-pharma"

# send us ONLY the .pub file — never ~/.ssh/pharma_sftp itself
cat ~/.ssh/pharma_sftp.pub

# and read us this fingerprint, so we can check we registered the right key
ssh-keygen -lf ~/.ssh/pharma_sftp.pub`;

  function copy(key, text) {
    navigator.clipboard.writeText(text);
    copied = key;
    setTimeout(() => (copied = ''), 1500);
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
    hostInput = localStorage.getItem(HOST_KEY) || '';
    load();
    loadConn().then(() => {
      // Pre-fill (not save) the detected host so the field shows a value the
      // operator can confirm or correct. Saving it here would quietly promote a
      // guess into a stored setting nobody ever looked at.
      if (!hostInput && !hostTouched && conn?.host_source === 'detected') hostInput = conn.host;
      if (conn) {
        loadKeys();
        loadIngestConfig();
      }
    });
    timer = setInterval(load, 10000);
  });
  onDestroy(() => clearInterval(timer));

  const size = (b) => (b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' KB');
  const when = (s) => new Date(s * 1000).toLocaleString();
</script>

<PageHeader
  title="SFTP uploads"
  subtitle="Everything a partner needs to push article and balance-stock exports. The worker ingests them automatically and busts the cache."
>
  {#snippet actions()}
    <input type="file" accept=".xlsx,.csv" bind:this={fileInput} onchange={onUpload} class="hidden" />
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
      class="inline-flex items-center gap-2 rounded-lg bg-accent px-3.5 py-2 text-[13px] font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
    >
      <RefreshCw size={15} class={ingesting ? 'animate-spin' : ''} />
      {ingesting ? 'Ingesting' : 'Ingest now'}
    </button>
  {/snippet}
</PageHeader>

{#if msg}
  <div class="mb-6 rounded-xl border border-line bg-surface px-4 py-3 text-[13px] text-ink-2">{msg}</div>
{/if}

{#snippet block(key, label, code, note)}
  <section class="mb-6">
    <div class="mb-2 flex items-center">
      <span class="text-[14px] font-medium text-ink">{label}</span>
      <button
        onclick={() => copy(key, code)}
        class="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1 text-[12px] text-ink-2 transition-colors hover:bg-surface-2"
      >
        {#if copied === key}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
      </button>
    </div>
    {#if note}<p class="mb-2 text-[13px] text-ink-2">{note}</p>{/if}
    <pre
      class="overflow-x-auto rounded-[14px] border border-line bg-surface-2 p-4 text-[12.5px] leading-relaxed text-ink"><code
        >{code}</code
      ></pre>
  </section>
{/snippet}

{#if error}
  <div class="rounded-xl border border-line bg-surface px-5 py-4 text-[14px] text-ink-2">
    <p class="font-medium text-ink">Backend offline</p>
    <p class="mt-1">Could not reach the agent at {API_BASE}.</p>
  </div>
{:else if loading}
  <p class="text-[14px] text-ink-2">Loading…</p>
{:else if status}
  <!-- 1. Connection -->
  <section class="mb-6 rounded-[14px] border border-line bg-surface p-4">
    <div class="mb-1 flex items-center gap-2">
      <Server size={16} class="text-ink-2" />
      <span class="text-[14px] font-medium text-ink">Connection</span>
      {#if usingDetected}<Badge tone="warn">host detected — confirm</Badge>
      {:else if conn && !conn.host_configured}<Badge tone="warn">host not configured</Badge>{/if}
      {#if isLocal}<Badge tone="warn">not reachable by a partner</Badge>{/if}
    </div>

    {#if connError === 'super_admin'}
      <p class="mt-2 flex items-start gap-1.5 text-[13px] text-warning">
        <AlertTriangle size={14} class="mt-0.5 shrink-0" />
        <span>
          Connection details are <span class="font-medium">super_admin only</span> — they include the
          shared SFTP password. Ask a super admin for the handover, or sign in as one.
        </span>
      </p>
    {:else if connError}
      <p class="mt-2 text-[13px] text-danger">Could not load connection details: {connError}</p>
    {:else if conn}
      {#if !conn.host_configured}
        {#if conn.host_source === 'detected'}
          <p class="mb-2 text-[13px] text-ink-2">
            <span class="font-mono">SFTP_PUBLIC_HOST</span> is not set, so we filled this in with the
            hostname <span class="font-medium text-ink">you reached this console on</span> —
            <span class="font-mono text-ink">{conn.host}</span>.
            <span class="font-medium text-ink">Detected, not confirmed:</span> behind a proxy, or if the
            SFTP port is published on a different name, it is wrong. Check it is what a partner dials, then
            correct it or confirm it. It is baked into every snippet below.
          </p>
        {:else}
          <p class="mb-2 text-[13px] text-ink-2">
            <span class="font-mono">SFTP_PUBLIC_HOST</span> is not set and this request carried no host
            we could read. Type the host a partner would dial — it is baked into every snippet below.
          </p>
        {/if}
        <div class="mb-3 flex items-center gap-2">
          <input
            type="text"
            value={hostInput}
            oninput={(e) => saveHost(e.currentTarget.value)}
            spellcheck="false"
            placeholder="sftp.example.com"
            class="flex-1 rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-[13px] text-ink outline-none focus:border-accent"
          />
          {#if usingDetected && !hostTouched}
            <button
              onclick={confirmDetected}
              class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-line px-2.5 py-2 text-[12px] text-ink-2 transition-colors hover:bg-surface-2"
            >
              <Check size={13} /> Confirm
            </button>
          {/if}
        </div>
        {#if isLocal}
          <p class="mb-3 flex items-start gap-1.5 text-[12.5px] text-warning">
            <AlertTriangle size={14} class="mt-0.5 shrink-0" />
            <span>
              <span class="font-mono">{host}</span> only resolves on this machine. A partner's server
              cannot connect to it — use the public hostname or IP.
            </span>
          </p>
        {/if}
      {/if}

      <div class="grid gap-x-8 gap-y-2 text-[13px] sm:grid-cols-2">
        <div class="flex justify-between gap-4">
          <span class="text-ink-2">Host</span>
          <span class="font-mono text-ink">{hostKnown ? host : 'not configured'}</span>
        </div>
        <div class="flex justify-between gap-4">
          <span class="text-ink-2">Port</span><span class="tnum font-mono text-ink">{port}</span>
        </div>
        <div class="flex justify-between gap-4">
          <span class="text-ink-2">User</span><span class="font-mono text-ink">{user}</span>
        </div>
        <div class="flex justify-between gap-4">
          <span class="text-ink-2">Upload path</span><span class="font-mono text-ink">{path}</span>
        </div>
      </div>

      <!-- 2. Password -->
      <div class="mt-4 border-t border-line pt-4">
        <div class="mb-1 flex items-center gap-2">
          <Lock size={14} class="text-ink-2" />
          <span class="text-[13px] font-medium text-ink">Password</span>
          <Badge tone="warn">shared account</Badge>
        </div>
        <p class="mb-2 text-[12.5px] text-ink-3">
          One credential for the whole <span class="font-mono">{user}</span> account — everyone who has
          it uploads as the same user, and rotating it means re-issuing it to every partner. Prefer key
          auth below for anything long-lived.
        </p>
        <div class="flex items-center gap-2">
          <span
            class="flex-1 rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-[13px] text-ink"
            >{revealed ? conn.password : '•'.repeat(Math.max(8, conn.password.length))}</span
          >
          <button
            onclick={() => (revealed = !revealed)}
            class="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-2 text-[12px] text-ink-2 transition-colors hover:bg-surface-2"
          >
            {#if revealed}<EyeOff size={13} /> Hide{:else}<Eye size={13} /> Reveal{/if}
          </button>
          <button
            onclick={() => copy('pw', conn.password)}
            class="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-2 text-[12px] text-ink-2 transition-colors hover:bg-surface-2"
          >
            {#if copied === 'pw'}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
          </button>
        </div>
      </div>

      <p class="mt-4 border-t border-line pt-3 text-[12px] text-ink-3">
        Auto-ingest polls every {conn.poll_seconds}s. A file is only read once its size stops changing,
        so a slow upload is never ingested half-written.
      </p>
    {/if}
  </section>

  {#if conn}
    <!-- 3. Snippets -->
    {@render block(
      'sftp',
      'Interactive session',
      sftpSnippet,
      'One-off or manual pushes. `cd upload` first — a file dropped in the home directory is not watched.'
    )}
    {@render block('scp', 'One-shot push (scp)', scpSnippet, 'Single file, no session — the simplest thing that works.')}
    {@render block(
      'cron',
      'Nightly push (cron)',
      cronSnippet,
      'Unattended export from the partner’s box. Runs at 01:15; the agent picks it up within the poll interval.'
    )}
    {@render block(
      'py',
      'Python (paramiko)',
      pythonSnippet,
      'Drop into an existing pipeline. Uploads to a .part name and renames — atomic, so the watcher never sees a partial file.'
    )}
    {@render block('winscp', 'WinSCP (Windows)', winscpSnippet, 'Paste into a new WinSCP site.')}

    <!-- 4. Filename rules -->
    <section class="mb-6 rounded-[14px] border border-line bg-surface p-4">
      <div class="mb-1 flex items-center gap-2">
        <FileCheck2 size={16} class="text-ink-2" />
        <span class="text-[14px] font-medium text-ink">Filename rules</span>
      </div>
      <p class="mb-3 text-[13px] text-ink-2">
        <span class="font-medium text-ink">The filename is the contract.</span> Nothing reads the file's
        contents to work out what it is — the <em>name</em> decides, and a name that matches nothing is
        moved to <span class="font-mono">failed/</span> without being ingested.
      </p>

      <div class="mb-3 grid gap-2 text-[13px] sm:grid-cols-2">
        {#each conn.rules.kinds as k (k.kind)}
          <div class="rounded-lg border border-line bg-surface-2 px-3 py-2">
            <div class="mb-1 text-[12px] uppercase tracking-wide text-ink-3">{k.kind}</div>
            <div class="flex flex-wrap gap-1.5">
              {#each k.keywords as kw (kw)}
                <span class="rounded border border-line bg-surface px-1.5 py-0.5 font-mono text-[12px] text-ink"
                  >{kw}</span
                >
              {/each}
            </div>
          </div>
        {/each}
      </div>
      <p class="mb-3 text-[12.5px] text-ink-3">
        The name must contain one of those words (case-insensitive) and end in
        {#each conn.rules.extensions as ext, i (ext)}<span class="font-mono text-ink-2">{ext}</span
          >{i < conn.rules.extensions.length - 1 ? ' or ' : ''}{/each}. Nothing else is read.
      </p>

      <div class="grid gap-4 sm:grid-cols-2">
        <div>
          <div class="mb-1.5 flex items-center gap-1.5 text-[13px] font-medium text-ink">
            <Check size={14} class="text-accent" /> Good
          </div>
          <ul class="space-y-1">
            {#each conn.rules.good as g (g.name)}
              <li class="text-[12.5px]">
                <span class="font-mono text-ink">{g.name}</span>
                <span class="text-ink-3"> → {g.kind}</span>
              </li>
            {/each}
          </ul>
        </div>
        <div>
          <div class="mb-1.5 flex items-center gap-1.5 text-[13px] font-medium text-ink">
            <AlertTriangle size={14} class="text-warning" /> Bad
          </div>
          <ul class="space-y-1">
            {#each conn.rules.bad as b (b.name)}
              <li class="text-[12.5px]">
                <span class="font-mono text-ink">{b.name}</span>
                <span class="text-ink-3"> → {conn.rules.unmatched_dir}</span>
              </li>
            {/each}
          </ul>
        </div>
      </div>

      <p class="mt-3 border-t border-line pt-3 text-[12.5px] text-ink-3">
        After a run: recognised files move to <span class="font-mono">{conn.rules.archive_dir}</span>,
        unrecognised or broken ones to <span class="font-mono">{conn.rules.unmatched_dir}</span> —
        both listed below. A file still being written is skipped until its size stops changing.
      </p>
    </section>

    <!-- 5. Partner keys -->
    <section class="mb-6 rounded-[14px] border border-line bg-surface p-4">
      <div class="mb-1 flex items-center gap-2">
        <KeyRound size={16} class="text-ink-2" />
        <span class="text-[14px] font-medium text-ink">Partner keys</span>
        <Badge>{keys.length} registered</Badge>
      </div>
      <p class="mb-3 text-[13px] text-ink-2">
        Paste a partner's <span class="font-medium text-ink">public</span> key and it works on their
        <span class="font-medium text-ink">next connection</span> — no restart: the SFTP server re-reads
        its key list every time someone connects. In production the shared password is disabled, so
        <span class="font-medium text-ink">the key is the access</span> — registering one grants it and
        deleting one revokes it, immediately.
      </p>

      {#if keysError}
        <p class="mb-3 flex items-start gap-1.5 rounded-lg border border-line bg-surface-2 px-3 py-2 text-[12.5px] text-warning">
          <AlertTriangle size={14} class="mt-0.5 shrink-0" />
          <span>{keysError}</span>
        </p>
      {:else}
        <!-- registered keys -->
        {#if keys.length}
          <div class="mb-4 overflow-hidden rounded-xl border border-line">
            <table class="tbl">
              <thead>
                <tr>
                  <th>Label</th>
                  <th>Fingerprint</th>
                  <th>Added</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {#each keys as k (k.label)}
                  <tr>
                    <td class="text-ink">{k.label}<span class="ml-2 text-[11px] text-ink-3">{k.type}</span></td>
                    <td class="font-mono text-[11.5px] text-ink-2">{k.fingerprint}</td>
                    <td class="text-ink-2">{addedOn(k.added_at)}</td>
                    <td class="text-right">
                      {#if confirmDelete === k.label}
                        <button
                          onclick={() => removeKey(k.label)}
                          disabled={keyBusy}
                          class="rounded-lg border border-line px-2 py-1 text-[12px] text-danger transition-colors hover:bg-surface-2 disabled:opacity-60"
                        >
                          Revoke {k.label}?
                        </button>
                        <button
                          onclick={() => (confirmDelete = null)}
                          class="ml-1 rounded-lg px-2 py-1 text-[12px] text-ink-3 hover:text-ink"
                        >
                          Cancel
                        </button>
                      {:else}
                        <button
                          onclick={() => (confirmDelete = k.label)}
                          aria-label={`Revoke ${k.label}`}
                          class="rounded-lg border border-line px-2 py-1 text-ink-3 transition-colors hover:bg-surface-2 hover:text-danger"
                        >
                          <Trash2 size={13} />
                        </button>
                      {/if}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
          <p class="mb-4 text-[12px] text-ink-3">
            Check the fingerprint against the one the partner reads you from
            <span class="font-mono">ssh-keygen -lf</span> — that, not the key text, is what tells you
            you registered the right key.
          </p>
        {:else}
          <p class="mb-4 rounded-lg border border-line bg-surface-2 px-3 py-2 text-[12.5px] text-ink-3">
            No keys registered — every partner is on the shared password above.
          </p>
        {/if}

        <!-- add a key -->
        <div class="rounded-xl border border-line bg-surface-2 p-3">
          <div class="mb-2 text-[13px] font-medium text-ink">Register a key</div>
          <input
            type="text"
            bind:value={keyLabel}
            spellcheck="false"
            placeholder="label — e.g. acme-pharma (letters, digits, . - _)"
            class="mb-2 w-full rounded-lg border border-line bg-surface px-3 py-2 text-[13px] text-ink outline-none focus:border-accent"
          />
          <textarea
            bind:value={keyMaterial}
            spellcheck="false"
            rows="3"
            placeholder="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA… partner@corp"
            class="mb-2 w-full resize-y rounded-lg border border-line bg-surface px-3 py-2 font-mono text-[12px] text-ink outline-none focus:border-accent"
          ></textarea>
          <div class="flex items-center gap-2">
            <button
              onclick={addKey}
              disabled={keyBusy || !keyLabel.trim() || !keyMaterial.trim()}
              class="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-[12.5px] font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
            >
              <Plus size={14} />
              {keyBusy ? 'Saving' : 'Register key'}
            </button>
            <span class="text-[12px] text-ink-3">
              The <span class="font-mono">.pub</span> line only — never their private key.
            </span>
          </div>
          {#if keyErr}
            <p class="mt-2 flex items-start gap-1.5 text-[12.5px] text-danger">
              <AlertTriangle size={14} class="mt-0.5 shrink-0" />
              <span>{keyErr}</span>
            </p>
          {/if}
          {#if keyMsg}
            <p class="mt-2 flex items-start gap-1.5 text-[12.5px] text-ink-2">
              <Check size={14} class="mt-0.5 shrink-0 text-accent" />
              <span>{keyMsg}</span>
            </p>
          {/if}
        </div>
      {/if}

      <div class="mt-4 border-t border-line pt-3">
        <div class="mb-2 flex items-center">
          <span class="text-[13px] font-medium text-ink">Send this to the partner</span>
          <button
            onclick={() => copy('keygen', keygenSnippet)}
            class="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1 text-[12px] text-ink-2 transition-colors hover:bg-surface-2"
          >
            {#if copied === 'keygen'}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
          </button>
        </div>
        <pre
          class="overflow-x-auto rounded-[14px] border border-line bg-surface-2 p-4 text-[12.5px] leading-relaxed text-ink"><code
            >{keygenSnippet}</code
          ></pre>
      </div>
    </section>

    <!-- 6. Ingest settings -->
    {#if ingestCfg}
      <section class="mb-6 rounded-[14px] border border-line bg-surface p-4">
        <div class="mb-1 flex items-center gap-2">
          <RefreshCw size={16} class="text-ink-2" />
          <span class="text-[14px] font-medium text-ink">Ingest settings</span>
        </div>
        <p class="mb-4 text-[13px] text-ink-2">
          Sync happens when a partner pushes a file — there is no schedule. An inventory file
          <span class="font-medium text-ink">fully replaces</span> stock (it is a snapshot); an article
          file <span class="font-medium text-ink">merges</span> into the catalog (or full-syncs, below).
        </p>

        <!-- poll interval -->
        <div class="mb-4 border-t border-line pt-4">
          <div class="mb-1 flex items-center gap-2">
            <Clock size={14} class="text-ink-2" />
            <span class="text-[13px] font-medium text-ink">Poll interval</span>
          </div>
          <p class="mb-2 text-[12.5px] text-ink-3">
            How often the worker looks for new drops. A file is only read once its size stops changing,
            so this is the longest a stable file waits. 5–3600 seconds.
          </p>
          <div class="flex items-center gap-2">
            <input
              type="number"
              min="5"
              max="3600"
              bind:value={pollInput}
              class="w-28 rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-[13px] text-ink outline-none focus:border-accent"
            />
            <span class="text-[12.5px] text-ink-3">seconds</span>
            <button
              onclick={savePoll}
              disabled={savingCfg || !pollInput}
              class="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-line bg-surface px-3 py-2 text-[12.5px] font-medium text-ink transition-colors hover:bg-surface-2 disabled:opacity-60"
            >
              <Check size={14} />
              {savingCfg ? 'Saving' : 'Save'}
            </button>
          </div>
        </div>

        <!-- catalog mode -->
        <div class="mb-4 border-t border-line pt-4">
          <div class="mb-1 flex items-center gap-2">
            <FileSpreadsheet size={14} class="text-ink-2" />
            <span class="text-[13px] font-medium text-ink">Article file mode</span>
          </div>
          <p class="mb-3 text-[12.5px] text-ink-3">
            How an incoming article export changes the catalog.
          </p>
          <div class="grid gap-2 sm:grid-cols-2">
            <button
              onclick={() => setMode('merge')}
              disabled={savingCfg}
              class="rounded-xl border p-3 text-left transition-colors disabled:opacity-60 {ingestCfg.catalog_mode ===
              'merge'
                ? 'border-accent bg-accent-soft'
                : 'border-line bg-surface-2 hover:bg-surface'}"
            >
              <div class="mb-0.5 flex items-center gap-1.5 text-[13px] font-medium text-ink">
                {#if ingestCfg.catalog_mode === 'merge'}<Check size={14} class="text-accent" />{/if}
                Merge
              </div>
              <div class="text-[12px] text-ink-2">
                Add new articles and update existing ones. Nothing is ever deleted.
              </div>
            </button>
            <button
              onclick={() => setMode('full_sync')}
              disabled={savingCfg}
              class="rounded-xl border p-3 text-left transition-colors disabled:opacity-60 {ingestCfg.catalog_mode ===
              'full_sync'
                ? 'border-warning bg-surface-2'
                : 'border-line bg-surface-2 hover:bg-surface'}"
            >
              <div class="mb-0.5 flex items-center gap-1.5 text-[13px] font-medium text-ink">
                {#if ingestCfg.catalog_mode === 'full_sync'}<Check size={14} class="text-warning" />{/if}
                Full sync <span class="text-[11px] font-normal text-ink-3">default</span>
              </div>
              <div class="flex items-start gap-1 text-[12px] text-ink-2">
                <AlertTriangle size={13} class="mt-0.5 shrink-0 text-warning" />
                <span>Deletes catalog rows not present in the latest article file.</span>
              </div>
            </button>
          </div>
        </div>

        {#if cfgErr}
          <p class="mb-2 flex items-start gap-1.5 text-[12.5px] text-danger">
            <AlertTriangle size={14} class="mt-0.5 shrink-0" /><span>{cfgErr}</span>
          </p>
        {/if}
        {#if cfgMsg}
          <p class="mb-2 flex items-start gap-1.5 text-[12.5px] text-ink-2">
            <Check size={14} class="mt-0.5 shrink-0 text-accent" /><span>{cfgMsg}</span>
          </p>
        {/if}

        <!-- stale purge -->
        <div class="border-t border-line pt-4">
          <div class="mb-1 flex items-center gap-2">
            <Trash2 size={14} class="text-ink-2" />
            <span class="text-[13px] font-medium text-ink">Remove stale articles</span>
          </div>
          <p class="mb-2 text-[12.5px] text-ink-3">
            Manually drop catalog rows not seen in a recent article file — the operator-triggered
            alternative to full sync. Preview first; nothing is deleted until you confirm.
          </p>
          <div class="mb-2 flex items-center gap-2">
            <span class="text-[12.5px] text-ink-2">Not seen in</span>
            <input
              type="number"
              min="1"
              bind:value={staleDays}
              class="w-20 rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-[13px] text-ink outline-none focus:border-accent"
            />
            <span class="text-[12.5px] text-ink-2">days</span>
            <button
              onclick={previewStale}
              disabled={previewing || !staleDays}
              class="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-line bg-surface px-3 py-2 text-[12.5px] font-medium text-ink transition-colors hover:bg-surface-2 disabled:opacity-60"
            >
              <Eye size={14} />
              {previewing ? 'Checking' : 'Preview'}
            </button>
          </div>

          {#if stalePreview}
            <div class="rounded-xl border border-line bg-surface-2 p-3">
              <p class="text-[13px] text-ink">
                <span class="font-medium">{stalePreview.count.toLocaleString()}</span>
                {stalePreview.count === 1 ? 'row would be removed' : 'rows would be removed'}
                {#if stalePreview.legacy_count > 0}
                  <span class="text-ink-2">
                    (includes {stalePreview.legacy_count.toLocaleString()} never-updated legacy
                    {stalePreview.legacy_count === 1 ? 'row' : 'rows'} — rows with no last-seen date yet)
                  </span>
                {/if}
              </p>
              {#if stalePreview.count > 0}
                <div class="mt-3 flex items-center gap-2">
                  {#if purgeConfirm}
                    <button
                      onclick={purgeStale}
                      disabled={purging}
                      class="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-[12.5px] font-medium text-danger transition-colors hover:bg-surface disabled:opacity-60"
                    >
                      <Trash2 size={14} />
                      {purging ? 'Removing' : `Remove ${stalePreview.count.toLocaleString()} rows`}
                    </button>
                    <button
                      onclick={() => (purgeConfirm = false)}
                      class="rounded-lg px-2 py-2 text-[12.5px] text-ink-3 hover:text-ink"
                    >
                      Cancel
                    </button>
                  {:else}
                    <button
                      onclick={() => (purgeConfirm = true)}
                      class="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-[12.5px] font-medium text-ink transition-colors hover:bg-surface"
                    >
                      <Trash2 size={14} /> Remove…
                    </button>
                  {/if}
                </div>
              {/if}
            </div>
          {/if}

          {#if purgeErr}
            <p class="mt-2 flex items-start gap-1.5 text-[12.5px] text-danger">
              <AlertTriangle size={14} class="mt-0.5 shrink-0" /><span>{purgeErr}</span>
            </p>
          {/if}
          {#if purgeMsg}
            <p class="mt-2 flex items-start gap-1.5 text-[12.5px] text-ink-2">
              <Check size={14} class="mt-0.5 shrink-0 text-accent" /><span>{purgeMsg}</span>
            </p>
          {/if}
        </div>
      </section>
    {/if}
  {/if}

  <!-- file lists -->
  <div class="mb-2 mt-2 flex items-center gap-2 text-[14px] font-medium text-ink">
    <FolderOpen size={16} /> Files
    {#if status.incoming_dir}
      <span class="font-mono text-[12px] font-normal text-ink-3">on the server at {status.incoming_dir}/</span>
    {/if}
  </div>
  <p class="mb-4 text-[13px] text-ink-2">
    A pushed file lands in <span class="font-mono text-ink">upload/</span> (Pending), and within
    {status.poll_seconds ?? 15}s the worker moves it to <span class="font-mono text-ink">upload/archive/</span>
    when it loads or <span class="font-mono text-ink">upload/failed/</span> when the name or contents are wrong.
  </p>

  {#each [ ['Pending', status.pending, Clock, 'upload/', 'just arrived — not yet ingested'], ['Archived', status.archived, Check, 'upload/archive/', 'ingested and filed away'], ['Failed', status.failed, AlertTriangle, 'upload/failed/', 'rejected — bad filename or parse error'] ] as [title, rows, Icon, folder, hint]}
    <section class="mb-6">
      <div class="mb-2 flex items-center gap-2 text-[14px] font-medium text-ink">
        <Icon size={15} /> {title}
        <span class="font-mono text-[12px] font-normal text-ink-3">{folder}</span>
        <Badge>{rows.length}</Badge>
      </div>
      <div class="overflow-hidden rounded-xl border border-line bg-surface">
        {#if rows.length === 0}
          <p class="px-5 py-4 text-[13px] text-ink-3">No files here — {hint}.</p>
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
