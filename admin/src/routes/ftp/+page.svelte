<script>
  import { dialog } from '$lib/aurora/dialog.js';
  /**
   * SFTP uploads.
   *
   * The old page stacked seven cards down one scroll and put the FILES — the
   * only thing anyone opens this page for — last, split across three separate
   * lists (pending / archived / failed) that could not be sorted together.
   *
   * Now: five tabs, landing on one file list. The folder a file sits in is its
   * status, and `GET /admin/sftp/files` merges the three folders into one
   * history. Everything else — connecting, naming, keys, clean-up — is setup
   * you touch once, so it moves off the landing tab.
   */
  import { API_BASE } from '$lib/apiBase.js';
  import { onMount, onDestroy } from 'svelte';
  import {
    Upload,
    RefreshCw,
    Server,
    Check,
    AlertTriangle,
    Copy,
    Eye,
    EyeOff,
    KeyRound,
    FileCheck2,
    Lock,
    Plus,
    Trash2,
    Download,
    RotateCcw,
    X,
    Play,
    Eraser,
    SlidersHorizontal,
    FileText,
    Loader2
  } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';
  import { ApiError, getJSON } from '$lib/api.js';
  import ErrorState from '$lib/ErrorState.svelte';
  import TabStrip from '$lib/TabStrip.svelte';

  const base = API_BASE;

  /**
   * A one-line, status-aware reason an ACTION failed, for a toast beside a
   * button. Never blames a stopped backend for a request the backend answered.
   */
  function reason(e, verb) {
    const s = Number(e?.status ?? 0);
    if (s === 401) return `Your sign-in has timed out. Sign in again, then ${verb}.`;
    if (s === 403) return `Your account is not permitted to ${verb}.`;
    if (s > 0) return e?.message || `The backend answered ${s}.`;
    return `No response from the backend — could not ${verb}.`;
  }

  // SFTP_PUBLIC_HOST is the authoritative answer. Without it the backend falls
  // back to the hostname THIS request arrived on (host_source="detected") — a
  // starting point, not a fact: behind a proxy, or when the sftp port is not
  // published on the same name as the console, it is simply wrong. So a detected
  // host is shown pre-filled and asks to be confirmed, and an operator override
  // beats it and persists per browser.
  const HOST_KEY = 'sftp_public_host';

  const TABS = [
    { id: 'files', label: 'Files', icon: FileText },
    { id: 'connect', label: 'How to connect', icon: Server },
    { id: 'rules', label: 'Naming rules', icon: FileCheck2 },
    { id: 'keys', label: 'Partner keys', icon: KeyRound },
    { id: 'clean', label: 'Clean up', icon: Eraser }
  ];
  let tab = $state('files');

  let conn = $state(null);
  let connError = $state(null); // 403 => not a super_admin
  let loading = $state(true);
  let error = $state(null);
  let timer;

  // ---- files ---------------------------------------------------------------
  let files = $state([]);
  let counts = $state({ wait: 0, ok: 0, bad: 0, live: 0 });
  // Defaults to `live`: the first question anyone opens this page with is
  // "which file is the agent answering from", and a list of every copy we have
  // ever kept does not answer it.
  let filter = $state('live');
  let selected = $state(null); // the file whose drawer is open
  let events = $state([]);
  let eventsError = $state(null);
  let eventsLoading = $state(false);
  let busyFile = $state(''); // name of the file an action is running against
  let allowShrink = $state(false); // per-file override, armed inside the drawer
  let confirmDeleteFile = $state('');

  const shown = $derived(
    filter === 'all'
      ? files
      : filter === 'live'
        ? files.filter((f) => f.live)
        : files.filter((f) => f.state === filter)
  );

  async function loadFiles() {
    error = null;
    try {
      const body = await getJSON('/admin/sftp/files');
      files = body.files || [];
      counts = body.counts || counts;
      if (body.poll_seconds != null) pollInput = String(body.poll_seconds);
      if (body.enabled != null) autoLoad = body.enabled;
    } catch (e) {
      if (e?.status === 403) {
        connError = 'super_admin';
        return;
      }
      // The error OBJECT: a 401 here is an expired token, and telling someone
      // to restart a running backend is what this page used to do.
      error = e;
    } finally {
      loading = false;
    }
  }

  async function openFile(f) {
    selected = f;
    allowShrink = false;
    confirmDeleteFile = '';
    events = [];
    eventsError = null;
    eventsLoading = true;
    try {
      const body = await getJSON(`/admin/sftp/file/${encodeURIComponent(f.name)}/history`);
      events = body.events || [];
    } catch (e) {
      // An empty timeline used to mean both "nothing was recorded" and "we
      // could not read it". Only the first is a fact about the file.
      events = [];
      eventsError = e;
    } finally {
      eventsLoading = false;
    }
  }

  /**
   * Download the kept copy.
   *
   * Not an <a href>: the endpoint needs the admin bearer token, which a plain
   * link cannot carry. Fetch it (the layout's fetch wrapper adds the header),
   * then hand the browser a blob URL. Revoked straight after, or every download
   * leaks the whole file until the tab is closed.
   */
  async function download(f) {
    busyFile = f.name;
    try {
      // Stays a raw fetch: the body is a file, not JSON. The status still has
      // to survive to the message.
      const res = await fetch(base + `/admin/sftp/file/${encodeURIComponent(f.stored_as)}`);
      if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement('a');
      a.href = url;
      a.download = f.name;
      a.click();
      URL.revokeObjectURL(url);
      toast(`Downloaded ${f.name}.`);
    } catch (e) {
      toast(reason(e, 'download this file'), true);
    } finally {
      busyFile = '';
    }
  }

  async function retry(f) {
    busyFile = f.name;
    try {
      const body = await getJSON(
        `/admin/sftp/file/${encodeURIComponent(f.stored_as)}/retry`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ allow_shrink: allowShrink })
        }
      );
      const done = (body.processed ?? []).length;
      const bad = (body.failed ?? [])[0];
      toast(
        bad
          ? `${f.name} was refused again — ${bad.reason}`
          : `${f.name} loaded${done ? '' : ' (nothing to do)'}. Saved answers cleared.`,
        Boolean(bad)
      );
      selected = null;
      await loadFiles();
    } catch (e) {
      toast(reason(e, 'load this file again'), true);
    } finally {
      busyFile = '';
    }
  }

  async function removeFile(f) {
    busyFile = f.name;
    try {
      await getJSON(`/admin/sftp/file/${encodeURIComponent(f.stored_as)}`, {
        method: 'DELETE'
      });
      toast(`${f.name} deleted. Loaded data is unchanged.`);
      selected = null;
      await loadFiles();
    } catch (e) {
      toast(reason(e, 'delete this file'), true);
    } finally {
      busyFile = '';
      confirmDeleteFile = '';
    }
  }

  // ---- ingest settings -----------------------------------------------------
  let pollInput = $state('15');
  let autoLoad = $state(true);
  let savingCfg = $state(false);

  async function saveConfig(updates, note) {
    savingCfg = true;
    try {
      const body = await getJSON('/admin/ingest/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates)
      });
      pollInput = String(body.poll_seconds);
      autoLoad = body.enabled;
      toast(note);
    } catch (e) {
      toast(reason(e, 'save this setting'), true);
    } finally {
      savingCfg = false;
    }
  }

  const savePoll = (v) =>
    saveConfig({ poll_seconds: Number(v) }, `Now checking every ${describeSeconds(Number(v))}.`);

  const toggleAuto = () =>
    saveConfig(
      { enabled: !autoLoad },
      autoLoad
        ? 'Automatic loading off — files will wait until you load them.'
        : 'Automatic loading on.'
    );

  function describeSeconds(s) {
    if (s < 60) return `${s} seconds`;
    if (s < 3600) return s === 60 ? 'minute' : `${Math.round(s / 60)} minutes`;
    return 'hour';
  }

  // ---- upload + manual run -------------------------------------------------
  let uploading = $state(false);
  let ingesting = $state(false);
  let rejection = $state(null); // the 422 report, shown in full
  let fileInput;

  async function onUpload(e) {
    const f = e.target.files?.[0];
    if (!f) return;
    uploading = true;
    rejection = null;
    try {
      const fd = new FormData();
      fd.append('file', f);
      // Stays a raw fetch: a 422 carries a STRUCTURED rejection report that the
      // shared getJSON would flatten to a one-line message.
      const res = await fetch(base + '/admin/upload', { method: 'POST', body: fd });
      const body = await res.json().catch(() => ({}));
      if (res.status === 422) {
        // Not a failure to report in one line — the reasons are the whole point.
        rejection = body.detail || {};
        tab = 'files';
        return;
      }
      if (!res.ok)
        throw new ApiError(
          res.status,
          typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`
        );
      const done = (body.processed ?? [])
        .map((x) => `${x.kind} ${Number(x.rows).toLocaleString()}`)
        .join(', ');
      toast(`Loaded ${body.file}${done ? ' — ' + done : ''}. Saved answers cleared.`);
      await loadFiles();
    } catch (err) {
      toast(reason(err, 'upload this file'), true);
    } finally {
      uploading = false;
      if (e.target) e.target.value = '';
    }
  }

  async function ingestNow() {
    ingesting = true;
    try {
      const j = await getJSON('/api/embed/ingest', { method: 'POST' });
      toast(`Read the folder — ${(j.processed ?? []).length} loaded, saved answers cleared.`);
      await loadFiles();
    } catch (err) {
      toast(reason(err, 'read the drop folder'), true);
    } finally {
      ingesting = false;
    }
  }

  // ---- partner keys --------------------------------------------------------
  let keys = $state([]);
  let keysError = $state(null);
  let keyLabel = $state('');
  let keyMaterial = $state('');
  let keyBusy = $state(false);
  let confirmDelete = $state(null);

  async function loadKeys() {
    keysError = null;
    try {
      keys = await getJSON('/admin/sftp/keys');
    } catch (e) {
      if (e?.status === 403) return;
      keys = [];
      // The object: the keys tab must not print "backend offline" for a 401.
      keysError = e;
    }
  }

  async function addKey() {
    keyBusy = true;
    try {
      const body = await getJSON('/admin/sftp/keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: keyLabel.trim(), public_key: keyMaterial })
      });
      toast(`${body.label} registered — ${body.fingerprint}. Live on their next connection.`);
      keyLabel = '';
      keyMaterial = '';
      await loadKeys();
    } catch (e) {
      toast(reason(e, 'register this key'), true);
    } finally {
      keyBusy = false;
    }
  }

  async function removeKey(label) {
    keyBusy = true;
    try {
      await getJSON('/admin/sftp/keys/' + encodeURIComponent(label), { method: 'DELETE' });
      toast(`${label} revoked — that key can no longer connect.`);
      await loadKeys();
    } catch (e) {
      toast(reason(e, 'revoke this key'), true);
    } finally {
      keyBusy = false;
      confirmDelete = null;
    }
  }

  // ---- stale purge ---------------------------------------------------------
  let staleDays = $state(90);
  let stalePreview = $state(null);
  let previewing = $state(false);
  let purging = $state(false);

  async function previewStale() {
    previewing = true;
    stalePreview = null;
    try {
      stalePreview = await getJSON('/admin/ingest/stale?days=' + encodeURIComponent(staleDays));
    } catch (e) {
      toast(reason(e, 'check for stale products'), true);
    } finally {
      previewing = false;
    }
  }

  async function purgeStale() {
    purging = true;
    try {
      const body = await getJSON('/admin/ingest/purge-stale', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ days: Number(staleDays) })
      });
      toast(`Removed ${Number(body.deleted).toLocaleString()} products. Saved answers cleared.`);
      stalePreview = null;
    } catch (e) {
      toast(reason(e, 'remove the stale products'), true);
    } finally {
      purging = false;
    }
  }

  // ---- host + snippets -----------------------------------------------------
  let hostInput = $state('');
  let hostTouched = $state(false);
  let revealed = $state(false);
  let copied = $state('');

  function saveHost(v) {
    hostInput = v;
    hostTouched = true;
    localStorage.setItem(HOST_KEY, v);
    toast('Address saved. Every command below uses it.');
  }

  // Precedence: the env-configured host (authoritative) > what the operator
  // typed (they know better than we do) > what we detected off the request
  // (a guess). A detected host must never outrank a human.
  const envHost = $derived(conn?.host_source === 'env' ? (conn.host || '').trim() : '');
  const detectedHost = $derived(conn?.host_source === 'detected' ? (conn.host || '').trim() : '');
  const host = $derived(envHost || (hostInput || '').trim() || detectedHost);
  const hostKnown = $derived(host !== '');
  const usingDetected = $derived(!envHost && !!detectedHost && host === detectedHost && !hostTouched);
  const isLocal = $derived(/^(localhost|127\.|0\.0\.0\.0|\[::1\]|::1)/i.test(host));
  const port = $derived(conn?.port ?? 2222);
  const user = $derived(conn?.username ?? 'pharma');
  const path = $derived(conn?.upload_path ?? 'upload/');
  // Shouted, so nobody pastes it into a real script by accident.
  const h = $derived(hostKnown ? host : 'SFTP_HOST_NOT_SET');

  const snippets = $derived([
    {
      key: 'sftp',
      title: 'Command line',
      note: 'one file, by hand',
      body: `sftp -P ${port} ${user}@${h}
# password: the shared one above
sftp> cd ${path.replace(/\/$/, '')}
sftp> put articles-export-2026-08-03.csv
sftp> put balance_stock_20260803.xlsx
sftp> bye`
    },
    {
      key: 'scp',
      title: 'One line',
      note: 'scp, for a script',
      body: `scp -P ${port} balance_stock_20260803.xlsx ${user}@${h}:${path}`
    },
    {
      key: 'cron',
      title: 'Every night',
      note: 'cron, 01:15',
      body: `# /etc/cron.d/pharma-export — nightly push at 01:15
# sshpass keeps the password off the command line. Better still: register a
# key on the Partner keys tab and drop SSHPASS entirely.
15 1 * * *  pharma  SSHPASS="$SFTP_PASSWORD" sshpass -e \\
  sftp -oBatchMode=no -oStrictHostKeyChecking=accept-new -P ${port} \\
  -b - ${user}@${h} <<< $'cd ${path.replace(/\/$/, '')}\\nput /exports/balance_stock_$(date +%Y%m%d).xlsx'`
    },
    {
      key: 'py',
      title: 'From Python',
      note: 'paramiko',
      body: `# pip install paramiko
import os
from datetime import date

import paramiko

HOST, PORT = "${h}", ${port}
USER = "${user}"
PASSWORD = os.environ["PHARMA_SFTP_PASSWORD"]   # never hardcode it

# The name is the contract — see the Naming rules tab.
local = f"/exports/balance_stock_{date.today():%Y%m%d}.xlsx"
remote = f"${path}{os.path.basename(local)}"

transport = paramiko.Transport((HOST, PORT))
transport.connect(username=USER, password=PASSWORD)
try:
    sftp = paramiko.SFTPClient.from_transport(transport)
    # Upload to a temp name, then rename: we only read a file whose size has
    # stopped changing, and a rename is atomic — so a half-written file is
    # never picked up mid-flight.
    sftp.put(local, remote + ".part")
    sftp.rename(remote + ".part", remote)
finally:
    transport.close()`
    },
    {
      key: 'winscp',
      title: 'WinSCP',
      note: 'settings to type in',
      body: `File protocol:      SFTP
Host name:          ${h}
Port number:        ${port}
User name:          ${user}
Password:           <shared password from this page>
Remote directory:   /${path.replace(/\/$/, '')}

Transfer mode:      Binary
Resume support:     ON   (WinSCP writes a .filepart then renames, so we
                          never read a partial file)`
    }
  ]);

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

  // ---- toasts --------------------------------------------------------------
  let toasts = $state([]);
  let toastSeq = 0;

  function toast(message, bad = false) {
    const id = ++toastSeq;
    toasts = [...toasts, { id, message, bad }];
    setTimeout(() => (toasts = toasts.filter((t) => t.id !== id)), 5000);
  }

  // ---- formatting ----------------------------------------------------------
  const size = (b) => (b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' KB');
  const when = (s) => new Date(s * 1000).toLocaleString();
  const addedOn = (secs) => new Date(secs * 1000).toLocaleDateString();
  const KIND = { catalog: 'Product list', inventory: 'Stock levels' };
  const STATE_LABEL = { wait: 'Still arriving', ok: 'Loaded', bad: 'Rejected' };
  const FOLDER_LABEL = { incoming: 'Waiting', archive: 'Loaded', failed: 'Rejected' };

  async function loadConn() {
    connError = null;
    try {
      conn = await getJSON('/admin/sftp/connection');
    } catch (e) {
      // 403 keeps its own sentinel — it drives the "super admin only" screen,
      // which is a permissions ANSWER, not a failure. Everything else is the
      // error object, so the panel can name the status.
      connError = e?.status === 403 ? 'super_admin' : e;
    }
  }

  onMount(() => {
    hostInput = localStorage.getItem(HOST_KEY) || '';
    hostTouched = localStorage.getItem(HOST_KEY) != null;
    loadFiles();
    loadConn().then(() => {
      // Pre-fill (not save) the detected host so the field shows a value the
      // operator can confirm or correct. Saving it here would quietly promote a
      // guess into a stored setting nobody ever looked at.
      if (!hostInput && !hostTouched && conn?.host_source === 'detected') hostInput = conn.host;
      if (conn) loadKeys();
    });
    // Refresh the list while the page is open — a partner's drop should appear
    // without anyone pressing anything. Paused while a drawer is open, so the
    // list cannot reshuffle under a decision being made.
    timer = setInterval(() => {
      if (!selected) loadFiles();
    }, 10000);
  });
  onDestroy(() => clearInterval(timer));
</script>

<PageHeader
  title="Data pipeline"
  subtitle="Everything a partner needs to send us stock files, and everything that happened to the files they sent."
>
  {#snippet actions()}
    <input type="file" accept=".xlsx,.csv" bind:this={fileInput} onchange={onUpload} class="hidden" />
    <button
      onclick={ingestNow}
      disabled={ingesting}
      class="inline-flex items-center gap-2 rounded-panel border border-line bg-surface px-3.5 py-2 text-body-sm font-medium text-ink transition-colors hover:bg-surface-2 disabled:opacity-60"
    >
      <RefreshCw size={15} class={ingesting ? 'animate-spin' : ''} />
      {ingesting ? 'Reading' : 'Check now'}
    </button>
    <button
      onclick={() => fileInput?.click()}
      disabled={uploading}
      class="inline-flex items-center gap-2 rounded-panel bg-accent px-3.5 py-2 text-body-sm font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
    >
      <Upload size={15} class={uploading ? 'animate-pulse' : ''} />
      {uploading ? 'Checking' : 'Upload a file'}
    </button>
  {/snippet}
</PageHeader>

{#if connError === 'super_admin'}
  <div class="rounded-card border border-line bg-surface px-5 py-4 text-body-sm text-ink-2">
    <p class="font-medium text-ink">Super admin only</p>
    <p class="mt-1">
      This page carries the shared SFTP password and the partner key list, so it is limited to super
      admins.
    </p>
  </div>
{:else if error && !files.length}
  <ErrorState {error} retry={loadFiles} what="the uploaded files" />
{:else}
  <!-- ---------- tabs ----------
       The shared strip. This copy is the reason TabStrip exists: it was the one
       hand-rolled strip with no roving tabindex and no arrow keys, so these five
       tabs could not be reached from the keyboard at all. -->
  <TabStrip tabs={TABS} bind:value={tab} label="SFTP sections">
    {#snippet trailing(t)}
      {#if t.id === 'keys' && keys.length}<Badge>{keys.length}</Badge>{/if}
    {/snippet}
  </TabStrip>

  <!-- ================= FILES ================= -->
  {#if tab === 'files'}
    {#if rejection}
      <div class="mb-4 rounded-panel border border-danger/40 bg-danger-soft p-4">
        <div class="mb-1 flex items-center gap-2">
          <AlertTriangle size={16} class="text-danger" />
          <span class="text-body-sm font-semibold text-ink">
            {rejection.file || 'That file'} was refused — nothing changed
          </span>
          <button
            onclick={() => (rejection = null)}
            aria-label="Dismiss"
            class="ml-auto rounded-panel p-1 text-ink-3 hover:bg-surface hover:text-ink"
          >
            <X size={15} />
          </button>
        </div>
        <ul class="mt-2 space-y-1 text-body-sm text-ink-2">
          {#each rejection.errors ?? [] as e}
            <li class="flex items-start gap-2"><X size={14} class="mt-0.5 shrink-0 text-danger" />{e}</li>
          {/each}
          {#each rejection.warnings ?? [] as w}
            <li class="flex items-start gap-2">
              <AlertTriangle size={14} class="mt-0.5 shrink-0 text-warning" />{w}
            </li>
          {/each}
        </ul>
        <p class="mt-2 text-meta text-ink-3">
          The loaded data was never touched — a refused file cannot replace good data.
        </p>
      </div>
    {/if}

    <!-- controls -->
    <section class="mb-4 overflow-hidden rounded-panel border border-line bg-surface">
      <div class="flex items-center gap-2 border-b border-line px-4 py-3">
        <SlidersHorizontal size={16} class="text-ink-2" />
        <span class="text-body-sm font-medium text-ink">How files are handled</span>
        <span class="ml-auto text-meta text-ink-3">Applies to the next file — no restart</span>
      </div>
      <div class="grid sm:grid-cols-3">
        <div class="border-b border-line px-4 py-3.5 sm:border-b-0 sm:border-r">
          <div class="text-meta font-semibold text-ink">Check for new files</div>
          <p class="mt-0.5 text-label text-ink-3">How often we look in the folder.</p>
          <select
            value={pollInput}
            onchange={(e) => savePoll(e.currentTarget.value)}
            disabled={savingCfg}
            aria-label="Check interval"
            class="mt-2 w-full rounded-panel border border-line bg-surface-2 px-2.5 py-1.5 text-body-sm text-ink outline-none focus:border-accent"
          >
            <option value="5">Every 5 seconds</option>
            <option value="15">Every 15 seconds</option>
            <option value="60">Every minute</option>
            <option value="300">Every 5 minutes</option>
            <option value="3600">Every hour</option>
          </select>
        </div>

        <div class="border-b border-line px-4 py-3.5 sm:border-b-0 sm:border-r">
          <div class="text-meta font-semibold text-ink">Load files automatically</div>
          <p class="mt-0.5 text-label text-ink-3">
            Off means files wait here until you load them.
          </p>
          <button
            onclick={toggleAuto}
            disabled={savingCfg}
            role="switch"
            aria-checked={autoLoad}
            class="mt-2 inline-flex items-center gap-2.5 text-meta text-ink disabled:opacity-60"
          >
            <span
              class="relative h-[22px] w-[38px] shrink-0 rounded-full transition-colors {autoLoad
                ? 'bg-accent'
                : 'bg-line'}"
            >
              <span
                class="absolute top-[3px] h-4 w-4 rounded-full bg-surface shadow transition-all {autoLoad
                  ? 'left-[19px]'
                  : 'left-[3px]'}"
              ></span>
            </span>
            {autoLoad ? 'On' : 'Off'}
          </button>
        </div>

        <div class="px-4 py-3.5">
          <div class="flex items-center gap-1.5 text-meta font-semibold text-ink">
            What a file does <Lock size={13} class="text-ink-3" />
          </div>
          <p class="mt-0.5 text-label text-ink-3">
            A file always replaces the rows it covers. Merging left deleted products in the catalog
            forever.
          </p>
          <span
            class="mt-2 inline-flex items-center gap-2 rounded-panel border border-line bg-surface-2 px-2.5 py-1.5 text-meta text-ink-2"
          >
            <Lock size={13} /> Replaces all rows
          </span>
        </div>
      </div>
    </section>

    <!-- file list -->
    <section class="overflow-hidden rounded-panel border border-line bg-surface">
      <div class="flex flex-wrap gap-1 border-b border-line px-3 py-2.5">
        <!--
          `Live` is a different question from the three that follow it — those
          filter by what HAPPENED to a file, this one asks which file the data
          currently in the database came from. At most one per kind, so the
          count is 2 on a healthy install: one product list, one stock file.
        -->
        {#each [['live', 'Live', counts.live], ['all', 'All', files.length], ['wait', 'Waiting', counts.wait], ['ok', 'Loaded', counts.ok], ['bad', 'Rejected', counts.bad]] as [id, label, n] (id)}
          <!--
            These are FILTERS, not tabs. They were `role="tab"` without a
            tablist, so a screen reader announced a second, phantom tab set
            inside the Files tab — a keyboard probe on this page counted nine
            tabs where there are five. `aria-pressed` is what a toggle button
            says.
          -->
          <button
            onclick={() => (filter = id)}
            aria-pressed={filter === id}
            class="inline-flex items-center gap-1.5 rounded-card border px-3 py-1.5 text-body-sm transition-colors
              {filter === id
              ? 'border-accent/30 bg-accent-soft font-semibold text-accent'
              : 'border-transparent font-medium text-ink-2 hover:bg-surface-2 hover:text-ink'}"
          >
            <!-- The counts start at 0 and are only real once a load returns.
                 Printing "Rejected 0" over a list nobody has read yet is a
                 claim about the drop folder we have not earned. -->
            {label}{#if !loading}<span class="tnum text-label opacity-75">{n}</span>{/if}
          </button>
        {/each}
      </div>

      {#if loading}
        <div class="space-y-2 p-4">
          {#each [1, 2, 3] as i (i)}<div class="skel h-8"></div>{/each}
        </div>
      {:else if !shown.length}
        <p class="px-5 py-10 text-center text-body-sm text-ink-3">
          {filter === 'all'
            ? 'No files yet — nothing has been sent.'
            : filter === 'live'
              ? 'Nothing is loaded yet — no file has replaced the data.'
              : 'Nothing here.'}
        </p>
      {:else}
        <div class="overflow-x-auto">
          <table class="tbl">
            <thead>
              <tr>
                <th>File</th>
                <th class="hidden sm:table-cell">Contains</th>
                <th>Status</th>
                <th class="num hidden sm:table-cell">Rows</th>
                <th class="num hidden sm:table-cell">Size</th>
                <th class="num">When</th>
              </tr>
            </thead>
            <tbody>
              {#each shown as f (f.stored_as)}
                <tr
                  role="button"
                  tabindex="0"
                  aria-current={selected?.stored_as === f.stored_as}
                  onclick={() => openFile(f)}
                  onkeydown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      openFile(f);
                    }
                  }}
                >
                  <td>
                    <div class="flex items-center gap-3">
                      <span
                        class="w-[3px] self-stretch rounded-control {f.state === 'ok'
                          ? 'bg-success'
                          : f.state === 'bad'
                            ? 'bg-danger'
                            : 'bg-warning'}"
                        style="min-height:30px"
                      ></span>
                      <span class="min-w-0">
                        <span class="block break-all font-mono text-meta text-ink">
                          {f.name}
                          <!--
                            On the ROW, not only behind the Live filter. Four
                            uploads listed under two names all read "Loaded";
                            without this the page still cannot say which two the
                            agent is using unless you happen to pick that chip.
                          -->
                          {#if f.live}
                            <span
                              class="ml-1.5 whitespace-nowrap rounded-full bg-accent-soft px-1.5 py-0.5 align-middle font-sans text-micro font-semibold uppercase tracking-wide text-accent"
                              >Live</span
                            >
                          {/if}
                        </span>
                        <span class="block text-label text-ink-3">
                          <!-- The separator sits INSIDE the expression: Svelte
                               trims the whitespace before a block tag, so a
                               leading space in the markup renders as "Loaded·". -->
                          {FOLDER_LABEL[f.folder]}{#if f.state === 'ok' && !f.live}{' · superseded'}{/if}
                        </span>
                      </span>
                    </div>
                  </td>
                  <td class="hidden sm:table-cell">
                    <span class="rounded-full border border-line bg-surface-2 px-2 py-0.5 text-label text-ink-2">
                      {KIND[f.kind] ?? 'Unknown'}
                    </span>
                  </td>
                  <td>
                    <span
                      class="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-label font-semibold
                        {f.state === 'ok'
                        ? 'bg-success-soft text-success'
                        : f.state === 'bad'
                          ? 'bg-danger-soft text-danger'
                          : 'bg-warning-soft text-warning'}"
                    >
                      <span class="h-1.5 w-1.5 rounded-full bg-current"></span>
                      {STATE_LABEL[f.state]}
                    </span>
                  </td>
                  <td class="num hidden sm:table-cell text-ink-2">
                    {f.rows == null ? '—' : Number(f.rows).toLocaleString()}
                  </td>
                  <td class="num hidden sm:table-cell text-ink-2">{size(f.size)}</td>
                  <td class="num text-ink-2">{when(f.mtime)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>
    <p class="mt-3 text-meta text-ink-3">
      Pick any row to see what happened to it, download the copy we kept, or try it again.
    </p>
  {/if}

  <!-- ================= CONNECT ================= -->
  {#if tab === 'connect' && connError}
    <!-- Without `conn` every field below falls back to a built-in default —
         port 2222, user "pharma", a blank password — which reads as the real
         connection details and would be handed to a partner as such. -->
    <ErrorState error={connError} retry={loadConn} what="the connection details" />
  {:else if tab === 'connect'}
    <section class="mb-4 rounded-panel border border-line bg-surface p-4">
      <div class="mb-3 flex items-center gap-2">
        <Server size={16} class="text-ink-2" />
        <span class="text-body-sm font-medium text-ink">Where the partner connects</span>
      </div>

      {#if envHost}
        <p class="mb-3 rounded-r-panel border-l-[3px] border-success bg-success-soft px-3.5 py-2.5 text-meta text-ink">
          Set on the server. This is the address, not a guess.
        </p>
      {:else if usingDetected}
        <p class="mb-3 rounded-r-panel border-l-[3px] border-warning bg-warning-soft px-3.5 py-2.5 text-meta text-ink">
          <span class="font-semibold">Confirm this address.</span> We are guessing it from the address
          you opened this page on. Behind a proxy, or if the file port is published on a different name,
          this is wrong — and every command below is wrong with it.
        </p>
      {/if}
      {#if isLocal}
        <p class="mb-3 rounded-r-panel border-l-[3px] border-danger bg-danger-soft px-3.5 py-2.5 text-meta text-ink">
          <span class="font-semibold">That is this machine.</span> A partner cannot reach
          <span class="font-mono">{host}</span> from anywhere else — set the real hostname before
          sending any of this out.
        </p>
      {/if}

      <div class="flex flex-wrap items-end gap-2">
        <label class="min-w-[220px] flex-1">
          <span class="mb-1.5 block text-micro font-bold uppercase tracking-wider text-ink-3">Address</span>
          <input
            type="text"
            value={hostInput}
            oninput={(e) => (hostInput = e.currentTarget.value)}
            disabled={Boolean(envHost)}
            spellcheck="false"
            placeholder="sftp.yourcompany.com"
            class="w-full rounded-panel border border-line bg-surface-2 px-3 py-2 text-body-sm text-ink outline-none focus:border-accent disabled:opacity-60"
          />
        </label>
        {#if !envHost}
          <button
            onclick={() => saveHost(hostInput)}
            class="rounded-panel border border-line bg-surface px-3.5 py-2 text-body-sm font-medium text-ink hover:bg-surface-2"
          >
            Save
          </button>
        {/if}
      </div>

      <div class="mt-4 flex flex-wrap gap-x-7 gap-y-1.5 border-t border-line pt-3 text-meta">
        <span><span class="text-ink-3">Port</span> <span class="tnum font-mono text-ink">{port}</span></span>
        <span><span class="text-ink-3">User</span> <span class="font-mono text-ink">{user}</span></span>
        <span><span class="text-ink-3">Folder</span> <span class="font-mono text-ink">{path}</span></span>
      </div>
    </section>

    <section class="mb-4 rounded-panel border border-line bg-surface p-4">
      <div class="mb-3 flex items-center gap-2">
        <Lock size={16} class="text-ink-2" />
        <span class="text-body-sm font-medium text-ink">Shared password</span>
        <button
          onclick={() => (revealed = !revealed)}
          class="ml-auto inline-flex items-center gap-1.5 rounded-panel border border-line px-2.5 py-1 text-meta text-ink-2 hover:bg-surface-2"
        >
          {#if revealed}<EyeOff size={13} /> Hide{:else}<Eye size={13} /> Show{/if}
        </button>
      </div>
      <div class="flex flex-wrap items-center gap-2.5">
        <code
          class="rounded-panel border border-line bg-surface-2 px-3.5 py-2 text-body-sm tracking-wider text-ink"
          >{revealed ? (conn?.password ?? '') : '••••••••'}</code
        >
        <button
          onclick={() => copy('pw', conn?.password ?? '')}
          class="inline-flex items-center gap-1.5 rounded-panel border border-line px-2.5 py-1.5 text-meta text-ink-2 hover:bg-surface-2"
        >
          {#if copied === 'pw'}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
        </button>
      </div>
      <p class="mt-3 rounded-r-panel border-l-[3px] border-danger bg-danger-soft px-3.5 py-2.5 text-meta text-ink">
        <span class="font-semibold">Everyone with this password is the same account to us.</span>
        There is no way to tell one sender from another, and no way to cut one off without cutting
        off all of them. Give each partner a key instead — then you revoke one line and the rest keep
        working.
      </p>
    </section>

    <section class="rounded-panel border border-line bg-surface p-4">
      <div class="mb-3 flex items-center gap-2">
        <FileText size={16} class="text-ink-2" />
        <span class="text-body-sm font-medium text-ink">Ready-to-run commands</span>
        <span class="ml-auto text-meta text-ink-3">Address filled in from above</span>
      </div>
      <div class="space-y-3">
        {#each snippets as s (s.key)}
          <div class="overflow-hidden rounded-card border border-line">
            <div class="flex items-center gap-2 border-b border-line bg-surface-2 px-3.5 py-2">
              <span class="text-meta font-semibold text-ink">{s.title}</span>
              <span class="text-label text-ink-3">{s.note}</span>
              <button
                onclick={() => copy(s.key, s.body)}
                class="ml-auto inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2 py-0.5 text-meta text-ink-2 hover:bg-surface-2"
              >
                {#if copied === s.key}<Check size={12} /> Copied{:else}<Copy size={12} /> Copy{/if}
              </button>
            </div>
            <pre class="overflow-x-auto px-3.5 py-3 text-label leading-relaxed text-ink-2"><code
                >{s.body}</code
              ></pre>
          </div>
        {/each}
      </div>
    </section>
  {/if}

  <!-- ================= RULES ================= -->
  {#if tab === 'rules'}
    <section class="rounded-panel border border-line bg-surface p-4">
      <div class="mb-2 flex items-center gap-2">
        <FileCheck2 size={16} class="text-ink-2" />
        <span class="text-body-sm font-medium text-ink">The name decides what a file is</span>
      </div>
      <p class="mb-3.5 text-body-sm text-ink-2">
        Nothing opens a file to work out what it holds — <span class="font-medium text-ink"
          >the name decides</span
        >. A name that matches nothing is set aside without being read.
      </p>

      {#if conn?.rules}
        <div class="mb-3 grid gap-2 sm:grid-cols-2">
          {#each conn.rules.kinds as k (k.kind)}
            <div class="rounded-panel border border-line bg-surface-2 px-3 py-2">
              <div class="mb-1.5 text-label uppercase tracking-wide text-ink-3">
                {KIND[k.kind] ?? k.kind}
              </div>
              <div class="flex flex-wrap gap-1.5">
                {#each k.keywords as kw (kw)}
                  <span class="rounded border border-line bg-surface px-1.5 py-0.5 font-mono text-meta text-ink">
                    {kw}
                  </span>
                {/each}
              </div>
            </div>
          {/each}
        </div>
        <p class="mb-4 text-meta text-ink-3">
          The name must contain one of those words — upper or lower case — and end in
          {#each conn.rules.extensions as ext, i (ext)}<span class="font-mono text-ink-2">{ext}</span
            >{i < conn.rules.extensions.length - 1 ? ' or ' : ''}{/each}. Nothing else is read.
        </p>

        <div class="grid gap-5 sm:grid-cols-2">
          <div>
            <div class="mb-2 flex items-center gap-1.5 text-meta font-semibold text-ink">
              <Check size={14} class="text-success" /> Works
            </div>
            <ul class="space-y-1.5">
              {#each conn.rules.good as g (g.name)}
                <li class="text-meta">
                  <span class="font-mono text-ink">{g.name}</span>
                  <span class="text-ink-3"> → {KIND[g.kind] ?? g.kind}</span>
                </li>
              {/each}
            </ul>
          </div>
          <div>
            <div class="mb-2 flex items-center gap-1.5 text-meta font-semibold text-ink">
              <AlertTriangle size={14} class="text-warning" /> Set aside
            </div>
            <ul class="space-y-1.5">
              {#each conn.rules.bad as b (b.name)}
                <li class="text-meta">
                  <span class="font-mono text-ink">{b.name}</span>
                  <span class="text-ink-3"> → no matching word</span>
                </li>
              {/each}
            </ul>
          </div>
        </div>

        <p class="mt-4 border-t border-line pt-3 text-meta text-ink-3">
          Once read, a file is kept under <span class="font-medium text-ink-2">Loaded</span>; one we
          could not use is kept under <span class="font-medium text-ink-2">Rejected</span>. A file
          still being written is left alone until its size stops changing.
        </p>
      {/if}
    </section>
  {/if}

  <!-- ================= KEYS ================= -->
  {#if tab === 'keys'}
    <section class="rounded-panel border border-line bg-surface p-4">
      <div class="mb-2 flex items-center gap-2">
        <KeyRound size={16} class="text-ink-2" />
        <span class="text-body-sm font-medium text-ink">Partner keys</span>
        <span class="ml-auto text-meta text-ink-3">Live on their next connection — no restart</span>
      </div>
      <p class="mb-3.5 text-body-sm text-ink-2">
        A key replaces the shared password for one partner. Revoke a key and only that partner is cut
        off.
      </p>

      {#if keysError}
        <!-- The key list is a security fact. If it did not load, say so with
             the status rather than showing an empty (= "no keys") table. -->
        <ErrorState error={keysError} retry={loadKeys} what="the partner keys" />
      {:else}
        {#if keys.length}
          <div class="mb-3 overflow-hidden rounded-card border border-line">
            <table class="tbl">
              <thead>
                <tr><th>Label</th><th>Fingerprint</th><th>Added</th><th></th></tr>
              </thead>
              <tbody>
                {#each keys as k (k.label)}
                  <tr>
                    <td class="text-ink"
                      >{k.label}<span class="ml-2 text-label text-ink-3">{k.type}</span></td
                    >
                    <td class="break-all font-mono text-label text-ink-2">{k.fingerprint}</td>
                    <td class="text-ink-2">{addedOn(k.added_at)}</td>
                    <td class="text-right">
                      {#if confirmDelete === k.label}
                        <button
                          onclick={() => removeKey(k.label)}
                          disabled={keyBusy}
                          class="rounded-panel border border-danger/40 px-2 py-1 text-meta text-danger hover:bg-danger-soft disabled:opacity-60"
                        >
                          Revoke {k.label}?
                        </button>
                        <button
                          onclick={() => (confirmDelete = null)}
                          class="ml-1 rounded-panel px-2 py-1 text-meta text-ink-3 hover:text-ink"
                        >
                          Cancel
                        </button>
                      {:else}
                        <button
                          onclick={() => (confirmDelete = k.label)}
                          aria-label={`Revoke ${k.label}`}
                          class="rounded-panel border border-line px-2 py-1 text-ink-3 hover:bg-surface-2 hover:text-danger"
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
        {:else}
          <p class="mb-3 rounded-panel border border-line bg-surface-2 px-3 py-2 text-meta text-ink-3">
            No keys yet — every partner is on the shared password.
          </p>
        {/if}

        <div class="rounded-card border border-line bg-surface-2 p-3.5">
          <div class="mb-2.5 text-body-sm font-semibold text-ink">Add a key</div>
          <input
            type="text"
            bind:value={keyLabel}
            aria-label="Key label"
            spellcheck="false"
            placeholder="who it is for — e.g. acme-pharma"
            class="mb-2 w-full rounded-panel border border-line bg-surface px-3 py-2 text-body-sm text-ink outline-none focus:border-accent"
          />
          <textarea
            bind:value={keyMaterial}
            aria-label="Public key"
            spellcheck="false"
            rows="3"
            placeholder="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA… partner@corp"
            class="mb-2 w-full resize-y rounded-panel border border-line bg-surface px-3 py-2 font-mono text-meta text-ink outline-none focus:border-accent"
          ></textarea>
          <button
            onclick={addKey}
            disabled={keyBusy || !keyLabel.trim() || !keyMaterial.trim()}
            class="inline-flex items-center gap-1.5 rounded-panel bg-accent px-3 py-2 text-meta font-medium text-on-accent hover:bg-accent-hover disabled:opacity-60"
          >
            {#if keyBusy}<Loader2 size={14} class="animate-spin" />{:else}<Plus size={14} />{/if}
            Register key
          </button>
          <p class="mt-2.5 text-meta text-ink-3">
            <span class="font-medium text-ink-2">Check the fingerprint against what they read you</span>
            from <span class="font-mono">ssh-keygen -lf</span>. A key pasted out of an email nobody
            verified is a way in for whoever sent that email. Send the
            <span class="font-mono">.pub</span> line only — never their private key.
          </p>
        </div>

        <div class="mt-4 border-t border-line pt-3">
          <div class="mb-2 flex items-center">
            <span class="text-body-sm font-medium text-ink">Send this to the partner</span>
            <button
              onclick={() => copy('keygen', keygenSnippet)}
              class="ml-auto inline-flex items-center gap-1.5 rounded-panel border border-line px-2.5 py-1 text-meta text-ink-2 hover:bg-surface-2"
            >
              {#if copied === 'keygen'}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
            </button>
          </div>
          <pre
            class="overflow-x-auto rounded-panel border border-line bg-surface-2 p-4 text-meta leading-relaxed text-ink"><code
              >{keygenSnippet}</code
            ></pre>
        </div>
      {/if}
    </section>
  {/if}

  <!-- ================= CLEAN UP ================= -->
  {#if tab === 'clean'}
    <section class="mb-4 rounded-panel border border-line bg-surface p-4">
      <div class="mb-2 flex items-center gap-2">
        <Eraser size={16} class="text-ink-2" />
        <span class="text-body-sm font-medium text-ink">Remove products nobody sends any more</span>
      </div>
      <p class="mb-3.5 text-body-sm text-ink-2">
        Every file we load stamps the products it contains. A product that has not appeared in any
        file for a long time is probably discontinued — this removes those.
        <span class="font-medium text-ink">The number is always shown before anything is deleted.</span>
      </p>
      <div class="flex flex-wrap items-end gap-2.5">
        <label class="w-[180px]">
          <span class="mb-1.5 block text-micro font-bold uppercase tracking-wider text-ink-3">Not seen in</span>
          <select
            bind:value={staleDays}
            class="w-full rounded-panel border border-line bg-surface-2 px-2.5 py-1.5 text-body-sm text-ink outline-none focus:border-accent"
          >
            <option value={30}>30 days</option>
            <option value={60}>60 days</option>
            <option value={90}>90 days</option>
            <option value={180}>180 days</option>
          </select>
        </label>
        <button
          onclick={previewStale}
          disabled={previewing}
          class="rounded-panel border border-line bg-surface px-3.5 py-2 text-body-sm font-medium text-ink hover:bg-surface-2 disabled:opacity-60"
        >
          {previewing ? 'Checking' : 'Show me how many'}
        </button>
      </div>

      {#if stalePreview}
        <div class="mt-4">
          {#if stalePreview.count === 0}
            <p class="rounded-r-panel border-l-[3px] border-success bg-success-soft px-3.5 py-2.5 text-meta text-ink">
              Nothing to remove — every product has been sent within {staleDays} days.
            </p>
          {:else}
            <p class="rounded-r-panel border-l-[3px] border-warning bg-warning-soft px-3.5 py-2.5 text-meta text-ink">
              <span class="font-semibold">{stalePreview.count.toLocaleString()} products</span>
              have not appeared in any file for {staleDays} days.
              {#if stalePreview.legacy_count}
                <br /><span class="text-ink-2">
                  Of those, {stalePreview.legacy_count.toLocaleString()} have never been stamped at all
                  — they were loaded before we started recording it, so their real age is unknown.
                </span>
              {/if}
            </p>
            <div class="mt-3 flex flex-wrap items-center gap-2.5">
              <button
                onclick={purgeStale}
                disabled={purging}
                class="inline-flex items-center gap-1.5 rounded-panel bg-danger px-3.5 py-2 text-body-sm font-medium text-white hover:opacity-90 disabled:opacity-60"
              >
                <Trash2 size={14} />
                {purging ? 'Removing' : `Delete ${stalePreview.count.toLocaleString()} products`}
              </button>
              <span class="text-meta text-ink-3">This cannot be undone.</span>
            </div>
          {/if}
        </div>
      {/if}
    </section>

    <section class="rounded-panel border border-line bg-surface p-4">
      <div class="mb-2 flex items-center gap-2">
        <Play size={16} class="text-ink-2" />
        <span class="text-body-sm font-medium text-ink">Load everything waiting, now</span>
      </div>
      <p class="mb-3 text-body-sm text-ink-2">
        Reads every file sitting in the folder right now, without waiting for the next check. Use
        this after dropping files by hand, or when automatic loading is off.
      </p>
      <button
        onclick={ingestNow}
        disabled={ingesting}
        class="inline-flex items-center gap-2 rounded-panel bg-accent px-3.5 py-2 text-body-sm font-medium text-on-accent hover:bg-accent-hover disabled:opacity-60"
      >
        <RefreshCw size={15} class={ingesting ? 'animate-spin' : ''} />
        {ingesting ? 'Reading' : 'Load everything waiting'}
      </button>
    </section>
  {/if}
{/if}

<!-- ================= DRAWER ================= -->
{#if selected}
  <!-- Pointer affordance only. Escape is the keyboard route out and the
       drawer's use:dialog owns it. -->
  <div
    class="fixed inset-0 z-40 cursor-default bg-black/35"
    onclick={() => (selected = null)}
    aria-hidden="true"
  ></div>
  <!-- The three ARIA attributes were already right and everything else was
       wrong: focus stayed on the file row, two extra stops (the row, then the
       invisible scrim) sat between the user and the panel, Escape did nothing,
       and tabbing past Delete dropped them behind the backdrop. <div> because
       role="dialog" supersedes the complementary landmark. -->
  <div
    use:dialog={{ onclose: () => (selected = null) }}
    class="fixed bottom-0 right-0 top-0 z-50 flex w-full max-w-[470px] flex-col border-l border-line bg-surface shadow-2xl outline-none"
    role="dialog"
    aria-modal="true"
    aria-label={selected.name}
    tabindex="-1"
  >
    <div class="flex items-start gap-3 border-b border-line px-5 py-4">
      <div class="min-w-0 flex-1">
        <p class="text-micro font-bold uppercase tracking-wider text-ink-3">
          {KIND[selected.kind] ?? 'Unknown'}
        </p>
        <h2 class="mt-1 break-all font-mono text-body-sm font-semibold leading-snug text-ink">
          {selected.name}
        </h2>
      </div>
      <button
        onclick={() => (selected = null)}
        aria-label="Close"
        class="rounded-panel p-1.5 text-ink-3 hover:bg-surface-2 hover:text-ink"
      >
        <X size={17} />
      </button>
    </div>

    <div class="flex-1 overflow-y-auto px-5 py-4">
      {#if selected.detail}
        <p
          class="rounded-r-panel border-l-[3px] px-3.5 py-2.5 text-meta leading-relaxed text-ink
            {selected.state === 'ok'
            ? 'border-success bg-success-soft'
            : selected.state === 'bad'
              ? 'border-danger bg-danger-soft'
              : 'border-warning bg-warning-soft'}"
        >
          {selected.detail}
        </p>
      {/if}

      <!--
        The shrink override lives HERE, on the file it applies to, next to the
        number it would delete — not in the settings card. A file is refused
        because of what IT contains, so the decision to take it anyway belongs
        to that file and expires with it.
      -->
      {#if selected.state === 'bad'}
        <div class="mt-3.5 rounded-card border border-warning/40 p-3.5">
          <button
            onclick={() => (allowShrink = !allowShrink)}
            role="switch"
            aria-checked={allowShrink}
            class="inline-flex items-center gap-2.5 text-meta font-semibold text-ink"
          >
            <span
              class="relative h-[22px] w-[38px] shrink-0 rounded-full transition-colors {allowShrink
                ? 'bg-warning'
                : 'bg-line'}"
            >
              <span
                class="absolute top-[3px] h-4 w-4 rounded-full bg-surface shadow transition-all {allowShrink
                  ? 'left-[19px]'
                  : 'left-[3px]'}"
              ></span>
            </span>
            Load it anyway
          </button>
          <p class="mt-2 text-meta leading-relaxed text-ink-3">
            Skips the guard that refuses a file which would delete more than half the rows. Only do
            this if you know the partner really did discontinue them.
            <span class="font-medium text-ink-2">Applies to this file only.</span>
          </p>
        </div>
      {/if}

      <dl class="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-meta">
        <dt class="text-ink-3">Arrived</dt>
        <dd class="tnum m-0 font-mono text-ink">{when(selected.mtime)}</dd>
        <dt class="text-ink-3">Size</dt>
        <dd class="tnum m-0 font-mono text-ink">{size(selected.size)}</dd>
        <dt class="text-ink-3">Rows loaded</dt>
        <dd class="tnum m-0 font-mono text-ink">
          {selected.rows == null ? 'none' : Number(selected.rows).toLocaleString()}
        </dd>
        <dt class="text-ink-3">Kept as</dt>
        <dd class="m-0 break-all font-mono text-ink">{selected.stored_as}</dd>
      </dl>

      <p class="mt-5 text-micro font-bold uppercase tracking-wider text-ink-3">What happened</p>
      {#if eventsLoading}
        <div class="mt-3 space-y-2">
          {#each [1, 2, 3] as i (i)}<div class="skel h-8"></div>{/each}
        </div>
      {:else if eventsError}
        <!-- "No history for this file" is a statement about the file. We only
             failed to read the timeline; the file's own history is unknown. -->
        <div class="mt-3">
          <ErrorState
            error={eventsError}
            retry={() => openFile(selected)}
            what="this file's history"
          />
        </div>
      {:else if !events.length}
        <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
          No history for this file. It arrived before we started keeping one, or the recorder was
          unavailable at the time — the file itself is unaffected.
        </p>
      {:else}
        <ol class="mt-3 list-none p-0">
          {#each events as e, i (e.id)}
            <li class="relative pb-4 pl-6">
              <span
                class="absolute left-[3px] top-1 h-2.5 w-2.5 rounded-full border-2 {e.status === 'ok'
                  ? 'border-success bg-success'
                  : e.status === 'bad'
                    ? 'border-danger bg-danger'
                    : 'border-warning bg-warning'}"
              ></span>
              {#if i < events.length - 1}
                <span class="absolute bottom-0 left-[7px] top-4 w-px bg-line"></span>
              {/if}
              <div class="text-body-sm font-medium text-ink">{STEP_TITLE[e.step] ?? e.step}</div>
              <div class="tnum mt-0.5 text-label text-ink-3">
                {new Date(e.at).toLocaleTimeString()}
              </div>
              {#if e.detail}
                <div class="mt-1 text-meta leading-relaxed text-ink-2">{e.detail}</div>
              {/if}
            </li>
          {/each}
        </ol>
      {/if}
    </div>

    <div class="flex flex-wrap gap-2 border-t border-line px-5 py-3.5">
      {#if selected.state === 'wait'}
        <button
          disabled
          class="inline-flex items-center gap-1.5 rounded-panel border border-line px-3.5 py-2 text-body-sm text-ink opacity-45"
        >
          <Download size={14} /> Not ready yet
        </button>
      {:else}
        <button
          onclick={() => download(selected)}
          disabled={busyFile === selected.name}
          class="inline-flex items-center gap-1.5 rounded-panel bg-accent px-3.5 py-2 text-body-sm font-medium text-on-accent hover:bg-accent-hover disabled:opacity-60"
        >
          <Download size={14} /> Download
        </button>
        <button
          onclick={() => retry(selected)}
          disabled={busyFile === selected.name}
          class="inline-flex items-center gap-1.5 rounded-panel border border-line px-3.5 py-2 text-body-sm font-medium text-ink hover:bg-surface-2 disabled:opacity-60"
        >
          {#if busyFile === selected.name}
            <Loader2 size={14} class="animate-spin" />
          {:else}
            <RotateCcw size={14} />
          {/if}
          {selected.state === 'bad' ? 'Try again' : 'Load again'}
        </button>
        {#if confirmDeleteFile === selected.stored_as}
          <button
            onclick={() => removeFile(selected)}
            disabled={busyFile === selected.name}
            class="inline-flex items-center gap-1.5 rounded-panel bg-danger px-3.5 py-2 text-body-sm font-medium text-white hover:opacity-90 disabled:opacity-60"
          >
            <Trash2 size={14} /> Delete the file?
          </button>
          <button
            onclick={() => (confirmDeleteFile = '')}
            class="rounded-panel px-2.5 py-2 text-body-sm text-ink-3 hover:text-ink"
          >
            Cancel
          </button>
        {:else}
          <button
            onclick={() => (confirmDeleteFile = selected.stored_as)}
            class="inline-flex items-center gap-1.5 rounded-panel border border-danger/35 px-3.5 py-2 text-body-sm font-medium text-danger hover:bg-danger-soft"
          >
            <Trash2 size={14} /> Delete
          </button>
        {/if}
      {/if}
    </div>
  </div>
{/if}

<!-- ================= TOASTS ================= -->
{#if toasts.length}
  <div class="fixed bottom-5 right-5 z-[60] flex flex-col gap-2">
    {#each toasts as t (t.id)}
      <div
        role="status"
        class="max-w-[340px] rounded-card border border-line border-l-[3px] bg-surface px-4 py-3 text-meta text-ink shadow-lg
          {t.bad ? 'border-l-danger' : 'border-l-success'}"
      >
        {t.message}
      </div>
    {/each}
  </div>
{/if}

<script module>
  // Step keys come from app/ingest_events.py. The backend supplies the sentence
  // (`detail`); this is only the heading above it, so a step the frontend has
  // not heard of still renders — it just shows its raw key.
  export const STEP_TITLE = {
    arrived: 'Arrived over SFTP',
    waiting: 'Waiting for the upload to finish',
    detected: 'Read the name',
    unrecognised: 'Could not tell what this is',
    checked: 'Checked',
    rejected: 'Rejected',
    loaded: 'Replaced the data',
    indexed: 'Rebuilt search',
    cache_cleared: 'Cleared saved answers',
    stored: 'Copy kept',
    set_aside: 'Kept for you to look at'
  };
</script>
