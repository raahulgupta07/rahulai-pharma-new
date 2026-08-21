<script>
  import { dialog } from '$lib/aurora/dialog.js';
  /**
   * SFTP uploads.
   *
   * ONE page, no tab bar. The tabs hid the shape of the job: 54% of the words
   * lived on "How to connect", and almost every warning on it existed because a
   * single value — the address partners reach us on — was still `localhost`.
   * Warnings cannot fix a setting; a step that shows a tick when it is right
   * can.
   *
   * Order is the order of the questions people actually arrive with:
   *   1. is it running, and did anything land          → the status strip
   *   2. what came in, and what happened to it         → Files
   *   3. who is allowed to send, and how do I add one  → Partners
   *   4. the things you read once                      → Reference (collapsed)
   */
  import { API_BASE } from '$lib/apiBase.js';
  import { onMount, onDestroy } from 'svelte';
  import {
    Upload,
    RefreshCw,
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
    ChevronDown,
    ChevronRight,
    Loader2
  } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import { ApiError, getJSON } from '$lib/api.js';
  import ErrorState from '$lib/ErrorState.svelte';
  import TerminalCast from '$lib/TerminalCast.svelte';

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

  // ---- "it already loads by itself" ---------------------------------------
  // The page presented two BUTTONS and nothing else, so it read as though a
  // human has to act for a file to land. The watcher has always polled the drop
  // folder and loaded whatever settled there. When the catalogue goes stale that
  // difference is the whole diagnosis: a partner who stopped sending looks
  // exactly like a broken loader if the screen never says which one it is.
  //
  // Ticks so the two relative times below do not freeze at whatever they said
  // when the page opened.
  let now = $state(Date.now());
  let tick;
  // Set by loadFiles. This is when THE CONSOLE last re-read the list — NOT when
  // the watcher last looked at the folder. Nothing records that; no endpoint
  // returns it, so the strip does not claim it.
  let lastRefresh = $state(0);

  // The newest file we hold, from its real mtime. Zero when we hold none.
  const lastArrival = $derived(
    files.length ? Math.max(...files.map((f) => Number(f.mtime) || 0)) * 1000 : 0
  );
  // A day and a half. A daily export that has missed more than one send is worth
  // pointing at — as a fact about the SENDER, not as an error on our side.
  const arrivalStale = $derived(lastArrival > 0 && now - lastArrival > 36 * 3600 * 1000);

  function ago(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return `${s} seconds ago`;
    const m = Math.round(s / 60);
    if (m < 60) return m === 1 ? 'a minute ago' : `${m} minutes ago`;
    const h = Math.round(m / 60);
    if (h < 24) return h === 1 ? 'an hour ago' : `${h} hours ago`;
    const d = Math.round(h / 24);
    return d === 1 ? 'yesterday' : `${d} days ago`;
  }

  async function loadFiles() {
    error = null;
    try {
      const body = await getJSON('/admin/sftp/files');
      files = body.files || [];
      counts = body.counts || counts;
      if (body.poll_seconds != null) pollInput = String(body.poll_seconds);
      if (body.enabled != null) autoLoad = body.enabled;
      lastRefresh = Date.now();
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
        // It renders at the top of the page, above the file list it concerns.
        rejection = body.detail || {};
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

  // ---- the partner drawer --------------------------------------------------
  //
  // Everything about one partner lives in a right-hand drawer rather than in an
  // area unfolded beneath the row. The row itself stays a plain <tr>: the
  // opener is a real <button> in the first cell, so Revoke is still its own tab
  // stop and reachable WITHOUT opening the drawer — pressing it cannot open the
  // panel it lives beside.
  //
  // `drawerKey` holds a LABEL, not a partner object: the key list reloads after
  // a regenerate, and a captured object would keep showing the fingerprint that
  // has just stopped working. Everything the drawer renders is looked up from
  // `keys` by that label, so the panel follows the data.
  let drawerKey = $state('');
  let drawerTitleId = $state(''); // the h2 the dialog is named by
  const drawerPartner = $derived(keys.find((k) => k.label === drawerKey) ?? null);

  // The script for an EXISTING partner, from POST /admin/sftp/partner-script.
  // It comes back with a PLACEHOLDER where the private key goes, because we
  // never stored theirs — that absence is the safe part, so it is stated on
  // screen rather than hidden.
  let scriptText = $state('');
  let scriptLoading = $state(false);
  let scriptError = $state(null);

  async function loadPartnerScript(label) {
    // Blocked means the SERVER has no address to build a runnable script from,
    // and it would refuse. Do not ask; the drawer falls back to the local
    // commands, which shout `SFTP_HOST_NOT_SET` through `h`.
    if (packBlocked) return;
    scriptLoading = true;
    scriptError = null;
    try {
      const body = await getJSON('/admin/sftp/partner-script', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label })
      });
      // Opening a second partner while the first is still in flight must not
      // put one partner's script under another's name.
      if (drawerKey !== label) return;
      scriptText = body.script || '';
    } catch (e) {
      if (drawerKey !== label) return;
      scriptText = '';
      // The object, so the panel can name the status rather than blaming a
      // backend that answered.
      scriptError = e;
    } finally {
      if (drawerKey === label) scriptLoading = false;
    }
  }

  function openPartner(label) {
    drawerKey = label;
    drawerTitleId = 'partner-drawer-title-' + safeName(label);
    // An armed confirm belongs to the panel that armed it. Opening a different
    // partner must not carry a primed destructive action across.
    regenArmed = '';
    scriptText = '';
    scriptError = null;
    scriptLoading = false;
    loadPartnerScript(label);
  }

  function closePartner() {
    drawerKey = '';
    regenArmed = '';
    scriptText = '';
    scriptError = null;
    scriptLoading = false;
    // `generated` is deliberately NOT cleared here. Closing the drawer is not
    // the explicit dismissal, so a freshly regenerated private key is hidden,
    // not destroyed — reopening the partner shows it again. Only "I have saved
    // it" takes it out of state.
  }

  // ---- state A vs state B --------------------------------------------------
  //
  // B is "the complete script exists in this browser, right now, and nowhere
  // else" — which is true of exactly one thing: the response to the regenerate
  // (or generate) this operator just made, held in `generated`. It is never
  // reconstructed and never re-fetched, so the test is simply whether that
  // response belongs to the partner on screen and carries a script.
  //
  // A is everything else: the server-built copy, with the key line blanked.
  const freshScript = $derived(
    generated && generatedFrom === drawerKey && generated.script ? generated.script : ''
  );
  const scriptComplete = $derived(Boolean(freshScript));
  // What the code block shows, and the exact string Copy and Download hand out
  // — one value, so a button can never copy something other than what is read.
  // The local commands are the fallback when the server copy is unavailable;
  // they are built from `h`, which shouts when the address is unusable.
  const scriptShown = $derived(
    freshScript || scriptText || (drawerKey ? partnerCommands(drawerKey) : '')
  );

  /**
   * Hand the operator a .sh of the script above.
   *
   * The script arrives as JSON, so the FILE is built here from that string —
   * Blob, object URL, a temporary anchor, then revoke. (The partner PACK is a
   * different thing entirely: that endpoint returns binary and has its own
   * handler in `downloadPartnerPack`.) The object URL is revoked immediately,
   * or a script that may contain a private key stays readable for as long as
   * the tab is open.
   */
  function downloadScript(label, text) {
    // Belt and braces, like downloadHandover: the buttons are genuinely
    // disabled and the action still refuses. A script carrying an address no
    // partner can reach must not leave this page by any route.
    if (packBlocked || !text) return;
    const url = URL.createObjectURL(new Blob([text], { type: 'text/x-shellscript' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `citycare-${safeName(label)}.sh`;
    a.click();
    URL.revokeObjectURL(url);
  }

  /**
   * What THIS partner types to send us a file.
   *
   * Built from the same resolved `h`/`port`/`user`/`path` as the handover and
   * the casts, and it names the key file the keygen snippet on this page tells
   * them to create — so nothing here teaches a path or an address that does not
   * exist.
   */
  function partnerCommands(label) {
    // `h` already shouts when the address is unusable — see its definition.
    return [
      `# ${label} — send one file`,
      `sftp -i ~/.ssh/pharma_sftp -P ${port} ${user}@${h}`,
      `sftp> cd ${folderPath}`,
      'sftp> put balance_stock_20260803.xlsx',
      'sftp> bye'
    ].join('\n');
  }

  // Step 3. `paste` is preselected because it is the option where the partner's
  // private key never exists on our side at all — there is nothing here to leak.
  let proofMode = $state('paste');
  let labelInput; // focus target for "give one a key"

  async function loadKeys() {
    keysError = null;
    try {
      keys = await getJSON('/admin/sftp/keys');
    } catch (e) {
      if (e?.status === 403) return;
      keys = [];
      // The object: the key list must not print "backend offline" for a 401.
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
      // Step 4 is about this partner now, so it does not have to be re-picked.
      handoverKey = body.label;
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
      if (handoverKey === label) handoverKey = '';
      // The drawer is about a partner that no longer exists. Closing it here
      // also hands focus back through the dialog action rather than leaving it
      // on a control inside a panel about nothing.
      if (drawerKey === label) closePartner();
      await loadKeys();
    } catch (e) {
      toast(reason(e, 'revoke this key'), true);
    } finally {
      keyBusy = false;
      confirmDelete = null;
    }
  }

  // ---- we generate the pair for them --------------------------------------
  //
  // POST /admin/sftp/keys/generate answers with the private key ONCE and never
  // again — nothing stores it, so there is no second chance and no "show me
  // that again". Everything below follows from that one fact:
  //
  //   • it is never logged, never put in a URL, never sent anywhere. The .txt
  //     is built in the browser from the string already on screen;
  //   • the panel is dismissed EXPLICITLY. It must not be able to vanish on a
  //     re-render — the file list polls every ten seconds, and that would
  //     otherwise be able to destroy the only copy mid-copy;
  //   • dismissing clears it out of component state, so it is not still sitting
  //     in memory for the rest of the session.
  let generating = $state(false);
  let generated = $state(null); // { label, fingerprint, public_key, private_key }
  // Which control produced it: 'add' for step 3, otherwise the row's label. One
  // panel, rendered where the operator is looking — not two panels holding two
  // copies of a secret.
  let generatedFrom = $state('');

  async function generateKey() {
    generating = true;
    try {
      const body = await getJSON('/admin/sftp/keys/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: keyLabel.trim() })
      });
      generated = body;
      generatedFrom = 'add';
      handoverKey = body.label;
      keyLabel = '';
      await loadKeys();
    } catch (e) {
      // reason() carries the backend's own sentence for 400 (bad or duplicate
      // label) and 503 (the keys directory is not mounted) — both are things
      // only the backend can know.
      toast(reason(e, 'generate a key'), true);
    } finally {
      generating = false;
    }
  }

  function dismissGenerated() {
    generated = null;
    generatedFrom = '';
  }

  // ---- replace a partner's key, in place ----------------------------------
  //
  // POST /admin/sftp/keys/{label}/regenerate answers with the same shape as
  // /generate plus `previous_fingerprint`, so the panel above renders it
  // unchanged — one private-key panel, one dismissal, one place the value is
  // held. Everything that made that panel safe (explicit dismissal, no logging,
  // no second copy in state) applies here for free.
  //
  // It is DESTRUCTIVE to that partner: the old key stops working the moment the
  // server writes the new one. So it takes two presses — `regenArmed` names the
  // row whose confirm is showing — and `regenBusy` makes a double-click one
  // regeneration rather than two.
  let regenArmed = $state('');
  let regenBusy = $state('');

  async function regenerateKey(label) {
    if (regenBusy) return;
    regenBusy = label;
    try {
      const body = await getJSON(
        '/admin/sftp/keys/' + encodeURIComponent(label) + '/regenerate',
        { method: 'POST' }
      );
      generated = body;
      generatedFrom = label;
      regenArmed = '';
      // The row still shows the fingerprint that has just stopped working.
      await loadKeys();
      // Refresh the PLACEHOLDER copy behind the complete one. `freshScript`
      // wins while the panel is up; the moment it is dismissed the drawer falls
      // back to this, and it must not be the copy fetched for the old key.
      if (drawerKey === label) loadPartnerScript(label);
      toast(
        `${label}'s key was replaced — ${body.previous_fingerprint} is now ${body.fingerprint}. ` +
          'The old key no longer connects.'
      );
    } catch (e) {
      // reason() carries the backend's own sentence: 404 (no such label), 400
      // (a label it will not accept) and 503 (the keys directory is not
      // mounted) are all things only the server can know.
      toast(reason(e, 'replace this key'), true);
    } finally {
      regenBusy = '';
    }
  }

  function downloadPrivateKey() {
    if (!generated?.private_key) return;
    const safe = generated.label.replace(/[^A-Za-z0-9._-]+/g, '-');
    const url = URL.createObjectURL(new Blob([generated.private_key], { type: 'text/plain' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `${safe}-private-key.txt`;
    a.click();
    // Immediately: an object URL is a readable handle on the private key for as
    // long as the tab is open.
    URL.revokeObjectURL(url);
  }

  /** The shared-password row's action, and the pointer out of the password panel. */
  function focusAddPartner() {
    labelInput?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    labelInput?.focus({ preventScroll: true });
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
    toast('Address saved. Every command on this page uses it.');
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
  //
  // Gated on `handoverBlocked`, NOT on `hostKnown`. The original condition only
  // caught an EMPTY address, and `localhost` is not empty — so every command on
  // this page (the snippets, the ready-to-run block, the replay, the per-partner
  // panel) happily printed `pharma@localhost`. Copy and Download were disabled,
  // but the text sits on screen and can be selected by hand, and a few lines of
  // shell are exactly what somebody pastes into an email. One derived value, so
  // all of them shout together instead of each needing its own guard.
  const h = $derived(handoverBlocked ? 'SFTP_HOST_NOT_SET' : host);

  const snippets = $derived([
    {
      key: 'sftp',
      title: 'Command line',
      note: 'one file, by hand',
      body: `sftp -P ${port} ${user}@${h}
# password: the shared one on this page
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
# key for them above and drop SSHPASS entirely.
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

# The name is the contract — see Naming rules at the foot of the page.
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

  // ---- the handover block --------------------------------------------------
  //
  // One block an operator can send a partner as it stands. Every value in it is
  // reported, never invented: port / user / folder come from
  // GET /admin/sftp/connection, the address from the resolution above (env >
  // typed > detected), and the fingerprint from GET /admin/sftp/keys — which is
  // public by definition. A private key is never involved and the shared
  // password is deliberately not in the text.
  let handoverKey = $state(''); // '' = a partner still on the shared password
  const handoverPartner = $derived(keys.find((k) => k.label === handoverKey) ?? null);

  // A handover that cannot work must not be copyable. Three ways the address is
  // unusable to somebody else: we have none; it names THIS machine, which no
  // partner can reach; or it is only the hostname the request happened to
  // arrive on and nobody has confirmed it. In all three the commands below
  // would fail on the partner's side, so Copy and Download are switched off
  // rather than shipped with a caveat nobody reads.
  const handoverBlocked = $derived(!hostKnown || isLocal || usingDetected);

  // The PACK is built by the server, so it is gated on what the SERVER knows —
  // not on what this browser was told.
  //
  // The address field writes to localStorage. Typing a hostname and pressing
  // Save clears `handoverBlocked` for THIS browser, because the handover text
  // is just text the operator is about to send and a typed address is a fine
  // basis for it. The zip is different: `POST /admin/sftp/partner-pack` resolves
  // the address from `SFTP_PUBLIC_HOST` on the server and refuses with 400
  // while it is unset. Gating both on the same flag let the buttons enable
  // against a server that would reject them — measured: with a hostname in
  // localStorage the controls went live while `/admin/sftp/connection` still
  // reported `host_source: "detected"`, `host_configured: false`.
  //
  // `envHost` is only non-empty when host_source === 'env', which is exactly
  // "the server has a real address", so it is the right thing to require.
  const packBlocked = $derived(handoverBlocked || !envHost);
  const packBlockReason = $derived(
    envHost
      ? handoverBlockReason
      : 'The zip is built on the server, which reads the address from SFTP_PUBLIC_HOST. ' +
        'That is not set, so the server would refuse. Typing an address here fills in the ' +
        'commands for you, but it stays in this browser.'
  );
  const handoverBlockReason = $derived(
    !hostKnown
      ? 'No address is set, so there is nothing for a partner to connect to — set the address in step 1.'
      : isLocal
        ? `${host} is this machine, so a partner running these commands would connect to their own computer — set the real hostname in step 1.`
        : 'This address is only the one you opened this page on, not a confirmed setting, so it is probably wrong for a partner — confirm it in step 1.'
  );
  // Step 1 is DONE when the address is one a partner could actually reach. The
  // same condition that gates the handover, stated positively: a tick that can
  // be earned, in place of the four warnings that used to say it negatively.
  const addressReady = $derived(!handoverBlocked);

  const folderPath = $derived(path.replace(/\/$/, ''));

  // ---- the two things a partner has to do, replayed ------------------------
  // Both are built from `h`/`port`/`user`/`path` — the same resolved values the
  // handover block uses — so a cast can never teach an address that does not
  // exist. `handoverBlocked` gates them for the same reason it gates Copy.
  //
  // Output lines are what these commands really print. Nothing is invented to
  // look successful: the sftp session shows the `Connected to` banner and the
  // real `put` progress line, and the key step ends at the fingerprint the
  // operator is told to verify OUT OF BAND, not at a cheerful "done".
  const castSetup = $derived([
    { kind: 'note', text: '# ON THE PARTNER\'S MACHINE — once, to create the key' },
    { kind: 'cmd', text: `ssh-keygen -t ed25519 -f ~/.ssh/pharma_sftp -C "acme-pharma"` },
    { kind: 'out', text: 'Generating public/private ed25519 key pair.' },
    { kind: 'out', text: 'Your identification has been saved in /home/acme/.ssh/pharma_sftp' },
    { kind: 'out', text: 'Your public key has been saved in /home/acme/.ssh/pharma_sftp.pub' },
    { kind: 'note', text: '' },
    { kind: 'note', text: '# read the fingerprint out to us — we check it before we trust the key' },
    { kind: 'cmd', text: 'ssh-keygen -lf ~/.ssh/pharma_sftp.pub' },
    { kind: 'out', text: '256 SHA256:9f3k7Qm1cVb8oTz2yR4sN6wX0aJhL5pD8eG1uY7iK3M acme-pharma (ED25519)' },
    { kind: 'note', text: '' },
    { kind: 'note', text: '# send us the .pub line ONLY — never the file without .pub' },
    { kind: 'cmd', text: 'cat ~/.ssh/pharma_sftp.pub' },
    { kind: 'out', text: 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH8k2… acme-pharma' },
    { kind: 'note', text: '' },
    { kind: 'note', text: '# then WE paste that line into step 3 above, and it is live' },
    { kind: 'note', text: '# on their next connection — no restart.' }
  ]);

  const castSend = $derived([
    { kind: 'note', text: '# ON THE PARTNER\'S MACHINE — every time they send a file' },
    { kind: 'cmd', text: `sftp -i ~/.ssh/pharma_sftp -P ${port} ${user}@${h}` },
    { kind: 'out', text: `Connected to ${h}.` },
    { kind: 'cmd', text: `cd ${path}` },
    { kind: 'cmd', text: 'put articles-export.xlsx' },
    { kind: 'out', text: 'Uploading articles-export.xlsx to /upload/articles-export.xlsx' },
    { kind: 'out', text: 'articles-export.xlsx                     100%  841KB   4.1MB/s   00:00' },
    { kind: 'cmd', text: 'bye' },
    { kind: 'note', text: '' },
    { kind: 'note', text: '# nothing else to do. The folder is watched, and the file loads' },
    { kind: 'note', text: '# by itself — it will appear under Files within about 15 seconds.' }
  ]);

  const handoverText = $derived(
    [
      'SFTP handover — pharmacy stock files',
      '',
      `Host:    ${h}`,
      `Port:    ${port}`,
      `User:    ${user}`,
      `Folder:  ${folderPath}`,
      handoverPartner
        ? `Sign in: your own SSH key, registered with us as "${handoverPartner.label}"`
        : 'Sign in: the shared password — sent to you separately, never written in this file',
      ...(handoverPartner
        ? [
            `Fingerprint: ${handoverPartner.fingerprint}`,
            '',
            'Check that fingerprint against your own key before you connect:',
            '  ssh-keygen -lf /path/to/your_key.pub'
          ]
        : []),
      '',
      'Send one file, by hand:',
      `  sftp -P ${port} ${user}@${h}`,
      `  sftp> cd ${folderPath}`,
      '  sftp> put balance_stock_20260803.xlsx',
      '  sftp> bye',
      '',
      'Or unattended, from a script or a cron job:',
      `  lftp -c "open -p ${port} sftp://${user}@${h}; cd ${folderPath}; put balance_stock_20260803.xlsx"`,
      '',
      'The file NAME decides what we do with it — keep the name your export already',
      'produces, and ask us before you change it.'
    ].join('\n')
  );

  /**
   * Hand the operator a .txt of the block above.
   *
   * Built in the browser from text already on screen — nothing is sent
   * anywhere. The object URL is revoked immediately, or the string is held
   * for as long as the tab is open.
   */
  function downloadHandover() {
    // Belt and braces: the button is genuinely disabled, and the action still
    // refuses. A blocked handover must not leave this page by any route.
    if (handoverBlocked) return;
    const who = handoverPartner ? handoverPartner.label : 'shared-password';
    const url = URL.createObjectURL(new Blob([handoverText], { type: 'text/plain' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `sftp-handover-${who.replace(/[^A-Za-z0-9._-]+/g, '-')}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  }

  // ---- the partner pack ----------------------------------------------------
  //
  // POST /admin/sftp/partner-pack answers with a ZIP the partner can unzip and
  // run. Two modes, and the flag is the whole difference:
  //
  //   include_private_key: false — they generate their own key and send us the
  //     .pub. The zip is the address, the folder, the ssh-keygen line and what
  //     to send back; nothing secret is in it.
  //   include_private_key: true  — the server generates the pair, keeps and
  //     registers the public half, and puts the PRIVATE half in the zip. That
  //     download is the only time it exists, exactly like the panel above.
  //
  // Not `getJSON`: that reads the body as text and parses it, which would eat a
  // zip. A raw fetch is right here for the same reason `download()` is one, and
  // the layout's fetch wrapper still adds the admin bearer, so nothing about
  // authentication changes.
  let packBusy = $state(''); // '' | 'paste' | 'generate' | `row:<label>` — never two at once

  const safeName = (s) => s.replace(/[^A-Za-z0-9._-]+/g, '-');

  /**
   * The filename the SERVER chose, when it sent one — it names the pack after
   * the label it actually used, which is not always the label we typed.
   *
   * RFC 6266 allows `filename="…"` and the percent-encoded `filename*=UTF-8''…`;
   * the encoded form wins where both are present. Any directory part is
   * discarded: this string goes on an <a download> and it is not ours.
   */
  function filenameFrom(disposition) {
    if (!disposition) return '';
    let name = '';
    const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition);
    if (star) {
      try {
        name = decodeURIComponent(star[1].trim());
      } catch {
        /* a malformed encoding is not a reason to fail a good download */
      }
    }
    if (!name) {
      const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(disposition);
      name = plain ? plain[1].trim() : '';
    }
    return name.split(/[\\/]/).pop() || '';
  }

  /**
   * What each refusal actually means, in place of one sentence for all three.
   * Used only when the backend sends no `detail` of its own — its own sentence
   * always outranks ours, because only it knows which half of a 400 it hit.
   */
  function packMessage(status, label) {
    if (status === 400)
      return handoverBlocked
        ? handoverBlockReason
        : `"${label}" is not a name the server will accept — letters, numbers, dots, dashes and underscores.`;
    if (status === 409)
      return `"${label}" is already registered, and generating a key for a name that exists would replace the key that partner is using. Pick another name, or revoke the existing one first.`;
    if (status === 503)
      return 'The keys directory is not mounted on the server, so no key can be written. That is a deployment setting — nothing on this page can fix it.';
    return `The backend answered ${status}.`;
  }

  /**
   * `forLabel` is the ONE difference between step 3's two buttons and the one
   * on an expanded partner row: which name the pack is built for. Step 3 reads
   * the add-a-partner input; a row passes its own label and never touches that
   * input. Everything else — the gate, the refusal messages, the filename the
   * server chose, revoking the object URL — is shared rather than copied.
   *
   * `packBusy` gets a row-specific token so one row's build does not read as
   * the whole page being busy; every existing `Boolean(packBusy)` check keeps
   * meaning "a pack is being built", which is still the right thing to block on
   * — the server writes a key on the generate path and two at once is not a
   * thing to allow.
   */
  async function downloadPartnerPack(withKey, forLabel = null) {
    const label = (forLabel ?? keyLabel).trim();
    // Belt and braces, like downloadHandover: the buttons are genuinely
    // disabled and the action still refuses. The server enforces the address
    // too — this must agree with it, never stand in for it.
    if (handoverBlocked || !label || packBusy) return;
    packBusy = forLabel ? `row:${forLabel}` : withKey ? 'generate' : 'paste';
    const verb = withKey ? 'generate a key and build a pack' : 'build a setup pack';
    try {
      const res = await fetch(base + '/admin/sftp/partner-pack', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label, include_private_key: withKey })
      });
      if (!res.ok) {
        // A success is a zip; a failure is still JSON, so the backend's own
        // sentence survives when it sends one.
        const detail = await res
          .json()
          .then((j) => (typeof j?.detail === 'string' ? j.detail : ''))
          .catch(() => '');
        throw new ApiError(res.status, detail || packMessage(res.status, label));
      }
      const name =
        filenameFrom(res.headers.get('Content-Disposition')) ||
        `citycare-sftp-${safeName(label)}.zip`;
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      a.click();
      // Immediately. With the key inside, an object URL is a readable handle on
      // the private half for as long as the tab is open.
      URL.revokeObjectURL(url);
      if (withKey) {
        // The public half is registered now, so the table above and step 4 are
        // both out of date until this returns.
        await loadKeys();
        if (keys.some((k) => k.label === label)) handoverKey = label;
        keyLabel = '';
        toast(`${name} downloaded — it holds the only copy of ${label}'s private key.`);
      } else {
        // The label is deliberately NOT cleared here: nothing was registered,
        // and it is the name their key gets registered under when it arrives.
        toast(`${name} downloaded — no key in it, so it is safe to email.`);
      }
    } catch (e) {
      toast(reason(e, verb), true);
    } finally {
      packBusy = '';
    }
  }

  // ---- suggest a new shared password ---------------------------------------
  //
  // Deliberately NOT a "set the password" field. The shared password is
  // `get_settings().sftp_password`, read from the environment at boot; there is
  // no endpoint that rotates it and the sftp container has to restart to pick a
  // new one up. A field that appeared to save, and then displayed a password
  // that does not work, would be worse than no field at all — so this generates
  // the value and hands the operator the one line they have to change, and says
  // plainly that nothing has happened yet.
  //
  // No I, l, 1, O or 0: this gets read down a phone to a partner.
  const PW_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
  let newPassword = $state('');

  function generatePassword(len = 24) {
    const a = PW_ALPHABET;
    // Reject the tail of the byte range rather than taking `% 57` of it: 256 is
    // not a multiple of 57, so a plain modulo would make the first 28 letters
    // likelier than the other 29. Math.random is not usable here at any length
    // — it is not a CSPRNG.
    const limit = 256 - (256 % a.length);
    const buf = new Uint8Array(64);
    let out = '';
    while (out.length < len) {
      crypto.getRandomValues(buf);
      for (const b of buf) {
        if (b >= limit) continue;
        out += a[b % a.length];
        if (out.length === len) break;
      }
    }
    return out;
  }

  const envLine = $derived(`SFTP_PASSWORD=${newPassword}`);

  function copy(key, text) {
    navigator.clipboard.writeText(text);
    copied = key;
    setTimeout(() => (copied = ''), 1500);
  }

  // ---- disclosures ---------------------------------------------------------
  // The two Reference rows, and three "there is more if you want it" blocks
  // inside Partners. All closed by default: none is needed to finish the job on
  // screen, and every one of them used to be a permanently-open tab.
  let openRules = $state(false);
  let openClean = $state(false);
  let openSnippets = $state(false);
  let openCasts = $state(false);
  let openPassword = $state(false);

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
    // Separate from the 10s refresh: the strip counts in seconds, and a clock
    // that only moves when the list moves reads as a stopped clock.
    tick = setInterval(() => (now = Date.now()), 1000);
  });
  onDestroy(() => {
    clearInterval(timer);
    clearInterval(tick);
  });
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

<!-- ================= THE PRIVATE KEY, ONCE =================
     Declared at the top level so BOTH things that can produce a private key
     render the same panel: step 3's "Generate a key pair", and Regenerate on a
     partner row. A second copy of this markup would be a second place to get
     the dismissal, the object-URL revoke or the wording wrong, and the value it
     shows exists exactly once.

     `generated` is the only state holding it, so there is one thing to clear
     and one thing that can be cleared. Dismissal stays EXPLICIT — the file list
     polls every ten seconds and anything that cleared on a re-render could
     destroy the only copy mid-copy. -->
{#snippet privateKeyPanel(g)}
  <div class="mt-3 rounded-card border border-danger/40 bg-danger-soft p-3.5">
    <div class="mb-2 flex items-center gap-2">
      <AlertTriangle size={15} class="shrink-0 text-danger" />
      <span class="text-body-sm font-semibold text-ink">
        This is the only time this key is shown
      </span>
    </div>
    <p class="mb-3 text-meta leading-relaxed text-ink">
      Nothing stores the private half — not this page, not the server. Copy or download it now, get
      it to <span class="font-medium">{g.label}</span> safely, then dismiss this. Leaving without
      saving it means generating another.
    </p>

    {#if g.script}
      <!-- The same one-time fact, said about the SCRIPT: the complete file
           exists only because the key above is on screen, so it dies with this
           panel. Everything after the dismissal is the placeholder copy. -->
      <p class="mb-3 text-meta leading-relaxed text-ink">
        A <span class="font-medium">complete</span> setup script for {g.label} exists for as long as
        this panel is open — this key is in it, so it runs as it stands. Download the
        <span class="font-mono">.sh</span> now if that is how you are sending it; once dismissed, the
        only copy left here is the one with the key line blank.
      </p>
    {/if}

    {#if g.previous_fingerprint}
      <!-- Only a REGENERATED key has a previous fingerprint. It is what the
           operator reads back to the partner, and it is the difference between
           "your key changed" and "which of these two is mine now". -->
      <p class="mb-3 rounded-r-panel border-l-[3px] border-warning bg-warning-soft px-3.5 py-2.5 text-meta leading-relaxed text-ink">
        <span class="font-semibold">{g.label}'s old key has stopped working.</span> Anything they had
        set up with it fails until they install the key below. Read them the new fingerprint so they
        can check they installed the right one.
      </p>
    {/if}

    <dl class="mb-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-meta">
      <dt class="text-ink-2">Partner</dt>
      <dd class="m-0 break-all text-ink">{g.label}</dd>
      {#if g.previous_fingerprint}
        <dt class="text-ink-2">Fingerprint before</dt>
        <dd class="m-0 break-all font-mono text-ink-3">{g.previous_fingerprint}</dd>
        <dt class="text-ink-2">Fingerprint now</dt>
      {:else}
        <dt class="text-ink-2">Fingerprint</dt>
      {/if}
      <dd class="m-0 break-all font-mono text-ink">{g.fingerprint}</dd>
    </dl>

    <pre
      class="max-h-40 overflow-auto rounded-panel border border-line bg-surface p-3 text-label leading-relaxed text-ink"><code
        >{g.private_key}</code
      ></pre>

    <div class="mt-3 flex flex-wrap items-center gap-2.5">
      <button
        onclick={() => copy('privkey', g.private_key)}
        class="inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta text-ink-2 hover:bg-surface-2"
      >
        {#if copied === 'privkey'}
          <Check size={13} /> Copied
        {:else}
          <Copy size={13} /> Copy private key
        {/if}
      </button>
      <button
        onclick={downloadPrivateKey}
        class="inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta text-ink-2 hover:bg-surface-2"
      >
        <Download size={13} /> Download .txt
      </button>
      {#if g.script}
        <!-- The COMPLETE file, built here from the JSON string — the pack
             endpoint's zip is a different thing and keeps its own handler.
             Gated like every other download on this page: the script carries
             the address, and an address a partner cannot reach is not worth
             sending. -->
        <button
          onclick={() => downloadScript(g.label, g.script)}
          disabled={packBlocked}
          class="inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta text-ink-2 hover:bg-surface-2 disabled:opacity-60"
        >
          <FileText size={13} /> Download .sh
        </button>
      {/if}
      <button
        onclick={dismissGenerated}
        class="inline-flex items-center gap-1.5 rounded-panel bg-accent px-3 py-1.5 text-meta font-medium text-on-accent hover:bg-accent-hover"
      >
        <Check size={13} /> I have saved it
      </button>
    </div>

    {#if g.script && packBlocked}
      <!-- Why the .sh above is off, at the control. Copy and Download .txt stay
           on: those carry the key, which is the thing that cannot be had
           again. -->
      <p class="mt-2 text-meta leading-relaxed text-ink-2">{packBlockReason}</p>
    {/if}
  </div>
{/snippet}

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

  <!-- ================= 1 · STATUS STRIP =================
       Answers "is this thing running, and did anything land" before anything
       else asks for a decision. `Check now` and `Upload a file` in the header
       are real manual overrides — this is what stops them reading as the only
       way a file can arrive. -->
  <section class="mb-5 rounded-panel border border-line bg-surface px-4 py-3.5">
    <div class="flex items-start gap-2.5">
      {#if autoLoad}
        <RefreshCw size={16} class="mt-0.5 shrink-0 text-accent" />
      {:else}
        <AlertTriangle size={16} class="mt-0.5 shrink-0 text-warning" />
      {/if}
      <div class="min-w-0">
        {#if autoLoad}
          <p class="text-body-sm font-medium text-ink">Files load by themselves.</p>
          <p class="mt-0.5 text-meta leading-relaxed text-ink-2">
            We watch the drop folder and load whatever a partner sends, once it has finished
            arriving — we look
            <span class="font-medium text-ink">every {describeSeconds(Number(pollInput))}</span>.
            Nothing on this page needs pressing.
          </p>
        {:else}
          <p class="text-body-sm font-medium text-ink">Automatic loading is off.</p>
          <p class="mt-0.5 text-meta leading-relaxed text-ink-2">
            We are still watching the folder, but a file that arrives will sit here as
            <span class="font-medium text-ink">Waiting</span> until someone loads it.
          </p>
        {/if}

        <!-- Only values that are actually reported.
             • the interval and the on/off state come from /admin/sftp/files
               (poll_seconds, enabled);
             • the arrival time is the newest file's real mtime;
             • "this list refreshed" is measured in the browser and is named as
               the LIST refreshing, because nothing records when the watcher
               itself last looked. There is no last-scan timestamp in the API,
               so the strip does not print one. -->
        <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-label">
          {#if loading}
            <span class="text-ink-3">Reading the folder…</span>
          {:else if lastArrival}
            <span class={arrivalStale ? 'font-semibold text-warning' : 'text-ink-3'}>
              Last file arrived {ago(now - lastArrival)}
            </span>
          {:else}
            <span class="text-ink-3">No file has ever been sent</span>
          {/if}
          {#if lastRefresh}
            <span class="text-ink-3">This list refreshed {ago(now - lastRefresh)}</span>
          {/if}
        </div>

        {#if arrivalStale}
          <!-- The honest framing. Nothing here is broken; the sender stopped.
               Warning, not danger — this is not our failure to report. -->
          <p
            class="mt-2.5 rounded-r-panel border-l-[3px] border-warning bg-warning-soft px-3.5 py-2.5 text-meta text-ink"
          >
            <span class="font-semibold">Nothing has been sent for a while.</span>
            {#if autoLoad}
              The folder is still being watched and would load a file the moment one landed — so if
              the data looks old, ask the partner whether their export is still running. It is not
              waiting on anything here.
            {:else}
              Automatic loading is off, so even when a file does arrive it will sit here as Waiting
              until someone loads it. Two different things can make the data look old: nobody sent
              anything, or something was sent and is still waiting on this page.
            {/if}
          </p>
        {/if}
      </div>
    </div>
  </section>

  <!-- ================= 2 · FILES ================= -->
  <h2 class="mb-2.5 flex items-center gap-2 text-body-sm font-semibold text-ink">
    <FileText size={16} class="text-ink-2" /> Files
  </h2>

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
          tablist, so a screen reader announced a second, phantom tab set.
          `aria-pressed` is what a toggle button says.
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

  <!-- ================= 3 · PARTNERS ================= -->
  <h2 class="mb-2.5 mt-8 flex items-center gap-2 text-body-sm font-semibold text-ink">
    <KeyRound size={16} class="text-ink-2" /> Partners
  </h2>

  {#if connError}
    <!-- Without `conn` every field below falls back to a built-in default —
         port 2222, user "pharma", a blank password — which reads as the real
         connection details and would be handed to a partner as such. -->
    <ErrorState error={connError} retry={loadConn} what="the connection details" />
  {:else}
    <!-- ---------- who may send ----------
         One table, so "who can put a file in our folder" is one glance rather
         than a comparison between two tabs. The shared password is a ROW in it,
         because it IS one of the ways in — and as a row, its
         indistinguishability is visible instead of argued. -->
    <section class="mb-4 overflow-hidden rounded-panel border border-line bg-surface">
      <div class="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <span class="text-body-sm font-medium text-ink">Who may send us files</span>
        <span class="ml-auto text-meta text-ink-3">
          A key is live on their next connection — no restart
        </span>
      </div>

      {#if keysError}
        <!-- The key list is a security fact. If it did not load, say so with
             the status rather than showing an empty (= "no keys") table. -->
        <div class="p-4">
          <ErrorState error={keysError} retry={loadKeys} what="the partner keys" />
        </div>
      {:else}
        <div class="overflow-x-auto">
          <table class="tbl">
            <thead>
              <tr>
                <th>Who</th>
                <th>Signs in with</th>
                <th>Fingerprint</th>
                <th class="hidden sm:table-cell">Added</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {#each keys as k (k.label)}
                <tr>
                  <td class="text-ink">
                    <!-- The opener is a BUTTON inside the row, not the row
                         wearing role="button". A <tr> with a role and a tabindex
                         that also contains Revoke is two interactive things in
                         one, and the inner one then has to fight the outer one's
                         handler to be pressable at all. This way Revoke stays
                         its own tab stop and stays reachable WITHOUT opening the
                         drawer.

                         `aria-haspopup="dialog"`, not `aria-expanded`: what this
                         opens is a modal panel elsewhere in the document, not a
                         region unfolded beneath the row. The drawer names itself
                         from its own heading; a control cannot `aria-controls` a
                         node that does not exist while it is pressed. -->
                    <button
                      onclick={() => openPartner(k.label)}
                      aria-haspopup="dialog"
                      class="flex w-full items-center gap-1.5 rounded-panel px-1.5 py-0.5 text-left text-ink hover:bg-surface-2"
                    >
                      <ChevronRight size={14} class="shrink-0 text-ink-3" />
                      <span class="min-w-0 break-all">{k.label}</span>
                    </button>
                  </td>
                  <td>
                    <span
                      class="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-success-soft px-2.5 py-0.5 text-label font-semibold text-success"
                    >
                      <KeyRound size={12} /> Own key
                    </span>
                    <span class="ml-1.5 text-label text-ink-3">{k.type}</span>
                  </td>
                  <td class="break-all font-mono text-label text-ink-2">{k.fingerprint}</td>
                  <td class="hidden sm:table-cell text-ink-2">{addedOn(k.added_at)}</td>
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
                        class="inline-flex items-center gap-1.5 rounded-panel border border-line px-2 py-1 text-meta text-ink-3 hover:bg-surface-2 hover:text-danger"
                      >
                        <Trash2 size={13} /> Revoke
                      </button>
                    {/if}
                  </td>
                </tr>
              {/each}

              <!-- The row that is always there. Every partner without a key of
                   their own is on this one account: one password, no way to
                   tell two senders apart, no way to cut one off. -->
              <tr>
                <td class="text-ink">everyone else</td>
                <td>
                  <span
                    class="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-warning-soft px-2.5 py-0.5 text-label font-semibold text-warning"
                  >
                    <Lock size={12} /> Shared password
                  </span>
                </td>
                <td class="text-ink-3">no way to tell them apart</td>
                <td class="hidden sm:table-cell text-ink-3">—</td>
                <td class="text-right">
                  <button
                    onclick={focusAddPartner}
                    class="inline-flex items-center gap-1.5 rounded-panel border border-line px-2 py-1 text-meta text-ink-2 hover:bg-surface-2 hover:text-ink"
                  >
                    <Plus size={13} /> Give one a key
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- The shared password itself: still readable, still copyable, still
             able to suggest a replacement. Collapsed, because on a healthy
             install nobody should be reaching for it. -->
        <div class="border-t border-line">
          <button
            onclick={() => (openPassword = !openPassword)}
            aria-expanded={openPassword}
            class="flex w-full items-center gap-2 px-4 py-3 text-left text-body-sm font-medium text-ink hover:bg-surface-2"
          >
            <Lock size={15} class="text-ink-2" />
            The shared password
            <span class="text-meta font-normal text-ink-3">read it, copy it, or suggest a new one</span>
            <ChevronDown
              size={15}
              class="ml-auto shrink-0 text-ink-3 transition-transform {openPassword ? 'rotate-180' : ''}"
            />
          </button>

          {#if openPassword}
            <div class="border-t border-line px-4 py-3.5">
              <!-- Above the value, not under it: the consequence has to be
                   readable at the moment the password is read, not after it has
                   already been copied. -->
              <p
                class="mb-3 rounded-r-panel border-l-[3px] border-danger bg-danger-soft px-3.5 py-2.5 text-meta leading-relaxed text-ink"
              >
                <span class="font-semibold">One account, shared by every partner.</span> There is no
                way to tell one sender from another, and no way to cut one off without cutting off
                all of them. <span class="font-semibold">This console cannot change it</span> — the
                file service reads it from its environment once, when it starts. Rotating it means
                editing that setting and restarting, and the moment you do, every partner is cut off
                at once until each has been told the new one.
              </p>

              <div class="flex flex-wrap items-center gap-2.5">
                <code
                  class="rounded-panel border border-line bg-surface-2 px-3.5 py-2 text-body-sm tracking-wider text-ink"
                  >{revealed ? (conn?.password ?? '') : '••••••••'}</code
                >
                <button
                  onclick={() => (revealed = !revealed)}
                  class="inline-flex items-center gap-1.5 rounded-panel border border-line px-2.5 py-1.5 text-meta text-ink-2 hover:bg-surface-2"
                >
                  {#if revealed}<EyeOff size={13} /> Hide{:else}<Eye size={13} /> Show{/if}
                </button>
                <button
                  onclick={() => copy('pw', conn?.password ?? '')}
                  class="inline-flex items-center gap-1.5 rounded-panel border border-line px-2.5 py-1.5 text-meta text-ink-2 hover:bg-surface-2"
                >
                  {#if copied === 'pw'}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
                </button>
                <button
                  onclick={() => (newPassword = generatePassword())}
                  class="inline-flex items-center gap-1.5 rounded-panel border border-line px-2.5 py-1.5 text-meta text-ink-2 hover:bg-surface-2"
                >
                  <RefreshCw size={13} />
                  {newPassword ? 'Generate another' : 'Generate new…'}
                </button>
              </div>

              {#if newPassword}
                <!-- A SUGGESTION, not a setting. Nothing on this page can change
                     the running password: the sftp service reads SFTP_PASSWORD
                     from the environment once, at boot, and no endpoint rewrites
                     it. So this gives the value and the exact line, and is
                     explicit that the live password is still the one above. -->
                <div class="mt-3 rounded-card border border-line bg-surface-2 p-3.5">
                  <div class="mb-2.5 flex items-center gap-2">
                    <span class="text-body-sm font-semibold text-ink">A new password to put in place</span>
                    <button
                      onclick={() => (newPassword = '')}
                      aria-label="Discard this suggestion"
                      class="ml-auto rounded-panel p-1 text-ink-3 hover:bg-surface hover:text-ink"
                    >
                      <X size={15} />
                    </button>
                  </div>

                  <div class="flex flex-wrap items-center gap-2.5">
                    <code
                      class="break-all rounded-panel border border-line bg-surface px-3.5 py-2 font-mono text-body-sm text-ink"
                      >{newPassword}</code
                    >
                    <button
                      onclick={() => copy('newpw', newPassword)}
                      class="inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta text-ink-2 hover:bg-surface-2"
                    >
                      {#if copied === 'newpw'}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
                    </button>
                  </div>

                  <p class="mt-3 text-meta font-medium text-ink-2">
                    Change this one line where the service is configured:
                  </p>
                  <div class="mt-1.5 flex flex-wrap items-center gap-2.5">
                    <code
                      class="break-all rounded-panel border border-line bg-surface px-3.5 py-2 font-mono text-meta text-ink"
                      >{envLine}</code
                    >
                    <button
                      onclick={() => copy('envline', envLine)}
                      class="inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta text-ink-2 hover:bg-surface-2"
                    >
                      {#if copied === 'envline'}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
                    </button>
                  </div>

                  <p
                    class="mt-3 rounded-r-panel border-l-[3px] border-warning bg-warning-soft px-3.5 py-2.5 text-meta leading-relaxed text-ink"
                  >
                    <span class="font-semibold">Nothing has changed yet.</span> This console cannot set
                    the shared password — the file service reads it from its environment once, when it
                    starts. It becomes the real password only after you change that line and restart
                    the sftp container. Until then the password above is still the live one, and every
                    partner keeps using it. Tell them before you restart: the moment you do, the old
                    password stops working for all of them at once.
                  </p>
                  <p class="mt-2 text-meta text-ink-3">
                    Generated in this browser and never sent anywhere. Close this page and it is gone
                    — copy it somewhere safe first.
                  </p>
                </div>
              {/if}

              <p class="mt-3 text-meta text-ink-3">
                A key names one partner, and revoking it cuts off only them.
                <button
                  onclick={focusAddPartner}
                  class="ml-1 inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1 text-meta font-medium text-ink hover:bg-surface-2"
                >
                  <KeyRound size={13} /> Add a partner
                </button>
              </p>
            </div>
          {/if}
        </div>
      {/if}
    </section>

    <!-- ---------- add a partner, in four steps ----------
         The old "How to connect" tab was 1,062 words and 30 controls, and most
         of its warnings existed because ONE value was unset. Four steps in
         order, step 1 carrying a tick it can earn, replaces the argument with a
         state. -->
    <section class="rounded-panel border border-line bg-surface p-4">
      <h3 class="mb-1 flex items-center gap-2 text-body-sm font-semibold text-ink">
        <Plus size={16} class="text-ink-2" /> Add a partner
      </h3>
      <p class="mb-4 text-meta text-ink-2">
        Four steps. Everything the partner needs comes out of step 4 as one block you can send them.
      </p>

      <ol class="list-none space-y-3 p-0">
        <!-- ---- 1 · where they reach us ---- -->
        <li class="rounded-card border border-line bg-surface-2 p-3.5">
          <div class="mb-2.5 flex flex-wrap items-center gap-2.5">
            <span
              class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface text-meta font-bold text-ink-2"
              aria-hidden="true">1</span
            >
            <span class="text-body-sm font-semibold text-ink">Where partners reach us</span>
            {#if addressReady}
              <span
                class="ml-auto inline-flex items-center gap-1.5 whitespace-nowrap rounded-full bg-success-soft px-2.5 py-0.5 text-label font-semibold text-success"
              >
                <Check size={12} /> Address set
              </span>
            {:else}
              <span
                class="ml-auto inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border border-line bg-surface px-2.5 py-0.5 text-label font-semibold text-ink-3"
              >
                Not set yet
              </span>
            {/if}
          </div>

          {#if envHost}
            <p class="mb-2.5 text-meta text-ink-2">
              Set on the server — this is the address, not a guess.
            </p>
          {:else if isLocal}
            <p
              class="mb-2.5 rounded-r-panel border-l-[3px] border-danger bg-danger-soft px-3.5 py-2 text-meta leading-relaxed text-ink"
            >
              <span class="font-mono">{host}</span> is this machine — a partner running these commands
              would connect to their own computer. Type the hostname they can reach us on.
            </p>
          {:else if usingDetected}
            <p
              class="mb-2.5 rounded-r-panel border-l-[3px] border-warning bg-warning-soft px-3.5 py-2 text-meta leading-relaxed text-ink"
            >
              This is only the address you opened this page on. Behind a proxy, or if the file port is
              published on a different name, it is wrong — confirm or correct it, then Save.
            </p>
          {/if}

          <div class="flex flex-wrap items-end gap-2">
            <label class="min-w-[220px] flex-1">
              <span class="mb-1.5 block text-micro font-bold uppercase tracking-wider text-ink-3"
                >Address</span
              >
              <input
                type="text"
                value={hostInput}
                oninput={(e) => (hostInput = e.currentTarget.value)}
                disabled={Boolean(envHost)}
                spellcheck="false"
                placeholder="sftp.yourcompany.com"
                class="w-full rounded-panel border border-line bg-surface px-3 py-2 text-body-sm text-ink outline-none focus:border-accent disabled:opacity-60"
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

          <div class="mt-3 flex flex-wrap gap-x-7 gap-y-1.5 border-t border-line pt-2.5 text-meta">
            <span><span class="text-ink-3">Port</span> <span class="tnum font-mono text-ink">{port}</span></span>
            <span><span class="text-ink-3">User</span> <span class="font-mono text-ink">{user}</span></span>
            <span><span class="text-ink-3">Folder</span> <span class="font-mono text-ink">{path}</span></span>
          </div>
        </li>

        <!-- ---- 2 · who is it for ---- -->
        <li class="rounded-card border border-line bg-surface-2 p-3.5">
          <div class="mb-2.5 flex items-center gap-2.5">
            <span
              class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface text-meta font-bold text-ink-2"
              aria-hidden="true">2</span
            >
            <span class="text-body-sm font-semibold text-ink">Who is it for</span>
          </div>
          <label class="block max-w-sm">
            <span class="mb-1.5 block text-micro font-bold uppercase tracking-wider text-ink-3">
              Partner name
            </span>
            <input
              type="text"
              bind:this={labelInput}
              bind:value={keyLabel}
              spellcheck="false"
              placeholder="e.g. acme-pharma"
              class="w-full rounded-panel border border-line bg-surface px-3 py-2 text-body-sm text-ink outline-none focus:border-accent"
            />
          </label>
          <p class="mt-2 text-meta text-ink-3">
            The name their key is listed and revoked under.
          </p>
        </li>

        <!-- ---- 3 · how they prove it is them ---- -->
        <li class="rounded-card border border-line bg-surface-2 p-3.5">
          <div class="mb-2.5 flex items-center gap-2.5">
            <span
              class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface text-meta font-bold text-ink-2"
              aria-hidden="true">3</span
            >
            <span class="text-body-sm font-semibold text-ink">How do they prove it is them</span>
          </div>

          <!-- Each option is a wrapper, not a bare button: the download control
               sits ON the card it belongs to, and a <button> cannot be nested
               inside the picker <button>. -->
          <div class="grid gap-2.5 sm:grid-cols-2">
            <div class="flex flex-col gap-2">
              <!-- Preselected, and recommended, because in this option their
                   private key never exists on our side at all. -->
              <button
                onclick={() => (proofMode = 'paste')}
                aria-pressed={proofMode === 'paste'}
                class="rounded-card border p-3 text-left transition-colors
                  {proofMode === 'paste'
                  ? 'border-accent bg-accent-soft'
                  : 'border-line bg-surface hover:bg-surface-2'}"
              >
                <span class="flex flex-wrap items-center gap-2">
                  <span class="text-body-sm font-semibold text-ink">They send us their key</span>
                  <span
                    class="rounded-full bg-success-soft px-1.5 py-0.5 text-micro font-semibold uppercase tracking-wide text-success"
                    >Recommended</span
                  >
                </span>
                <span class="mt-1 block text-meta leading-relaxed text-ink-2">
                  Their private key never leaves their machine, so there is nothing here to leak.
                </span>
              </button>

              <button
                onclick={() => downloadPartnerPack(false)}
                disabled={packBlocked || !keyLabel.trim() || Boolean(packBusy)}
                class="inline-flex items-center justify-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta font-medium text-ink-2 hover:bg-surface-2 disabled:opacity-60"
              >
                {#if packBusy === 'paste'}
                  <Loader2 size={13} class="animate-spin" />
                {:else}
                  <Download size={13} />
                {/if}
                {packBusy === 'paste' ? 'Building the pack…' : 'Download setup pack'}
              </button>
              {#if packBlocked}
                <p class="mt-1.5 text-meta leading-relaxed text-ink-3">{packBlockReason}</p>
              {/if}
              <p class="text-label leading-relaxed text-ink-3">
                A zip they unzip and run: the address and folder from step 1, the one command that
                makes their key, and what to send back. Nothing secret in it.
              </p>
            </div>

            <div class="flex flex-col gap-2">
              <button
                onclick={() => (proofMode = 'generate')}
                aria-pressed={proofMode === 'generate'}
                class="rounded-card border p-3 text-left transition-colors
                  {proofMode === 'generate'
                  ? 'border-accent bg-accent-soft'
                  : 'border-line bg-surface hover:bg-surface-2'}"
              >
                <span class="text-body-sm font-semibold text-ink">We generate one for them</span>
                <span class="mt-1 block text-meta leading-relaxed text-ink-2">
                  Shown once, never stored. You are then responsible for getting it to them safely.
                </span>
              </button>

              <!-- Before the button, not after it. The zip carries the private
                   half and nothing keeps a copy, so pressing this is the whole
                   of the partner's ability to connect — it has to read as a
                   deliberate act at the moment of pressing. -->
              <p
                class="rounded-r-panel border-l-[3px] border-warning bg-warning-soft px-2.5 py-1.5 text-label leading-relaxed text-ink"
              >
                The zip contains the private key, and this download is the only time it can be
                obtained.
              </p>
              <button
                onclick={() => downloadPartnerPack(true)}
                disabled={packBlocked || !keyLabel.trim() || Boolean(packBusy)}
                class="inline-flex items-center justify-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta font-medium text-ink-2 hover:bg-surface-2 disabled:opacity-60"
              >
                {#if packBusy === 'generate'}
                  <Loader2 size={13} class="animate-spin" />
                {:else}
                  <KeyRound size={13} />
                {/if}
                {packBusy === 'generate'
                  ? 'Generating the key…'
                  : 'Download pack with the private key'}
              </button>
              {#if packBlocked}
                <p class="mt-1.5 text-meta leading-relaxed text-ink-3">{packBlockReason}</p>
              {/if}
            </div>
          </div>

          <!-- Why the two controls above are off, at the controls. The server
               refuses the same cases; this must agree with it and never be the
               only thing that checks. -->
          {#if handoverBlocked}
            <p
              class="mt-2.5 rounded-r-panel border-l-[3px] border-danger bg-danger-soft px-3.5 py-2 text-meta leading-relaxed text-ink"
            >
              <span class="font-semibold">Neither pack can be built yet.</span>
              {handoverBlockReason}
            </p>
          {:else if !keyLabel.trim()}
            <p class="mt-2.5 text-meta text-ink-3">
              A pack is named after the partner it is for — type a name in step 2 to build one.
            </p>
          {/if}

          {#if proofMode === 'paste'}
            <div class="mt-3">
              <label class="block">
                <span class="mb-1.5 block text-micro font-bold uppercase tracking-wider text-ink-3">
                  Their public key
                </span>
                <textarea
                  bind:value={keyMaterial}
                  spellcheck="false"
                  rows="3"
                  placeholder="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA… partner@corp"
                  class="w-full resize-y rounded-panel border border-line bg-surface px-3 py-2 font-mono text-meta text-ink outline-none focus:border-accent"
                ></textarea>
              </label>
              <button
                onclick={addKey}
                disabled={keyBusy || !keyLabel.trim() || !keyMaterial.trim()}
                class="mt-2 inline-flex items-center gap-1.5 rounded-panel bg-accent px-3 py-2 text-meta font-medium text-on-accent hover:bg-accent-hover disabled:opacity-60"
              >
                {#if keyBusy}<Loader2 size={14} class="animate-spin" />{:else}<Plus size={14} />{/if}
                Register key
              </button>
              <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
                <span class="font-medium text-ink-2">Check the fingerprint against what they read you</span>
                from <span class="font-mono">ssh-keygen -lf</span>. A key pasted out of an unverified
                email is a way in for whoever sent that email. The <span class="font-mono">.pub</span>
                line only — never their private key.
              </p>

              <div class="mt-3 overflow-hidden rounded-card border border-line">
                <div class="flex items-center gap-2 border-b border-line bg-surface px-3.5 py-2">
                  <span class="text-meta font-semibold text-ink">Send them this to run</span>
                  <button
                    onclick={() => copy('keygen', keygenSnippet)}
                    class="ml-auto inline-flex items-center gap-1.5 rounded-panel border border-line px-2.5 py-1 text-meta text-ink-2 hover:bg-surface-2"
                  >
                    {#if copied === 'keygen'}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
                  </button>
                </div>
                <pre
                  class="overflow-x-auto bg-surface p-4 text-meta leading-relaxed text-ink"><code
                    >{keygenSnippet}</code
                  ></pre>
              </div>
            </div>
          {:else}
            <div class="mt-3">
              <button
                onclick={generateKey}
                disabled={generating || !keyLabel.trim() || Boolean(generated)}
                class="inline-flex items-center gap-1.5 rounded-panel bg-accent px-3 py-2 text-meta font-medium text-on-accent hover:bg-accent-hover disabled:opacity-60"
              >
                {#if generating}<Loader2 size={14} class="animate-spin" />{:else}<KeyRound size={14} />{/if}
                Generate a key pair
              </button>
              <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
                We keep the public half and register it. The private half is shown to you
                <span class="font-medium text-ink-2">once</span> and is never stored — if it is lost,
                the only way back is to generate another. Send it on a different channel from the
                address.
              </p>

              <!-- Rendered where it was produced. A key regenerated from a
                   partner row shows on THAT row instead, so the only copy is
                   never on screen in two places at once. -->
              {#if generated && generatedFrom === 'add'}
                {@render privateKeyPanel(generated)}
              {/if}
            </div>
          {/if}
        </li>

        <!-- ---- 4 · send them the handover ---- -->
        <li class="rounded-card border border-line bg-surface-2 p-3.5">
          <div class="mb-2.5 flex items-center gap-2.5">
            <span
              class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface text-meta font-bold text-ink-2"
              aria-hidden="true">4</span
            >
            <span class="text-body-sm font-semibold text-ink">Send them the handover</span>
          </div>

          <label class="mb-3 block max-w-xs">
            <span class="mb-1.5 block text-micro font-bold uppercase tracking-wider text-ink-3">
              Who it is for
            </span>
            <select
              bind:value={handoverKey}
              class="w-full rounded-panel border border-line bg-surface px-2.5 py-1.5 text-body-sm text-ink outline-none focus:border-accent"
            >
              <option value="">A partner on the shared password</option>
              {#each keys as k (k.label)}
                <option value={k.label}>{k.label} — signs in with their own key</option>
              {/each}
            </select>
          </label>

          {#if handoverBlocked}
            <!-- Not a caveat beside a working button: the buttons are OFF. A
                 handover that cannot work must not be able to leave this page. -->
            <p
              class="mb-3 rounded-r-panel border-l-[3px] border-danger bg-danger-soft px-3.5 py-2.5 text-meta leading-relaxed text-ink"
            >
              <span class="font-semibold">This cannot be sent yet.</span>
              {handoverBlockReason}
            </p>
          {/if}

          <div class="overflow-hidden rounded-card border border-line">
            <div class="flex flex-wrap items-center gap-2 border-b border-line bg-surface px-3.5 py-2">
              <span class="text-meta font-semibold text-ink">Handover</span>
              <span class="text-label text-ink-3">
                {handoverPartner ? `for ${handoverPartner.label}` : 'shared password'}
              </span>
              <button
                onclick={() => copy('handover', handoverText)}
                disabled={handoverBlocked}
                class="ml-auto inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2 py-0.5 text-meta text-ink-2 hover:bg-surface-2 disabled:opacity-60"
              >
                {#if copied === 'handover'}<Check size={12} /> Copied{:else}<Copy size={12} /> Copy{/if}
              </button>
              <button
                onclick={downloadHandover}
                disabled={handoverBlocked}
                class="inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2 py-0.5 text-meta text-ink-2 hover:bg-surface-2 disabled:opacity-60"
              >
                <Download size={12} /> Download .txt
              </button>
            </div>
            <pre class="overflow-x-auto bg-surface px-3.5 py-3 text-label leading-relaxed text-ink-2"><code
                >{handoverText}</code
              ></pre>
          </div>

          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            The shared password is deliberately not in this block — send it on a different channel
            from the address, or give them a key and stop sending passwords at all. The fingerprint is
            safe to send: it is public, and it is what lets them check we hold the right key.
          </p>

          <!-- The same two steps, watchable. AFTER the copyable text, not
               instead of it: the text is what an operator actually sends, and a
               replay is no use in an email. -->
          <div class="mt-3 overflow-hidden rounded-card border border-line">
            <button
              onclick={() => (openCasts = !openCasts)}
              aria-expanded={openCasts}
              class="flex w-full items-center gap-2 bg-surface px-3.5 py-2 text-left text-meta font-semibold text-ink hover:bg-surface-2"
            >
              <Play size={14} class="text-ink-2" />
              Watch what the partner does
              <span class="font-normal text-ink-3">create the key, then send a file</span>
              <ChevronDown
                size={14}
                class="ml-auto shrink-0 text-ink-3 transition-transform {openCasts ? 'rotate-180' : ''}"
              />
            </button>
            {#if openCasts}
              <div class="space-y-3 border-t border-line bg-surface p-3.5">
                <TerminalCast title="1 · Create the key (once)" lines={castSetup} blocked={false} />
                <TerminalCast
                  title="2 · Send a file (every time)"
                  lines={castSend}
                  blocked={handoverBlocked}
                  blockedReason={handoverBlockReason}
                />
              </div>
            {/if}
          </div>

          <!-- The five ready-to-run recipes. Collapsed: the handover above
               already carries the two commands most partners need, and every
               value in here is filled in from step 1 either way. -->
          <div class="mt-2.5 overflow-hidden rounded-card border border-line">
            <button
              onclick={() => (openSnippets = !openSnippets)}
              aria-expanded={openSnippets}
              class="flex w-full items-center gap-2 bg-surface px-3.5 py-2 text-left text-meta font-semibold text-ink hover:bg-surface-2"
            >
              <FileText size={14} class="text-ink-2" />
              Other ways to send
              <span class="font-normal text-ink-3">command line, scp, cron, Python, WinSCP</span>
              <ChevronDown
                size={14}
                class="ml-auto shrink-0 text-ink-3 transition-transform {openSnippets ? 'rotate-180' : ''}"
              />
            </button>
            {#if openSnippets}
              <div class="space-y-3 border-t border-line bg-surface p-3.5">
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
            {/if}
          </div>
        </li>
      </ol>
    </section>
  {/if}

  <!-- ================= 4 · REFERENCE =================
       Two things you read once and never again. Closed by default, and neither
       is needed to finish anything above. -->
  <h2 class="mb-2.5 mt-8 text-body-sm font-semibold text-ink">Reference</h2>

  <div class="overflow-hidden rounded-panel border border-line bg-surface">
    <!-- ---- naming rules ---- -->
    <button
      onclick={() => (openRules = !openRules)}
      aria-expanded={openRules}
      class="flex w-full items-center gap-2 px-4 py-3 text-left text-body-sm font-medium text-ink hover:bg-surface-2"
    >
      <FileCheck2 size={15} class="text-ink-2" />
      Naming rules
      <span class="text-meta font-normal text-ink-3">the name decides what a file is</span>
      <ChevronDown
        size={15}
        class="ml-auto shrink-0 text-ink-3 transition-transform {openRules ? 'rotate-180' : ''}"
      />
    </button>
    {#if openRules}
      <div class="border-t border-line px-4 py-3.5">
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
      </div>
    {/if}

    <!-- ---- clean up ---- -->
    <button
      onclick={() => (openClean = !openClean)}
      aria-expanded={openClean}
      class="flex w-full items-center gap-2 border-t border-line px-4 py-3 text-left text-body-sm font-medium text-ink hover:bg-surface-2"
    >
      <Eraser size={15} class="text-ink-2" />
      Clean up
      <span class="text-meta font-normal text-ink-3">stale products, and load everything waiting</span>
      <ChevronDown
        size={15}
        class="ml-auto shrink-0 text-ink-3 transition-transform {openClean ? 'rotate-180' : ''}"
      />
    </button>
    {#if openClean}
      <div class="border-t border-line px-4 py-3.5">
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

        <div class="mt-5 border-t border-line pt-3.5">
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
        </div>
      </div>
    {/if}
  </div>
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

<!-- ================= THE PARTNER DRAWER =================
     Everything about one partner, in one panel, instead of an area unfolded
     under the row. Nothing here is new behaviour: the commands, the pack, the
     `packBlocked` gate, Regenerate's two presses and the one-time private-key
     panel are the same code that used to render inside the table.

     `drawerPartner` is looked up from `keys` by label on every render, so a
     regenerate that reloads the list updates the fingerprint in the header
     rather than leaving the drawer showing the key that has just stopped
     working. -->
{#if drawerPartner}
  <!-- Pointer affordance only. Escape is the keyboard route out and the
       drawer's use:dialog owns it. -->
  <div
    class="fixed inset-0 z-40 cursor-default bg-black/35"
    onclick={closePartner}
    aria-hidden="true"
  ></div>
  <!-- use:dialog does the four things this panel has to do for a keyboard —
       focus in, Tab trapped, Escape on `window`, focus back to the row button
       that opened it — plus the scroll lock that stops the page moving behind
       it. It is the same action the file drawer uses; a second implementation
       here is exactly how five of this console's dialogs used to disagree. -->
  <div
    use:dialog={{ onclose: closePartner }}
    class="fixed bottom-0 right-0 top-0 z-50 flex w-full max-w-[640px] flex-col border-l border-line bg-surface shadow-2xl outline-none"
    role="dialog"
    aria-modal="true"
    aria-labelledby={drawerTitleId}
    tabindex="-1"
  >
    <div class="flex items-start gap-3 border-b border-line px-5 py-4">
      <div class="min-w-0 flex-1">
        <p class="text-micro font-bold uppercase tracking-wider text-ink-3">Partner</p>
        <h2
          id={drawerTitleId}
          class="mt-1 break-all text-body-sm font-semibold leading-snug text-ink"
        >
          {drawerPartner.label}
        </h2>
        <!-- The FULL fingerprint, not the row's truncation: this is the string
             an operator reads back to a partner, and half of it proves
             nothing. -->
        <p class="mt-1.5 break-all font-mono text-label text-ink-2">{drawerPartner.fingerprint}</p>
        <p class="mt-1 text-label text-ink-3">
          {drawerPartner.type} · added {addedOn(drawerPartner.added_at)}
        </p>
      </div>
      <button
        onclick={closePartner}
        aria-label="Close"
        class="rounded-panel p-1.5 text-ink-3 hover:bg-surface-2 hover:text-ink"
      >
        <X size={17} />
      </button>
    </div>

    <div class="flex-1 overflow-y-auto px-5 py-4">
      <!-- ---- where they connect ---- -->
      <p class="text-micro font-bold uppercase tracking-wider text-ink-3">Where they connect</p>
      <dl class="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-meta">
        <dt class="text-ink-3">Host</dt>
        <!-- `h`, never `host`: it shouts SFTP_HOST_NOT_SET while the address is
             unusable, so nothing on this page can be read off the screen and
             pasted into a real script. -->
        <dd class="m-0 break-all font-mono text-ink">{h}</dd>
        <dt class="text-ink-3">Port</dt>
        <dd class="m-0 font-mono text-ink">{port}</dd>
        <dt class="text-ink-3">User</dt>
        <dd class="m-0 break-all font-mono text-ink">{user}</dd>
        <dt class="text-ink-3">Folder</dt>
        <dd class="m-0 break-all font-mono text-ink">{folderPath}</dd>
      </dl>

      <!-- ---- the script they run ---- -->
      <div class="mt-5 border-t border-line pt-3.5">
        <p class="text-micro font-bold uppercase tracking-wider text-ink-3">
          The script {drawerPartner.label} runs
        </p>

        {#if scriptComplete}
          <!-- B. The regenerate that just happened is the only reason a
               complete file exists anywhere. -->
          <p
            class="mt-2 rounded-r-panel border-l-[3px] border-success bg-success-soft px-3.5 py-2.5 text-meta leading-relaxed text-ink"
          >
            <span class="font-semibold">This is the complete script, and this is the only time it
              exists.</span>
            The key just generated is in it, so it runs as it stands. Save it now — once the panel
            below is dismissed, the copy this page can build has the key line blank again.
          </p>
        {:else if scriptText}
          <!-- A. The server-built copy, with the key blanked. Said plainly,
               because an operator who emails this expecting it to work is the
               failure this wording exists to prevent. -->
          <p
            class="mt-2 rounded-r-panel border-l-[3px] border-warning bg-warning-soft px-3.5 py-2.5 text-meta leading-relaxed text-ink"
          >
            <span class="font-semibold">This copy will not run as it stands.</span>
            Where the private key belongs, it has a placeholder — {drawerPartner.label}'s key was
            never stored, not on this page and not on the server, and that is exactly what makes this
            the safe way round. Either they paste their own key into it, or you regenerate below and
            get one complete file, once.
          </p>
        {:else if !scriptLoading}
          <p class="mt-2 text-meta leading-relaxed text-ink-2">
            Their setup script could not be built, so this is the connection commands this page
            writes itself — the same address, folder and file name, without the rest of the script.
          </p>
        {/if}

        {#if scriptLoading}
          <p class="mt-2 inline-flex items-center gap-1.5 text-meta text-ink-3">
            <Loader2 size={13} class="animate-spin" /> Building their script…
          </p>
        {/if}

        <pre
          class="mt-2 max-h-80 overflow-auto rounded-card border border-line bg-surface-2 p-3 text-label leading-relaxed text-ink-2"><code
            >{scriptShown}</code
          ></pre>

        <div class="mt-2.5 flex flex-wrap items-center gap-2">
          <!-- Copy and Download hand out `scriptShown` — the same value the
               block above renders — so a button can never give out something
               other than what was read. Both off while the address is
               unusable, for the same reason the pack is. -->
          <button
            onclick={() => copy(`script:${drawerPartner.label}`, scriptShown)}
            disabled={packBlocked || !scriptShown}
            class="inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta font-medium text-ink-2 hover:bg-surface-2 disabled:opacity-60"
          >
            {#if copied === `script:${drawerPartner.label}`}
              <Check size={13} /> Copied
            {:else}
              <Copy size={13} /> Copy script
            {/if}
          </button>
          <!-- Built in the browser from the JSON string. The pack below is a
               ZIP off a different endpoint and keeps its own handler; the two
               must not be confused. -->
          <button
            onclick={() => downloadScript(drawerPartner.label, scriptShown)}
            disabled={packBlocked || !scriptShown}
            class="inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta font-medium text-ink-2 hover:bg-surface-2 disabled:opacity-60"
          >
            <FileText size={13} /> Download .sh
          </button>
          <!-- The SAME pack the add-a-partner step builds, for this partner's
               label — same endpoint, same gate, same refusal messages. -->
          <button
            onclick={() => downloadPartnerPack(false, drawerPartner.label)}
            disabled={packBlocked || Boolean(packBusy)}
            class="inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta font-medium text-ink-2 hover:bg-surface-2 disabled:opacity-60"
          >
            {#if packBusy === `row:${drawerPartner.label}`}
              <Loader2 size={13} class="animate-spin" />
            {:else}
              <Download size={13} />
            {/if}
            {packBusy === `row:${drawerPartner.label}`
              ? 'Building the pack…'
              : 'Download setup pack'}
          </button>
        </div>

        {#if scriptError}
          <p class="mt-2 text-meta leading-relaxed text-ink-2">
            {reason(scriptError, 'build their setup script')}
          </p>
        {/if}
        {#if packBlocked}
          <!-- Why all three controls above are off, at the controls. -->
          <p class="mt-2 text-meta leading-relaxed text-ink-2">{packBlockReason}</p>
        {/if}
      </div>

      <!-- ---- their key ---- -->
      <div class="mt-5 border-t border-line pt-3.5">
        <p class="text-micro font-bold uppercase tracking-wider text-ink-3">
          {drawerPartner.label}'s key
        </p>

        <!-- The question an operator arrives at this panel with. Answered
             before they go looking for a button that does not exist. -->
        <p class="mt-2 text-meta leading-relaxed text-ink-3">
          <span class="font-medium text-ink-2"
            >Their current private key cannot be shown again.</span
          >
          It was never stored — not here, not on the server. If they have lost it, regenerating is the
          only way back; there is no recovery.
        </p>

        <p
          class="mt-2.5 rounded-r-panel border-l-[3px] border-danger bg-danger-soft px-3.5 py-2.5 text-meta leading-relaxed text-ink"
        >
          <span class="font-semibold">Regenerating replaces this partner's key.</span>
          The key {drawerPartner.label} is using now
          <span class="font-semibold">stops working immediately</span>, and they cannot send us
          anything until they have installed the new one. Only do this when you can get the new key
          to them.
        </p>

        <div class="mt-2.5 flex flex-wrap items-center gap-2">
          <!-- Two presses, and `regenBusy` makes a double-click one
               regeneration rather than two — the second would replace the key
               the first just handed out. -->
          {#if regenBusy === drawerPartner.label}
            <button
              disabled
              class="inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta font-medium text-ink-2 disabled:opacity-60"
            >
              <Loader2 size={13} class="animate-spin" /> Replacing the key…
            </button>
          {:else if regenArmed === drawerPartner.label}
            <button
              onclick={() => regenerateKey(drawerPartner.label)}
              disabled={Boolean(regenBusy)}
              class="inline-flex items-center gap-1.5 rounded-panel bg-danger px-2.5 py-1.5 text-meta font-medium text-white hover:opacity-90 disabled:opacity-60"
            >
              <KeyRound size={13} /> Replace {drawerPartner.label}'s key now
            </button>
            <button
              onclick={() => (regenArmed = '')}
              class="rounded-panel px-2 py-1.5 text-meta text-ink-3 hover:text-ink"
            >
              Cancel
            </button>
          {:else}
            <button
              onclick={() => (regenArmed = drawerPartner.label)}
              disabled={Boolean(regenBusy)}
              class="inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta font-medium text-ink-2 hover:bg-surface-2 disabled:opacity-60"
            >
              <RotateCcw size={13} /> Regenerate key
            </button>
          {/if}

          <!-- The same control as the one on the row: it arms and fires the
               same `confirmDelete` / removeKey, so there is one revocation
               state, not two that can disagree. -->
          {#if confirmDelete === drawerPartner.label}
            <button
              onclick={() => removeKey(drawerPartner.label)}
              disabled={keyBusy}
              class="rounded-panel border border-danger/40 px-2.5 py-1.5 text-meta text-danger hover:bg-danger-soft disabled:opacity-60"
            >
              Revoke {drawerPartner.label}?
            </button>
            <button
              onclick={() => (confirmDelete = null)}
              class="rounded-panel px-2 py-1.5 text-meta text-ink-3 hover:text-ink"
            >
              Cancel
            </button>
          {:else}
            <button
              onclick={() => (confirmDelete = drawerPartner.label)}
              class="inline-flex items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta font-medium text-ink-3 hover:bg-surface-2 hover:text-danger"
            >
              <Trash2 size={13} /> Revoke
            </button>
          {/if}
        </div>

        <!-- The one private-key panel, rendered here because this is where the
             key was produced. Same snippet as step 3's, so the "only time this
             is shown" framing, the explicit "I have saved it" dismissal that
             clears it from state, and the object-URL revokes are the same code
             rather than a second copy of them. -->
        {#if generated && generatedFrom === drawerPartner.label}
          {@render privateKeyPanel(generated)}
        {/if}
      </div>
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
