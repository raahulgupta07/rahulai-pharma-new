<script>
  import { onMount, onDestroy } from 'svelte';
  import { base as appBase } from '$app/paths';
  import { API_BASE } from '$lib/apiBase.js';
  import {
    ShieldCheck,
    Store,
    CodeXml,
    Play,
    Copy,
    Check,
    Download,
    ExternalLink,
    RotateCw,
    Lock,
    Clock,
    ListChecks,
    TriangleAlert,
    CircleAlert,
    Circle,
    Globe,
    Zap
  } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';
  import ErrorState from '$lib/ErrorState.svelte';
  import { getJSON } from '$lib/api.js';

  // Same key/default as /admin/embed: the snippet handed to a customer must carry
  // the PUBLIC backend URL, which is not necessarily the origin we are browsing.
  const PUBLIC_BASE_KEY = 'embed_public_base';
  // The outlet's own site. Nothing in the backend stores it, so the console
  // remembers it locally — same treatment as the public base above.
  const CUSTOMER_ORIGIN_KEY = 'embed_customer_origin';
  const DEFAULT_ACCENT = '#2F3293';
  // Probe question for the streaming check. It must hit a `_plan_line` branch
  // (app/api.py:206 — "price"), because that frame is a pure template emitted
  // BEFORE any model call. We abort the moment it lands, so no LLM ever runs.
  const PROBE_MESSAGE = 'price check';
  const PROBE_FAST_MS = 1500;
  const PROBE_TIMEOUT_MS = 8000;

  let publicBase = $state(API_BASE);

  let creds = $state([]);
  let credsLoading = $state(true);
  let credsError = $state(null);
  let selected = $state('');

  let outlets = $state([]);
  let outletsLoading = $state(true);
  let outletsError = $state(null);
  let outletSel = $state('');

  let title = $state('');
  let accent = $state(DEFAULT_ACCENT);
  let stream = $state(true);

  const cleanBase = $derived((publicBase || '').trim().replace(/\/+$/, ''));
  const cred = $derived(creds.find((c) => c.embed_id === selected) ?? null);
  const hasCred = $derived(cred !== null);
  const embedId = $derived(cred?.embed_id ?? '');
  const publicKey = $derived(cred?.public_key ?? '');
  const outlet = $derived(outlets.find((o) => o.site_code === outletSel) ?? null);
  const effectiveTitle = $derived(title.trim() || (outletSel ? `Pharmacy · ${outletSel}` : ''));

  function fmt(n) {
    return n === null || n === undefined ? '—' : Number(n).toLocaleString();
  }

  onMount(() => {
    const saved = localStorage.getItem(PUBLIC_BASE_KEY);
    if (saved) publicBase = saved;
    const savedOrigin = localStorage.getItem(CUSTOMER_ORIGIN_KEY);
    if (savedOrigin) customerDomain = savedOrigin;
    loadCreds();
    loadOutlets();
    loadCors();
    tick = setInterval(() => (now = Date.now()), 1000);
  });

  let tick;
  onDestroy(() => {
    clearInterval(tick);
    // Leaving the page mid-probe must not leave the stream open behind us.
    probeCtl?.abort();
  });

  async function loadCreds() {
    credsLoading = true;
    credsError = null;
    try {
      // getJSON carries the status on the thrown error, so an expired session
      // is not reported as a stopped backend.
      const data = await getJSON('/admin/credentials');
      creds = Array.isArray(data) ? data : [];
      if (creds.length && !creds.some((c) => c.embed_id === selected)) selected = creds[0].embed_id;
    } catch (e) {
      credsError = e;
    } finally {
      credsLoading = false;
    }
  }

  async function loadOutlets() {
    outletsLoading = true;
    outletsError = null;
    try {
      const data = await getJSON('/admin/embed/outlets');
      outlets = Array.isArray(data) ? data : [];
      if (outlets.length && !outlets.some((o) => o.site_code === outletSel)) {
        outletSel = outlets[0].site_code;
      }
    } catch (e) {
      outletsError = e;
    } finally {
      outletsLoading = false;
    }
  }

  // ---- CORS allowlist (for the preflight row, read-only) --------------------
  let corsOrigins = $state(null); // null = not read yet
  let corsError = $state(null);
  async function loadCors() {
    try {
      const d = await getJSON('/admin/cors-origins');
      corsOrigins = [...(d.env ?? []), ...(d.runtime ?? [])];
    } catch (e) {
      corsError = e;
    }
  }
  // ---- customer origin ------------------------------------------------------
  let customerDomain = $state('');
  let allowBusy = $state(false);
  let allowError = $state(null);

  function rememberDomain() {
    try {
      localStorage.setItem(CUSTOMER_ORIGIN_KEY, customerDomain.trim());
    } catch {
      /* private mode — the check still works, it just won't be remembered */
    }
  }

  // scheme + host + non-default port, lowercased, no trailing slash. A bare
  // `shop.example.com` is read as https, which is what a customer site is.
  function normalizeOrigin(value) {
    const raw = (value || '').trim();
    if (!raw) return '';
    try {
      const u = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(raw) ? raw : `https://${raw}`);
      if (!u.hostname) return '';
      return u.origin.toLowerCase();
    } catch {
      return '';
    }
  }

  const customerOrigin = $derived(normalizeOrigin(customerDomain));
  // Prefer the allowlist the preflight response carried; fall back to the direct
  // read. `null` means "not read yet" and must NOT be treated as "not allowed".
  const allowlist = $derived(
    Array.isArray(preflight?.cors?.origins)
      ? preflight.cors.origins
      : Array.isArray(corsOrigins)
        ? corsOrigins
        : null
  );
  const allowlistNormalized = $derived(
    allowlist === null ? null : allowlist.map((o) => (o === '*' ? '*' : normalizeOrigin(o)))
  );
  const originAllowed = $derived(
    !!customerOrigin && !!allowlistNormalized?.includes(customerOrigin)
  );
  const corsWildcard = $derived(
    preflight?.cors?.wildcard === true || !!allowlist?.some((o) => o === '*')
  );

  async function allowCustomerOrigin() {
    if (!customerOrigin) return;
    allowBusy = true;
    allowError = null;
    try {
      const res = await fetch(`${API_BASE}/admin/cors-origins`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ origin: customerOrigin })
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(d.detail || `request failed (${res.status})`);
      await loadCors();
      if (preflight) await runPreflight();
    } catch (e) {
      allowError = e.message || 'could not allow this origin';
    } finally {
      allowBusy = false;
    }
  }

  // ---- backend preflight probe ----------------------------------------------
  // POST /admin/embed/preflight answers three tri-state checks. `ok === null`
  // means UNKNOWN and renders grey — a false green is worse than a grey row.
  let preflight = $state(null);
  let preflightBusy = $state(false);
  let preflightError = $state(null);
  let preflightMissing = $state(false);

  async function runPreflight() {
    if (!hasCred || !outletSel) return;
    preflightBusy = true;
    preflightError = null;
    preflightMissing = false;
    try {
      const res = await fetch(`${API_BASE}/admin/embed/preflight`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ store_id: outletSel, embed_id: embedId, public_key: publicKey })
      });
      if (res.status === 404) {
        preflight = null;
        preflightMissing = true;
        throw new Error('this backend has no /admin/embed/preflight endpoint yet');
      }
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || `request failed (${res.status})`);
      }
      const d = await res.json();
      preflight = d && typeof d === 'object' ? d : null;
      if (preflight?.credential?.ok === true) credentialAccepted = true;
    } catch (e) {
      preflightError = e.message || 'backend offline';
    } finally {
      preflightBusy = false;
    }
  }

  // ---- streaming probe (time to first SSE frame, no LLM cost) ---------------
  // app/api.py:1233 yields `event: plan` before any model call, and frames are
  // split on a blank line. We time the FIRST frame and abort immediately, so the
  // model is never invoked. This only proves the path from THIS console to the
  // API is unbuffered — it says nothing about the outlet's own CDN or proxy.
  let probe = $state({ state: 'idle', ms: 0, detail: '' });
  let probeCtl = null;

  async function probeStream() {
    if (!hasCred || probe.state === 'running') return;
    const controller = new AbortController();
    probeCtl = controller;
    probe = { state: 'running', ms: 0, detail: 'Opening a stream…' };
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, PROBE_TIMEOUT_MS);
    try {
      const s = await fetch(`${API_BASE}/api/embed/session/create`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ embed_id: embedId, public_key: publicKey }),
        signal: controller.signal
      });
      if (!s.ok) throw new Error(`session/create failed (${s.status})`);
      const { session_token: token } = await s.json();
      if (!token) throw new Error('session/create returned no token');

      const t0 = performance.now();
      const res = await fetch(`${API_BASE}/api/embed/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_token: token, message: PROBE_MESSAGE }),
        signal: controller.signal
      });
      if (!res.ok) throw new Error(`chat/stream failed (${res.status})`);
      if (!res.body) throw new Error('this browser gave no readable response body');

      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      let ms = null;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        if (buf.includes('\n\n')) {
          ms = Math.round(performance.now() - t0);
          break;
        }
      }
      if (ms === null) throw new Error('the stream closed before a whole frame arrived');

      if (ms <= PROBE_FAST_MS) {
        probe = {
          state: 'ok',
          ms,
          detail: `First frame in ${ms}ms — no buffering between this console and the API. The outlet's own proxy or CDN is not covered by this probe.`
        };
      } else {
        probe = {
          state: 'warn',
          ms,
          detail: `First frame took ${ms}ms. It did arrive before the answer, so the stream is not fully buffered, but something between this console and the API is slow.`
        };
      }
    } catch (e) {
      if (timedOut) {
        probe = {
          state: 'fail',
          ms: PROBE_TIMEOUT_MS,
          detail: `No frame in ${PROBE_TIMEOUT_MS / 1000}s — the first frame is a template emitted before any model call, so something between this console and the API is buffering the stream.`
        };
      } else if (e?.name === 'AbortError') {
        probe = { state: 'unknown', ms: 0, detail: 'The probe was cancelled before a frame arrived.' };
      } else {
        probe = { state: 'unknown', ms: 0, detail: `Could not run the probe: ${e.message}` };
      }
    } finally {
      clearTimeout(timer);
      // Abort in `finally`, never only on the happy path: an error thrown mid
      // read would otherwise leave the stream open and let the model run.
      controller.abort();
      if (probeCtl === controller) probeCtl = null;
    }
  }

  async function runAllChecks() {
    await runPreflight();
    if (stream) await probeStream();
  }

  // ---- shared request body (same shape the snippet endpoint takes) ----------
  function body(baseUrl) {
    return {
      store_id: outletSel,
      embed_id: embedId,
      public_key: publicKey,
      base_url: baseUrl,
      title: effectiveTitle,
      accent,
      stream
    };
  }

  async function postJson(path, payload) {
    const res = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || `request failed (${res.status})`);
    }
    return res;
  }

  // ---- snippet --------------------------------------------------------------
  let snippet = $state('');
  let snippetBusy = $state(false);
  let snippetError = $state(null);
  let zipBusy = $state(false);
  let copied = $state('');

  function copy(key, text) {
    navigator.clipboard.writeText(text);
    copied = key;
    setTimeout(() => (copied = ''), 1500);
  }

  // A detached <a> whose object URL is revoked on the same tick never downloads
  // (the click is async). Append, click, revoke on a later tick.
  function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      a.remove();
      URL.revokeObjectURL(url);
    }, 0);
  }

  async function genSnippet() {
    if (!hasCred || !outletSel) return null;
    snippetBusy = true;
    snippetError = null;
    try {
      const res = await postJson('/admin/embed/snippet', body(cleanBase));
      const data = await res.json();
      snippet = data.snippet;
      credentialAccepted = true;
      return data;
    } catch (e) {
      snippetError = e.message;
      snippet = '';
      return null;
    } finally {
      snippetBusy = false;
    }
  }

  async function downloadPage() {
    const data = await genSnippet();
    if (data) saveBlob(new Blob([data.demo_html], { type: 'text/html' }), `outlet-${outletSel}.html`);
  }

  async function downloadZip() {
    if (!hasCred || !outlets.length) return;
    zipBusy = true;
    snippetError = null;
    try {
      const res = await postJson('/admin/embed/snippets.zip', body(cleanBase));
      saveBlob(await res.blob(), 'outlet-embeds.zip');
    } catch (e) {
      snippetError = e.message;
    } finally {
      zipBusy = false;
    }
  }

  // ---- the test URL ---------------------------------------------------------
  // The preview page must be SAME-ORIGIN with this console, so base_url here is
  // the origin we are browsing — not the public base the snippet carries. An
  // iframe on another origin (or a srcdoc iframe, whose origin is `null`) has
  // every embed API call rejected by the CORS allowlist.
  let previewUrl = $state('');
  let previewStore = $state('');
  let previewExpiresAt = $state(0);
  let previewBusy = $state(false);
  let previewError = $state(null);
  let previewMissing = $state(false); // endpoint not deployed yet (404)
  let now = $state(Date.now());
  let frameKey = $state(0);

  // What the current link was minted for. If any of these move, the link on
  // screen no longer matches the form, and we say so rather than pretending.
  let linkedFor = $state('');
  const configKey = $derived(
    [outletSel, embedId, publicKey, effectiveTitle, accent, stream].join(' ')
  );
  const linkStale = $derived(!!previewUrl && linkedFor !== configKey);
  const secondsLeft = $derived(
    previewExpiresAt ? Math.max(0, Math.round((previewExpiresAt - now) / 1000)) : 0
  );
  const expired = $derived(!!previewUrl && previewExpiresAt > 0 && secondsLeft === 0);

  // Set once a backend call actually accepted the (embed_id, public_key) pair.
  let credentialAccepted = $state(false);

  async function makeLink() {
    if (!hasCred || !outletSel) return;
    previewBusy = true;
    previewError = null;
    previewMissing = false;
    try {
      const res = await fetch(`${API_BASE}/admin/embed/preview-link`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body(API_BASE))
      });
      if (res.status === 404) {
        previewMissing = true;
        throw new Error('this backend has no /admin/embed/preview-link endpoint yet');
      }
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || `request failed (${res.status})`);
      }
      const d = await res.json();
      previewUrl = d.url || '';
      previewStore = d.store_id || outletSel;
      previewExpiresAt = Date.now() + (Number(d.expires_in) || 0) * 1000;
      linkedFor = configKey;
      credentialAccepted = true;
      frameKey += 1;
    } catch (e) {
      previewError = e.message;
      previewUrl = '';
      previewExpiresAt = 0;
    } finally {
      previewBusy = false;
    }
  }

  function reloadFrame() {
    frameKey += 1;
  }

  function openLink() {
    if (previewUrl) window.open(previewUrl, '_blank', 'noopener');
  }

  function expiryText() {
    if (!previewExpiresAt) return '';
    if (secondsLeft === 0) return 'Link has expired';
    const m = Math.floor(secondsLeft / 60);
    return m >= 1 ? `Link expires in ${m} min` : `Link expires in ${secondsLeft}s`;
  }

  // ---- preflight ------------------------------------------------------------
  // Four rows, every one of them backed by something actually probed — or left
  // grey. `ok: null` from the backend is UNKNOWN, not a pass: a false green here
  // is worse than a grey row, so an unknown never renders as a tick.
  //
  // States: ok (green) · warn (amber) · fail (red) · unknown (grey).
  function triState(node) {
    if (!node) return 'unknown';
    if (node.ok === true) return 'ok';
    if (node.ok === false) return 'fail';
    return 'unknown';
  }

  const scopeNode = $derived(preflight?.scope ?? null);
  const credNode = $derived(preflight?.credential ?? null);

  const checks = $derived([
    {
      key: 'cred',
      state: credNode
        ? triState(credNode)
        : credentialAccepted
          ? 'ok'
          : previewError || snippetError
            ? 'fail'
            : 'unknown',
      title: credNode
        ? credNode.ok === true
          ? 'Credential accepted'
          : credNode.ok === false
            ? 'Credential rejected'
            : 'Credential — not checked'
        : credentialAccepted
          ? 'Credential accepted'
          : previewError || snippetError
            ? 'Credential not accepted'
            : 'Credential — not checked',
      // The backend names exactly what it checked; render it verbatim.
      detail:
        credNode?.detail ||
        (credentialAccepted
          ? `${embedId} / ${publicKey} was accepted by the backend`
          : previewError || snippetError
            ? previewError || snippetError
            : 'Run the checks, or generate a test URL / snippet — the backend validates the pair.')
    },
    {
      key: 'scope',
      state: triState(scopeNode),
      title:
        scopeNode?.ok === true
          ? 'Store scope enforced'
          : scopeNode?.ok === false
            ? 'Store scope LEAKED a sibling branch'
            : 'Store scope — not checked',
      detail:
        scopeNode?.detail ||
        (preflightMissing
          ? 'this backend has no /admin/embed/preflight endpoint yet'
          : preflightError
            ? `Could not run the probe (${preflightError}).`
            : preflightBusy
              ? 'Probing…'
              : 'Not probed yet — run the checks.'),
      meta: scopeNode
        ? [
            scopeNode.rows_checked != null ? `${fmt(scopeNode.rows_checked)} rows checked` : '',
            scopeNode.sites_visible != null ? `${fmt(scopeNode.sites_visible)} site(s) visible` : '',
            scopeNode.sibling_leaked === true
              ? 'a sibling branch was visible'
              : scopeNode.sibling_leaked === false
                ? 'no sibling branch visible'
                : ''
          ]
            .filter(Boolean)
            .join(' · ')
        : ''
    },
    {
      key: 'stream',
      state: !stream ? 'unknown' : probe.state === 'idle' || probe.state === 'running' ? 'unknown' : probe.state,
      title: !stream
        ? 'Streaming — not checked'
        : probe.state === 'ok'
          ? 'Streaming reaches this console unbuffered'
          : probe.state === 'warn'
            ? 'Streaming is slow to first frame'
            : probe.state === 'fail'
              ? 'The stream is being buffered'
              : probe.state === 'running'
                ? 'Streaming — probing…'
                : 'Streaming — not checked',
      detail: !stream
        ? 'Streaming is turned off in this snippet, so there is nothing to time.'
        : probe.state === 'idle'
          ? 'Not probed yet — run the checks. The probe reads the first SSE frame and aborts, so no model call is made.'
          : probe.detail
    },
    {
      key: 'cors',
      state: corsWildcard
        ? 'fail'
        : !customerDomain.trim()
          ? 'unknown'
          : !customerOrigin
            ? 'unknown'
            : allowlistNormalized === null
              ? 'unknown'
              : originAllowed
                ? 'ok'
                : 'warn',
      title: corsWildcard
        ? 'CORS allowlist is a wildcard'
        : !customerDomain.trim()
          ? 'Customer origin — not checked'
          : !customerOrigin
            ? 'Customer origin — could not be read'
            : allowlistNormalized === null
              ? 'Customer origin — allowlist not read'
              : originAllowed
                ? 'Customer origin is allowed'
                : 'Customer origin is NOT on the allowlist',
      detail: corsWildcard
        ? 'The allowlist contains "*", so every site on the internet can call the embed API. That outranks any per-domain pass — tighten it before go-live.'
        : !customerDomain.trim()
          ? "Enter the outlet's domain to check."
          : !customerOrigin
            ? `“${customerDomain.trim()}” could not be read as a domain. Try https://shop.example.com.`
            : allowlistNormalized === null
              ? `Could not read the allowlist${corsError ? ` (${corsError.message ?? corsError})` : ''}.`
              : originAllowed
                ? `${customerOrigin} is on the allowlist (${allowlistNormalized.length} origin(s) allowed).`
                : `${customerOrigin} is not among the ${allowlistNormalized.length} allowed origin(s), so the widget's calls from that site are refused by CORS.`,
      action:
        !corsWildcard && customerOrigin && allowlistNormalized !== null && !originAllowed
          ? 'allow-origin'
          : ''
    }
  ]);
  const uncheckedCount = $derived(checks.filter((c) => c.state === 'unknown').length);
  const failCount = $derived(checks.filter((c) => c.state === 'fail').length);
</script>

<PageHeader
  level={2}
  title="Embed test"
  subtitle="Mint a short-lived, store-scoped preview URL and watch the real widget answer on a real page — before the snippet leaves the building."
>
  {#snippet meta()}
    <Badge tone={hasCred ? 'ok' : 'warn'}>
      {hasCred ? `credential ${embedId}` : 'no credential'}
    </Badge>
    <Badge tone="neutral">{outlets.length} outlets</Badge>
  {/snippet}
</PageHeader>

<div class="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
  <!-- ============ LEFT · configure ============ -->
  <div class="flex flex-col gap-4">
    <!-- Credential -->
    <section class="elev rounded-panel border border-line bg-surface">
      <div class="flex items-center gap-2 border-b border-line px-4 py-3">
        <ShieldCheck size={16} class="text-ink-3" />
        <span class="text-body-sm font-semibold text-ink">Credential</span>
        <span class="ml-auto">
          {#if credsLoading}
            <Badge tone="neutral">loading…</Badge>
          {:else if hasCred}
            <Badge tone="ok">Registered</Badge>
          {:else}
            <Badge tone="warn">None registered</Badge>
          {/if}
        </span>
      </div>
      <div class="p-4">
        {#if credsError}
          <ErrorState error={credsError} retry={loadCreds} what="embed credentials" />
        {:else if credsLoading}
          <p class="text-body-sm text-ink-3">Loading credentials…</p>
        {:else if !creds.length}
          <p class="mb-3 flex items-start gap-1.5 text-body-sm text-warning">
            <TriangleAlert size={14} class="mt-0.5 shrink-0" />
            <span>
              No embed credentials are registered, so no preview can be minted. The embed API is
              <span class="font-medium">fail-closed</span> — an unregistered pair is rejected.
            </span>
          </p>
          <a
            href={appBase + '/tenants'}
            class="inline-flex items-center gap-1.5 rounded-panel bg-accent px-3 py-1.5 text-body-sm font-medium text-on-accent transition-colors hover:bg-accent-hover"
          >
            Mint a credential on Tenants <ExternalLink size={13} />
          </a>
        {:else}
          <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label class="block">
              <span class="mb-1.5 block text-label font-bold uppercase tracking-[0.05em] text-ink-3"
                >Embed ID</span
              >
              <select
                bind:value={selected}
                class="w-full rounded-card border border-line bg-surface-2 px-3 py-2 font-mono text-body-sm text-ink outline-none focus:border-accent"
              >
                {#each creds as c (c.embed_id)}
                  <option value={c.embed_id}>{c.embed_id}</option>
                {/each}
              </select>
            </label>
            <div>
              <span class="mb-1.5 block text-label font-bold uppercase tracking-[0.05em] text-ink-3"
                >Public key</span
              >
              <div
                class="truncate rounded-card border border-line bg-surface-2 px-3 py-2 font-mono text-body-sm text-ink"
              >
                {publicKey}
              </div>
            </div>
          </div>
          <p class="mt-3 text-meta text-ink-3">
            The embed API is fail-closed — only registered pairs are accepted.
          </p>
        {/if}
      </div>
    </section>

    <!-- Outlet -->
    <section class="elev rounded-panel border border-line bg-surface">
      <div class="flex items-center gap-2 border-b border-line px-4 py-3">
        <Store size={16} class="text-ink-3" />
        <span class="text-body-sm font-semibold text-ink">Outlet</span>
        <span class="ml-auto">
          <Badge tone="neutral">{outletsLoading ? 'loading…' : `${outlets.length} available`}</Badge>
        </span>
      </div>
      <div class="p-4">
        {#if outletsError}
          <ErrorState error={outletsError} retry={loadOutlets} what="the outlet list" />
        {:else}
          <label class="mb-3 block">
            <span class="mb-1.5 block text-label font-bold uppercase tracking-[0.05em] text-ink-3"
              >Store</span
            >
            <select
              bind:value={outletSel}
              class="w-full rounded-card border border-line bg-surface-2 px-3 py-2 font-mono text-body-sm text-ink outline-none focus:border-accent"
            >
              {#each outlets as o (o.site_code)}
                <option value={o.site_code}
                  >{o.site_code} · {fmt(o.skus)} SKUs · {fmt(o.units)} units</option
                >
              {/each}
            </select>
          </label>
          {#if outlet}
            <p class="mb-3 text-meta text-ink-3">
              <span class="font-mono">{outlet.site_code}</span>
              · {fmt(outlet.skus)} SKUs · {fmt(outlet.units)} units on hand
            </p>
          {/if}

          <div class="mb-3 flex flex-wrap gap-3">
            <label class="min-w-[200px] flex-1">
              <span class="mb-1.5 block text-label font-bold uppercase tracking-[0.05em] text-ink-3"
                >Widget title</span
              >
              <input
                type="text"
                bind:value={title}
                placeholder={outletSel ? `Pharmacy · ${outletSel}` : 'Pharmacy'}
                class="w-full rounded-card border border-line bg-surface-2 px-3 py-2 text-body-sm text-ink outline-none placeholder:text-ink-3 focus:border-accent"
              />
            </label>
            <label class="w-[150px]">
              <span class="mb-1.5 block text-label font-bold uppercase tracking-[0.05em] text-ink-3"
                >Accent</span
              >
              <span
                class="flex items-center gap-2 rounded-card border border-line bg-surface-2 px-2 py-1.5"
              >
                <input
                  type="color"
                  bind:value={accent}
                  aria-label="Widget accent colour"
                  class="h-6 w-7 cursor-pointer border-0 bg-transparent p-0"
                />
                <span class="font-mono text-meta text-ink">{accent}</span>
              </span>
            </label>
          </div>

          <label class="flex cursor-pointer items-center gap-2 text-body-sm text-ink-2">
            <input
              type="checkbox"
              bind:checked={stream}
              class="h-4 w-4 rounded border-line accent-[var(--c-accent)]"
            />
            Streaming responses
          </label>
        {/if}
      </div>
    </section>

    <!-- Snippet -->
    <section class="elev rounded-panel border border-line bg-surface">
      <div class="flex items-center gap-2 border-b border-line px-4 py-3">
        <CodeXml size={16} class="text-ink-3" />
        <span class="text-body-sm font-semibold text-ink">Snippet</span>
        <span class="ml-auto"><Badge tone="ok">Pre-signed · store-locked</Badge></span>
      </div>
      <div class="p-4">
        {#if !hasCred}
          <p class="text-body-sm text-ink-3">Register a credential first — a snippet needs one.</p>
        {:else}
          {#if snippet}
            <pre
              class="overflow-x-auto rounded-card border border-line bg-surface-2 p-3 text-label leading-relaxed text-ink-2"><code
                >{snippet}</code
              ></pre>
          {:else}
            <p class="rounded-card border border-dashed border-line bg-surface-2 px-3 py-4 text-meta text-ink-3">
              No snippet generated yet — it is signed server-side for
              <span class="font-mono">{outletSel || 'the selected store'}</span>.
            </p>
          {/if}

          <p class="mt-2 text-meta text-ink-3">
            Snippet <span class="font-mono">base_url</span>:
            <span class="font-mono">{cleanBase}</span> (change it on
            <a href={appBase + '/embed?tab=widget'} class="text-accent hover:underline">Embed widget</a>).
          </p>

          <div class="mt-3 flex flex-wrap gap-2">
            <button
              onclick={() => (snippet ? copy('snippet', snippet) : genSnippet())}
              disabled={snippetBusy || !outletSel}
              class="inline-flex h-[38px] items-center gap-2 rounded-card bg-accent px-3.5 text-body-sm font-semibold text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
            >
              {#if copied === 'snippet'}<Check size={15} /> Copied{:else}<Copy size={15} />
                {snippet ? 'Copy snippet' : snippetBusy ? 'Working…' : 'Generate snippet'}{/if}
            </button>
            <button
              onclick={downloadPage}
              disabled={snippetBusy || !outletSel}
              class="inline-flex h-[38px] items-center gap-2 rounded-card border border-line bg-surface px-3.5 text-body-sm font-semibold text-ink transition-colors hover:bg-surface-2 disabled:opacity-60"
            >
              <Download size={15} /> Download page
            </button>
            <button
              onclick={downloadZip}
              disabled={zipBusy || !outlets.length}
              class="inline-flex h-[38px] items-center gap-2 rounded-card border border-line bg-surface px-3.5 text-body-sm font-semibold text-ink transition-colors hover:bg-surface-2 disabled:opacity-60"
            >
              <Download size={15} />
              {zipBusy ? 'Building…' : `All ${outlets.length} outlets (.zip)`}
            </button>
          </div>

          {#if snippetError}
            <p class="mt-3 flex items-start gap-1.5 text-body-sm text-danger">
              <CircleAlert size={14} class="mt-0.5 shrink-0" />
              <span>{snippetError}</span>
            </p>
          {/if}
        {/if}
      </div>
    </section>
  </div>

  <!-- ============ RIGHT · live test ============ -->
  <div class="flex flex-col gap-4">
    <!-- Test URL -->
    <section class="elev rounded-panel border border-line bg-surface">
      <div class="flex items-center gap-2 border-b border-line px-4 py-3">
        <Play size={16} class="text-ink-3" />
        <span class="text-body-sm font-semibold text-ink">Test on a customer domain</span>
        <span class="ml-auto">
          {#if previewUrl && !expired}
            <Badge tone="ok">Session live</Badge>
          {:else if expired}
            <Badge tone="warn">Expired</Badge>
          {:else}
            <Badge tone="neutral">Not started</Badge>
          {/if}
        </span>
      </div>
      <div class="p-4">
        <span class="mb-1.5 block text-label font-bold uppercase tracking-[0.05em] text-ink-3">
          Test URL
          <span class="font-medium normal-case tracking-normal text-ink-3">
            — real page, real widget, real answers</span
          >
        </span>

        {#if previewUrl}
          <div class="flex items-center gap-2 rounded-card border border-line bg-surface-2 px-2.5 py-2">
            <Lock size={14} class="shrink-0 text-ink-3" />
            <span class="min-w-0 flex-1 truncate font-mono text-meta text-ink-2">{previewUrl}</span>
            <button
              onclick={() => copy('url', previewUrl)}
              aria-label="Copy test URL"
              class="inline-flex h-[30px] items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 text-meta font-medium text-ink-2 transition-colors hover:border-accent hover:text-accent"
            >
              {#if copied === 'url'}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
            </button>
            <button
              onclick={openLink}
              aria-label="Open test URL in a new tab"
              class="inline-flex h-[30px] items-center gap-1.5 rounded-panel bg-accent px-2.5 text-meta font-medium text-on-accent transition-colors hover:bg-accent-hover"
            >
              <ExternalLink size={13} /> Open
            </button>
          </div>

          <div class="mt-2 flex flex-wrap items-center gap-1.5 text-label text-ink-3">
            <Clock size={12} class="shrink-0" />
            <span>
              {expiryText()} · scoped to <span class="font-mono">{previewStore}</span> · super_admin
              only
            </span>
            <button
              onclick={makeLink}
              disabled={previewBusy}
              class="ml-auto inline-flex h-6 items-center gap-1.5 rounded-panel border border-line px-2 text-label font-medium text-ink-2 transition-colors hover:border-accent hover:text-accent disabled:opacity-60"
            >
              <RotateCw size={11} />
              {previewBusy ? 'Working…' : 'Regenerate'}
            </button>
          </div>

          {#if linkStale}
            <p class="mt-2 flex items-start gap-1.5 text-meta text-warning">
              <TriangleAlert size={13} class="mt-0.5 shrink-0" />
              <span>The settings above changed after this link was minted — regenerate it.</span>
            </p>
          {/if}
        {:else}
          <button
            onclick={makeLink}
            disabled={previewBusy || !hasCred || !outletSel}
            class="inline-flex h-[38px] items-center gap-2 rounded-card bg-accent px-3.5 text-body-sm font-semibold text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
          >
            <Play size={15} />
            {previewBusy ? 'Minting…' : 'Generate test URL'}
          </button>
          <p class="mt-2 text-meta text-ink-3">
            A short-lived link to a real page carrying the real widget, scoped to
            <span class="font-mono">{outletSel || '—'}</span>. Nothing is sent to the outlet.
          </p>
        {/if}

        {#if previewError}
          <p class="mt-3 flex items-start gap-1.5 rounded-card bg-danger-soft px-3 py-2 text-meta text-danger">
            <CircleAlert size={14} class="mt-0.5 shrink-0" />
            <span>
              {previewError}
              {#if previewMissing}
                <br />This console is newer than the backend it is talking to. Everything else on
                this page still works.
              {/if}
            </span>
          </p>
        {/if}
      </div>
    </section>

    <!-- Live preview -->
    <section class="elev rounded-panel border border-line bg-surface p-3">
      <div class="overflow-hidden rounded-panel border border-line-2 bg-surface">
        <div class="flex h-[38px] items-center gap-2 border-b border-line bg-surface-2 px-3">
          <span class="flex gap-1.5" aria-hidden="true">
            <span class="block h-[9px] w-[9px] rounded-full bg-line-2"></span>
            <span class="block h-[9px] w-[9px] rounded-full bg-line-2"></span>
            <span class="block h-[9px] w-[9px] rounded-full bg-line-2"></span>
          </span>
          <span
            class="flex h-6 min-w-0 flex-1 items-center gap-1.5 overflow-hidden rounded-control border border-line bg-surface px-2 text-label text-ink-3"
          >
            <Lock size={11} class="shrink-0" />
            <span class="truncate font-mono">{previewUrl || 'no preview link yet'}</span>
          </span>
          <button
            onclick={reloadFrame}
            disabled={!previewUrl}
            aria-label="Reload preview"
            class="inline-flex h-6 items-center gap-1.5 rounded-panel border border-line px-2 text-label font-medium text-ink-2 transition-colors hover:border-accent hover:text-accent disabled:opacity-60"
          >
            <RotateCw size={12} /> Reload
          </button>
        </div>

        <div class="h-[432px] bg-surface-2">
          {#if previewUrl && !expired}
            {#key frameKey}
              <iframe
                src={previewUrl}
                title="Embed widget preview for {previewStore}"
                class="h-full w-full border-0 bg-surface"
              ></iframe>
            {/key}
          {:else}
            <div class="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
              <Play size={22} class="text-ink-3" />
              <p class="text-body-sm text-ink-2">
                {expired
                  ? 'The preview link expired — regenerate it to load the page again.'
                  : 'Generate a test URL to load the real page here.'}
              </p>
              <p class="max-w-[380px] text-meta text-ink-3">
                The preview is loaded from a URL on this origin, not from inline HTML — an inline
                (srcdoc) frame has a null origin and every embed call from it is refused by CORS.
              </p>
            </div>
          {/if}
        </div>
      </div>
    </section>

    <!-- Preflight -->
    <section class="elev rounded-panel border border-line bg-surface">
      <div class="flex items-center gap-2 border-b border-line px-4 py-3">
        <ListChecks size={16} class="text-ink-3" />
        <span class="text-body-sm font-semibold text-ink">Preflight</span>
        <span class="ml-auto flex items-center gap-2">
          <Badge tone={failCount ? 'danger' : uncheckedCount ? 'warn' : 'ok'}>
            {failCount
              ? `${failCount} failing`
              : uncheckedCount
                ? `${uncheckedCount} not checked`
                : 'all checked'}
          </Badge>
          <button
            onclick={runAllChecks}
            disabled={preflightBusy || probe.state === 'running' || !hasCred || !outletSel}
            class="inline-flex h-[30px] items-center gap-1.5 rounded-panel bg-accent px-2.5 text-meta font-semibold text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
          >
            <Zap size={13} />
            {preflightBusy || probe.state === 'running' ? 'Checking…' : 'Run checks'}
          </button>
        </span>
      </div>

      <div class="border-b border-line px-4 py-3">
        <label for="customer-domain" class="mb-1.5 block text-label font-bold uppercase tracking-[0.05em] text-ink-3">
          Customer domain
        </label>
        <div class="flex items-center gap-2 rounded-card border border-line bg-surface-2 px-2.5 py-1.5">
          <Globe size={14} class="shrink-0 text-ink-3" />
          <input
            id="customer-domain"
            type="text"
            bind:value={customerDomain}
            oninput={rememberDomain}
            placeholder="https://shop.example.com"
            class="min-w-0 flex-1 bg-transparent text-body-sm text-ink outline-none placeholder:text-ink-3"
          />
          {#if customerOrigin}
            <span class="shrink-0 font-mono text-label text-ink-3">{customerOrigin}</span>
          {/if}
        </div>
        <p class="mt-1.5 text-meta text-ink-3">
          The site the snippet will live on. Nothing stores it server-side, so this console remembers
          it in your browser and checks it against the CORS allowlist.
        </p>
      </div>

      <div class="px-4 py-1" aria-live="polite" aria-busy={preflightBusy || probe.state === 'running'}>
        {#each checks as c (c.key)}
          <div class="flex items-start gap-2.5 border-b border-line py-2.5 text-body-sm last:border-b-0">
            <span class="mt-0.5 shrink-0">
              {#if c.state === 'ok'}
                <Check size={15} class="text-success" />
              {:else if c.state === 'fail'}
                <CircleAlert size={15} class="text-danger" />
              {:else if c.state === 'warn'}
                <TriangleAlert size={15} class="text-warning" />
              {:else}
                <Circle size={15} class="text-ink-3" />
              {/if}
            </span>
            <span class="min-w-0 flex-1">
              <span class="font-medium text-ink">{c.title}</span>
              <span class="text-meta text-ink-3"> — {c.detail}</span>
              {#if c.meta}
                <span class="mt-1 block text-label text-ink-3">{c.meta}</span>
              {/if}
              {#if c.action === 'allow-origin'}
                <span class="mt-1.5 flex flex-wrap items-center gap-2">
                  <button
                    onclick={allowCustomerOrigin}
                    disabled={allowBusy}
                    class="inline-flex h-[28px] items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 text-meta font-semibold text-ink transition-colors hover:border-accent hover:text-accent disabled:opacity-60"
                  >
                    <Globe size={12} />
                    {allowBusy ? 'Allowing…' : 'Allow this origin'}
                  </button>
                  {#if allowError}
                    <span class="text-meta text-danger">{allowError}</span>
                  {/if}
                </span>
              {/if}
            </span>
          </div>
        {/each}
      </div>
      <div class="border-t border-line px-4 py-2.5 text-meta text-ink-3">
        Rows marked “not checked” are exactly that — nothing on this page probed them, and an
        inconclusive probe stays grey rather than passing. The streaming probe reads the first SSE
        frame and aborts, so it costs no model call, and it only covers the path from this console to
        the API — not the outlet's own proxy or CDN. Manage the CORS allowlist on
        <a href={appBase + '/tenants'} class="text-accent hover:underline">Tenants</a>.
      </div>
    </section>
  </div>
</div>
